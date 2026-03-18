"""Definition management routes — agents, skills, MCP servers."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.db import get_pool
from lucent.logging import get_logger
from lucent.rbac import Role

from ._shared import _check_csrf, _parse_env_vars, get_user_context, templates

logger = get_logger("web.routes.definitions")

router = APIRouter()


def _require_admin_or_owner(user) -> None:
    """Raise 403 if user is not admin or owner."""
    role_value = user.role if isinstance(user.role, str) else user.role.value
    if role_value not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Permission denied")


def _require_admin(user: object) -> None:
    """Raise 403 if user is not admin or owner."""
    if user.role not in (Role.ADMIN, Role.OWNER):
        raise HTTPException(status_code=403, detail="Permission denied")


# =============================================================================
# Definitions Management
# =============================================================================


@router.get("/definitions", response_class=HTMLResponse)
async def definitions_page(request: Request, tab: str = "agents"):
    """Agent and skill definitions management page."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)

    agents = await repo.list_agents(org_id)
    skills = await repo.list_skills(org_id)
    mcp_servers = await repo.list_mcp_servers(org_id)

    return templates.TemplateResponse(
        request,
        "definitions.html",
        {
            "user": user,
            "agents": agents,
            "skills": skills,
            "mcp_servers": mcp_servers,
            "tab": tab,
        },
    )


@router.get("/definitions/agents/{agent_id}", response_class=HTMLResponse)
async def agent_detail_page(request: Request, agent_id: str):
    """Agent definition detail page."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)

    agent = await repo.get_agent(agent_id, org_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get all skills and MCP servers for the assignment dropdowns
    all_skills = await repo.list_skills(org_id, status="active")
    all_mcp = await repo.list_mcp_servers(org_id, status="active")
    assigned_skills = await repo.get_agent_skills(agent_id)
    assigned_mcp = await repo.get_agent_mcp_servers(agent_id)

    # Get assigned skill/mcp IDs for easier template logic
    assigned_skill_ids = {str(s["id"]) for s in assigned_skills}
    assigned_mcp_ids = {str(s["id"]) for s in assigned_mcp}

    return templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition": agent,
            "definition_type": "agent",
            "all_skills": all_skills,
            "all_mcp": all_mcp,
            "assigned_skills": assigned_skills,
            "assigned_mcp": assigned_mcp,
            "assigned_skill_ids": assigned_skill_ids,
            "assigned_mcp_ids": assigned_mcp_ids,
        },
    )


@router.get("/definitions/skills/{skill_id}", response_class=HTMLResponse)
async def skill_detail_page(request: Request, skill_id: str):
    """Skill definition detail page."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)

    skill = await repo.get_skill(skill_id, org_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    return templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition": skill,
            "definition_type": "skill",
        },
    )


@router.get("/definitions/mcp-servers/{server_id}", response_class=HTMLResponse)
async def mcp_server_detail_page(request: Request, server_id: str):
    """MCP server definition detail page."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)

    server = await repo.get_mcp_server(server_id, org_id)
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    return templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition": server,
            "definition_type": "mcp-server",
        },
    )


@router.post("/definitions/agents/{agent_id}/approve")
async def approve_agent_web(request: Request, agent_id: str):
    """Approve an agent definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.approve_agent(agent_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/reject")
async def reject_agent_web(request: Request, agent_id: str):
    """Reject an agent definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.reject_agent(agent_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/skills/{skill_id}/approve")
async def approve_skill_web(request: Request, skill_id: str):
    """Approve a skill definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.approve_skill(skill_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/skills/{skill_id}", status_code=303)


@router.post("/definitions/skills/{skill_id}/reject")
async def reject_skill_web(request: Request, skill_id: str):
    """Reject a skill definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.reject_skill(skill_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/skills/{skill_id}", status_code=303)


@router.post("/definitions/mcp-servers/{server_id}/approve")
async def approve_mcp_web(request: Request, server_id: str):
    """Approve an MCP server definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.approve_mcp_server(server_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/mcp-servers/{server_id}", status_code=303)


@router.post("/definitions/mcp-servers/{server_id}/reject")
async def reject_mcp_web(request: Request, server_id: str):
    """Reject an MCP server definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.reject_mcp_server(server_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/mcp-servers/{server_id}", status_code=303)


@router.post("/definitions/agents/create")
async def create_agent_web(request: Request):
    """Create a new agent definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)

    agent = await repo.create_agent(
        name=str(form.get("name", "")).strip(),
        org_id=org_id,
        description=str(form.get("description", "")).strip(),
        content=str(form.get("definition", "")).strip(),
        created_by=str(user.id),
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
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)

    await repo.update_agent(
        agent_id,
        org_id,
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        content=str(form.get("definition", "")).strip(),
    )
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/delete")
async def delete_agent_web(request: Request, agent_id: str):
    """Delete an agent definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
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
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)

    skill = await repo.create_skill(
        name=str(form.get("name", "")).strip(),
        org_id=org_id,
        description=str(form.get("description", "")).strip(),
        content=str(form.get("definition", "")).strip(),
        created_by=str(user.id),
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
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)

    await repo.update_skill(
        skill_id,
        org_id,
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        content=str(form.get("definition", "")).strip(),
    )
    return RedirectResponse(url=f"/definitions/skills/{skill_id}", status_code=303)


@router.post("/definitions/skills/{skill_id}/delete")
async def delete_skill_web(request: Request, skill_id: str):
    """Delete a skill definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
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
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)

    command = str(form.get("command", "")).strip()
    args_raw = str(form.get("args", "")).strip()
    args = [a.strip() for a in args_raw.split("\n") if a.strip()] if args_raw else []
    env_vars = _parse_env_vars(str(form.get("env_vars", "")))

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
    )
    return RedirectResponse(url=f"/definitions/mcp-servers/{server['id']}", status_code=303)


@router.post("/definitions/mcp-servers/{server_id}/update")
async def update_mcp_web(request: Request, server_id: str):
    """Update an MCP server definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)

    command = str(form.get("command", "")).strip()
    args_raw = str(form.get("args", "")).strip()
    args = [a.strip() for a in args_raw.split("\n") if a.strip()] if args_raw else []
    env_vars = _parse_env_vars(str(form.get("env_vars", "")))

    await repo.update_mcp_server(
        server_id,
        org_id,
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        command=command,
        args=args,
        env_vars=env_vars,
    )
    return RedirectResponse(url=f"/definitions/mcp-servers/{server_id}", status_code=303)


@router.post("/definitions/mcp-servers/{server_id}/delete")
async def delete_mcp_web(request: Request, server_id: str):
    """Delete an MCP server definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.delete_mcp_server(server_id, str(user.organization_id))
    return RedirectResponse(url="/definitions?tab=mcp", status_code=303)


@router.post("/definitions/agents/{agent_id}/grant-skill")
async def grant_skill_web(request: Request, agent_id: str):
    """Grant a skill to an agent."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    skill_id = str(form.get("skill_id", ""))
    await repo.assign_skill_to_agent(agent_id, skill_id, str(user.organization_id))
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/revoke-skill/{skill_id}")
async def revoke_skill_web(request: Request, agent_id: str, skill_id: str):
    """Revoke a skill from an agent."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.remove_skill_from_agent(agent_id, skill_id, str(user.organization_id))
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/grant-mcp")
async def grant_mcp_web(request: Request, agent_id: str):
    """Grant an MCP server to an agent."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    server_id = str(form.get("server_id", ""))
    await repo.assign_mcp_to_agent(agent_id, server_id, str(user.organization_id))
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/revoke-mcp/{server_id}")
async def revoke_mcp_web(request: Request, agent_id: str, server_id: str):
    """Revoke an MCP server from an agent."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.remove_mcp_from_agent(agent_id, server_id, str(user.organization_id))
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/mcp-tools/{server_id}")
async def update_mcp_tools_web(request: Request, agent_id: str, server_id: str):
    """Update allowed tools for an agent's MCP server assignment."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)

    tools_raw = str(form.get("allowed_tools", "")).strip()
    allowed_tools = [t.strip() for t in tools_raw.split(",") if t.strip()] if tools_raw else None

    await repo.update_agent_mcp_tools(agent_id, server_id, org_id, allowed_tools=allowed_tools)
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)
