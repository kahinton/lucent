"""Access log API endpoints."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from lucent.api.deps import AdminUser, AuthenticatedUser
from lucent.api.models import (
    AccessLogEntry,
    AccessLogResponse,
    AuthAccessGrantListResponse,
    AuthAccessGrantResponse,
    AuthAccessGrantRevoke,
    AuthAccessGrantUpsert,
    MostAccessedItem,
)
from lucent.db import AccessRepository, AuthAccessRepository, get_pool
from lucent.rbac import Permission

router = APIRouter()


def _entry_to_response(entry: dict[str, Any]) -> AccessLogEntry:
    """Convert an access log entry dict to a response model."""
    return AccessLogEntry(
        id=entry["id"],
        memory_id=entry["memory_id"],
        user_id=entry.get("user_id"),
        organization_id=entry.get("organization_id"),
        access_type=entry["access_type"],
        accessed_at=entry["accessed_at"],
        context=entry.get("context", {}),
    )


def _grant_response(grant: dict[str, Any]) -> AuthAccessGrantResponse:
    """Map the database principal type back to the public grant type."""
    return AuthAccessGrantResponse(
        id=grant["id"],
        resource_type=grant["resource_type"],
        resource_id=grant["resource_id"],
        organization_id=grant["organization_id"],
        grantee_type=(
            "organization"
            if grant["principal_type"] == "org"
            else grant["principal_type"]
        ),
        grantee_id=grant["principal_id"],
        role=grant["role"],
        granted_by=grant["granted_by"],
        created_at=grant["created_at"],
        user_display_name=grant.get("user_display_name"),
        user_email=grant.get("user_email"),
        group_name=grant.get("group_name"),
        organization_name=grant.get("organization_name"),
    )


@router.get("/resources/{resource_type}/{resource_id}/grants")
async def list_resource_grants(
    resource_type: str,
    resource_id: UUID,
    user: AuthenticatedUser,
) -> AuthAccessGrantListResponse:
    """List roles granted to principals for one auth-ID-backed resource."""
    user.require_permission(Permission.ACCESS_GRANT)
    pool = await get_pool()
    try:
        grants = await AuthAccessRepository(pool).list_grants(
            resource_type,
            resource_id,
            organization_id=user.organization_id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return AuthAccessGrantListResponse(grants=[_grant_response(grant) for grant in grants])


@router.put("/resources/{resource_type}/{resource_id}/grants")
async def update_resource_grant(
    resource_type: str,
    resource_id: UUID,
    grant: AuthAccessGrantUpsert,
    user: AuthenticatedUser,
) -> AuthAccessGrantResponse:
    """Set one principal's role for one auth-ID-backed resource."""
    user.require_permission(Permission.ACCESS_GRANT)
    pool = await get_pool()
    try:
        result = await AuthAccessRepository(pool).upsert_grant(
            resource_type,
            resource_id,
            grantee_type=grant.grantee_type,
            grantee_id=grant.grantee_id,
            role=grant.role,
            granted_by=user.id,
            organization_id=user.organization_id,
        )
    except ValueError as exc:
        detail = str(exc)
        http_status = (
            status.HTTP_404_NOT_FOUND
            if "not found" in detail.lower()
            else status.HTTP_403_FORBIDDEN
            if "owner" in detail.lower()
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(http_status, detail) from exc
    return _grant_response(result)


@router.delete("/resources/{resource_type}/{resource_id}/grants")
async def delete_resource_grant(
    resource_type: str,
    resource_id: UUID,
    grant: AuthAccessGrantRevoke,
    user: AuthenticatedUser,
) -> dict[str, bool]:
    """Remove one principal's role for one auth-ID-backed resource."""
    user.require_permission(Permission.ACCESS_GRANT)
    pool = await get_pool()
    try:
        revoked = await AuthAccessRepository(pool).revoke_grant(
            resource_type,
            resource_id,
            grantee_type=grant.grantee_type,
            grantee_id=grant.grantee_id,
            granted_by=user.id,
            organization_id=user.organization_id,
        )
    except ValueError as exc:
        detail = str(exc)
        http_status = (
            status.HTTP_404_NOT_FOUND
            if "not found" in detail.lower()
            else status.HTTP_403_FORBIDDEN
            if "owner" in detail.lower()
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(http_status, detail) from exc
    if not revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Access grant not found")
    return {"revoked": True}


@router.get(
    "/memory/{memory_id}",
    response_model=AccessLogResponse,
)
async def get_memory_access_history(
    memory_id: UUID,
    user: AuthenticatedUser,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> AccessLogResponse:
    """Get access history for a specific memory.

    Users can view access history for their own memories.
    Admins can view access history for any memory in their organization.
    """
    pool = await get_pool()
    access_repo = AccessRepository(pool)

    result = await access_repo.get_access_history(
        memory_id=memory_id,
        offset=offset,
        limit=limit,
        organization_id=user.organization_id,
        user_id=user.id,
    )

    # Filter based on permissions
    entries = result["entries"]
    if not user.has_permission(Permission.ACCESS_VIEW_ORG):
        # Only show own accesses
        entries = [e for e in entries if e.get("user_id") == user.id]
    else:
        # Admin: filter to same org
        entries = [e for e in entries if e.get("organization_id") == user.organization_id]

    return AccessLogResponse(
        entries=[_entry_to_response(e) for e in entries],
        total_count=len(entries),
        offset=offset,
        limit=limit,
        has_more=result["has_more"],
    )


@router.get(
    "/user/{user_id}",
    response_model=list[AccessLogEntry],
)
async def get_user_access_activity(
    user_id: UUID,
    user: AuthenticatedUser,
    since: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AccessLogEntry]:
    """Get memory access activity for a user.

    Users can view their own activity.
    Admins can view activity for any user in their organization.
    """
    # Check permissions
    if user_id != user.id:
        if not user.has_permission(Permission.ACCESS_VIEW_ORG):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only view your own access activity",
            )

    pool = await get_pool()
    access_repo = AccessRepository(pool)

    entries = await access_repo.get_user_activity(
        user_id=user_id,
        since=since,
        limit=limit,
        organization_id=user.organization_id,
    )

    return [_entry_to_response(e) for e in entries]


@router.get(
    "/most-accessed",
    response_model=list[MostAccessedItem],
)
async def get_most_accessed_memories(
    user: AuthenticatedUser,
    since: datetime | None = Query(default=None, description="Count accesses after this date"),
    limit: int = Query(default=20, ge=1, le=100),
    organization_wide: bool = Query(
        default=False, description="Include all org members (admin only)"
    ),
) -> list[MostAccessedItem]:
    """Get the most frequently accessed memories.

    By default, shows only your own accesses.
    Admins can set organization_wide=true to see org-wide stats.
    """
    pool = await get_pool()
    access_repo = AccessRepository(pool)

    if organization_wide:
        if not user.has_permission(Permission.ACCESS_VIEW_ORG):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organization-wide access stats require admin permissions",
            )
        result = await access_repo.get_most_accessed(
            organization_id=user.organization_id,
            since=since,
            limit=limit,
        )
    else:
        result = await access_repo.get_most_accessed(
            user_id=user.id,
            organization_id=user.organization_id,
            since=since,
            limit=limit,
        )

    return [
        MostAccessedItem(
            memory_id=r["memory_id"],
            access_count=r["access_count"],
            last_accessed=r["last_accessed"],
        )
        for r in result
    ]


