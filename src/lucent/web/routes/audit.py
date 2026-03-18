"""Audit log routes."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from lucent.db import AuditRepository, get_pool
from lucent.mode import is_team_mode

from ._shared import get_user_context, templates

router = APIRouter()


# =============================================================================
# Audit Logs
# =============================================================================


@router.get("/audit", response_class=HTMLResponse)
async def audit_logs(
    request: Request,
    page: int = 1,
    action_type: str | None = None,
):
    """View audit logs (team mode only)."""
    if not is_team_mode():
        raise HTTPException(status_code=404, detail="Audit logs require team mode")
    user = await get_user_context(request)
    pool = await get_pool()
    audit_repo = AuditRepository(pool)

    limit = 50
    offset = (page - 1) * limit

    result = await audit_repo.get_by_organization_id(
        organization_id=user.organization_id,
        action_type=action_type,
        offset=offset,
        limit=limit,
    )

    total_pages = (result["total_count"] + limit - 1) // limit

    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "user": user,
            "entries": result["entries"],
            "total_count": result["total_count"],
            "page": page,
            "total_pages": total_pages,
            "action_type": action_type,
            "action_types": ["create", "update", "delete", "share", "unshare"],
        },
    )
