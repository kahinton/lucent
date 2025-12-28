"""Authentication and user context management for Hindsight."""

import os
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from hindsight.db.client import UserRepository, get_pool

# Context variable to store the current user for the request
_current_user: ContextVar[dict[str, Any] | None] = ContextVar("current_user", default=None)

# Development mode settings
DEV_MODE = os.environ.get("HINDSIGHT_DEV_MODE", "false").lower() in ("true", "1", "yes")
DEV_USER_ID = os.environ.get("HINDSIGHT_DEV_USER_ID", "dev-user")
DEV_USER_NAME = os.environ.get("HINDSIGHT_DEV_USER_NAME", "Development User")


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


def get_current_user_id() -> UUID | None:
    """Get the current user's ID.
    
    Returns:
        The current user's UUID, or None if not authenticated.
    """
    user = get_current_user()
    if user:
        return user["id"]
    return None


async def ensure_dev_user() -> dict[str, Any]:
    """Ensure the development user exists and return it.
    
    This is used in DEV_MODE to create/get a local user for testing
    without requiring external authentication.
    
    Returns:
        The development user record.
    """
    pool = await get_pool()
    user_repo = UserRepository(pool)
    
    user, created = await user_repo.get_or_create(
        external_id=DEV_USER_ID,
        provider="local",
        email="dev@localhost",
        display_name=DEV_USER_NAME,
        provider_metadata={"dev_mode": True},
    )
    
    if created:
        print(f"Created development user: {user['id']}")
    
    return user


async def get_or_create_user_from_oauth(
    provider: str,
    external_id: str,
    email: str | None = None,
    display_name: str | None = None,
    avatar_url: str | None = None,
    provider_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Get or create a user from OAuth provider data.
    
    Args:
        provider: The OAuth provider (google, github).
        external_id: The user's ID from the provider.
        email: The user's email.
        display_name: The user's display name.
        avatar_url: URL to the user's avatar.
        provider_metadata: Additional provider-specific data.
        
    Returns:
        The user record.
    """
    pool = await get_pool()
    user_repo = UserRepository(pool)
    
    user, created = await user_repo.get_or_create(
        external_id=external_id,
        provider=provider,
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


def is_dev_mode() -> bool:
    """Check if running in development mode.
    
    Returns:
        True if HINDSIGHT_DEV_MODE is enabled.
    """
    return DEV_MODE
