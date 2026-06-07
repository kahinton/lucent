"""Uniform access-grant management routes.

A single, resource-type-agnostic surface for managing who may *use* a resource.
Every resource detail page (agents, skills, mcp servers, hooks, managed tools,
sandbox templates, workflows, models, secrets) renders the shared
``_access_panel.html`` partial and posts to these endpoints, so the access model
is identical everywhere.

Authorization: only the resource's managing owner or an org admin/owner may view
and edit grants (``AccessControlService.can_modify``). Grants control *use*, not
*management*.
"""

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from lucent.access_control import (
    AccessControlService,
    CANONICAL_RESOURCE_TYPE,
    RESOURCE_TABLE_MAP,
    normalize_resource_type,
)
from lucent.db import get_pool
from lucent.logging import get_logger

from ._shared import _check_csrf, get_user_context

logger = get_logger("web.routes.access_ui")

router = APIRouter()

# Canonical token per accepted alias (e.g. "agents" -> "agent").
_CANONICAL = {
    alias: CANONICAL_RESOURCE_TYPE[table]
    for alias, table in RESOURCE_TABLE_MAP.items()
}

# Human-readable labels for each canonical resource type, shown in the panel.
RESOURCE_LABELS = {
    "agent": "agent",
    "skill": "skill",
    "mcp_server": "MCP server",
    "hook": "hook",
    "managed_tool": "tool",
    "sandbox_template": "sandbox template",
    "workflow": "workflow",
    "model": "model",
    "secret": "secret",
}


def _safe_redirect(value: str | None, default: str) -> str:
    """Return a safe in-app redirect path, guarding against open redirects."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return default
    return value


async def _org_members(pool, org_id: str) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, COALESCE(display_name, email, 'Unknown user') AS name "
            "FROM users WHERE organization_id = $1 ORDER BY name",
            UUID(org_id),
        )
    return [{"id": str(r["id"]), "name": r["name"]} for r in rows]


async def _org_groups(pool, org_id: str) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name FROM groups WHERE organization_id = $1 ORDER BY name",
            UUID(org_id),
        )
    return [{"id": str(r["id"]), "name": r["name"]} for r in rows]


async def build_access_context(
    pool, *, resource_type: str, resource_id: str, org_id: str, user, redirect: str
) -> dict:
    """Assemble the template context consumed by ``_access_panel.html``.

    Returns a dict under the key the caller stores as ``access`` containing the
    current grants, candidate principals for the add form, and a ``can_manage``
    flag. Safe to call for any caller; it never raises on a missing resource.
    """
    canonical = normalize_resource_type(resource_type)
    rtype = _CANONICAL[canonical]
    acl = AccessControlService(pool)
    grants = await acl.list_access_grants(rtype, resource_id, org_id)
    can_manage = await acl.can_modify(str(user.id), rtype, resource_id, org_id)

    org_granted = any(g["principal_type"] == "org" for g in grants)
    user_grants = [g for g in grants if g["principal_type"] == "user"]
    group_grants = [g for g in grants if g["principal_type"] == "group"]
    granted_user_ids = {g["principal_id"] for g in user_grants}
    granted_group_ids = {g["principal_id"] for g in group_grants}

    candidate_users: list[dict] = []
    candidate_groups: list[dict] = []
    if can_manage:
        candidate_users = [
            m for m in await _org_members(pool, org_id)
            if m["id"] not in granted_user_ids
        ]
        candidate_groups = [
            g for g in await _org_groups(pool, org_id)
            if g["id"] not in granted_group_ids
        ]

    return {
        "resource_type": rtype,
        "resource_id": str(resource_id),
        "label": RESOURCE_LABELS.get(rtype, rtype),
        "org_granted": org_granted,
        "user_grants": user_grants,
        "group_grants": group_grants,
        "candidate_users": candidate_users,
        "candidate_groups": candidate_groups,
        "can_manage": can_manage,
        "redirect": redirect,
    }


async def _require_manage(pool, user, resource_type: str, resource_id: str) -> str:
    """Validate resource_type, enforce manage permission, return canonical token."""
    try:
        canonical = normalize_resource_type(resource_type)
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown resource type")
    rtype = _CANONICAL[canonical]
    acl = AccessControlService(pool)
    if not await acl.can_modify(
        str(user.id), rtype, resource_id, str(user.organization_id)
    ):
        raise HTTPException(status_code=403, detail="Permission denied")
    return rtype


@router.post("/access/{resource_type}/{resource_id}/grant")
async def grant_access_web(request: Request, resource_type: str, resource_id: str):
    """Add an access grant (user, group, or org-wide) to a resource."""
    user = await get_user_context(request)
    pool = await get_pool()
    await _check_csrf(request)
    rtype = await _require_manage(pool, user, resource_type, resource_id)

    form = await request.form()
    principal_type = str(form.get("principal_type", "")).strip().lower()
    principal_id = str(form.get("principal_id", "")).strip() or None
    redirect = _safe_redirect(str(form.get("redirect", "")), "/")

    if principal_type not in ("user", "group", "org"):
        raise HTTPException(status_code=400, detail="Invalid principal type")
    if principal_type in ("user", "group") and not principal_id:
        raise HTTPException(status_code=400, detail="Principal is required")

    acl = AccessControlService(pool)
    try:
        await acl.grant_access(
            resource_type=rtype,
            resource_id=resource_id,
            org_id=str(user.organization_id),
            principal_type=principal_type,
            principal_id=principal_id,
            granted_by=str(user.id),
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"{redirect}?access_error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(url=redirect, status_code=303)


@router.post("/access/{resource_type}/{resource_id}/revoke")
async def revoke_access_web(request: Request, resource_type: str, resource_id: str):
    """Remove an access grant from a resource."""
    user = await get_user_context(request)
    pool = await get_pool()
    await _check_csrf(request)
    rtype = await _require_manage(pool, user, resource_type, resource_id)

    form = await request.form()
    principal_type = str(form.get("principal_type", "")).strip().lower()
    principal_id = str(form.get("principal_id", "")).strip() or None
    redirect = _safe_redirect(str(form.get("redirect", "")), "/")

    if principal_type not in ("user", "group", "org"):
        raise HTTPException(status_code=400, detail="Invalid principal type")

    acl = AccessControlService(pool)
    await acl.revoke_access(
        resource_type=rtype,
        resource_id=resource_id,
        org_id=str(user.organization_id),
        principal_type=principal_type,
        principal_id=principal_id,
    )
    return RedirectResponse(url=redirect, status_code=303)
