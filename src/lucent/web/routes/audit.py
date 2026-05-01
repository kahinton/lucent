"""Audit log routes — admin/security action history."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from lucent.db import AdminAuditRepository, AuditRepository, get_pool

from ._shared import get_user_context, templates

router = APIRouter()


# =============================================================================
# Audit Logs
# =============================================================================


def _normalize_admin_entry(entry: dict) -> dict:
    """Massage an admin_audit_log row into the shape audit.html expects.

    The legacy audit.html template was built for memory_audit_log rows. To keep
    it backward-compatible while we add admin events, we adapt admin rows into
    a similar shape (action_type, user_id, created_at, changed_fields,
    old_values, new_values, context, notes). The template renders these via
    generic ``entry.<field>`` accessors.
    """
    out = dict(entry)
    out["action_type"] = entry.get("action") or "unknown"
    out["memory_id"] = entry.get("entity_id")  # template just shows it as the target id
    return out


@router.get("/audit", response_class=HTMLResponse)
async def audit_logs(
    request: Request,
    page: int = 1,
    action_type: str | None = None,
    source: str = "admin",
):
    """View the audit log.

    Defaults to the admin-action audit (user/role/api-key/etc.). Pass
    ``?source=memory`` to view the legacy memory-change log.
    """
    user = await get_user_context(request)
    role_val = user.role.value if hasattr(user.role, "value") else str(user.role)
    if role_val not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Admin access required")

    pool = await get_pool()
    page = max(1, page)
    limit = 50
    offset = (page - 1) * limit

    if source == "memory":
        repo = AuditRepository(pool)
        result = await repo.get_by_organization_id(
            organization_id=user.organization_id,
            action_type=action_type,
            offset=offset,
            limit=limit,
        )
        action_types = ["create", "update", "delete", "share", "unshare"]
        entries = result["entries"]
        total_count = result["total_count"]
    else:
        admin_repo = AdminAuditRepository(pool)
        result = await admin_repo.list_for_org(
            organization_id=user.organization_id,
            action=action_type,
            limit=limit,
            offset=offset,
        )
        action_types = await admin_repo.list_actions(user.organization_id)
        entries = [_normalize_admin_entry(e) for e in result["entries"]]
        total_count = result["total_count"]

    total_pages = (total_count + limit - 1) // limit

    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "user": user,
            "entries": entries,
            "total_count": total_count,
            "page": page,
            "total_pages": total_pages,
            "action_type": action_type,
            "action_types": action_types,
            "source": source,
        },
    )
