"""Pluggable authentication provider system for Lucent.

Supports multiple auth backends that can be configured via LUCENT_AUTH_PROVIDER.
Each provider handles credential validation and user lookup for the web UI.

API key authentication for MCP/API endpoints is always available regardless
of the configured web auth provider.

Available providers:
- basic: Username/password authentication (default)
- api_key: Authenticate with an API key (legacy, simple)

Future providers:
- oauth: GitHub/Google OAuth
- saml: Enterprise SAML/SCIM
"""

import hashlib
import hmac
import os
import secrets
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import bcrypt
from asyncpg import Pool

from lucent.db import ApiKeyRepository, UserRepository, get_pool
from lucent.logging import get_logger

logger = get_logger("auth.providers")

# Session configuration
SESSION_COOKIE_NAME = "lucent_session"
SESSION_TTL_HOURS = int(os.environ.get("LUCENT_SESSION_TTL_HOURS", "72"))
SECURE_COOKIES = os.environ.get("LUCENT_SECURE_COOKIES", "false").lower() in ("true", "1", "yes")

# CSRF configuration — simple double-submit cookie pattern
CSRF_COOKIE_NAME = "lucent_csrf"
CSRF_FIELD_NAME = "csrf_token"

# Signing secret for impersonation cookies — MUST be persistent across restarts
# If not set, generates a random one (impersonation cookies won't survive restarts)
SIGNING_SECRET = os.environ.get("LUCENT_SIGNING_SECRET", secrets.token_urlsafe(32))


def get_cookie_params() -> dict:
    """Get common cookie security parameters.

    Returns:
        Dict with httponly, samesite, secure, path keys.
    """
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": SECURE_COOKIES,
        "path": "/",
    }


def generate_csrf_token() -> str:
    """Generate a random CSRF token for double-submit cookie pattern."""
    return secrets.token_urlsafe(32)


def validate_csrf_token(token: str | None) -> bool:
    """Validate a CSRF token is non-empty. Actual security comes from
    comparing cookie == form field in _check_csrf, not from token validation."""
    return bool(token and len(token) > 8)


def sign_value(value: str) -> str:
    """Sign a value with HMAC for tamper detection.

    Used for the impersonation cookie to prevent forgery.
    """
    signature = hmac.new(
        SIGNING_SECRET.encode(), value.encode(), hashlib.sha256
    ).hexdigest()
    return f"{value}.{signature}"


def verify_signed_value(signed: str | None) -> str | None:
    """Verify and extract a signed value."""
    if not signed or "." not in signed:
        return None
    value, signature = signed.rsplit(".", 1)
    expected = hmac.new(
        SIGNING_SECRET.encode(), value.encode(), hashlib.sha256
    ).hexdigest()
    if hmac.compare_digest(signature, expected):
        return value
    return None


class AuthProvider(ABC):
    """Abstract base class for authentication providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """The provider identifier (e.g., 'basic', 'api_key', 'oauth')."""
        ...

    @abstractmethod
    async def authenticate(self, credentials: dict[str, str]) -> dict[str, Any] | None:
        """Validate credentials and return the user record if valid.

        Args:
            credentials: Provider-specific credential dict.
                - basic: {"username": str, "password": str}
                - api_key: {"api_key": str}

        Returns:
            User record dict if authentication succeeds, None otherwise.
        """
        ...

    @abstractmethod
    def get_login_fields(self) -> list[dict[str, str]]:
        """Return the form fields needed for the login page.

        Returns:
            List of dicts with keys: name, label, type, placeholder.
        """
        ...


class BasicAuthProvider(AuthProvider):
    """Username/password authentication with bcrypt hashing."""

    def __init__(self, pool: Pool):
        self.pool = pool

    @property
    def name(self) -> str:
        return "basic"

    async def authenticate(self, credentials: dict[str, str]) -> dict[str, Any] | None:
        """Authenticate with username (email or display_name) and password."""
        username = credentials.get("username", "").strip()
        password = credentials.get("password", "")

        if not username or not password:
            return None

        user = await self._find_user(username)
        if user is None:
            return None

        if not user.get("password_hash"):
            return None

        if not bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8")):
            return None

        # Update last login
        user_repo = UserRepository(self.pool)
        await user_repo.update_last_login(user["id"])

        return user

    async def _find_user(self, username: str) -> dict[str, Any] | None:
        """Find a user by email or display_name."""
        query = """
            SELECT id, external_id, provider, organization_id, email, display_name,
                   avatar_url, provider_metadata, is_active, created_at, updated_at,
                   last_login_at, role, password_hash
            FROM users
            WHERE (LOWER(email) = LOWER($1) OR LOWER(display_name) = LOWER($1))
              AND is_active = true
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, username)

        if row is None:
            return None

        return dict(row)

    def get_login_fields(self) -> list[dict[str, str]]:
        return [
            {
                "name": "username",
                "label": "Username or Email",
                "type": "text",
                "placeholder": "Enter your username or email",
            },
            {
                "name": "password",
                "label": "Password",
                "type": "password",
                "placeholder": "Enter your password",
            },
        ]


class ApiKeyAuthProvider(AuthProvider):
    """Authenticate using an API key (simple/legacy mode)."""

    def __init__(self, pool: Pool):
        self.pool = pool

    @property
    def name(self) -> str:
        return "api_key"

    async def authenticate(self, credentials: dict[str, str]) -> dict[str, Any] | None:
        """Authenticate with an API key."""
        api_key = credentials.get("api_key", "").strip()
        if not api_key:
            return None

        api_key_repo = ApiKeyRepository(self.pool)
        key_info = await api_key_repo.verify(api_key)
        if not key_info:
            return None

        user_repo = UserRepository(self.pool)
        user = await user_repo.get_by_id(key_info["user_id"])

        if user:
            await user_repo.update_last_login(user["id"])

        return user

    def get_login_fields(self) -> list[dict[str, str]]:
        return [
            {
                "name": "api_key",
                "label": "API Key",
                "type": "password",
                "placeholder": "Enter your API key (hs_...)",
            },
        ]


# --- Session Management ---


def generate_session_token() -> str:
    """Generate a cryptographically secure session token."""
    return secrets.token_urlsafe(48)


def hash_session_token(token: str) -> str:
    """Hash a session token for storage (SHA-256, not bcrypt — sessions are ephemeral)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(pool: Pool, user_id: UUID) -> str:
    """Create a new session for a user and return the raw token.

    Args:
        pool: Database connection pool.
        user_id: The user to create a session for.

    Returns:
        The raw session token (to be set as a cookie).
    """
    token = generate_session_token()
    token_hash = hash_session_token(token)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)

    query = """
        UPDATE users
        SET session_token = $1, session_expires_at = $2
        WHERE id = $3
    """
    async with pool.acquire() as conn:
        await conn.execute(query, token_hash, expires_at, str(user_id))

    return token


async def validate_session(pool: Pool, token: str) -> dict[str, Any] | None:
    """Validate a session token and return the user if valid.

    Args:
        pool: Database connection pool.
        token: The raw session token from the cookie.

    Returns:
        The user record if the session is valid, None otherwise.
    """
    if not token:
        return None

    token_hash = hash_session_token(token)

    query = """
        SELECT id, external_id, provider, organization_id, email, display_name,
               avatar_url, provider_metadata, is_active, created_at, updated_at,
               last_login_at, role, session_expires_at
        FROM users
        WHERE session_token = $1
          AND session_expires_at > NOW()
          AND is_active = true
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, token_hash)

    if row is None:
        return None

    return dict(row)


async def destroy_session(pool: Pool, user_id: UUID) -> None:
    """Destroy a user's session.

    Args:
        pool: Database connection pool.
        user_id: The user whose session to destroy.
    """
    query = """
        UPDATE users
        SET session_token = NULL, session_expires_at = NULL
        WHERE id = $1
    """
    async with pool.acquire() as conn:
        await conn.execute(query, str(user_id))


# --- Password Utilities ---


def hash_password(password: str) -> str:
    """Hash a password with bcrypt.

    Args:
        password: The plaintext password.

    Returns:
        The bcrypt hash string.
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


async def set_user_password(pool: Pool, user_id: UUID, password: str) -> None:
    """Set or update a user's password.

    Args:
        pool: Database connection pool.
        user_id: The user's UUID.
        password: The new plaintext password (will be hashed).
    """
    pw_hash = hash_password(password)
    query = "UPDATE users SET password_hash = $1 WHERE id = $2"
    async with pool.acquire() as conn:
        await conn.execute(query, pw_hash, str(user_id))


# --- Provider Factory ---


_provider_classes: dict[str, type[AuthProvider]] = {
    "basic": BasicAuthProvider,
    "api_key": ApiKeyAuthProvider,
}


def get_auth_provider_name() -> str:
    """Get the configured auth provider name."""
    return os.environ.get("LUCENT_AUTH_PROVIDER", "basic").lower().strip()


async def get_auth_provider() -> AuthProvider:
    """Get the configured authentication provider instance.

    Configured via LUCENT_AUTH_PROVIDER env var. Defaults to 'basic'.

    Returns:
        An AuthProvider instance.
    """
    provider_name = get_auth_provider_name()
    pool = await get_pool()

    provider_class = _provider_classes.get(provider_name)
    if provider_class is None:
        logger.warning(
            f"Unknown auth provider '{provider_name}', falling back to 'basic'. "
            f"Available: {', '.join(_provider_classes.keys())}"
        )
        provider_class = BasicAuthProvider

    return provider_class(pool)


def register_auth_provider(name: str, provider_class: type[AuthProvider]) -> None:
    """Register a custom auth provider.

    This allows extensions to add their own auth backends (OAuth, SAML, etc.).

    Args:
        name: The provider name (used in LUCENT_AUTH_PROVIDER).
        provider_class: The AuthProvider subclass.
    """
    _provider_classes[name] = provider_class
    logger.info(f"Registered auth provider: {name}")


# --- First-Run Detection ---


async def is_first_run(pool: Pool) -> bool:
    """Check if this is the first run (no users exist).

    Returns:
        True if no users exist in the database.
    """
    query = "SELECT EXISTS(SELECT 1 FROM users LIMIT 1)"
    async with pool.acquire() as conn:
        return not await conn.fetchval(query)


async def create_initial_user(
    pool: Pool,
    display_name: str,
    email: str | None,
    password: str,
) -> tuple[dict[str, Any], str]:
    """Create the initial user and organization during first-run setup.

    Args:
        pool: Database connection pool.
        display_name: The user's display name.
        email: Optional email address.
        password: The user's password.

    Returns:
        Tuple of (user record, api_key string).
    """
    from lucent.db import ApiKeyRepository, OrganizationRepository

    # Create organization
    org_repo = OrganizationRepository(pool)
    org, _ = await org_repo.get_or_create(name=f"{display_name}'s Organization")

    # Create user
    user_repo = UserRepository(pool)
    user = await user_repo.create(
        external_id=display_name.lower().replace(" ", "-"),
        provider="basic",
        organization_id=org["id"],
        email=email,
        display_name=display_name,
    )

    # Set password
    await set_user_password(pool, user["id"], password)

    # Promote to owner
    user = await user_repo.update_role(user["id"], "owner")

    # Create an API key
    api_key_repo = ApiKeyRepository(pool)
    key_name = "Default API Key"
    _key_record, raw_key = await api_key_repo.create(
        user_id=user["id"],
        organization_id=org["id"],
        name=key_name,
    )

    logger.info(f"Initial user created: {display_name} (owner)")

    return user, raw_key
