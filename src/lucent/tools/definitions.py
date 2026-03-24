"""MCP tools for agent, skill, and MCP server definition management."""

import json
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from lucent.db import get_pool
from lucent.db.definitions import DefinitionRepository
from lucent.tools.memories import _get_current_user_context


async def _get_definition_repository() -> DefinitionRepository:
    """Get a DefinitionRepository instance."""
    pool = await get_pool()
    return DefinitionRepository(pool)


def _serialize(obj):
    """JSON serializer for UUIDs and datetimes."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    return str(obj)


def register_definition_tools(mcp: FastMCP) -> None:
    """Register definition management tools with the MCP server."""

    # ── Read-only tools ──────────────────────────────────────────────────

    @mcp.tool(
        description="""List agent definitions in the organization.

Filter by status to see only proposed, active, or rejected agents.
Returns paginated results with agent metadata (no content field).

Args:
    status: Optional filter — 'proposed', 'active', or 'rejected'
    limit: Max results to return (default 25, max 100)
    offset: Pagination offset (default 0)

Returns: JSON with items array, total_count, and pagination info."""
    )
    async def list_agent_definitions(
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        user_id, org_id, role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_definition_repository()
        result = await repo.list_agents(
            str(org_id),
            status=status,
            limit=min(limit, 100),
            offset=offset,
            requester_user_id=str(user_id) if user_id else None,
            requester_role=role,
        )
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Get full details of an agent definition by ID.

Returns the agent with its content, granted skill names, and MCP server names.

Args:
    agent_id: UUID of the agent definition

Returns: JSON with the agent details, or an error if not found."""
    )
    async def get_agent_definition(agent_id: str) -> str:
        user_id, org_id, role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_definition_repository()
        agent = await repo.get_agent(
            agent_id,
            str(org_id),
            requester_user_id=str(user_id) if user_id else None,
            requester_role=role,
        )
        if not agent:
            return json.dumps({"error": "Agent not found"})
        return json.dumps(agent, default=_serialize)

    @mcp.tool(
        description="""List skill definitions in the organization.

Filter by status to see only proposed, active, or rejected skills.
Returns paginated results with skill metadata (no content field).

Args:
    status: Optional filter — 'proposed', 'active', or 'rejected'
    limit: Max results to return (default 25, max 100)
    offset: Pagination offset (default 0)

Returns: JSON with items array, total_count, and pagination info."""
    )
    async def list_skill_definitions(
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        user_id, org_id, role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_definition_repository()
        result = await repo.list_skills(
            str(org_id),
            status=status,
            limit=min(limit, 100),
            offset=offset,
            requester_user_id=str(user_id) if user_id else None,
            requester_role=role,
        )
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Get full details of a skill definition by ID.

Returns the skill with its full content.

Args:
    skill_id: UUID of the skill definition

Returns: JSON with the skill details, or an error if not found."""
    )
    async def get_skill_definition(skill_id: str) -> str:
        user_id, org_id, role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_definition_repository()
        skill = await repo.get_skill(
            skill_id,
            str(org_id),
            requester_user_id=str(user_id) if user_id else None,
            requester_role=role,
        )
        if not skill:
            return json.dumps({"error": "Skill not found"})
        return json.dumps(skill, default=_serialize)

    @mcp.tool(
        description="""List all pending proposals (agents, skills, and MCP servers
awaiting approval).

Returns: JSON with agents, skills, mcp_servers arrays and total count."""
    )
    async def list_proposals() -> str:
        _, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_definition_repository()
        result = await repo.get_pending_proposals(str(org_id))
        return json.dumps(result, default=_serialize)

    # ── Write tools ──────────────────────────────────────────────────────

    @mcp.tool(
        description="""Create a new agent definition.

The agent starts in 'proposed' status and must be approved by an admin
before it can be used for task dispatch.

Args:
    name: Agent name (max 64 chars, e.g. 'code', 'research', 'documentation')
    description: What this agent does
    content: Full agent definition content (markdown prompt)

Returns: JSON with the created agent including its ID and status."""
    )
    async def create_agent_definition(
        name: str,
        description: str = "",
        content: str = "",
    ) -> str:
        user_id, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if not name or len(name) > 64:
            return json.dumps({"error": "name is required and must be <= 64 characters"})
        if not content:
            return json.dumps({"error": "content is required"})

        repo = await _get_definition_repository()
        agent = await repo.create_agent(
            name=name,
            description=description,
            content=content,
            org_id=str(org_id),
            created_by=str(user_id),
            owner_user_id=str(user_id),
        )
        return json.dumps(agent, default=_serialize)

    @mcp.tool(
        description="""Create a new skill definition.

The skill starts in 'proposed' status and must be approved by an admin
before it can be granted to agents.

Args:
    name: Skill name (max 64 chars, e.g. 'code-review', 'security-audit')
    description: What this skill provides
    content: Full skill definition content (markdown prompt)

Returns: JSON with the created skill including its ID and status."""
    )
    async def create_skill_definition(
        name: str,
        description: str = "",
        content: str = "",
    ) -> str:
        user_id, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if not name or len(name) > 64:
            return json.dumps({"error": "name is required and must be <= 64 characters"})
        if not content:
            return json.dumps({"error": "content is required"})

        repo = await _get_definition_repository()
        skill = await repo.create_skill(
            name=name,
            description=description,
            content=content,
            org_id=str(org_id),
            created_by=str(user_id),
            owner_user_id=str(user_id),
        )
        return json.dumps(skill, default=_serialize)

    @mcp.tool(
        description="""Grant a skill to an agent definition.

Both the agent and skill must exist in the organization. Once granted,
the agent will have access to the skill when dispatched.

Args:
    agent_id: UUID of the agent definition
    skill_id: UUID of the skill definition to grant

Returns: JSON with status 'granted', or an error if either is not found."""
    )
    async def grant_skill_to_agent(
        agent_id: str,
        skill_id: str,
    ) -> str:
        user_id, org_id, role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        repo = await _get_definition_repository()

        # Verify agent exists and is accessible
        agent = await repo.get_agent(
            agent_id,
            str(org_id),
            requester_user_id=str(user_id),
            requester_role=role,
        )
        if not agent:
            return json.dumps({"error": "Agent not found"})

        # Verify skill exists and is accessible
        skill = await repo.get_skill(
            skill_id,
            str(org_id),
            requester_user_id=str(user_id),
            requester_role=role,
        )
        if not skill:
            return json.dumps({"error": "Skill not found"})

        success = await repo.grant_skill(
            agent_id, skill_id,
            org_id=str(org_id),
            user_id=str(user_id),
        )
        if not success:
            return json.dumps({"error": "Failed to grant skill"})
        return json.dumps({"status": "granted", "agent_id": agent_id, "skill_id": skill_id})

    @mcp.tool(
        description="""Update an agent definition's name, description, or content.

Args:
    agent_id: UUID of the agent definition
    name: New name (optional, max 64 chars)
    description: New description (optional)
    content: New content (optional, markdown prompt)

Returns: JSON with the updated agent, or an error if not found."""
    )
    async def update_agent_definition(
        agent_id: str,
        name: str | None = None,
        description: str | None = None,
        content: str | None = None,
    ) -> str:
        user_id, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        kwargs = {}
        if name is not None:
            kwargs["name"] = name
        if description is not None:
            kwargs["description"] = description
        if content is not None:
            kwargs["content"] = content

        repo = await _get_definition_repository()
        result = await repo.update_agent(agent_id, str(org_id), **kwargs)
        if not result:
            return json.dumps({"error": "Agent not found"})
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Approve an agent definition (admin/owner only).

Moves the agent from 'proposed' to 'active' status, making it available for dispatch.

Args:
    agent_id: UUID of the agent definition

Returns: JSON with the updated agent, or an error if not found or not in proposed status."""
    )
    async def approve_agent_definition(agent_id: str) -> str:
        user_id, org_id, user_role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if user_role not in ("admin", "owner"):
            return json.dumps({"error": "Admin or owner role required"})

        repo = await _get_definition_repository()
        result = await repo.approve_agent(agent_id, str(org_id), str(user_id))
        if not result:
            return json.dumps({"error": "Agent not found or not in proposed status"})
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Reject an agent definition (admin/owner only).

Moves the agent from 'proposed' to 'rejected' status.

Args:
    agent_id: UUID of the agent definition

Returns: JSON with the updated agent, or an error if not found or not in proposed status."""
    )
    async def reject_agent_definition(agent_id: str) -> str:
        user_id, org_id, user_role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if user_role not in ("admin", "owner"):
            return json.dumps({"error": "Admin or owner role required"})

        repo = await _get_definition_repository()
        result = await repo.reject_agent(agent_id, str(org_id), str(user_id))
        if not result:
            return json.dumps({"error": "Agent not found or not in proposed status"})
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Delete an agent definition.

Args:
    agent_id: UUID of the agent definition

Returns: JSON with status 'deleted', or an error if not found."""
    )
    async def delete_agent_definition(agent_id: str) -> str:
        user_id, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        repo = await _get_definition_repository()
        success = await repo.delete_agent(agent_id, str(org_id))
        if not success:
            return json.dumps({"error": "Agent not found"})
        return json.dumps({"status": "deleted", "agent_id": agent_id})

    @mcp.tool(
        description="""Revoke a skill from an agent definition.

Args:
    agent_id: UUID of the agent definition
    skill_id: UUID of the skill to revoke

Returns: JSON with status 'revoked', or an error if not found."""
    )
    async def revoke_skill_from_agent(agent_id: str, skill_id: str) -> str:
        user_id, org_id, role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        repo = await _get_definition_repository()
        agent = await repo.get_agent(
            agent_id, str(org_id),
            requester_user_id=str(user_id),
            requester_role=role,
        )
        if not agent:
            return json.dumps({"error": "Agent not found"})

        await repo.revoke_skill(agent_id, skill_id, org_id=str(org_id), user_id=str(user_id))
        return json.dumps({"status": "revoked", "agent_id": agent_id, "skill_id": skill_id})

    @mcp.tool(
        description="""Grant an MCP server to an agent definition.

Args:
    agent_id: UUID of the agent definition
    definition_id: UUID of the MCP server definition to grant

Returns: JSON with status 'granted', or an error if not found."""
    )
    async def grant_mcp_server_to_agent(agent_id: str, definition_id: str) -> str:
        user_id, org_id, role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        repo = await _get_definition_repository()
        agent = await repo.get_agent(
            agent_id, str(org_id),
            requester_user_id=str(user_id),
            requester_role=role,
        )
        if not agent:
            return json.dumps({"error": "Agent not found"})

        success = await repo.grant_mcp_server(
            agent_id, definition_id, org_id=str(org_id), user_id=str(user_id)
        )
        if not success:
            return json.dumps({"error": "Failed to grant MCP server"})
        return json.dumps({"status": "granted", "agent_id": agent_id, "server_id": definition_id})

    @mcp.tool(
        description="""Revoke an MCP server from an agent definition.

Args:
    agent_id: UUID of the agent definition
    server_id: UUID of the MCP server to revoke

Returns: JSON with status 'revoked', or an error if not found."""
    )
    async def revoke_mcp_server_from_agent(agent_id: str, server_id: str) -> str:
        user_id, org_id, role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        repo = await _get_definition_repository()
        agent = await repo.get_agent(
            agent_id, str(org_id),
            requester_user_id=str(user_id),
            requester_role=role,
        )
        if not agent:
            return json.dumps({"error": "Agent not found"})

        await repo.revoke_mcp_server(agent_id, server_id, org_id=str(org_id), user_id=str(user_id))
        return json.dumps({"status": "revoked", "agent_id": agent_id, "server_id": server_id})

    # ── Skill write tools ─────────────────────────────────────────────────

    @mcp.tool(
        description="""Approve a skill definition (admin/owner only).

Moves the skill from 'proposed' to 'active' status.

Args:
    skill_id: UUID of the skill definition

Returns: JSON with the updated skill, or an error if not found or not in proposed status."""
    )
    async def approve_skill_definition(skill_id: str) -> str:
        user_id, org_id, user_role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if user_role not in ("admin", "owner"):
            return json.dumps({"error": "Admin or owner role required"})

        repo = await _get_definition_repository()
        result = await repo.approve_skill(skill_id, str(org_id), str(user_id))
        if not result:
            return json.dumps({"error": "Skill not found or not in proposed status"})
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Reject a skill definition (admin/owner only).

Moves the skill from 'proposed' to 'rejected' status.

Args:
    skill_id: UUID of the skill definition

Returns: JSON with the updated skill, or an error if not found or not in proposed status."""
    )
    async def reject_skill_definition(skill_id: str) -> str:
        user_id, org_id, user_role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if user_role not in ("admin", "owner"):
            return json.dumps({"error": "Admin or owner role required"})

        repo = await _get_definition_repository()
        result = await repo.reject_skill(skill_id, str(org_id), str(user_id))
        if not result:
            return json.dumps({"error": "Skill not found or not in proposed status"})
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Delete a skill definition.

Args:
    skill_id: UUID of the skill definition

Returns: JSON with status 'deleted', or an error if not found."""
    )
    async def delete_skill_definition(skill_id: str) -> str:
        user_id, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        repo = await _get_definition_repository()
        success = await repo.delete_skill(skill_id, str(org_id))
        if not success:
            return json.dumps({"error": "Skill not found"})
        return json.dumps({"status": "deleted", "skill_id": skill_id})

    # ── MCP Server tools ──────────────────────────────────────────────────

    @mcp.tool(
        description="""List MCP server definitions in the organization.

Filter by status to see only proposed, active, or rejected servers.

Args:
    status: Optional filter — 'proposed', 'active', or 'rejected'
    limit: Max results to return (default 25, max 100)
    offset: Pagination offset (default 0)

Returns: JSON with items array, total_count, and pagination info."""
    )
    async def list_mcp_server_definitions(
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        user_id, org_id, role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_definition_repository()
        result = await repo.list_mcp_servers(
            str(org_id),
            status=status,
            limit=min(limit, 100),
            offset=offset,
            requester_user_id=str(user_id) if user_id else None,
            requester_role=role,
        )
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Create a new MCP server definition.

The server starts in 'proposed' status and must be approved by an admin.

Args:
    name: Server name (max 64 chars)
    description: What this server provides
    server_type: 'http' (default) or 'stdio'
    url: Server URL (for http type)
    command: Command to run (for stdio type)
    args: Command arguments (for stdio type)
    env_vars: Environment variables as a JSON object string (optional)

Returns: JSON with the created server including its ID and status."""
    )
    async def create_mcp_server_definition(
        name: str,
        description: str = "",
        server_type: str = "http",
        url: str | None = None,
        command: str | None = None,
        args: str | None = None,
        env_vars: str | None = None,
    ) -> str:
        user_id, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if not name or len(name) > 64:
            return json.dumps({"error": "name is required and must be <= 64 characters"})

        parsed_args = None
        parsed_env_vars = None
        if args is not None:
            try:
                parsed_args = json.loads(args)
            except (json.JSONDecodeError, ValueError):
                return json.dumps({"error": "args must be a valid JSON array string"})
        if env_vars is not None:
            try:
                parsed_env_vars = json.loads(env_vars)
            except (json.JSONDecodeError, ValueError):
                return json.dumps({"error": "env_vars must be a valid JSON object string"})

        repo = await _get_definition_repository()
        server = await repo.create_mcp_server(
            name=name,
            description=description,
            server_type=server_type,
            url=url,
            org_id=str(org_id),
            created_by=str(user_id),
            command=command,
            args=parsed_args,
            env_vars=parsed_env_vars,
            owner_user_id=str(user_id),
        )
        return json.dumps(server, default=_serialize)

    @mcp.tool(
        description="""Update an MCP server definition.

Args:
    server_id: UUID of the MCP server definition
    name: New name (optional, max 64 chars)
    description: New description (optional)
    url: New URL (optional)
    server_type: New server type (optional)
    command: New command (optional, for stdio type)

Returns: JSON with the updated server, or an error if not found."""
    )
    async def update_mcp_server_definition(
        server_id: str,
        name: str | None = None,
        description: str | None = None,
        url: str | None = None,
        server_type: str | None = None,
        command: str | None = None,
    ) -> str:
        user_id, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        kwargs = {}
        if name is not None:
            kwargs["name"] = name
        if description is not None:
            kwargs["description"] = description
        if url is not None:
            kwargs["url"] = url
        if server_type is not None:
            kwargs["server_type"] = server_type
        if command is not None:
            kwargs["command"] = command

        repo = await _get_definition_repository()
        result = await repo.update_mcp_server(server_id, str(org_id), **kwargs)
        if not result:
            return json.dumps({"error": "MCP server not found"})
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Approve an MCP server definition (admin/owner only).

Moves the server from 'proposed' to 'active' status.

Args:
    server_id: UUID of the MCP server definition

Returns: JSON with the updated server, or an error if not found or not in proposed status."""
    )
    async def approve_mcp_server_definition(server_id: str) -> str:
        user_id, org_id, user_role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if user_role not in ("admin", "owner"):
            return json.dumps({"error": "Admin or owner role required"})

        repo = await _get_definition_repository()
        result = await repo.approve_mcp_server(server_id, str(org_id), str(user_id))
        if not result:
            return json.dumps({"error": "MCP server not found or not in proposed status"})
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Reject an MCP server definition (admin/owner only).

Moves the server from 'proposed' to 'rejected' status.

Args:
    server_id: UUID of the MCP server definition

Returns: JSON with the updated server, or an error if not found or not in proposed status."""
    )
    async def reject_mcp_server_definition(server_id: str) -> str:
        user_id, org_id, user_role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if user_role not in ("admin", "owner"):
            return json.dumps({"error": "Admin or owner role required"})

        repo = await _get_definition_repository()
        result = await repo.reject_mcp_server(server_id, str(org_id), str(user_id))
        if not result:
            return json.dumps({"error": "MCP server not found or not in proposed status"})
        return json.dumps(result, default=_serialize)
