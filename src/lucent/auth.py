"""Authentication and user context management for Lucent.

This module uses ContextVars to store request-scoped user context.
ContextVars are automatically scoped per asyncio task, so each request
gets isolated context without interference from other requests.

Important: When setting context vars in middleware (like MCPAuthMiddleware),
use try/finally to ensure cleanup happens even on errors. For FastAPI
dependencies, context is naturally scoped to the request lifecycle.

Impersonation Feature:
    Allows admins/owners to view the system as another user for debugging
    and support purposes. Impersonation is cookie-based (lucent_impersonate)
    and is only available through the web UI (team mode only).
    
    Rules:
    - Owners can impersonate anyone except other owners
    - Admins can only impersonate members
    - Impersonation is logged and tracked via impersonator_id in CurrentUser
    - The original user is stored in _impersonating_user context var
    
    See: web/routes.py for /users/{id}/impersonate and /users/stop-impersonation
"""

import os
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from lucent.db import OrganizationRepository, UserRepository, get_pool, init_db

# Context variable to store the current user for the request
_current_user: ContextVar[dict[str, Any] | None] = ContextVar("current_user", default=None)

# Context variable to store the current API key ID (when authenticated via API key)
_current_api_key_id: ContextVar[UUID | None] = ContextVar("current_api_key_id", default=None)

# Context variable to store impersonation info (original user when impersonating)
_impersonating_user: ContextVar[dict[str, Any] | None] = ContextVar("impersonating_user", default=None)


async def _ensure_pool():
    """Ensure the database pool is initialized, lazily initializing if needed."""
    try:
        return await get_pool()
    except RuntimeError:
        # Pool not initialized yet, initialize it now
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL environment variable is required")
        return await init_db(database_url)


def get_current_user() -> dict[str, Any] | None:
    """Get the current authenticated user from context.
    
    Returns:
        The current user dict, or None if not authenticated.
    """
    return _current_user.get()


def set_current_user(user: dict[str, Any] | None) -> None:
    """Set the current authenticated user in context.
    
    Args:
        user: The user dict to set as current, or None to clear.
    """
    _current_user.set(user)


def get_current_api_key_id() -> UUID | None:
    """Get the current API key ID from context.
    
    Returns:
        The API key UUID if authenticated via API key, or None otherwise.
    """
    return _current_api_key_id.get()


def set_current_api_key_id(api_key_id: UUID | None) -> None:
    """Set the current API key ID in context.
    
    Args:
        api_key_id: The API key UUID, or None to clear.
    """
    _current_api_key_id.set(api_key_id)


def get_impersonating_user() -> dict[str, Any] | None:
    """Get the original user who is impersonating.
    
    Returns:
        The original user dict if currently impersonating, or None.
    """
    return _impersonating_user.get()


def set_impersonating_user(user: dict[str, Any] | None) -> None:
    """Set the original user who is doing the impersonation.
    
    Args:
        user: The original admin/owner user, or None to clear.
    """
    _impersonating_user.set(user)


def is_impersonating() -> bool:
    """Check if the current request is in impersonation mode.
    
    Returns:
        True if impersonating another user.
    """
    return _impersonating_user.get() is not None


def get_current_user_id() -> UUID | None:
    """Get the current user's ID.
    
    Returns:
        The current user's UUID, or None if not authenticated.
    """
    user = get_current_user()
    if user:
        return user["id"]
    return None


async def get_or_create_user_from_oauth(
    provider: str,
    external_id: str,
    organization_id: UUID,
    email: str | None = None,
    display_name: str | None = None,
    avatar_url: str | None = None,
    provider_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Get or create a user from OAuth provider data.
    
    Args:
        provider: The OAuth provider (google, github).
        external_id: The user's ID from the provider.
        organization_id: The organization this user belongs to.
        email: The user's email.
        display_name: The user's display name.
        avatar_url: URL to the user's avatar.
        provider_metadata: Additional provider-specific data.
        
    Returns:
        The user record.
    """
    pool = await _ensure_pool()
    user_repo = UserRepository(pool)
    
    user, created = await user_repo.get_or_create(
        external_id=external_id,
        provider=provider,
        organization_id=organization_id,
        email=email,
        display_name=display_name,
        avatar_url=avatar_url,
        provider_metadata=provider_metadata,
    )
    
    if not created:
        # Update user info from provider on each login
        user = await user_repo.update(
            user_id=user["id"],
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
            provider_metadata=provider_metadata,
        )
        await user_repo.update_last_login(user["id"])
    
    return user
