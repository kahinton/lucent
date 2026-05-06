"""Definition management routes — agents, skills, MCP servers, hooks."""

import json
from math import ceil
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from lucent.auth_providers import CSRF_COOKIE_NAME
from lucent.db import get_pool
from lucent.logging import get_logger
from lucent.rbac import Role

from ._shared import _check_csrf, _parse_env_vars, get_user_context, templates

logger = get_logger("web.routes.definitions")

router = APIRouter()

ALLOWED_PER_PAGE = {10, 25, 50, 100}


async def _get_user_groups(pool, user_id: str, org_id: str) -> list[dict]:
    from lucent.db.groups import GroupRepository

    repo = GroupRepository(pool)
    return await repo.get_user_groups(user_id, org_id)


async def _resolve_owner_maps(pool, items: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    user_ids = {str(item.get("owner_user_id")) for item in items if item.get("owner_user_id")}
    group_ids = {str(item.get("owner_group_id")) for item in items if item.get("owner_group_id")}
    user_map: dict[str, str] = {}
    group_map: dict[str, str] = {}
    if not user_ids and not group_ids:
        return user_map, group_map

    async with pool.acquire() as conn:
        if user_ids:
            user_rows = await conn.fetch(
                """
                SELECT id, COALESCE(display_name, email, 'Unknown user') AS owner_name
                FROM users
                WHERE id = ANY($1::uuid[])
                """,
                [UUID(uid) for uid in user_ids],
            )
            user_map = {str(row["id"]): row["owner_name"] for row in user_rows}
        if group_ids:
            group_rows = await conn.fetch(
                "SELECT id, name FROM groups WHERE id = ANY($1::uuid[])",
                [UUID(gid) for gid in group_ids],
            )
            group_map = {str(row["id"]): row["name"] for row in group_rows}
    return user_map, group_map


def _attach_owner_names(
    items: list[dict], user_map: dict[str, str], group_map: dict[str, str]
) -> list[dict]:
    enriched: list[dict] = []
    for item in items:
        d = dict(item)
        owner_user_id = str(d.get("owner_user_id")) if d.get("owner_user_id") else None
        owner_group_id = str(d.get("owner_group_id")) if d.get("owner_group_id") else None
        d["owner_user_name"] = user_map.get(owner_user_id) if owner_user_id else None
        d["owner_group_name"] = group_map.get(owner_group_id) if owner_group_id else None
        enriched.append(d)
    return enriched


async def _resolve_owner_scope(
    form_data, user, pool
) -> tuple[str | None, str | None, bool]:
    owner_scope = str(form_data.get("owner_scope", "me")).strip()
    if not owner_scope or owner_scope == "me":
        return str(user.id), None, False
    if owner_scope == "org":
        _require_admin_or_owner(user)
        return None, None, True
    if owner_scope.startswith("group:"):
        group_id = owner_scope.replace("group:", "", 1).strip()
        user_groups = await _get_user_groups(pool, str(user.id), str(user.organization_id))
        allowed_group_ids = {str(group["id"]) for group in user_groups}
        if group_id not in allowed_group_ids:
            raise HTTPException(status_code=403, detail="Permission denied")
        return None, group_id, False
    return str(user.id), None, False


def _require_admin_or_owner(user) -> None:
    """Raise 403 if user is not admin or owner."""
    role_value = user.role if isinstance(user.role, str) else user.role.value
    if role_value not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Permission denied")


def _require_admin(user: object) -> None:
    """Raise 403 if user is not admin or owner."""
    if user.role not in (Role.ADMIN, Role.OWNER):
        raise HTTPException(status_code=403, detail="Permission denied")


def _parse_json_object(value: str, default: dict | None = None) -> dict:
    """Parse a form JSON object, returning default on empty/invalid input."""
    if default is None:
        default = {}
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return default
    return parsed if isinstance(parsed, dict) else default


# =============================================================================
# Definitions Management
# =============================================================================


@router.get("/definitions", response_class=HTMLResponse)
async def definitions_page(
    request: Request,
    tab: str = "agents",
    page: int = 1,
    per_page: int = 25,
):
    """Agent and skill definitions management page."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    role_value = user.role if isinstance(user.role, str) else user.role.value

    page = max(1, page)
    per_page = per_page if per_page in ALLOWED_PER_PAGE else 25
    offset = (page - 1) * per_page

    # Only paginate the active tab; load others without pagination for counts
    agents_result = await repo.list_agents_with_grants(
        org_id,
        limit=per_page if tab == "agents" else 1000,
        offset=offset if tab == "agents" else 0,
        requester_user_id=str(user.id),
        requester_role=role_value,
    )
    skills_result = await repo.list_skills(
        org_id,
        limit=per_page if tab == "skills" else 1000,
        offset=offset if tab == "skills" else 0,
        requester_user_id=str(user.id),
        requester_role=role_value,
    )
    mcp_result = await repo.list_mcp_servers(
        org_id,
        limit=per_page if tab == "mcp" else 1000,
        offset=offset if tab == "mcp" else 0,
        requester_user_id=str(user.id),
        requester_role=role_value,
    )
    hooks_result = await repo.list_hooks(
        org_id,
        limit=per_page if tab == "hooks" else 1000,
        offset=offset if tab == "hooks" else 0,
        requester_user_id=str(user.id),
        requester_role=role_value,
    )
    proposals = await repo.get_pending_proposals(org_id)
    all_items = [
        *agents_result["items"],
        *skills_result["items"],
        *mcp_result["items"],
        *hooks_result["items"],
    ]
    user_map, group_map = await _resolve_owner_maps(pool, all_items)
    user_groups = await _get_user_groups(pool, str(user.id), org_id)
    agents = _attach_owner_names(agents_result["items"], user_map, group_map)
    skills = _attach_owner_names(skills_result["items"], user_map, group_map)
    mcp_servers = _attach_owner_names(mcp_result["items"], user_map, group_map)
    hooks = _attach_owner_names(hooks_result["items"], user_map, group_map)

    # Determine total_count and total_pages based on active tab
    if tab == "agents":
        total_count = agents_result["total_count"]
    elif tab == "skills":
        total_count = skills_result["total_count"]
    elif tab == "mcp":
        total_count = mcp_result["total_count"]
    elif tab == "hooks":
        total_count = hooks_result["total_count"]
    elif tab == "proposals":
        total_count = proposals["total"]
    else:
        total_count = 0

    total_pages = ceil(total_count / per_page) if total_count > 0 else 1
    page = min(page, total_pages)

    return templates.TemplateResponse(
        request,
        "definitions.html",
        {
            "user": user,
            "agents": agents,
            "skills": skills,
            "mcp_servers": mcp_servers,
            "hooks": hooks,
            "proposals": proposals,
            "owner_groups": user_groups,
            "agents_total": agents_result["total_count"],
            "skills_total": skills_result["total_count"],
            "mcp_total": mcp_result["total_count"],
            "hooks_total": hooks_result["total_count"],
            "tab": tab,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total_count": total_count,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.get("/definitions/agents/{agent_id}", response_class=HTMLResponse)
async def agent_detail_page(request: Request, agent_id: str):
    """Agent definition detail page."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)

    role_value = user.role if isinstance(user.role, str) else user.role.value
    agent = await repo.get_agent(
        agent_id, org_id, requester_user_id=str(user.id), requester_role=role_value
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    user_map, group_map = await _resolve_owner_maps(pool, [agent])
    agent = _attach_owner_names([agent], user_map, group_map)[0]
    user_groups = await _get_user_groups(pool, str(user.id), org_id)

    # Get all skills and MCP servers for the assignment dropdowns
    all_skills = (
        await repo.list_skills(
            org_id,
            status="active",
            requester_user_id=str(user.id),
            requester_role=role_value,
        )
    )["items"]
    all_mcp = (
        await repo.list_mcp_servers(
            org_id,
            status="active",
            requester_user_id=str(user.id),
            requester_role=role_value,
        )
    )["items"]
    all_hooks = (
        await repo.list_hooks(
            org_id,
            status="active",
            requester_user_id=str(user.id),
            requester_role=role_value,
        )
    )["items"]
    assigned_skills = await repo.get_agent_skills(agent_id)
    assigned_mcp = await repo.get_agent_mcp_servers(agent_id)
    assigned_hooks = await repo.get_agent_hooks(agent_id)

    # Get assigned skill/mcp IDs for easier template logic
    assigned_skill_ids = {str(s["id"]) for s in assigned_skills}
    assigned_mcp_ids = {str(s["id"]) for s in assigned_mcp}
    assigned_hook_ids = {str(h["id"]) for h in assigned_hooks}

    return templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition": agent,
            "definition_type": "agent",
            "owner_groups": user_groups,
            "all_skills": all_skills,
            "all_mcp": all_mcp,
            "all_hooks": all_hooks,
            "skills": assigned_skills,
            "mcp_servers": assigned_mcp,
            "hooks": assigned_hooks,
            "granted_skill_ids": assigned_skill_ids,
            "granted_mcp_ids": assigned_mcp_ids,
            "granted_hook_ids": assigned_hook_ids,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.get("/definitions/skills/{skill_id}", response_class=HTMLResponse)
async def skill_detail_page(request: Request, skill_id: str):
    """Skill definition detail page."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)

    role_value = user.role if isinstance(user.role, str) else user.role.value
    skill = await repo.get_skill(
        skill_id, org_id, requester_user_id=str(user.id), requester_role=role_value
    )
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    user_map, group_map = await _resolve_owner_maps(pool, [skill])
    skill = _attach_owner_names([skill], user_map, group_map)[0]
    user_groups = await _get_user_groups(pool, str(user.id), org_id)

    return templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition": skill,
            "definition_type": "skill",
            "owner_groups": user_groups,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.get("/definitions/mcp-servers/{server_id}", response_class=HTMLResponse)
async def mcp_server_detail_page(request: Request, server_id: str):
    """MCP server definition detail page."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)

    role_value = user.role if isinstance(user.role, str) else user.role.value
    server = await repo.get_mcp_server(
        server_id, org_id, requester_user_id=str(user.id), requester_role=role_value
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    user_map, group_map = await _resolve_owner_maps(pool, [server])
    server = _attach_owner_names([server], user_map, group_map)[0]
    user_groups = await _get_user_groups(pool, str(user.id), org_id)

    return templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition": server,
            "definition_type": "mcp-server",
            "owner_groups": user_groups,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.get("/definitions/hooks/{hook_id}", response_class=HTMLResponse)
async def hook_detail_page(request: Request, hook_id: str):
    """Hook definition detail page."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)

    role_value = user.role if isinstance(user.role, str) else user.role.value
    hook = await repo.get_hook(
        hook_id, org_id, requester_user_id=str(user.id), requester_role=role_value
    )
    if not hook:
        raise HTTPException(status_code=404, detail="Hook not found")
    user_map, group_map = await _resolve_owner_maps(pool, [hook])
    hook = _attach_owner_names([hook], user_map, group_map)[0]
    user_groups = await _get_user_groups(pool, str(user.id), org_id)

    return templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition": hook,
            "definition_type": "hook",
            "owner_groups": user_groups,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.get("/definitions/mcp-servers/{server_id}/discover-tools")
async def discover_tools_ajax(request: Request, server_id: str, refresh: bool = False):
    """AJAX endpoint: discover tools available on an MCP server."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository
    from lucent.services.mcp_discovery import (
        MCPDiscoveryError,
        discover_mcp_tools,
        get_tools_cached,
    )

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    role_value = user.role if isinstance(user.role, str) else user.role.value

    server = await repo.get_mcp_server(
        server_id, org_id, requester_user_id=str(user.id), requester_role=role_value
    )
    if not server:
        return JSONResponse(
            {
                "tools": [],
                "from_cache": False,
                "discovered_at": None,
                "error": "MCP server not found",
            },
            status_code=404,
        )

    try:
        if refresh:
            tools = await discover_mcp_tools(server, pool)
            from_cache = False
        else:
            tools, from_cache = await get_tools_cached(server_id, org_id, pool)
    except MCPDiscoveryError as exc:
        cached = await repo.get_discovered_tools(server_id, org_id)
        discovered_at = cached.get("tools_discovered_at") if cached else None
        return JSONResponse({
            "tools": [],
            "from_cache": False,
            "discovered_at": discovered_at.isoformat() if discovered_at else None,
            "error": f"Connection failed: {exc}",
        })

    cached = await repo.get_discovered_tools(server_id, org_id)
    discovered_at = cached.get("tools_discovered_at") if cached else None
    return JSONResponse({
        "tools": tools,
        "from_cache": from_cache,
        "discovered_at": discovered_at.isoformat() if discovered_at else None,
        "error": None,
    })


@router.post("/definitions/agents/{agent_id}/approve")
async def approve_agent_web(request: Request, agent_id: str):
    """Approve an agent definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.approve_agent(agent_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/reject")
async def reject_agent_web(request: Request, agent_id: str):
    """Reject an agent definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.reject_agent(agent_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/skills/{skill_id}/approve")
async def approve_skill_web(request: Request, skill_id: str):
    """Approve a skill definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.approve_skill(skill_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/skills/{skill_id}", status_code=303)


@router.post("/definitions/skills/{skill_id}/reject")
async def reject_skill_web(request: Request, skill_id: str):
    """Reject a skill definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.reject_skill(skill_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/skills/{skill_id}", status_code=303)


@router.post("/definitions/mcp-servers/{server_id}/approve")
async def approve_mcp_web(request: Request, server_id: str):
    """Approve an MCP server definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.approve_mcp_server(server_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/mcp-servers/{server_id}", status_code=303)


@router.post("/definitions/mcp-servers/{server_id}/reject")
async def reject_mcp_web(request: Request, server_id: str):
    """Reject an MCP server definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.reject_mcp_server(server_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/mcp-servers/{server_id}", status_code=303)


@router.post("/definitions/hooks/{hook_id}/approve")
async def approve_hook_web(request: Request, hook_id: str):
    """Approve a hook definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.approve_hook(hook_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/hooks/{hook_id}", status_code=303)


@router.post("/definitions/hooks/{hook_id}/reject")
async def reject_hook_web(request: Request, hook_id: str):
    """Reject a hook definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.reject_hook(hook_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/hooks/{hook_id}", status_code=303)


@router.post("/definitions/agents/create")
async def create_agent_web(request: Request):
    """Create a new agent definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    owner_user_id, owner_group_id, shared_with_org = await _resolve_owner_scope(
        form, user, pool
    )

    agent = await repo.create_agent(
        name=str(form.get("name", "")).strip(),
        org_id=org_id,
        description=str(form.get("description", "")).strip(),
        content=str(form.get("content", "") or form.get("definition", "")).strip(),
        created_by=str(user.id),
        owner_user_id=owner_user_id,
        owner_group_id=owner_group_id,
        shared_with_org=shared_with_org,
    )
    return RedirectResponse(url=f"/definitions/agents/{agent['id']}", status_code=303)


@router.post("/definitions/agents/{agent_id}/update")
async def update_agent_web(request: Request, agent_id: str):
    """Update an agent definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    owner_kwargs = {}
    if "owner_scope" in form:
        owner_user_id, owner_group_id, _shared_with_org = await _resolve_owner_scope(
            form, user, pool
        )
        owner_kwargs = {"owner_user_id": owner_user_id, "owner_group_id": owner_group_id}

    await repo.update_agent(
        agent_id,
        org_id,
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        content=str(form.get("content", "") or form.get("definition", "")).strip(),
        **owner_kwargs,
    )
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/delete")
async def delete_agent_web(request: Request, agent_id: str):
    """Delete an agent definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.delete_agent(agent_id, str(user.organization_id))
    return RedirectResponse(url="/definitions", status_code=303)


@router.post("/definitions/skills/create")
async def create_skill_web(request: Request):
    """Create a new skill definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    owner_user_id, owner_group_id, shared_with_org = await _resolve_owner_scope(
        form, user, pool
    )

    skill = await repo.create_skill(
        name=str(form.get("name", "")).strip(),
        org_id=org_id,
        description=str(form.get("description", "")).strip(),
        content=str(form.get("content", "") or form.get("definition", "")).strip(),
        created_by=str(user.id),
        owner_user_id=owner_user_id,
        owner_group_id=owner_group_id,
        shared_with_org=shared_with_org,
    )
    return RedirectResponse(url=f"/definitions/skills/{skill['id']}", status_code=303)


@router.post("/definitions/skills/{skill_id}/update")
async def update_skill_web(request: Request, skill_id: str):
    """Update a skill definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    owner_kwargs = {}
    if "owner_scope" in form:
        owner_user_id, owner_group_id, _shared_with_org = await _resolve_owner_scope(
            form, user, pool
        )
        owner_kwargs = {"owner_user_id": owner_user_id, "owner_group_id": owner_group_id}

    await repo.update_skill(
        skill_id,
        org_id,
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        content=str(form.get("content", "") or form.get("definition", "")).strip(),
        **owner_kwargs,
    )
    return RedirectResponse(url=f"/definitions/skills/{skill_id}", status_code=303)


@router.post("/definitions/skills/{skill_id}/delete")
async def delete_skill_web(request: Request, skill_id: str):
    """Delete a skill definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.delete_skill(skill_id, str(user.organization_id))
    return RedirectResponse(url="/definitions", status_code=303)


@router.post("/definitions/mcp-servers/create")
async def create_mcp_web(request: Request):
    """Create a new MCP server definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)

    command = str(form.get("command", "")).strip()
    args_raw = str(form.get("args", "")).strip()
    args = [a.strip() for a in args_raw.split("\n") if a.strip()] if args_raw else []
    env_vars = _parse_env_vars(str(form.get("env_vars", "")))
    owner_user_id, owner_group_id, shared_with_org = await _resolve_owner_scope(
        form, user, pool
    )

    server = await repo.create_mcp_server(
        name=str(form.get("name", "")).strip(),
        org_id=org_id,
        description=str(form.get("description", "")).strip(),
        server_type="stdio",
        url=None,
        created_by=str(user.id),
        command=command,
        args=args,
        env_vars=env_vars,
        owner_user_id=owner_user_id,
        owner_group_id=owner_group_id,
        shared_with_org=shared_with_org,
    )
    return RedirectResponse(url=f"/definitions/mcp-servers/{server['id']}", status_code=303)


@router.post("/definitions/hooks/create")
async def create_hook_web(request: Request):
    """Create a new hook definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    owner_user_id, owner_group_id, shared_with_org = await _resolve_owner_scope(
        form, user, pool
    )

    hook = await repo.create_hook(
        name=str(form.get("name", "")).strip(),
        org_id=org_id,
        description=str(form.get("description", "")).strip(),
        trigger_event=(
            str(form.get("trigger_event", "before_tool_call")).strip()
            or "before_tool_call"
        ),
        action_type=str(form.get("action_type", "static_context")).strip() or "static_context",
        content=str(form.get("content", "")).strip(),
        config=_parse_json_object(str(form.get("config", "")), {}),
        created_by=str(user.id),
        owner_user_id=owner_user_id,
        owner_group_id=owner_group_id,
        shared_with_org=shared_with_org,
    )
    return RedirectResponse(url=f"/definitions/hooks/{hook['id']}", status_code=303)


@router.post("/definitions/mcp-servers/{server_id}/update")
async def update_mcp_web(request: Request, server_id: str):
    """Update an MCP server definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)

    command = str(form.get("command", "")).strip()
    args_raw = str(form.get("args", "")).strip()
    args = [a.strip() for a in args_raw.split("\n") if a.strip()] if args_raw else []
    env_vars = _parse_env_vars(str(form.get("env_vars", "")))
    owner_kwargs = {}
    if "owner_scope" in form:
        owner_user_id, owner_group_id, _shared_with_org = await _resolve_owner_scope(
            form, user, pool
        )
        owner_kwargs = {"owner_user_id": owner_user_id, "owner_group_id": owner_group_id}

    await repo.update_mcp_server(
        server_id,
        org_id,
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        command=command,
        args=args,
        env_vars=env_vars,
        **owner_kwargs,
    )
    return RedirectResponse(url=f"/definitions/mcp-servers/{server_id}", status_code=303)


@router.post("/definitions/mcp-servers/{server_id}/delete")
async def delete_mcp_web(request: Request, server_id: str):
    """Delete an MCP server definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.delete_mcp_server(server_id, str(user.organization_id))
    return RedirectResponse(url="/definitions?tab=mcp", status_code=303)


@router.post("/definitions/hooks/{hook_id}/update")
async def update_hook_web(request: Request, hook_id: str):
    """Update a hook definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    owner_kwargs = {}
    if "owner_scope" in form:
        owner_user_id, owner_group_id, _shared_with_org = await _resolve_owner_scope(
            form, user, pool
        )
        owner_kwargs = {"owner_user_id": owner_user_id, "owner_group_id": owner_group_id}

    role_value = user.role if isinstance(user.role, str) else user.role.value
    await repo.update_hook(
        hook_id,
        org_id,
        requester_role=role_value,
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        trigger_event=(
            str(form.get("trigger_event", "before_tool_call")).strip()
            or "before_tool_call"
        ),
        action_type=str(form.get("action_type", "static_context")).strip() or "static_context",
        content=str(form.get("content", "")).strip(),
        config=_parse_json_object(str(form.get("config", "")), {}),
        **owner_kwargs,
    )
    return RedirectResponse(url=f"/definitions/hooks/{hook_id}", status_code=303)


@router.post("/definitions/hooks/{hook_id}/delete")
async def delete_hook_web(request: Request, hook_id: str):
    """Delete a hook definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.delete_hook(hook_id, str(user.organization_id))
    return RedirectResponse(url="/definitions?tab=hooks", status_code=303)


@router.post("/definitions/agents/{agent_id}/grant-skill")
async def grant_skill_web(request: Request, agent_id: str):
    """Grant a skill to an agent."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    skill_id = str(form.get("skill_id", ""))
    await repo.grant_skill(
        agent_id, skill_id,
        org_id=str(user.organization_id), user_id=str(user.id),
    )
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/revoke-skill/{skill_id}")
async def revoke_skill_web(request: Request, agent_id: str, skill_id: str):
    """Revoke a skill from an agent."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.revoke_skill(
        agent_id, skill_id,
        org_id=str(user.organization_id), user_id=str(user.id),
    )
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/grant-mcp")
async def grant_mcp_web(request: Request, agent_id: str):
    """Grant an MCP server to an agent."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    server_id = str(form.get("mcp_server_id", "") or form.get("server_id", ""))
    if server_id:
        await repo.grant_mcp_server(
            agent_id, server_id,
            org_id=str(user.organization_id), user_id=str(user.id),
        )
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/revoke-mcp/{server_id}")
async def revoke_mcp_web(request: Request, agent_id: str, server_id: str):
    """Revoke an MCP server from an agent."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.revoke_mcp_server(
        agent_id, server_id,
        org_id=str(user.organization_id), user_id=str(user.id),
    )
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/grant-hook")
async def grant_hook_web(request: Request, agent_id: str):
    """Grant a hook to an agent."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    hook_id = str(form.get("hook_id", ""))
    if hook_id:
        await repo.grant_hook(
            agent_id, hook_id,
            org_id=str(user.organization_id), user_id=str(user.id),
        )
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/revoke-hook/{hook_id}")
async def revoke_hook_web(request: Request, agent_id: str, hook_id: str):
    """Revoke a hook from an agent."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.revoke_hook(
        agent_id, hook_id,
        org_id=str(user.organization_id), user_id=str(user.id),
    )
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/mcp-tools/{server_id}")
async def update_mcp_tools_web(request: Request, agent_id: str, server_id: str):
    """Update allowed tools for an agent's MCP server assignment."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    tools_raw = str(form.get("allowed_tools", "")).strip()
    allowed_tools = [t.strip() for t in tools_raw.split(",") if t.strip()] if tools_raw else None

    await repo.update_mcp_tool_grants(
        agent_id, server_id, allowed_tools=allowed_tools,
        org_id=str(user.organization_id), user_id=str(user.id),
    )
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


# ── Import endpoints (session-authenticated for web UI) ──────────────────


@router.post("/definitions/import/preview")
async def import_preview_web(request: Request):
    """Preview an import — session-authenticated endpoint for the web UI."""
    await get_user_context(request)
    body = await request.json()

    from lucent.services.definition_import import ImportSourceType, import_definition

    result = await import_definition(
        source_type=ImportSourceType(body.get("source_type", "raw")),
        source=body.get("source", ""),
        definition_type_hint=body.get("definition_type"),
    )
    return JSONResponse(result.to_dict())


@router.post("/definitions/import/commit")
async def import_commit_web(request: Request):
    """Import a definition — session-authenticated endpoint for the web UI."""
    user = await get_user_context(request)
    body = await request.json()

    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository
    from lucent.services.definition_import import (
        DefinitionType,
        ImportSourceType,
        import_definition,
    )

    result = await import_definition(
        source_type=ImportSourceType(body.get("source_type", "raw")),
        source=body.get("source", ""),
        definition_type_hint=body.get("definition_type"),
    )

    if not result.success:
        return JSONResponse({"status": "error", "error": result.error}, status_code=400)

    if result.has_critical_findings:
        return JSONResponse({
            "status": "blocked",
            "reason": "Critical security findings detected.",
            **result.to_dict(),
        })

    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))

    created = None
    if result.definition_type == DefinitionType.AGENT:
        created = await repo.create_agent(
            name=result.name,
            description=result.description,
            content=result.content,
            org_id=str(user.organization_id),
            created_by=str(user.id),
        )
        if result.skill_names:
            created["suggested_skills"] = result.skill_names
    elif result.definition_type == DefinitionType.SKILL:
        created = await repo.create_skill(
            name=result.name,
            description=result.description,
            content=result.content,
            org_id=str(user.organization_id),
            created_by=str(user.id),
        )

    if not created:
        return JSONResponse(
            {"status": "error", "error": "Failed to create definition"},
            status_code=500,
        )

    return JSONResponse({
        "status": "imported",
        "definition": {k: str(v) if hasattr(v, 'hex') else v for k, v in created.items()},
        "security_findings": [
            {"severity": f.severity.value, "category": f.category, "detail": f.detail}
            for f in result.security_findings
        ],
        "source_url": result.source_url,
        "content_hash": result.content_hash,
    })
