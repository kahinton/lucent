"""API router for agent, skill, and MCP server definitions.

Provides CRUD endpoints and approval workflow for managing definitions.
Agents can be granted access to specific skills and MCP servers.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from lucent.api.deps import AuthenticatedUser
from lucent.db import DefinitionRepository, get_pool

router = APIRouter(prefix="/definitions", tags=["definitions"])


# ── Request Models ────────────────────────────────────────────────────────

class CreateAgent(BaseModel):
    name: str = Field(max_length=64)
    description: str | None = None
    content: str
    scope: str = "instance"

class CreateSkill(BaseModel):
    name: str = Field(max_length=64)
    description: str | None = None
    content: str
    scope: str = "instance"

class CreateMCPServer(BaseModel):
    name: str = Field(max_length=64)
    description: str | None = None
    server_type: str = "http"
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    headers: dict | None = None

class GrantAccess(BaseModel):
    target_id: str  # skill_id or mcp_server_id


# ── Agent Endpoints ──────────────────────────────────────────────────────

@router.get("/agents")
async def list_agents(
    user: AuthenticatedUser,
    status: str | None = None, scope: str | None = None,
):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    return await repo.list_agents(str(user.organization_id), status=status, scope=scope)

@router.post("/agents", status_code=201)
async def create_agent(body: CreateAgent, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    return await repo.create_agent(
        name=body.name, description=body.description or "", content=body.content,
        org_id=str(user.organization_id), created_by=str(user.id), scope=body.scope,
    )

@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    agent = await repo.get_agent(agent_id, str(user.organization_id))
    if not agent:
        raise HTTPException(404, "Agent not found")
    return agent

@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, body: CreateAgent, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    result = await repo.update_agent(
        agent_id, str(user.organization_id),
        name=body.name, description=body.description, content=body.content, scope=body.scope,
    )
    if not result:
        raise HTTPException(404, "Agent not found")
    return result

@router.post("/agents/{agent_id}/approve")
async def approve_agent(agent_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    result = await repo.approve_agent(agent_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "Agent not found or not in proposed status")
    return result

@router.post("/agents/{agent_id}/reject")
async def reject_agent(agent_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    result = await repo.reject_agent(agent_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "Agent not found or not in proposed status")
    return result

@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    if not await repo.delete_agent(agent_id, str(user.organization_id)):
        raise HTTPException(404, "Agent not found")

# ── Agent Access Grants ──────────────────────────────────────────────────

@router.post("/agents/{agent_id}/skills")
async def grant_skill_to_agent(agent_id: str, body: GrantAccess, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    if not await repo.grant_skill(agent_id, body.target_id):
        raise HTTPException(400, "Failed to grant skill")
    return {"status": "granted"}

@router.delete("/agents/{agent_id}/skills/{skill_id}")
async def revoke_skill_from_agent(agent_id: str, skill_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    await repo.revoke_skill(agent_id, skill_id)
    return {"status": "revoked"}

@router.post("/agents/{agent_id}/mcp-servers")
async def grant_mcp_to_agent(agent_id: str, body: GrantAccess, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    if not await repo.grant_mcp_server(agent_id, body.target_id):
        raise HTTPException(400, "Failed to grant MCP server")
    return {"status": "granted"}

@router.delete("/agents/{agent_id}/mcp-servers/{server_id}")
async def revoke_mcp_from_agent(agent_id: str, server_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    await repo.revoke_mcp_server(agent_id, server_id)
    return {"status": "revoked"}

# ── Skill Endpoints ──────────────────────────────────────────────────────

@router.get("/skills")
async def list_skills(
    user: AuthenticatedUser,
    status: str | None = None, scope: str | None = None,
):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    return await repo.list_skills(str(user.organization_id), status=status, scope=scope)

@router.post("/skills", status_code=201)
async def create_skill(body: CreateSkill, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    return await repo.create_skill(
        name=body.name, description=body.description or "", content=body.content,
        org_id=str(user.organization_id), created_by=str(user.id), scope=body.scope,
    )

@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    skill = await repo.get_skill(skill_id, str(user.organization_id))
    if not skill:
        raise HTTPException(404, "Skill not found")
    return skill

@router.post("/skills/{skill_id}/approve")
async def approve_skill(skill_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    result = await repo.approve_skill(skill_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "Skill not found or not in proposed status")
    return result

@router.post("/skills/{skill_id}/reject")
async def reject_skill(skill_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    result = await repo.reject_skill(skill_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "Skill not found or not in proposed status")
    return result

@router.delete("/skills/{skill_id}", status_code=204)
async def delete_skill(skill_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    if not await repo.delete_skill(skill_id, str(user.organization_id)):
        raise HTTPException(404, "Skill not found")

# ── MCP Server Endpoints ─────────────────────────────────────────────────

@router.get("/mcp-servers")
async def list_mcp_servers(user: AuthenticatedUser, status: str | None = None):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    return await repo.list_mcp_servers(str(user.organization_id), status=status)

@router.post("/mcp-servers", status_code=201)
async def create_mcp_server(body: CreateMCPServer, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    return await repo.create_mcp_server(
        name=body.name, description=body.description or "", server_type=body.server_type,
        url=body.url, org_id=str(user.organization_id), created_by=str(user.id),
        headers=body.headers, command=body.command, args=body.args,
    )

@router.post("/mcp-servers/{server_id}/approve")
async def approve_mcp_server(server_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    result = await repo.approve_mcp_server(server_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "MCP server not found or not in proposed status")
    return result

@router.post("/mcp-servers/{server_id}/reject")
async def reject_mcp_server(server_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    result = await repo.reject_mcp_server(server_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "MCP server not found or not in proposed status")
    return result

# ── Pending Proposals ─────────────────────────────────────────────────────

@router.get("/proposals")
async def list_proposals(user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool)
    return await repo.get_pending_proposals(str(user.organization_id))
