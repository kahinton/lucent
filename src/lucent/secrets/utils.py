"""Shared helpers for resolving runtime credential references in configs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from lucent.db.pool import scoped_acquire
from lucent.auth import get_current_user
from lucent.db import get_pool
from lucent.secrets.base import SecretProvider, SecretScope

SECRET_REF_PREFIX = "secret://"
CREDENTIAL_REF_PREFIX = "credential://"

_SENSITIVE_ENV_KEY_TOKENS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "client_secret",
    "private_key",
    "access_key",
    "auth",
)


def is_sensitive_env_key(key: str) -> bool:
    """Return whether an environment variable name conventionally carries a secret."""
    normalized = key.lower()
    return any(token in normalized for token in _SENSITIVE_ENV_KEY_TOKENS)


def validate_env_var_references(env_vars: dict[str, str] | None) -> None:
    """Require sensitive environment values to use a just-in-time reference."""
    for key, value in (env_vars or {}).items():
        if not is_sensitive_env_key(str(key)):
            continue
        if is_secret_reference(value) or is_credential_reference(value):
            continue
        raise ValueError(
            f"Environment variable '{key}' is sensitive and must use a "
            "secret:// or credential:// reference."
        )


def validate_sandbox_config_references(config: dict | str | None) -> None:
    """Validate secret references in a sandbox configuration's environment."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (TypeError, ValueError):
            return
    if not isinstance(config, dict) or "env_vars" not in config:
        return
    env_vars = config["env_vars"]
    if not isinstance(env_vars, dict):
        raise ValueError("sandbox_config.env_vars must be a JSON object")
    validate_env_var_references(env_vars)


def is_secret_reference(value: str) -> bool:
    """Return True if value uses the secret:// convention."""
    return isinstance(value, str) and value.startswith(SECRET_REF_PREFIX)


def is_credential_reference(value: str) -> bool:
    """Return True if value uses the credential:// convention."""
    return isinstance(value, str) and value.startswith(CREDENTIAL_REF_PREFIX)


def secret_key_from_reference(value: str) -> str:
    """Extract key name from secret:// reference."""
    return value[len(SECRET_REF_PREFIX):].strip()


async def _candidate_scopes(organization_id: str, user_id: str) -> list[SecretScope]:
    scopes = [SecretScope(organization_id=organization_id, owner_user_id=user_id)]
    try:
        pool = await get_pool()
    except Exception:
        return scopes

    try:
        async with scoped_acquire(
            organization_id=organization_id, user_id=user_id
        ) as conn:
            rows = await conn.fetch(
                "SELECT group_id FROM user_groups WHERE user_id = $1",
                user_id,
            )
        for row in rows:
            scopes.append(SecretScope(organization_id=organization_id, owner_group_id=str(row["group_id"])))
    except Exception:
        return scopes
    return scopes


async def resolve_secret_reference(
    value: str, secret_provider: SecretProvider, *, env_key: str = ""
) -> str:
    """Resolve a single secret:// reference string to its secret value."""
    user = get_current_user()
    if not user or not user.get("organization_id") or not user.get("id"):
        raise ValueError("Cannot resolve secret references without authenticated user context")

    secret_key = secret_key_from_reference(value)
    if not secret_key:
        target = f" for env var '{env_key}'" if env_key else ""
        raise ValueError(f"Invalid secret reference{target}: missing key name")

    scopes = await _candidate_scopes(str(user["organization_id"]), str(user["id"]))
    for scope in scopes:
        secret_value = await secret_provider.get(secret_key, scope)
        if secret_value is not None:
            return secret_value

    target = f" for env var '{env_key}'" if env_key else ""
    raise KeyError(f"Secret not found{target} (reference '{secret_key}')")


async def _get_connection_access_token(integration_type: str, user_id: str) -> str | None:
    """Return the latest active user connection access token for an integration."""
    pool = await get_pool()
    user = get_current_user() or {}
    async with scoped_acquire(
        organization_id=user.get("organization_id") or "",
        user_id=user.get("id") or "",
        role="system" if not user.get("organization_id") else None,
    ) as conn:
        rows = await conn.fetch(
            """
            SELECT encrypted_secret_payload, access_token_expires_at
            FROM enterprise_credentials
            WHERE integration_type = $1
              AND scope_type = 'user'
              AND owner_user_id = $2
              AND status = 'active'
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 5
            """,
            integration_type,
            user_id,
        )
    if not rows:
        return None

    from lucent.integrations.encryption import BackendCredentialEncryptor, EncryptionError

    try:
        encryptor = BackendCredentialEncryptor()
    except EncryptionError:
        return None
    now = datetime.now(UTC)
    for row in rows:
        expires_at = row["access_token_expires_at"]
        if expires_at is not None and expires_at <= now:
            continue
        try:
            payload = encryptor.decrypt(row["encrypted_secret_payload"])
        except Exception:
            continue
        token = payload.get("access_token")
        if token:
            return str(token)
    return None


async def resolve_credential_reference(
    value: str, secret_provider: SecretProvider, *, env_key: str = ""
) -> str:
    """Resolve a credential:// reference to a user connection token.

    Supported form:
        credential://github/access_token?fallback_secret=github.mcp.token

    The lookup is scoped to the current Lucent user. ``fallback_secret`` is
    optional and lets deployments keep working with a manually-entered secret
    when the user has not connected the provider in Settings → Connections.
    """
    user = get_current_user()
    if not user or not user.get("organization_id") or not user.get("id"):
        raise ValueError("Cannot resolve credential references without authenticated user context")

    parsed = urlparse(value)
    integration_type = (parsed.netloc or "").strip().lower()
    field = parsed.path.strip("/") or "access_token"
    if not integration_type:
        target = f" for env var '{env_key}'" if env_key else ""
        raise ValueError(f"Invalid credential reference{target}: missing integration type")
    if field != "access_token":
        target = f" for env var '{env_key}'" if env_key else ""
        raise ValueError(
            f"Invalid credential reference{target}: unsupported field '{field}'"
        )

    token = await _get_connection_access_token(integration_type, str(user["id"]))
    if token:
        return token

    query = parse_qs(parsed.query or "")
    fallback_secret = (query.get("fallback_secret") or query.get("fallback") or [""])[0]
    if fallback_secret:
        fallback_ref = (
            fallback_secret
            if fallback_secret.startswith(SECRET_REF_PREFIX)
            else f"{SECRET_REF_PREFIX}{fallback_secret}"
        )
        return await resolve_secret_reference(fallback_ref, secret_provider, env_key=env_key)

    target = f" for env var '{env_key}'" if env_key else ""
    raise KeyError(
        f"Credential not found{target} (reference '{integration_type}/{field}')"
    )


async def resolve_env_vars(env_vars: dict[str, str], secret_provider: SecretProvider) -> dict[str, str]:
    """Resolve runtime references in env vars using current user context."""
    validate_env_var_references(env_vars)
    resolved: dict[str, str] = {}
    for key, value in env_vars.items():
        if is_secret_reference(value):
            resolved[key] = await resolve_secret_reference(value, secret_provider, env_key=key)
        elif is_credential_reference(value):
            resolved[key] = await resolve_credential_reference(value, secret_provider, env_key=key)
        else:
            resolved[key] = value

    return resolved
