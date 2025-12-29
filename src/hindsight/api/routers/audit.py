"""Audit log API endpoints."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, HTTPException, status

from hindsight.api.deps import AuthenticatedUser, AdminUser
from hindsight.api.models import (
    AuditLogEntry,
    AuditLogResponse,
    ErrorResponse,
)
from hindsight.db.client import AuditRepository, get_pool
from hindsight.rbac import Permission


router = APIRouter()


def _entry_to_response(entry: dict[str, Any]) -> AuditLogEntry:
    """Convert an audit log entry dict to a response model."""
    return AuditLogEntry(
        id=entry["id"],
        memory_id=entry["memory_id"],
        user_id=entry.get("user_id"),
        organization_id=entry.get("organization_id"),
        action_type=entry["action_type"],
        created_at=entry["created_at"],
        changed_fields=entry.get("changed_fields"),
        old_values=entry.get("old_values"),
        new_values=entry.get("new_values"),
        context=entry.get("context", {}),
        notes=entry.get("notes"),
    )


@router.get(
    "/memory/{memory_id}",
    response_model=AuditLogResponse,
)
async def get_memory_audit_log(
    memory_id: UUID,
    user: AuthenticatedUser,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> AuditLogResponse:
    """Get audit log for a specific memory.
    
    Users can view audit logs for their own memories.
    Admins can view audit logs for any memory in their organization.
    """
    pool = await get_pool()
    audit_repo = AuditRepository(pool)
    
    # Get the audit log (will include org check)
    result = await audit_repo.get_by_memory_id(
        memory_id=memory_id,
        offset=offset,
        limit=limit,
    )
    
    # Filter to only entries the user can see
    # (own actions or admin viewing org actions)
    entries = result["entries"]
    if not user.has_permission(Permission.AUDIT_VIEW_ORG):
        # Only show entries for user's own actions
        entries = [e for e in entries if e.get("user_id") == user.id]
    else:
        # Admin: filter to same org
        entries = [e for e in entries if e.get("organization_id") == user.organization_id]
    
    return AuditLogResponse(
        entries=[_entry_to_response(e) for e in entries],
        total_count=len(entries),
        offset=offset,
        limit=limit,
        has_more=result["has_more"],
    )


@router.get(
    "/user/{user_id}",
    response_model=AuditLogResponse,
)
async def get_user_audit_log(
    user_id: UUID,
    user: AuthenticatedUser,
    action_type: str | None = Query(default=None, description="Filter by action type"),
    since: datetime | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> AuditLogResponse:
    """Get audit log for a specific user's actions.
    
    Users can view their own audit logs.
    Admins can view audit logs for any user in their organization.
    """
    # Check permissions
    if user_id != user.id:
        if not user.has_permission(Permission.AUDIT_VIEW_ORG):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only view your own audit logs",
            )
    
    pool = await get_pool()
    audit_repo = AuditRepository(pool)
    
    result = await audit_repo.get_by_user_id(
        user_id=user_id,
        action_type=action_type,
        since=since,
        offset=offset,
        limit=limit,
    )
    
    return AuditLogResponse(
        entries=[_entry_to_response(e) for e in result["entries"]],
        total_count=result["total_count"],
        offset=result["offset"],
        limit=result["limit"],
        has_more=result["has_more"],
    )


@router.get(
    "/organization",
    response_model=AuditLogResponse,
)
async def get_organization_audit_log(
    user: AdminUser,  # Requires admin role
    action_type: str | None = Query(default=None, description="Filter by action type"),
    since: datetime | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> AuditLogResponse:
    """Get audit log for the entire organization.
    
    Requires admin or owner role.
    """
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )
    
    pool = await get_pool()
    audit_repo = AuditRepository(pool)
    
    result = await audit_repo.get_by_organization_id(
        organization_id=user.organization_id,
        action_type=action_type,
        since=since,
        offset=offset,
        limit=limit,
    )
    
    return AuditLogResponse(
        entries=[_entry_to_response(e) for e in result["entries"]],
        total_count=result["total_count"],
        offset=result["offset"],
        limit=result["limit"],
        has_more=result["has_more"],
    )


@router.get(
    "/recent",
    response_model=list[AuditLogEntry],
)
async def get_recent_audit_entries(
    user: AdminUser,  # Requires admin role
    action_types: list[str] | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditLogEntry]:
    """Get recent audit entries for monitoring.
    
    Requires admin or owner role.
    """
    pool = await get_pool()
    audit_repo = AuditRepository(pool)
    
    entries = await audit_repo.get_recent(
        organization_id=user.organization_id,
        action_types=action_types,
        since=since,
        limit=limit,
    )
    
    return [_entry_to_response(e) for e in entries]
