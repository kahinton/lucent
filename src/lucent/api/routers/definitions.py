"""API router for agent, skill, and MCP server definitions.

Provides CRUD endpoints and approval workflow for managing definitions.
Agents can be granted access to specific skills and MCP servers.
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from lucent.access_control import AccessControlService
from lucent.api.deps import AdminUser, AuthenticatedUser
from lucent.db import DefinitionRepository, get_pool
from lucent.db.audit import AuditRepository
from lucent.db.definitions import BuiltInProtectionError
from lucent.rbac import Role
from lucent.security import scan_content_for_injection
from lucent.services.mcp_discovery import (
    MCPDiscoveryError,
    discover_mcp_tools,
    get_tools_cached,
)
from lucent.url_validation import SSRFError, validate_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/definitions", tags=["definitions"])

HookTriggerEvent = Literal[
    "tool_call",
    "before_model_call",
    "after_model_call",
    "before_tool_call",
    "after_tool_call",
]


def _validate_user_definition_create(status_value: str, scope_value: str) -> None:
    if status_value != "proposed" or scope_value != "instance":
        raise HTTPException(
            403,
            "Definitions created through the API must start as proposed instance definitions",
        )


def _require_admin_for_stdio(
    user: AuthenticatedUser,
    server_type: str | None,
    command: str | None = None,
) -> None:
    if (server_type == "stdio" or command is not None) and user.role < Role.ADMIN:
        raise HTTPException(403, "Stdio MCP servers require admin or owner role")


# ── Request Models ────────────────────────────────────────────────────────


class CreateAgent(BaseModel):
    name: str = Field(max_length=64)
    description: str | None = None
    content: str
    status: Literal["proposed", "active", "rejected"] = "proposed"
    scope: Literal["instance", "built-in"] = "instance"
    proposal_reason: str | None = None
    proposal_evidence: dict | None = None


class CreateSkill(BaseModel):
    name: str = Field(max_length=64)
    description: str | None = None
    content: str
    status: Literal["proposed", "active", "rejected"] = "proposed"
    scope: Literal["instance", "built-in"] = "instance"
    proposal_reason: str | None = None
    proposal_evidence: dict | None = None


class CreateMCPServer(BaseModel):
    name: str = Field(max_length=64)
    description: str | None = None
    server_type: str = "http"
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    headers: dict | None = None
    env_vars: dict[str, str] | None = None
    proposal_reason: str | None = None
    proposal_evidence: dict | None = None


class CreateHook(BaseModel):
    name: str = Field(max_length=64)
    description: str | None = None
    trigger_event: HookTriggerEvent = "before_tool_call"
    action_type: Literal["memory_lookup", "static_context", "command"]
    content: str | None = None
    config: dict | None = None
    status: Literal["proposed", "active", "rejected"] = "proposed"
    scope: Literal["instance", "built-in"] = "instance"
    proposal_reason: str | None = None
    proposal_evidence: dict | None = None


class UpdateHook(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    description: str | None = None
    trigger_event: HookTriggerEvent | None = None
    action_type: Literal["memory_lookup", "static_context", "command"] | None = None
    content: str | None = None
    config: dict | None = None


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


class ImportRequest(BaseModel):
    """Request body for importing a definition from an external source."""
    source_type: str = Field(
        ...,
        pattern=r"^(url|github|raw)$",
        description="Import source: 'url', 'github', or 'raw'",
    )
    source: str = Field(
        ...,
        min_length=1,
        max_length=100000,
        description="URL, GitHub path (owner/repo/path), or raw markdown content",
    )
    definition_type: str | None = Field(
        None,
        pattern=r"^(agent|skill)$",
        description="Hint: 'agent' or 'skill'. Auto-detected if not provided.",
    )


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
    _validate_user_definition_create(body.status, body.scope)
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    result = await repo.create_agent(
        name=body.name,
        description=body.description or "",
        content=body.content,
        org_id=str(user.organization_id),
        created_by=str(user.id),
        status=body.status,
        scope=body.scope,
        owner_user_id=str(user.id),
        proposal_reason=body.proposal_reason,
        proposal_evidence=body.proposal_evidence,
    )
    security_flags = scan_content_for_injection(body.content or "")
    if body.description:
        security_flags.extend(scan_content_for_injection(body.description))
    if security_flags:
        logger.warning("Definition '%s' flagged: %s", body.name, security_flags)
        result["security_warnings"] = security_flags
    return result


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
    try:
        result = await repo.update_agent(
            agent_id,
            str(user.organization_id),
            requester_role=user.role.value,
            name=body.name,
            description=body.description or "",
            content=body.content,
        )
    except BuiltInProtectionError as exc:
        raise HTTPException(403, str(exc))
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


@router.post("/agents/{agent_id}/hooks")
async def grant_hook_to_agent(agent_id: str, body: GrantAccess, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    if not await repo.get_agent(
        agent_id, org_id, requester_user_id=str(user.id), requester_role=user.role.value
    ):
        raise HTTPException(404, "Agent not found")
    hook = await repo.get_hook(
        body.target_id, org_id, requester_user_id=str(user.id), requester_role=user.role.value
    )
    if not hook or hook.get("status") != "active":
        raise HTTPException(404, "Active hook not found")
    if not await repo.grant_hook(agent_id, body.target_id, org_id=org_id, user_id=str(user.id)):
        raise HTTPException(400, "Failed to grant hook")
    return {"status": "granted"}


@router.delete("/agents/{agent_id}/hooks/{hook_id}")
async def revoke_hook_from_agent(agent_id: str, hook_id: str, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    if not await repo.get_agent(
        agent_id,
        str(user.organization_id),
        requester_user_id=str(user.id),
        requester_role=user.role.value,
    ):
        raise HTTPException(404, "Agent not found")
    await repo.revoke_hook(
        agent_id, hook_id,
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
    _validate_user_definition_create(body.status, body.scope)
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    result = await repo.create_skill(
        name=body.name,
        description=body.description or "",
        content=body.content,
        org_id=str(user.organization_id),
        created_by=str(user.id),
        status=body.status,
        scope=body.scope,
        owner_user_id=str(user.id),
        proposal_reason=body.proposal_reason,
        proposal_evidence=body.proposal_evidence,
    )
    security_flags = scan_content_for_injection(body.content or "")
    if body.description:
        security_flags.extend(scan_content_for_injection(body.description))
    if security_flags:
        logger.warning("Definition '%s' flagged: %s", body.name, security_flags)
        result["security_warnings"] = security_flags
    return result


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
    _require_admin_for_stdio(user, body.server_type, body.command)
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
        proposal_reason=body.proposal_reason,
        proposal_evidence=body.proposal_evidence,
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
    existing = await repo.get_mcp_server(
        server_id,
        str(user.organization_id),
        requester_user_id=str(user.id),
        requester_role=user.role.value,
    )
    if not existing:
        raise HTTPException(404, "MCP server not found")
    effective_type = body.server_type or existing.get("server_type")
    _require_admin_for_stdio(user, effective_type, body.command)
    updates = body.model_dump(exclude_none=True)
    try:
        result = await repo.update_mcp_server(
            server_id, str(user.organization_id),
            requester_role=user.role.value, **updates,
        )
    except BuiltInProtectionError as exc:
        raise HTTPException(403, str(exc))
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

    if refresh:
        if server.get("status") != "active":
            raise HTTPException(409, "MCP tool discovery requires an active approved server")
        if server.get("server_type") == "stdio" and user.role < Role.ADMIN:
            raise HTTPException(403, "Stdio MCP tool discovery requires admin or owner role")

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


# ── Hook Endpoints ───────────────────────────────────────────────────────


@router.get("/hooks")
async def list_hooks(
    user: AuthenticatedUser,
    status: Literal["proposed", "active", "rejected"] | None = None,
    limit: int = 25,
    offset: int = 0,
):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    return await repo.list_hooks(
        str(user.organization_id),
        status=status,
        limit=min(limit, 200),
        offset=offset,
        requester_user_id=str(user.id),
        requester_role=user.role.value,
    )


@router.post("/hooks", status_code=201)
async def create_hook(body: CreateHook, user: AuthenticatedUser):
    _validate_user_definition_create(body.status, body.scope)
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    content = body.content or ""
    security_flags = scan_content_for_injection(content)
    if body.description:
        security_flags.extend(scan_content_for_injection(body.description))
    result = await repo.create_hook(
        name=body.name,
        description=body.description or "",
        trigger_event=body.trigger_event,
        action_type=body.action_type,
        content=content,
        config=body.config or {},
        org_id=str(user.organization_id),
        created_by=str(user.id),
        status=body.status,
        scope=body.scope,
        owner_user_id=str(user.id),
        proposal_reason=body.proposal_reason,
        proposal_evidence=body.proposal_evidence,
    )
    if security_flags:
        logger.warning("Hook definition '%s' flagged: %s", body.name, security_flags)
        result["security_warnings"] = security_flags
    return result


@router.get("/hooks/{hook_id}")
async def get_hook(hook_id: str, user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    hook = await repo.get_hook(
        hook_id,
        str(user.organization_id),
        requester_user_id=str(user.id),
        requester_role=user.role.value,
    )
    if not hook:
        raise HTTPException(404, "Hook not found")
    return hook


@router.patch("/hooks/{hook_id}")
async def update_hook(hook_id: str, body: UpdateHook, user: AuthenticatedUser):
    pool = await get_pool()
    acl = AccessControlService(pool)
    if not await acl.can_modify(str(user.id), "hook", hook_id, str(user.organization_id)):
        raise HTTPException(404, "Hook not found")
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    updates = body.model_dump(exclude_none=True)
    try:
        result = await repo.update_hook(
            hook_id,
            str(user.organization_id),
            requester_role=user.role.value,
            **updates,
        )
    except BuiltInProtectionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not result:
        raise HTTPException(404, "Hook not found")
    return result


@router.post("/hooks/{hook_id}/approve")
async def approve_hook(hook_id: str, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    result = await repo.approve_hook(hook_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "Hook not found or not in proposed status")
    return result


@router.post("/hooks/{hook_id}/reject")
async def reject_hook(hook_id: str, user: AdminUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    result = await repo.reject_hook(hook_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "Hook not found or not in proposed status")
    return result


@router.delete("/hooks/{hook_id}", status_code=204)
async def delete_hook(hook_id: str, user: AuthenticatedUser):
    if user.role.value not in ("admin", "owner"):
        raise HTTPException(403, "Forbidden: admin or owner role required")
    pool = await get_pool()
    acl = AccessControlService(pool)
    if not await acl.can_modify(str(user.id), "hook", hook_id, str(user.organization_id)):
        raise HTTPException(404, "Hook not found")
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    if not await repo.delete_hook(hook_id, str(user.organization_id)):
        raise HTTPException(404, "Hook not found")


# ── Pending Proposals ─────────────────────────────────────────────────────


@router.get("/proposals")
async def list_proposals(user: AuthenticatedUser):
    pool = await get_pool()
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    return await repo.get_pending_proposals(str(user.organization_id))


# ── Import Endpoints ──────────────────────────────────────────────────────


@router.post("/import/preview")
async def preview_import(body: ImportRequest, user: AuthenticatedUser):
    """Preview an import — fetch, parse, and scan without creating anything.

    Returns the parsed definition, security findings, and what would be created.
    Use this to review before committing the import.
    """
    from lucent.services.definition_import import ImportSourceType, import_definition

    result = await import_definition(
        source_type=ImportSourceType(body.source_type),
        source=body.source,
        definition_type_hint=body.definition_type,
    )
    return result.to_dict()


@router.post("/import/commit", status_code=201)
async def commit_import(body: ImportRequest, user: AuthenticatedUser):
    """Import a definition — fetch, parse, scan, and create in proposed status.

    The definition goes through the normal approval workflow.
    Security findings are included in the response for admin review.
    """
    from lucent.services.definition_import import (
        DefinitionType,
        ImportSourceType,
        import_definition,
    )

    result = await import_definition(
        source_type=ImportSourceType(body.source_type),
        source=body.source,
        definition_type_hint=body.definition_type,
    )

    if not result.success:
        raise HTTPException(400, result.error or "Import failed")

    if result.has_critical_findings:
        # Don't create — return the findings for review
        return {
            "status": "blocked",
            "reason": "Critical security findings detected. Review findings before importing.",
            **result.to_dict(),
        }

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
        # If skill_names were specified, log them (can't auto-grant without existing skill IDs)
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
        raise HTTPException(500, "Failed to create definition")

    return {
        "status": "imported",
        "definition": created,
        "security_findings": [
            {"severity": f.severity.value, "category": f.category, "detail": f.detail}
            for f in result.security_findings
        ],
        "source_url": result.source_url,
        "content_hash": result.content_hash,
    }
