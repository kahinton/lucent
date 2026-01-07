"""Authentication and authorization dependencies for API endpoints."""

import os
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, Header, status

from mnememcp.auth import ensure_dev_user, is_dev_mode, set_current_user
from mnememcp.db.client import UserRepository, get_pool
from mnememcp.rbac import Permission, Role, has_permission, PermissionError as RBACPermissionError


class CurrentUser:
    """Dependency that provides the current authenticated user."""
    
    def __init__(
        self,
        id: UUID,
        organization_id: UUID | None,
        role: str,
        email: str | None,
        display_name: str | None,
    ):
        self.id = id
        self.organization_id = organization_id
        self.role = Role.from_string(role)
        self.email = email
        self.display_name = display_name
    
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


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header(alias="X-User-ID")] = None,
) -> CurrentUser:
    """Get the current authenticated user.
    
    In production, this would validate JWT tokens or API keys.
    In dev mode, it creates/uses a dev user.
    
    Headers:
        Authorization: Bearer token (for production)
        X-User-ID: User ID override (for dev mode only)
    """
    # Dev mode: use dev user or allow override
    if is_dev_mode():
        if x_user_id:
            # In dev mode, allow specifying a user ID for testing
            pool = await get_pool()
            user_repo = UserRepository(pool)
            user = await user_repo.get_by_id(UUID(x_user_id))
            if user:
                set_current_user(user)
                return CurrentUser(
                    id=user["id"],
                    organization_id=user.get("organization_id"),
                    role=user.get("role", "member"),
                    email=user.get("email"),
                    display_name=user.get("display_name"),
                )
        
        # Default to dev user
        dev_user = await ensure_dev_user()
        set_current_user(dev_user)
        return CurrentUser(
            id=dev_user["id"],
            organization_id=dev_user.get("organization_id"),
            role=dev_user.get("role", "member"),
            email=dev_user.get("email"),
            display_name=dev_user.get("display_name"),
        )
    
    # Production: Validate authorization header
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # TODO: Implement proper JWT/API key validation
    # For now, reject all requests in non-dev mode without proper auth
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Production authentication not yet implemented",
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
