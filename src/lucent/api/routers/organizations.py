"""Organization management API endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from lucent.api.deps import AuthenticatedUser, OwnerUser
from lucent.api.models import (
    ErrorResponse,
    OrganizationCreate,
    OrganizationListResponse,
    OrganizationResponse,
    OrganizationUpdate,
    SuccessResponse,
)
from lucent.db import OrganizationRepository, UserRepository, get_pool
from lucent.logging import get_logger
from lucent.rbac import Permission

logger = get_logger(__name__)


router = APIRouter()


def _org_to_response(org: dict[str, Any]) -> OrganizationResponse:
    """Convert an organization dict to a response model."""
    return OrganizationResponse(
        id=org["id"],
        name=org["name"],
        created_at=org["created_at"],
        updated_at=org["updated_at"],
    )


@router.get(
    "/current",
    response_model=OrganizationResponse,
)
async def get_current_organization(
    user: AuthenticatedUser,
) -> OrganizationResponse:
    """Get the current user's organization."""
    user.require_permission(Permission.ORG_VIEW)

    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)

    org = await org_repo.get_by_id(user.organization_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    return _org_to_response(org)


@router.patch(
    "/current",
    response_model=OrganizationResponse,
)
async def update_current_organization(
    data: OrganizationUpdate,
    user: OwnerUser,  # Requires owner role
) -> OrganizationResponse:
    """Update the current organization's settings.
    
    Requires owner role.
    """
    user.require_permission(Permission.ORG_UPDATE)

    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)

    result = await org_repo.update(
        organization_id=user.organization_id,
        name=data.name,
    )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    return _org_to_response(result)


@router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    data: OrganizationCreate,
    user: OwnerUser,
) -> OrganizationResponse:
    """Create a new organization.
    
    Requires owner role.
    """
    pool = await get_pool()
    org_repo = OrganizationRepository(pool)

    org, created = await org_repo.get_or_create(name=data.name)

    if not created:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization with this name already exists",
        )

    logger.info("Organization created: id=%s, name=%s, by=%s", org["id"], data.name, user.id)
    return _org_to_response(org)


@router.get(
    "/{organization_id}",
    response_model=OrganizationResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_organization(
    organization_id: UUID,
    user: AuthenticatedUser,
) -> OrganizationResponse:
    """Get an organization by ID.
    
    Users can only view their own organization.
    """
    # Only allow viewing own organization
    if organization_id != user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view your own organization",
        )

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)

    org = await org_repo.get_by_id(organization_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    return _org_to_response(org)


@router.get(
    "",
    response_model=OrganizationListResponse,
)
async def list_organizations(
    user: OwnerUser,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> OrganizationListResponse:
    """List all organizations.
    
    Requires owner role.
    """
    pool = await get_pool()
    org_repo = OrganizationRepository(pool)

    result = await org_repo.list(offset=offset, limit=limit)

    return OrganizationListResponse(
        organizations=[_org_to_response(o) for o in result["organizations"]],
        total_count=result["total_count"],
        offset=result["offset"],
        limit=result["limit"],
        has_more=result["has_more"],
    )


@router.delete(
    "/current",
    response_model=SuccessResponse,
)
async def delete_current_organization(
    user: OwnerUser,  # Requires owner role
) -> SuccessResponse:
    """Delete the current organization.
    
    Requires owner role. This will delete:
    - All organization memories
    - All organization users
    - The organization itself
    
    This action cannot be undone.
    """
    user.require_permission(Permission.ORG_DELETE)

    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)

    success = await org_repo.delete(user.organization_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    logger.info("Organization deleted: id=%s, by=%s", user.organization_id, user.id)
    return SuccessResponse(
        success=True,
        message=f"Organization {user.organization_id} deleted",
    )


@router.post(
    "/current/transfer",
    response_model=SuccessResponse,
)
async def transfer_ownership(
    new_owner_id: UUID,
    user: OwnerUser,  # Requires owner role
) -> SuccessResponse:
    """Transfer organization ownership to another user.
    
    Requires owner role. The new owner must be a member of the organization.
    The current owner will be demoted to admin.
    """
    user.require_permission(Permission.ORG_TRANSFER)

    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )

    if new_owner_id == user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You are already the owner",
        )

    pool = await get_pool()
    user_repo = UserRepository(pool)

    # Get new owner
    new_owner = await user_repo.get_by_id(new_owner_id)
    if new_owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Check same organization
    if new_owner.get("organization_id") != user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of your organization",
        )

    # Promote new owner
    await user_repo.update_role(new_owner_id, "owner")

    # Demote current owner to admin
    await user_repo.update_role(user.id, "admin")

    logger.info("Ownership transferred: org=%s, from=%s, to=%s", user.organization_id, user.id, new_owner_id)
    return SuccessResponse(
        success=True,
        message=f"Ownership transferred to user {new_owner_id}",
    )
