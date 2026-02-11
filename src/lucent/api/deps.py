"""Authentication and authorization dependencies for API endpoints."""

import os
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, Header, Cookie, Request, status

from lucent.auth import (
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


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    """Get the current authenticated user for API routes.
    
    API key authentication is always required.
    
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
