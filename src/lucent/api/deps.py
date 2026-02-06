"""Authentication and authorization dependencies for API endpoints."""

import os
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, Header, Cookie, Request, status

from lucent.auth import (
    ensure_dev_user, 
    is_dev_mode, 
    set_current_user,
    get_impersonating_user,
    set_impersonating_user,
    is_impersonating,
)
from lucent.db import UserRepository, ApiKeyRepository, get_pool
from lucent.rbac import Permission, Role, has_permission, PermissionError as RBACPermissionError


class CurrentUser:
    """Dependency that provides the current authenticated user."""
    
    def __init__(
        self,
        id: UUID,
        organization_id: UUID | None,
        role: str,
        email: str | None,
        display_name: str | None,
        auth_method: str = "session",  # "session", "api_key", "oauth"
        api_key_id: UUID | None = None,  # Set when authenticated via API key
        impersonator_id: UUID | None = None,  # Set when being impersonated
        impersonator_display_name: str | None = None,  # For UI display
    ):
        self.id = id
        self.organization_id = organization_id
        self.role = Role.from_string(role)
        self.email = email
        self.display_name = display_name
        self.auth_method = auth_method
        self.api_key_id = api_key_id
        self.impersonator_id = impersonator_id
        self.impersonator_display_name = impersonator_display_name
    
    @property
    def is_impersonated(self) -> bool:
        """Check if this user is being impersonated by another user."""
        return self.impersonator_id is not None
    
    def has_permission(self, permission: Permission) -> bool:
        """Check if this user has a specific permission."""
        return has_permission(self.role, permission)
    
    def require_permission(self, permission: Permission) -> None:
        """Raise HTTPException if user doesn't have permission."""
        if not self.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission.value}",
            )
    
    def get_audit_context(self) -> dict[str, Any]:
        """Get context dict for audit logging."""
        ctx = {"auth_method": self.auth_method}
        if self.api_key_id:
            ctx["api_key_id"] = str(self.api_key_id)
        if self.impersonator_id:
            ctx["impersonator_id"] = str(self.impersonator_id)
            ctx["impersonator_display_name"] = self.impersonator_display_name
            ctx["is_impersonated"] = True
        return ctx


async def _authenticate_with_api_key(api_key: str) -> CurrentUser | None:
    """Authenticate using an API key.
    
    Args:
        api_key: The API key (with or without 'Bearer ' prefix).
        
    Returns:
        CurrentUser if valid, None otherwise.
    """
    # Strip 'Bearer ' prefix if present
    if api_key.startswith("Bearer "):
        api_key = api_key[7:]
    
    if not api_key.startswith("mcp_"):
        return None
    
    pool = await get_pool()
    api_key_repo = ApiKeyRepository(pool)
    
    key_info = await api_key_repo.verify(api_key)
    if not key_info:
        return None
    
    # Get the full user record
    user_repo = UserRepository(pool)
    user = await user_repo.get_by_id(key_info["user_id"])
    if not user:
        return None
    
    set_current_user(user)
    return CurrentUser(
        id=user["id"],
        organization_id=user.get("organization_id"),
        role=user.get("role", "member"),
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_id=key_info["id"],  # Include the API key ID for auditing
    )


async def get_current_user_for_web(
    authorization: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header(alias="X-User-ID")] = None,
    impersonate_user_id: Annotated[str | None, Cookie(alias="lucent_impersonate")] = None,
) -> CurrentUser:
    """Get the current authenticated user for WEB UI routes.
    
    This allows dev mode fallback for browser-based access.
    Also supports impersonation for org owners/admins.
    
    Supports multiple auth methods:
    - API keys (Authorization: Bearer mcp_...)
    - Dev mode with optional X-User-ID override (web UI only)
    - OAuth tokens (future)
    - Impersonation via cookie (for admins/owners)
    
    Headers:
        Authorization: Bearer token (API key or OAuth token)
        X-User-ID: User ID override (for dev mode only)
    Cookies:
        lucent_impersonate: User ID to impersonate (requires admin/owner)
    """
    # Track the original authenticated user for impersonation
    original_user: CurrentUser | None = None
    
    # Try API key authentication first (works in both dev and prod)
    if authorization:
        user = await _authenticate_with_api_key(authorization)
        if user:
            original_user = user
        elif "mcp_" in authorization:
            # If it looks like an API key but failed, reject it
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
    
    # Dev mode: use dev user or allow override (WEB UI ONLY)
    if original_user is None and is_dev_mode():
        if x_user_id:
            # In dev mode, allow specifying a user ID for testing
            pool = await get_pool()
            user_repo = UserRepository(pool)
            user = await user_repo.get_by_id(UUID(x_user_id))
            if user:
                set_current_user(user)
                original_user = CurrentUser(
                    id=user["id"],
                    organization_id=user.get("organization_id"),
                    role=user.get("role", "member"),
                    email=user.get("email"),
                    display_name=user.get("display_name"),
                    auth_method="session",
                )
        
        if original_user is None:
            # Default to dev user
            dev_user = await ensure_dev_user()
            set_current_user(dev_user)
            original_user = CurrentUser(
                id=dev_user["id"],
                organization_id=dev_user.get("organization_id"),
                role=dev_user.get("role", "member"),
                email=dev_user.get("email"),
                display_name=dev_user.get("display_name"),
                auth_method="session",
            )
    
    # Production: Require authorization header
    if original_user is None:
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization header required. Use an API key (Bearer mcp_...) or OAuth token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        # TODO: Add OAuth token validation here when implemented
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization. Use a valid API key (Bearer mcp_...).",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Handle impersonation if cookie is set
    if impersonate_user_id:
        # Check if original user can impersonate (admin or owner)
        if original_user.role not in (Role.ADMIN, Role.OWNER):
            # Silently ignore impersonation cookie for non-admins
            return original_user
        
        try:
            target_user_id = UUID(impersonate_user_id)
        except ValueError:
            # Invalid UUID, ignore
            return original_user
        
        # Can't impersonate yourself
        if target_user_id == original_user.id:
            return original_user
        
        pool = await get_pool()
        user_repo = UserRepository(pool)
        target_user = await user_repo.get_by_id(target_user_id)
        
        if target_user is None:
            # Target user not found, ignore
            return original_user
        
        # Check same organization
        if target_user.get("organization_id") != original_user.organization_id:
            # Can't impersonate users from other orgs
            return original_user
        
        # Admins can only impersonate members
        # Owners can impersonate anyone except other owners
        target_role = target_user.get("role", "member")
        if original_user.role == Role.ADMIN and target_role != "member":
            return original_user
        if original_user.role == Role.OWNER and target_role == "owner":
            return original_user
        
        # Set up impersonation context
        set_current_user(target_user)
        set_impersonating_user({
            "id": original_user.id,
            "display_name": original_user.display_name,
            "role": original_user.role.value,
        })
        
        return CurrentUser(
            id=target_user["id"],
            organization_id=target_user.get("organization_id"),
            role=target_user.get("role", "member"),
            email=target_user.get("email"),
            display_name=target_user.get("display_name"),
            auth_method="impersonation",
            impersonator_id=original_user.id,
            impersonator_display_name=original_user.display_name,
        )
    
    return original_user


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    """Get the current authenticated user for API routes.
    
    API key authentication is ALWAYS required, even in dev mode.
    Dev mode only bypasses auth for the web UI, not for programmatic API access.
    
    Headers:
        Authorization: Bearer mcp_... (API key required)
    """
    # Try API key authentication
    if authorization:
        user = await _authenticate_with_api_key(authorization)
        if user:
            return user
        
        # Invalid API key
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # No authorization header - always reject for API routes
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API key required. Use Authorization: Bearer mcp_your_key_here",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_optional_user(
    authorization: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header(alias="X-User-ID")] = None,
) -> CurrentUser | None:
    """Get the current user if authenticated, or None."""
    try:
        return await get_current_user(authorization, x_user_id)
    except HTTPException:
        return None


def require_role(minimum_role: Role):
    """Dependency factory that requires a minimum role level.
    
    Usage:
        @router.get("/admin-only")
        async def admin_endpoint(user: CurrentUser = Depends(require_role(Role.ADMIN))):
            ...
    """
    async def check_role(
        user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if user.role < minimum_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {minimum_role.value} role or higher",
            )
        return user
    
    return check_role


def require_permission_dep(permission: Permission):
    """Dependency factory that requires a specific permission.
    
    Usage:
        @router.get("/audit")
        async def audit_endpoint(user: CurrentUser = Depends(require_permission_dep(Permission.AUDIT_VIEW_ORG))):
            ...
    """
    async def check_permission(
        user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if not user.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission.value}",
            )
        return user
    
    return check_permission


# Type alias for dependency injection
AuthenticatedUser = Annotated[CurrentUser, Depends(get_current_user)]
OptionalUser = Annotated[CurrentUser | None, Depends(get_optional_user)]
AdminUser = Annotated[CurrentUser, Depends(require_role(Role.ADMIN))]
OwnerUser = Annotated[CurrentUser, Depends(require_role(Role.OWNER))]
