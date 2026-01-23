"""User management API endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from mnememcp.api.deps import AuthenticatedUser, AdminUser, OwnerUser
from mnememcp.api.models import (
    UserCreate,
    UserUpdate,
    UserRoleUpdate,
    UserResponse,
    UserListResponse,
    ErrorResponse,
    SuccessResponse,
)
from mnememcp.db import UserRepository, get_pool
from mnememcp.rbac import Role, can_manage_user, can_assign_role, Permission


router = APIRouter()


def _user_to_response(user: dict[str, Any]) -> UserResponse:
    """Convert a user dict to a response model."""
    return UserResponse(
        id=user["id"],
        external_id=user["external_id"],
        provider=user["provider"],
        organization_id=user.get("organization_id"),
        email=user.get("email"),
        display_name=user.get("display_name"),
        avatar_url=user.get("avatar_url"),
        role=user.get("role", "member"),
        is_active=user.get("is_active", True),
        created_at=user["created_at"],
        updated_at=user["updated_at"],
        last_login_at=user.get("last_login_at"),
    )


@router.get(
    "/me",
    response_model=UserResponse,
)
async def get_current_user_info(
    user: AuthenticatedUser,
) -> UserResponse:
    """Get the current authenticated user's information."""
    pool = await get_pool()
    user_repo = UserRepository(pool)
    
    db_user = await user_repo.get_by_id(user.id)
    if db_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    return _user_to_response(db_user)


@router.patch(
    "/me",
    response_model=UserResponse,
)
async def update_current_user(
    data: UserUpdate,
    user: AuthenticatedUser,
) -> UserResponse:
    """Update the current user's profile."""
    pool = await get_pool()
    user_repo = UserRepository(pool)
    
    result = await user_repo.update(
        user_id=user.id,
        email=data.email,
        display_name=data.display_name,
        avatar_url=data.avatar_url,
    )
    
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    return _user_to_response(result)


@router.get(
    "",
    response_model=UserListResponse,
)
async def list_organization_users(
    user: AuthenticatedUser,
    role: str | None = None,
) -> UserListResponse:
    """List all users in the current user's organization."""
    user.require_permission(Permission.USERS_VIEW)
    
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )
    
    pool = await get_pool()
    user_repo = UserRepository(pool)
    
    users = await user_repo.get_by_organization(
        organization_id=user.organization_id,
        role=role,
    )
    
    return UserListResponse(
        users=[_user_to_response(u) for u in users],
        total_count=len(users),
    )


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_user(
    user_id: UUID,
    user: AuthenticatedUser,
) -> UserResponse:
    """Get a user by ID.
    
    Users can view others in their organization.
    """
    user.require_permission(Permission.USERS_VIEW)
    
    pool = await get_pool()
    user_repo = UserRepository(pool)
    
    db_user = await user_repo.get_by_id(user_id)
    if db_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    # Check same organization
    if db_user.get("organization_id") != user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    return _user_to_response(db_user)


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    data: UserCreate,
    user: AdminUser,  # Requires admin role
) -> UserResponse:
    """Create a new user in the organization.
    
    Requires admin or owner role.
    """
    user.require_permission(Permission.USERS_INVITE)
    
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )
    
    # Check if the admin can assign this role
    if not can_assign_role(user.role, data.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot assign the '{data.role}' role",
        )
    
    pool = await get_pool()
    user_repo = UserRepository(pool)
    
    # Check if user already exists
    existing = await user_repo.get_by_external_id(data.external_id, data.provider)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this external ID already exists",
        )
    
    new_user = await user_repo.create(
        external_id=data.external_id,
        provider=data.provider,
        organization_id=user.organization_id,
        email=data.email,
        display_name=data.display_name,
        avatar_url=data.avatar_url,
    )
    
    # Update role if not member
    if data.role != "member":
        new_user = await user_repo.update_role(new_user["id"], data.role)
    
    return _user_to_response(new_user)


@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    responses={404: {"model": ErrorResponse}},
)
async def update_user(
    user_id: UUID,
    data: UserUpdate,
    user: AdminUser,  # Requires admin role
) -> UserResponse:
    """Update a user's profile.
    
    Requires admin or owner role.
    """
    user.require_permission(Permission.USERS_MANAGE)
    
    pool = await get_pool()
    user_repo = UserRepository(pool)
    
    # Get target user
    target = await user_repo.get_by_id(user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    # Check same organization
    if target.get("organization_id") != user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    # Check if admin can manage this user
    if not can_manage_user(user.role, target.get("role", "member")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot manage this user",
        )
    
    result = await user_repo.update(
        user_id=user_id,
        email=data.email,
        display_name=data.display_name,
        avatar_url=data.avatar_url,
        is_active=data.is_active,
    )
    
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    return _user_to_response(result)


@router.patch(
    "/{user_id}/role",
    response_model=UserResponse,
    responses={404: {"model": ErrorResponse}},
)
async def update_user_role(
    user_id: UUID,
    data: UserRoleUpdate,
    user: AdminUser,  # Requires admin role
) -> UserResponse:
    """Update a user's role.
    
    Requires admin or owner role.
    Admins can only set member role.
    Owners can set any role.
    """
    user.require_permission(Permission.USERS_MANAGE)
    
    pool = await get_pool()
    user_repo = UserRepository(pool)
    
    # Get target user
    target = await user_repo.get_by_id(user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    # Check same organization
    if target.get("organization_id") != user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    # Check if admin can manage this user
    if not can_manage_user(user.role, target.get("role", "member")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot manage this user",
        )
    
    # Check if admin can assign this role
    if not can_assign_role(user.role, data.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot assign the '{data.role}' role",
        )
    
    # Validate role value
    valid_roles = ["member", "admin", "owner"]
    if data.role not in valid_roles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}",
        )
    
    result = await user_repo.update_role(user_id, data.role)
    
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    return _user_to_response(result)


@router.delete(
    "/{user_id}",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}},
)
async def delete_user(
    user_id: UUID,
    user: AdminUser,  # Requires admin role
) -> SuccessResponse:
    """Delete a user from the organization.
    
    Requires admin or owner role.
    This will also delete all of the user's memories.
    """
    user.require_permission(Permission.USERS_MANAGE)
    
    # Prevent self-deletion
    if user_id == user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete yourself",
        )
    
    pool = await get_pool()
    user_repo = UserRepository(pool)
    
    # Get target user
    target = await user_repo.get_by_id(user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    # Check same organization
    if target.get("organization_id") != user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    # Check if admin can manage this user
    if not can_manage_user(user.role, target.get("role", "member")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot delete this user",
        )
    
    success = await user_repo.delete(user_id)
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    return SuccessResponse(success=True, message=f"User {user_id} deleted")
