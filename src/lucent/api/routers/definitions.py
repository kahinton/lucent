"""API router for agent, skill, and MCP server definitions.

Provides CRUD endpoints and approval workflow for managing definitions.
Agents can be granted access to specific skills and MCP servers.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from lucent.access_control import AccessControlService
from lucent.api.deps import AdminUser, AuthenticatedUser
from lucent.db import DefinitionRepository, get_pool
from lucent.db.audit import AuditRepository
from lucent.services.mcp_discovery import (
    MCPDiscoveryError,
    discover_mcp_tools,
    get_tools_cached,
)
from lucent.url_validation import SSRFError, validate_url

router = APIRouter(prefix="/definitions", tags=["definitions"])


# ── Request Models ────────────────────────────────────────────────────────


class CreateAgent(BaseModel):
    name: str = Field(max_length=64)
    description: str | None = None
    content: str


class CreateSkill(BaseModel):
    name: str = Field(max_length=64)
    description: str | None = None
    content: str


class CreateMCPServer(BaseModel):
    name: str = Field(max_length=64)
    description: str | None = None
    server_type: str = "http"
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    headers: dict | None = None
    env_vars: dict[str, str] | None = None


class UpdateMCPServer(BaseModel):
    name: str | None = None
    description: str | None = None
    server_type: str | None = None
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    headers: dict | None = None
    env_vars: dict[str, str] | None = None


class GrantAccess(BaseModel):
    target_id: str  # skill_id or mcp_server_id


# ── Agent Endpoints ──────────────────────────────────────────────────────


@router.get("/agents")
async def list_agents(
    user: AuthenticatedUser,
    status: Literal["proposed", "active", "rejected"] | None = None,
    limit: int = 25,
    offset: int = 0,
):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    return await repo.list_agents(
        str(user.organization_id),
        status=status,
        limit=min(limit, 200),
        offset=offset,
        requester_user_id=str(user.id),
        requester_role=user.role.value,
    )


@router.post("/agents", status_code=201)
async def create_agent(body: CreateAgent, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    return await repo.create_agent(
        name=body.name,
        description=body.description or "",
        content=body.content,
        org_id=str(user.organization_id),
        created_by=str(user.id),
        owner_user_id=str(user.id),
    )


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    agent = await repo.get_agent(
        agent_id,
        str(user.organization_id),
        requester_user_id=str(user.id),
        requester_role=user.role.value,
    )
    if not agent:
        raise HTTPException(404, "Agent not found")
    return agent


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, body: CreateAgent, user: AuthenticatedUser):
    pool = await get_pool()
    acl = AccessControlService(pool)
    if not await acl.can_modify(str(user.id), "agent", agent_id, str(user.organization_id)):
        raise HTTPException(404, "Agent not found")
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    result = await repo.update_agent(
        agent_id,
        str(user.organization_id),
        name=body.name,
        description=body.description or "",
        content=body.content,
    )
    if not result:
        raise HTTPException(404, "Agent not found")
    return result


@router.post("/agents/{agent_id}/approve")
async def approve_agent(agent_id: str, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    result = await repo.approve_agent(agent_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "Agent not found or not in proposed status")
    return result


@router.post("/agents/{agent_id}/reject")
async def reject_agent(agent_id: str, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    result = await repo.reject_agent(agent_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "Agent not found or not in proposed status")
    return result


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, user: AuthenticatedUser):
    if user.role.value not in ("admin", "owner"):
        raise HTTPException(403, "Forbidden: admin or owner role required")
    pool = await get_pool()
    acl = AccessControlService(pool)
    if not await acl.can_modify(str(user.id), "agent", agent_id, str(user.organization_id)):
        raise HTTPException(404, "Agent not found")
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    if not await repo.delete_agent(agent_id, str(user.organization_id)):
        raise HTTPException(404, "Agent not found")


# ── Agent Access Grants ──────────────────────────────────────────────────


@router.post("/agents/{agent_id}/skills")
async def grant_skill_to_agent(agent_id: str, body: GrantAccess, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    if not await repo.get_agent(
        agent_id, org_id, requester_user_id=str(user.id), requester_role=user.role.value
    ):
        raise HTTPException(404, "Agent not found")
    if not await repo.get_skill(
        body.target_id, org_id, requester_user_id=str(user.id), requester_role=user.role.value
    ):
        raise HTTPException(404, "Skill not found")
    if not await repo.grant_skill(agent_id, body.target_id, org_id=org_id, user_id=str(user.id)):
        raise HTTPException(400, "Failed to grant skill")
    return {"status": "granted"}


@router.delete("/agents/{agent_id}/skills/{skill_id}")
async def revoke_skill_from_agent(agent_id: str, skill_id: str, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    if not await repo.get_agent(
        agent_id,
        str(user.organization_id),
        requester_user_id=str(user.id),
        requester_role=user.role.value,
    ):
        raise HTTPException(404, "Agent not found")
    await repo.revoke_skill(
        agent_id, skill_id,
        org_id=str(user.organization_id), user_id=str(user.id),
    )
    return {"status": "revoked"}


@router.post("/agents/{agent_id}/mcp-servers")
async def grant_mcp_to_agent(agent_id: str, body: GrantAccess, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    if not await repo.get_agent(
        agent_id, org_id, requester_user_id=str(user.id), requester_role=user.role.value
    ):
        raise HTTPException(404, "Agent not found")
    if not await repo.grant_mcp_server(
        agent_id, body.target_id, org_id=org_id, user_id=str(user.id),
    ):
        raise HTTPException(400, "Failed to grant MCP server")
    return {"status": "granted"}


@router.delete("/agents/{agent_id}/mcp-servers/{server_id}")
async def revoke_mcp_from_agent(agent_id: str, server_id: str, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    if not await repo.get_agent(
        agent_id,
        str(user.organization_id),
        requester_user_id=str(user.id),
        requester_role=user.role.value,
    ):
        raise HTTPException(404, "Agent not found")
    await repo.revoke_mcp_server(
        agent_id, server_id,
        org_id=str(user.organization_id), user_id=str(user.id),
    )
    return {"status": "revoked"}


# ── Skill Endpoints ──────────────────────────────────────────────────────


@router.get("/skills")
async def list_skills(
    user: AuthenticatedUser,
    status: Literal["proposed", "active", "rejected"] | None = None,
):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    return await repo.list_skills(
        str(user.organization_id),
        status=status,
        requester_user_id=str(user.id),
        requester_role=user.role.value,
    )


@router.post("/skills", status_code=201)
async def create_skill(body: CreateSkill, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    return await repo.create_skill(
        name=body.name,
        description=body.description or "",
        content=body.content,
        org_id=str(user.organization_id),
        created_by=str(user.id),
        owner_user_id=str(user.id),
    )


@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    skill = await repo.get_skill(
        skill_id,
        str(user.organization_id),
        requester_user_id=str(user.id),
        requester_role=user.role.value,
    )
    if not skill:
        raise HTTPException(404, "Skill not found")
    return skill


@router.post("/skills/{skill_id}/approve")
async def approve_skill(skill_id: str, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    result = await repo.approve_skill(skill_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "Skill not found or not in proposed status")
    return result


@router.post("/skills/{skill_id}/reject")
async def reject_skill(skill_id: str, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    result = await repo.reject_skill(skill_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "Skill not found or not in proposed status")
    return result


@router.delete("/skills/{skill_id}", status_code=204)
async def delete_skill(skill_id: str, user: AuthenticatedUser):
    if user.role.value not in ("admin", "owner"):
        raise HTTPException(403, "Forbidden: admin or owner role required")
    pool = await get_pool()
    acl = AccessControlService(pool)
    if not await acl.can_modify(str(user.id), "skill", skill_id, str(user.organization_id)):
        raise HTTPException(404, "Skill not found")
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    if not await repo.delete_skill(skill_id, str(user.organization_id)):
        raise HTTPException(404, "Skill not found")


# ── MCP Server Endpoints ─────────────────────────────────────────────────


@router.get("/mcp-servers")
async def list_mcp_servers(
    user: AuthenticatedUser, status: Literal["proposed", "active", "rejected"] | None = None
):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    return await repo.list_mcp_servers(
        str(user.organization_id),
        status=status,
        requester_user_id=str(user.id),
        requester_role=user.role.value,
    )


@router.post("/mcp-servers", status_code=201)
async def create_mcp_server(body: CreateMCPServer, user: AuthenticatedUser):
    # Validate URL against SSRF before persisting.
    if body.server_type == "http" and body.url:
        try:
            validate_url(body.url, purpose="MCP server")
        except SSRFError as exc:
            raise HTTPException(400, str(exc))
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    return await repo.create_mcp_server(
        name=body.name,
        description=body.description or "",
        server_type=body.server_type,
        url=body.url,
        org_id=str(user.organization_id),
        created_by=str(user.id),
        headers=body.headers,
        command=body.command,
        args=body.args,
        env_vars=body.env_vars,
        owner_user_id=str(user.id),
    )


@router.post("/mcp-servers/{server_id}/approve")
async def approve_mcp_server(server_id: str, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    result = await repo.approve_mcp_server(server_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "MCP server not found or not in proposed status")
    return result


@router.patch("/mcp-servers/{server_id}")
async def update_mcp_server(server_id: str, body: UpdateMCPServer, user: AuthenticatedUser):
    # Validate URL against SSRF if the update includes a URL change.
    if body.url is not None:
        effective_type = body.server_type or "http"
        if effective_type == "http":
            try:
                validate_url(body.url, purpose="MCP server")
            except SSRFError as exc:
                raise HTTPException(400, str(exc))
    pool = await get_pool()
    acl = AccessControlService(pool)
    if not await acl.can_modify(str(user.id), "mcp_server", server_id, str(user.organization_id)):
        raise HTTPException(404, "MCP server not found")
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    updates = body.model_dump(exclude_none=True)
    result = await repo.update_mcp_server(server_id, str(user.organization_id), **updates)
    if not result:
        raise HTTPException(404, "MCP server not found")
    return result


@router.post("/mcp-servers/{server_id}/reject")
async def reject_mcp_server(server_id: str, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    result = await repo.reject_mcp_server(server_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "MCP server not found or not in proposed status")
    return result


@router.get("/mcp-servers/{server_id}/tools")
async def discover_mcp_server_tools(
    server_id: str,
    user: AuthenticatedUser,
    refresh: bool = False,
):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    server = await repo.get_mcp_server(
        server_id,
        org_id,
        requester_user_id=str(user.id),
        requester_role=user.role.value,
    )
    if not server:
        raise HTTPException(404, "MCP server not found")

    try:
        if refresh:
            tools = await discover_mcp_tools(server, pool)
            from_cache = False
        else:
            tools, from_cache = await get_tools_cached(server_id, org_id, pool)
    except MCPDiscoveryError as exc:
        cached = await repo.get_discovered_tools(server_id, org_id)
        discovered_at = cached.get("tools_discovered_at") if cached else None
        return {
            "tools": [],
            "from_cache": False,
            "discovered_at": discovered_at.isoformat() if discovered_at else None,
            "error": f"Connection failed: {exc}",
        }

    cached = await repo.get_discovered_tools(server_id, org_id)
    discovered_at = cached.get("tools_discovered_at") if cached else None
    return {
        "tools": tools,
        "from_cache": from_cache,
        "discovered_at": discovered_at.isoformat() if discovered_at else None,
        "error": None,
    }


# ── Pending Proposals ─────────────────────────────────────────────────────


@router.get("/proposals")
async def list_proposals(user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    return await repo.get_pending_proposals(str(user.organization_id))
