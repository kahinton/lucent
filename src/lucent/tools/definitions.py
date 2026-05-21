"""MCP tools for agent, skill, and MCP server definition management."""

import json
import logging
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from lucent.db import get_pool
from lucent.db.definitions import BuiltInProtectionError, DefinitionRepository
from lucent.llm.context import get_llm_context
from lucent.tools.annotations import READ_ONLY
from lucent.tools.memories import _get_current_user_context
from lucent.url_validation import SSRFError, validate_url

logger = logging.getLogger(__name__)


async def _get_definition_repository() -> DefinitionRepository:
    """Get a DefinitionRepository instance."""
    pool = await get_pool()
    return DefinitionRepository(pool)


async def _can_modify_definition(
    user_id: str,
    org_id: str,
    resource_type: str,
    resource_id: str,
) -> bool:
    from lucent.access_control import AccessControlService

    pool = await get_pool()
    return await AccessControlService(pool).can_modify(
        user_id,
        resource_type,
        resource_id,
        org_id,
    )


def _serialize(obj):
    """JSON serializer for UUIDs and datetimes."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    return str(obj)


def _parse_config(config: dict | str | None) -> dict:
    if config is None:
        return {}
    if isinstance(config, dict):
        return config
    if isinstance(config, str):
        data = json.loads(config or "{}")
        if isinstance(data, dict):
            return data
    raise ValueError("config must be a JSON object")


def _parse_json_object(value: dict | str | None) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value or "{}")
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("value must be a JSON object")


def _parse_json_array(value: list | str | None) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value or "[]")
        if isinstance(parsed, list):
            return parsed
    raise ValueError("value must be a JSON array")


def _owner_args_for_context(
    user_id: UUID,
    user_role: str | None,
    memory_scope: str | None,
) -> dict:
    """Return repository ownership kwargs for the current MCP auth context."""
    if user_role == "daemon" and memory_scope != "user":
        return {"shared_with_org": True}
    return {"owner_user_id": str(user_id)}


def _requires_unscoped_human(memory_scope: str | None) -> str | None:
    """Return an error message when a scoped agent context tries to activate access.

    Scoped API keys are used by daemon-dispatched agents. They may create
    proposed definitions and follow-up requests, but they must not hot-patch
    active definitions, approve proposals, or grant themselves runtime powers.
    """
    if memory_scope is None:
        return None
    return (
        "Human approval required: scoped agent contexts may create proposals "
        "or follow-up requests, but cannot update active definitions, approve "
        "definitions, or grant skills/hooks/MCP servers."
    )


def register_definition_tools(mcp: FastMCP) -> None:
    """Register definition management tools with the MCP server."""

    # ── Read-only tools ──────────────────────────────────────────────────

    @mcp.tool(
        annotations=READ_ONLY,
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
        user_id, org_id, role, _, _ = await _get_current_user_context()
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
    # End of definition MCP tool registration.
    @mcp.tool(
        annotations=READ_ONLY,
        description="""Get full details of an agent definition by ID.

Returns the agent with its content, granted skill names, and MCP server names.

Args:
    agent_id: UUID of the agent definition

Returns: JSON with the agent details, or an error if not found."""
    )
    async def get_agent_definition(agent_id: str) -> str:
        user_id, org_id, role, _, _ = await _get_current_user_context()
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
        user_id, org_id, role, _, _ = await _get_current_user_context()
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
        user_id, org_id, role, _, _ = await _get_current_user_context()
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
        description="""List all pending proposals (agents, skills, MCP servers,
and sandbox templates awaiting approval).

Returns: JSON with agents, skills, mcp_servers, sandbox_templates arrays and total count."""
    )
    async def list_proposals() -> str:
        _, org_id, _, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_definition_repository()
        result = await repo.get_pending_proposals(str(org_id))

        # Include proposed sandbox templates so the planner sees what's
        # already been proposed (and won't duplicate) and admins can review
        # them in one place.
        try:
            from lucent.db import get_pool
            from lucent.db.sandbox_template import SandboxTemplateRepository

            pool = await get_pool()
            tpl_repo = SandboxTemplateRepository(pool)
            proposed_templates = await tpl_repo.list_proposed(str(org_id))
            result["sandbox_templates"] = [
                {
                    "id": str(t["id"]),
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "image": t.get("image"),
                    "network_mode": t.get("network_mode"),
                    "proposal_reason": t.get("proposal_reason"),
                    "proposed_by": str(t["proposed_by"]) if t.get("proposed_by") else None,
                    "created_at": (
                        t["created_at"].isoformat()
                        if hasattr(t.get("created_at"), "isoformat")
                        else t.get("created_at")
                    ),
                }
                for t in proposed_templates
            ]
            result["total"] = (
                result.get("total", 0) + len(proposed_templates)
            )
        except Exception:
            # Don't break the existing tool if sandbox lookup fails.
            result.setdefault("sandbox_templates", [])

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
        proposal_reason: str = "",
        proposal_evidence: dict | str | None = None,
    ) -> str:
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if not name or len(name) > 64:
            return json.dumps({"error": "name is required and must be <= 64 characters"})
        if not content:
            return json.dumps({"error": "content is required"})

        repo = await _get_definition_repository()
        try:
            evidence = _parse_json_object(proposal_evidence)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        agent = await repo.create_agent(
            name=name,
            description=description,
            content=content,
            org_id=str(org_id),
            created_by=str(user_id),
            proposal_reason=proposal_reason or None,
            proposal_evidence=evidence,
            **_owner_args_for_context(user_id, user_role, memory_scope),
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
        proposal_reason: str = "",
        proposal_evidence: dict | str | None = None,
    ) -> str:
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if not name or len(name) > 64:
            return json.dumps({"error": "name is required and must be <= 64 characters"})
        if not content:
            return json.dumps({"error": "content is required"})

        repo = await _get_definition_repository()
        try:
            evidence = _parse_json_object(proposal_evidence)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        skill = await repo.create_skill(
            name=name,
            description=description,
            content=content,
            org_id=str(org_id),
            created_by=str(user_id),
            proposal_reason=proposal_reason or None,
            proposal_evidence=evidence,
            **_owner_args_for_context(user_id, user_role, memory_scope),
        )
        return json.dumps(skill, default=_serialize)

    @mcp.tool(
        description="""Create a new hook definition.

    Hooks start in 'proposed' status and must be approved by an admin before they
    can be granted to agents. Supported action_type values:
    - 'memory_lookup': look up accessible memories for matched tool calls
    - 'static_context': inject fixed context when the matcher applies
    - 'command': run an approved shell command/script out-of-process with timeout
      and output limits; the hook event is passed as JSON on stdin

Args:
    name: Hook name (max 64 chars)
    description: What the hook does
    trigger_event: Event that triggers the hook. Supported values:
        'before_model_call', 'after_model_call', 'before_tool_call',
        'after_tool_call', and legacy alias 'tool_call'
    action_type: 'memory_lookup', 'static_context', or 'command'
    config: JSON object or JSON string with matcher/action settings
    content: Optional text for static_context hooks, or shell script body for
        command hooks when config.command is omitted

Returns: JSON with the created hook including its ID and status."""
    )
    async def create_hook_definition(
        name: str,
        description: str = "",
        trigger_event: str = "before_tool_call",
        action_type: str = "static_context",
        config: dict | str | None = None,
        content: str = "",
        proposal_reason: str = "",
        proposal_evidence: dict | str | None = None,
    ) -> str:
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if not name or len(name) > 64:
            return json.dumps({"error": "name is required and must be <= 64 characters"})

        try:
            parsed_config = _parse_config(config)
            evidence = _parse_json_object(proposal_evidence)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        repo = await _get_definition_repository()
        try:
            hook = await repo.create_hook(
                name=name,
                description=description,
                trigger_event=trigger_event,
                action_type=action_type,
                content=content,
                config=parsed_config,
                org_id=str(org_id),
                created_by=str(user_id),
                proposal_reason=proposal_reason or None,
                proposal_evidence=evidence,
                **_owner_args_for_context(user_id, user_role, memory_scope),
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(hook, default=_serialize)

    @mcp.tool(
        description="""Create a new managed tool definition.

Managed tools are persistent, reviewable capabilities that run in Lucent
sandbox containers. They start in 'proposed' status and must be approved by an
admin before agents can invoke them. Default auth requires both user access to
the tool and an explicit agent grant when called from an agent context.

Args:
    name: Tool name (max 64 chars, e.g. 'lookup_customer')
    description: What this tool does
    source_code: Python source code. Must define the entrypoint function.
    input_schema: JSON Schema object or JSON string for tool arguments
    output_schema: Optional JSON Schema object or JSON string for returned data
    entrypoint: Python function name to call; receives one dict argument
    requirements: JSON array/list of pip requirements
    env_vars: JSON object of environment variables or credential references
    network_policy: JSON object, default {"network_mode":"none","allowed_hosts":[]}
    resource_limits: JSON object for memory/cpu/disk limits
    timeout_seconds: Per-call execution timeout

Returns: JSON with the created proposed tool including its ID and status."""
    )
    async def create_tool_definition(
        name: str,
        description: str = "",
        source_code: str = "",
        input_schema: dict | str | None = None,
        output_schema: dict | str | None = None,
        entrypoint: str = "handler",
        requirements: list | str | None = None,
        runtime_config: dict | str | None = None,
        env_vars: dict | str | None = None,
        auth_policy: dict | str | None = None,
        network_policy: dict | str | None = None,
        resource_limits: dict | str | None = None,
        timeout_seconds: int = 300,
        proposal_reason: str = "",
        proposal_evidence: dict | str | None = None,
    ) -> str:
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if not name or len(name) > 64:
            return json.dumps({"error": "name is required and must be <= 64 characters"})
        if not source_code:
            return json.dumps({"error": "source_code is required"})

        try:
            parsed_input_schema = _parse_json_object(input_schema) or {
                "type": "object", "properties": {}
            }
            parsed_output_schema = _parse_json_object(output_schema) if output_schema else None
            parsed_requirements = _parse_json_array(requirements)
            parsed_runtime_config = _parse_json_object(runtime_config)
            parsed_env_vars = _parse_json_object(env_vars)
            parsed_auth_policy = _parse_json_object(auth_policy) or {
                "mode": "agent_grant", "require_user_access": True
            }
            parsed_network_policy = _parse_json_object(network_policy) or {
                "network_mode": "none", "allowed_hosts": []
            }
            parsed_resource_limits = _parse_json_object(resource_limits) or {
                "memory_limit": "512m", "cpu_limit": 1.0,
                "disk_limit": "1g", "timeout_seconds": timeout_seconds,
            }
            evidence = _parse_json_object(proposal_evidence)
        except (ValueError, json.JSONDecodeError) as exc:
            return json.dumps({"error": str(exc)})

        repo = await _get_definition_repository()
        try:
            tool = await repo.create_managed_tool(
                name=name,
                description=description,
                input_schema=parsed_input_schema,
                output_schema=parsed_output_schema,
                runtime_type="python",
                source_code=source_code,
                entrypoint=entrypoint,
                requirements=parsed_requirements,
                runtime_config=parsed_runtime_config,
                env_vars=parsed_env_vars,
                auth_policy=parsed_auth_policy,
                network_policy=parsed_network_policy,
                resource_limits=parsed_resource_limits,
                timeout_seconds=timeout_seconds,
                org_id=str(org_id),
                created_by=str(user_id),
                proposal_reason=proposal_reason or None,
                proposal_evidence=evidence,
                **_owner_args_for_context(user_id, user_role, memory_scope),
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(tool, default=_serialize)

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
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})
        if role not in ("admin", "owner"):
            return json.dumps(
                {"error": "Forbidden: admin or owner role required", "code": 403}
            )

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
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})

        if not await _can_modify_definition(
            str(user_id), str(org_id), "agent", agent_id,
        ):
            return json.dumps({"error": "Agent not found"})

        kwargs = {}
        if name is not None:
            kwargs["name"] = name
        if description is not None:
            kwargs["description"] = description
        if content is not None:
            kwargs["content"] = content

        repo = await _get_definition_repository()
        try:
            result = await repo.update_agent(
                agent_id, str(org_id), requester_role=user_role, **kwargs,
            )
        except BuiltInProtectionError as exc:
            return json.dumps({"error": str(exc), "code": 403})
        if not result:
            return json.dumps({"error": "Agent not found"})
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Update a skill definition's name, description, or content.

Use this when learning extraction or self-improvement finds a concrete flaw in
an existing instance skill. Built-in skills are protected: the tool returns a
403-style error and the caller should create a follow-up request for an owner to
edit the on-disk source file.

Args:
    skill_id: UUID of the skill definition
    name: New name (optional, max 64 chars)
    description: New description (optional)
    content: New content (optional, markdown prompt)

Returns: JSON with the updated skill, or an error if not found/protected."""
    )
    async def update_skill_definition(
        skill_id: str,
        name: str | None = None,
        description: str | None = None,
        content: str | None = None,
    ) -> str:
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})

        if not await _can_modify_definition(
            str(user_id), str(org_id), "skill", skill_id,
        ):
            return json.dumps({"error": "Skill not found"})

        kwargs = {}
        if name is not None:
            kwargs["name"] = name
        if description is not None:
            kwargs["description"] = description
        if content is not None:
            kwargs["content"] = content

        repo = await _get_definition_repository()
        try:
            result = await repo.update_skill(
                skill_id, str(org_id), requester_role=user_role, **kwargs,
            )
        except BuiltInProtectionError as exc:
            return json.dumps({"error": str(exc), "code": 403})
        if not result:
            return json.dumps({"error": "Skill not found"})
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Delete an agent definition.

Args:
    agent_id: UUID of the agent definition

Returns: JSON with status 'deleted', or an error if not found."""
    )
    async def delete_agent_definition(agent_id: str) -> str:
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})
        if user_role not in ("admin", "owner"):
            return json.dumps(
                {"error": "Forbidden: admin or owner role required", "code": 403}
            )

        if not await _can_modify_definition(
            str(user_id), str(org_id), "agent", agent_id,
        ):
            return json.dumps({"error": "Agent not found"})

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
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})
        if role not in ("admin", "owner"):
            return json.dumps(
                {"error": "Forbidden: admin or owner role required", "code": 403}
            )

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
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})
        if role not in ("admin", "owner"):
            return json.dumps(
                {"error": "Forbidden: admin or owner role required", "code": 403}
            )

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
        description="""Grant a hook to an agent definition.

Both the agent and hook must exist in the organization, and the hook must be
active. Once granted, the hook participates in that agent's runtime composition
alongside skills and MCP tools.

Args:
    agent_id: UUID of the agent definition
    hook_id: UUID of the active hook definition to grant

Returns: JSON with status 'granted', or an error if either is not found."""
    )
    async def grant_hook_to_agent(agent_id: str, hook_id: str) -> str:
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})
        if role not in ("admin", "owner"):
            return json.dumps(
                {"error": "Forbidden: admin or owner role required", "code": 403}
            )

        repo = await _get_definition_repository()
        agent = await repo.get_agent(
            agent_id,
            str(org_id),
            requester_user_id=str(user_id),
            requester_role=role,
        )
        if not agent:
            return json.dumps({"error": "Agent not found"})

        hook = await repo.get_hook(
            hook_id,
            str(org_id),
            requester_user_id=str(user_id),
            requester_role=role,
        )
        if not hook or hook.get("status") != "active":
            return json.dumps({"error": "Active hook not found"})

        success = await repo.grant_hook(
            agent_id, hook_id, org_id=str(org_id), user_id=str(user_id)
        )
        if not success:
            return json.dumps({"error": "Failed to grant hook"})
        return json.dumps({"status": "granted", "agent_id": agent_id, "hook_id": hook_id})

    @mcp.tool(
        description="""Grant a managed tool to an agent definition.

Both the agent and managed tool must exist in the organization, and the tool
must be active. Once granted, the agent can invoke it through run_managed_tool;
the executor still enforces caller access and sandbox policy.

Args:
    agent_id: UUID of the agent definition
    tool_id: UUID of the active managed tool definition to grant

Returns: JSON with status 'granted', or an error if either is not found."""
    )
    async def grant_tool_to_agent(agent_id: str, tool_id: str) -> str:
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})
        if role not in ("admin", "owner"):
            return json.dumps(
                {"error": "Forbidden: admin or owner role required", "code": 403}
            )

        repo = await _get_definition_repository()
        agent = await repo.get_agent(
            agent_id,
            str(org_id),
            requester_user_id=str(user_id),
            requester_role=role,
        )
        if not agent:
            return json.dumps({"error": "Agent not found"})

        tool = await repo.get_managed_tool(
            tool_id,
            str(org_id),
            requester_user_id=str(user_id),
            requester_role=role,
        )
        if not tool or tool.get("status") != "active":
            return json.dumps({"error": "Active managed tool not found"})

        success = await repo.grant_managed_tool(
            agent_id, tool_id, org_id=str(org_id), user_id=str(user_id)
        )
        if not success:
            return json.dumps({"error": "Failed to grant managed tool"})
        return json.dumps({"status": "granted", "agent_id": agent_id, "tool_id": tool_id})

    @mcp.tool(
        description="""Revoke an MCP server from an agent definition.

Args:
    agent_id: UUID of the agent definition
    server_id: UUID of the MCP server to revoke

Returns: JSON with status 'revoked', or an error if not found."""
    )
    async def revoke_mcp_server_from_agent(agent_id: str, server_id: str) -> str:
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})
        if role not in ("admin", "owner"):
            return json.dumps(
                {"error": "Forbidden: admin or owner role required", "code": 403}
            )

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

    @mcp.tool(
        description="""Revoke a hook from an agent definition.

Args:
    agent_id: UUID of the agent definition
    hook_id: UUID of the hook to revoke

Returns: JSON with status 'revoked', or an error if not found."""
    )
    async def revoke_hook_from_agent(agent_id: str, hook_id: str) -> str:
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})
        if role not in ("admin", "owner"):
            return json.dumps(
                {"error": "Forbidden: admin or owner role required", "code": 403}
            )

        repo = await _get_definition_repository()
        agent = await repo.get_agent(
            agent_id,
            str(org_id),
            requester_user_id=str(user_id),
            requester_role=role,
        )
        if not agent:
            return json.dumps({"error": "Agent not found"})

        await repo.revoke_hook(agent_id, hook_id, org_id=str(org_id), user_id=str(user_id))
        return json.dumps({"status": "revoked", "agent_id": agent_id, "hook_id": hook_id})

    @mcp.tool(
        description="""Revoke a managed tool from an agent definition.

Args:
    agent_id: UUID of the agent definition
    tool_id: UUID of the managed tool to revoke

Returns: JSON with status 'revoked', or an error if not found."""
    )
    async def revoke_tool_from_agent(agent_id: str, tool_id: str) -> str:
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})
        if role not in ("admin", "owner"):
            return json.dumps(
                {"error": "Forbidden: admin or owner role required", "code": 403}
            )

        repo = await _get_definition_repository()
        agent = await repo.get_agent(
            agent_id,
            str(org_id),
            requester_user_id=str(user_id),
            requester_role=role,
        )
        if not agent:
            return json.dumps({"error": "Agent not found"})

        await repo.revoke_managed_tool(
            agent_id, tool_id, org_id=str(org_id), user_id=str(user_id)
        )
        return json.dumps({"status": "revoked", "agent_id": agent_id, "tool_id": tool_id})

    # ── Skill write tools ─────────────────────────────────────────────────

    @mcp.tool(
        description="""Delete a skill definition.

Args:
    skill_id: UUID of the skill definition

Returns: JSON with status 'deleted', or an error if not found."""
    )
    async def delete_skill_definition(skill_id: str) -> str:
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})
        if user_role not in ("admin", "owner"):
            return json.dumps(
                {"error": "Forbidden: admin or owner role required", "code": 403}
            )

        if not await _can_modify_definition(
            str(user_id), str(org_id), "skill", skill_id,
        ):
            return json.dumps({"error": "Skill not found"})

        repo = await _get_definition_repository()
        success = await repo.delete_skill(skill_id, str(org_id))
        if not success:
            return json.dumps({"error": "Skill not found"})
        return json.dumps({"status": "deleted", "skill_id": skill_id})

    @mcp.tool(
        description="""Delete a hook definition.

Args:
    hook_id: UUID of the hook definition

Returns: JSON with status 'deleted', or an error if not found."""
    )
    async def delete_hook_definition(hook_id: str) -> str:
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})
        if user_role not in ("admin", "owner"):
            return json.dumps(
                {"error": "Forbidden: admin or owner role required", "code": 403}
            )

        if not await _can_modify_definition(
            str(user_id), str(org_id), "hook", hook_id,
        ):
            return json.dumps({"error": "Hook not found"})

        repo = await _get_definition_repository()
        success = await repo.delete_hook(hook_id, str(org_id))
        if not success:
            return json.dumps({"error": "Hook not found"})
        return json.dumps({"status": "deleted", "hook_id": hook_id})

    @mcp.tool(
        description="""Delete a managed tool definition.

Args:
    tool_id: UUID of the managed tool definition

Returns: JSON with status 'deleted', or an error if not found."""
    )
    async def delete_tool_definition(tool_id: str) -> str:
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})
        if user_role not in ("admin", "owner"):
            return json.dumps(
                {"error": "Forbidden: admin or owner role required", "code": 403}
            )

        if not await _can_modify_definition(
            str(user_id), str(org_id), "managed_tool", tool_id,
        ):
            return json.dumps({"error": "Managed tool not found"})

        repo = await _get_definition_repository()
        success = await repo.delete_managed_tool(tool_id, str(org_id))
        if not success:
            return json.dumps({"error": "Managed tool not found"})
        return json.dumps({"status": "deleted", "tool_id": tool_id})

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
        user_id, org_id, role, _, _ = await _get_current_user_context()
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
        description="""List hook definitions in the organization.

Hooks are declarative runtime middleware that can observe agent events such
as tool calls and inject additional context. Filter by status to see proposed,
active, or rejected hooks.

Args:
    status: Optional filter — 'proposed', 'active', or 'rejected'
    limit: Max results to return (default 25, max 100)
    offset: Pagination offset (default 0)

Returns: JSON with items array, total_count, and pagination info."""
    )
    async def list_hook_definitions(
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        user_id, org_id, role, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_definition_repository()
        result = await repo.list_hooks(
            str(org_id),
            status=status,
            limit=min(limit, 100),
            offset=offset,
            requester_user_id=str(user_id) if user_id else None,
            requester_role=role,
        )
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Get full details of a hook definition by ID.

Args:
    hook_id: UUID of the hook definition

Returns: JSON with the hook details, or an error if not found."""
    )
    async def get_hook_definition(hook_id: str) -> str:
        user_id, org_id, role, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_definition_repository()
        hook = await repo.get_hook(
            hook_id,
            str(org_id),
            requester_user_id=str(user_id) if user_id else None,
            requester_role=role,
        )
        if not hook:
            return json.dumps({"error": "Hook not found"})
        return json.dumps(hook, default=_serialize)

    @mcp.tool(
        annotations=READ_ONLY,
        description="""List managed tool definitions in the organization.

Managed tools are persistent, approved capabilities executed through Lucent's
sandbox wrapper. Filter by status to see proposed, active, or rejected tools.

Args:
    status: Optional filter — 'proposed', 'active', or 'rejected'
    limit: Max results to return (default 25, max 100)
    offset: Pagination offset (default 0)

Returns: JSON with items array, total_count, and pagination info."""
    )
    async def list_tool_definitions(
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        user_id, org_id, role, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_definition_repository()
        result = await repo.list_managed_tools(
            str(org_id),
            status=status,
            limit=min(limit, 100),
            offset=offset,
            requester_user_id=str(user_id) if user_id else None,
            requester_role=role,
        )
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        annotations=READ_ONLY,
        description="""Get full details of a managed tool definition by ID or name.

Args:
    tool: UUID or name of the managed tool definition

Returns: JSON with the managed tool details, or an error if not found."""
    )
    async def get_tool_definition(tool: str) -> str:
        user_id, org_id, role, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_definition_repository()
        found = None
        try:
            UUID(tool)
            found = await repo.get_managed_tool(
                tool,
                str(org_id),
                requester_user_id=str(user_id) if user_id else None,
                requester_role=role,
            )
        except (TypeError, ValueError):
            found = None
        if not found:
            found = await repo.get_managed_tool_by_name(
                tool,
                str(org_id),
                requester_user_id=str(user_id) if user_id else None,
                requester_role=role,
            )
        if not found:
            return json.dumps({"error": "Managed tool not found"})
        return json.dumps(found, default=_serialize)

    @mcp.tool(
        description="""Run an approved managed tool in a sandbox container.

The server enforces the default auth wrapper: the caller must be an
authenticated user with access to the managed tool. If the call comes from an
agent session, the tool must also be explicitly granted to that agent via the
trusted agent-definition header set by Lucent, not a model-supplied argument.

Args:
    tool: UUID or name of the active managed tool definition
    arguments: JSON object or JSON string matching the tool's input_schema

Returns: JSON with ok/result/stdout/stderr/run_id metadata, or an error."""
    )
    async def run_managed_tool(tool: str, arguments: dict | str | None = None) -> str:
        user_id, org_id, role, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        try:
            parsed_args = _parse_json_object(arguments)
        except (ValueError, json.JSONDecodeError) as exc:
            return json.dumps({"error": f"arguments must be a JSON object: {exc}"})

        pool = await get_pool()
        from lucent.access_control import AccessControlService
        from lucent.services.managed_tools import ManagedToolBlockedError, ManagedToolExecutor

        repo = DefinitionRepository(pool)
        found = None
        try:
            UUID(tool)
            found = await repo.get_managed_tool(
                tool,
                str(org_id),
                requester_user_id=str(user_id),
                requester_role=role,
            )
        except (TypeError, ValueError):
            found = None
        if not found:
            found = await repo.get_managed_tool_by_name(
                tool,
                str(org_id),
                requester_user_id=str(user_id),
                requester_role=role,
            )
        if not found:
            return json.dumps({"error": "Managed tool not found", "code": 404})
        if found.get("status") != "active":
            return json.dumps({"error": "Managed tool is not active", "code": 409})

        acl = AccessControlService(pool)
        if not await acl.can_access(str(user_id), "managed_tool", str(found["id"]), str(org_id)):
            return json.dumps({"error": "Managed tool not found", "code": 404})

        agent_id = get_llm_context().get("agent_definition_id")
        executor = ManagedToolExecutor(repo)
        try:
            result = await executor.execute(
                tool=found,
                arguments=parsed_args,
                org_id=str(org_id),
                user_id=str(user_id),
                user_role=role,
                agent_id=agent_id,
                enforce_agent_grant=bool(agent_id),
            )
        except ManagedToolBlockedError as exc:
            return json.dumps({"error": str(exc), "code": 403})
        except Exception as exc:
            logger.warning("Managed tool %s failed", tool, exc_info=True)
            return json.dumps({"error": str(exc), "code": 500})
        return json.dumps(result.to_dict(), default=_serialize)

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
        proposal_reason: str = "",
        proposal_evidence: dict | str | None = None,
    ) -> str:
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if not name or len(name) > 64:
            return json.dumps({"error": "name is required and must be <= 64 characters"})

        if server_type == "stdio" and user_role not in ("admin", "owner"):
            return json.dumps({"error": "Stdio MCP servers require admin or owner role"})
        if server_type == "http" and url:
            try:
                validate_url(url, purpose="MCP server")
            except SSRFError as exc:
                return json.dumps({"error": str(exc)})

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
        try:
            evidence = _parse_json_object(proposal_evidence)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

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
            proposal_reason=proposal_reason or None,
            proposal_evidence=evidence,
            **_owner_args_for_context(user_id, user_role, memory_scope),
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
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if scoped_error := _requires_unscoped_human(memory_scope):
            return json.dumps({"error": scoped_error, "code": 403})

        repo = await _get_definition_repository()
        existing = await repo.get_mcp_server(
            server_id,
            str(org_id),
            requester_user_id=str(user_id),
            requester_role=user_role,
        )
        if not existing:
            return json.dumps({"error": "MCP server not found"})
        if not await _can_modify_definition(
            str(user_id), str(org_id), "mcp_server", server_id,
        ):
            return json.dumps({"error": "MCP server not found"})

        effective_type = server_type or existing.get("server_type")
        if (
            (effective_type == "stdio" or command is not None)
            and user_role not in ("admin", "owner")
        ):
            return json.dumps({"error": "Stdio MCP servers require admin or owner role"})
        if url is not None and effective_type == "http":
            try:
                validate_url(url, purpose="MCP server")
            except SSRFError as exc:
                return json.dumps({"error": str(exc)})

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

        try:
            result = await repo.update_mcp_server(
                server_id, str(org_id), requester_role=user_role, **kwargs,
            )
        except BuiltInProtectionError as exc:
            return json.dumps({"error": str(exc), "code": 403})
        if not result:
            return json.dumps({"error": "MCP server not found"})
        return json.dumps(result, default=_serialize)
