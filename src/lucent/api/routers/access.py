"""Access log API endpoints."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, HTTPException, status

from lucent.api.deps import AuthenticatedUser, AdminUser
from lucent.api.models import (
    AccessLogEntry,
    AccessLogResponse,
    MostAccessedItem,
)
from lucent.db import AccessRepository, get_pool
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
    "/memory/{memory_id}/searches",
    response_model=list[AccessLogEntry],
)
async def get_memory_search_history(
    memory_id: UUID,
    user: AuthenticatedUser,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[AccessLogEntry]:
    """Get search queries that returned a specific memory.
    
    Useful for understanding how memories are being discovered.
    """
    pool = await get_pool()
    access_repo = AccessRepository(pool)
    
    entries = await access_repo.get_search_history(
        memory_id=memory_id,
        limit=limit,
    )
    
    # Filter based on permissions
    if not user.has_permission(Permission.ACCESS_VIEW_ORG):
        entries = [e for e in entries if e.get("user_id") == user.id]
    else:
        entries = [e for e in entries if e.get("organization_id") == user.organization_id]
    
    return [_entry_to_response(e) for e in entries]


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
    organization_wide: bool = Query(default=False, description="Include all org members (admin only)"),
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


@router.get(
    "/organization/activity",
    response_model=list[AccessLogEntry],
)
async def get_organization_access_activity(
    user: AdminUser,  # Requires admin role
    since: datetime | None = Query(default=None),
    access_type: str | None = Query(default=None, description="Filter: view or search_result"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AccessLogEntry]:
    """Get recent memory access activity for the organization.
    
    Requires admin or owner role.
    """
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )
    
    pool = await get_pool()
    access_repo = AccessRepository(pool)
    
    # TODO: Implement proper organization activity feed
    # For now, this endpoint is not available
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Organization activity feed is not yet implemented",
    )
