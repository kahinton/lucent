"""MCP tools for agent, skill, and MCP server definition management."""

import json
import logging
from uuid import UUID

from mcp.server import MCPServer as FastMCP

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


async def _get_authorized_definition_repository(
    user_id, org_id,
) -> DefinitionRepository:
    """Definition reads are clearance-driven for user-scoped contexts.

    Default deny: the pool carries the definitions policies, so listing and
    by-ID usage lookups resolve only what this principal is explicitly
    granted (own, group-owned, built-in org grant, or an explicit grant).
    """
    from lucent.db.definitions import get_authorized_definitions_pool

    pool = await get_pool()
    authorized = await get_authorized_definitions_pool(
        pool, {"id": str(user_id), "organization_id": str(org_id)}
    )
    return DefinitionRepository(authorized)


def _is_unscoped_daemon_role(role: str | None, memory_scope: str | None) -> bool:
    """True for unscoped daemon contexts — system actors reading org-wide.

    Mirrors the creation rule in _owner_args_for_context: the daemon is an
    actor, not a capability owner, and runtime composition trusts the agent
    for this first pass; such contexts keep the legacy org-shared reads.
    """
    return role == "daemon" and memory_scope != "user"


_USAGE_LISTERS = {
    "agents": ("list_agents", "list_agents_accessible_by"),
    "skills": ("list_skills", "list_skills_accessible_by"),
    "mcp_servers": ("list_mcp_servers", "list_mcp_servers_accessible_by"),
    "hooks": ("list_hooks", "list_hooks_accessible_by"),
    "managed_tools": ("list_managed_tools", "list_managed_tools_accessible_by"),
}


async def _usage_list_definitions(
    kind: str,
    *,
    org_id: str,
    user_id: str | None,
    role: str | None,
    memory_scope: str | None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict:
    """List definitions for a session context (clearance-driven).

    User contexts get default-deny clearance reads; unscoped daemon contexts
    keep the legacy org-shared listing.
    """
    legacy_method, cleared_method = _USAGE_LISTERS[kind]
    kwargs: dict = {
        "status": status,
        "limit": limit,
        "offset": offset,
    }
    if role == "daemon" and memory_scope != "user":
        repo = await _get_definition_repository()
        result = await getattr(repo, legacy_method)(
            org_id,
            requester_user_id=user_id if user_id else None,
            requester_role=role,
            **kwargs,
        )
    else:
        repo = await _get_authorized_definition_repository(user_id, org_id)
        cleared = getattr(repo, cleared_method)
        if search is not None:
            kwargs["search"] = search
        result = await cleared(str(user_id), org_id, **kwargs)
    return result


_USAGE_GETTERS = {
    "agents": "get_usable_agent",
    "skills": "get_usable_skill",
    "mcp_servers": "get_usable_mcp_server",
    "hooks": "get_usable_hook",
    "managed_tools": "get_usable_managed_tool",
}


async def _usage_get_definition_by_id(
    kind: str,
    resource_id: str,
    *,
    org_id: str,
    user_id: str | None,
    role: str | None,
    memory_scope: str | None,
) -> dict | None:
    """Resolve one definition by ID for *use* in a session (clearance-driven).

    Unscoped daemon contexts keep the legacy requester-scoped getter — the
    daemon is an actor and runtime composition trusts the agent for this
    first pass.
    """
    if _is_unscoped_daemon_role(role, memory_scope):
        legacy_getters = {
            "agents": "get_agent",
            "skills": "get_skill",
            "mcp_servers": "get_mcp_server",
            "hooks": "get_hook",
            "managed_tools": "get_managed_tool",
        }
        repo = await _get_definition_repository()
        return await getattr(repo, legacy_getters[kind])(
            resource_id,
            org_id,
            requester_user_id=user_id if user_id else None,
            requester_role=role,
        )
    repo = await _get_authorized_definition_repository(user_id, org_id)
    return await getattr(repo, _USAGE_GETTERS[kind])(resource_id)


async def _capability_gate_error(
    *,
    kind: str,
    resource_id: str,
    org_id: str,
    user_id: str | None,
    role: str | None,
    memory_scope: str | None,
) -> str | None:
    """Return an error message when an agent context may not use this capability.

    The consent model has to hold on the *on-demand* path too. When a model runs
    inside an agent it can name a capability by id — ``get_skill_definition``,
    ``get_hook_definition``, ``run_managed_tool`` — even for a capability that
    was never rendered into its prompt because the user declined it or had not
    approved it yet. Without this gate, per-user consent would be decorative: a
    model holding a leaked or remembered id could read a body it was never
    granted.

    Returns ``None`` when there is no agent context (bare chat / unscoped
    daemon), where the clearance and managed-tool grant checks still apply and
    behavior is unchanged.
    """
    agent_id = (get_llm_context() or {}).get("agent_definition_id")
    if not agent_id or not user_id:
        return None
    capability_type = _CAPABILITY_KIND_MAP.get(kind)
    if not capability_type:
        return None

    from lucent.agent_capabilities import assert_capability_permitted

    pool = await get_pool()
    permitted = await assert_capability_permitted(
        pool=pool,
        user={"id": str(user_id), "organization_id": str(org_id)},
        agent_id=str(agent_id),
        org_id=str(org_id),
        capability_type=capability_type,
        capability_id=str(resource_id),
    )
    if permitted:
        return None
    return (
        f"This {capability_type.replace('_', ' ')} is not available in the "
        "current agent: it is not approved for this user. Approve it on the "
        "agent's page under Definitions, then retry."
    )


_CAPABILITY_KIND_MAP = {
    "skills": "skill",
    "managed_tools": "managed_tool",
    "hooks": "hook",
    "mcp_servers": "mcp_server",
}


async def _capability_write_denied(
    role: str | None, memory_scope: str | None,
) -> str | None:
    """Shared guard for attach/detach tools.

    A scoped agent context may never change an agent's composition — that is a
    human decision (`_requires_unscoped_human`). There is no role gate any more:
    attaching is authorized by read clearance on both endpoints inside
    ``DefinitionRepository``, and detaching by agent-or-capability ownership, so
    a member who can see both ends may attach to an agent they can see.
    """
    if scoped_error := _requires_unscoped_human(memory_scope):
        return json.dumps({"error": scoped_error, "code": 403})
    return None


def _attach_response(
    outcome, *, agent_id: str, id_field: str, capability_id: str,
) -> str:
    """Render a GrantOutcome for an MCP caller."""
    from lucent.db.definitions import GrantOutcome

    if outcome is GrantOutcome.ALREADY_ATTACHED:
        return json.dumps(
            {
                "status": "already_attached",
                "warning": (
                    "This capability is already attached to this agent by another "
                    "user. Approve it for yourself to use it; it cannot be "
                    "attached twice."
                ),
                "agent_id": agent_id,
                id_field: capability_id,
            }
        )
    if outcome is GrantOutcome.DENIED:
        return json.dumps(
            {
                "error": (
                    "Not permitted: you must be able to see both the agent and "
                    "the capability to attach it."
                ),
                "code": 403,
            }
        )
    if outcome is GrantOutcome.FAILED:
        return json.dumps({"error": "Failed to attach capability", "code": 500})
    return json.dumps(
        {"status": "granted", "agent_id": agent_id, id_field: capability_id}
    )


async def _usage_get_managed_tool_by_name(
    name: str,
    *,
    org_id: str,
    user_id: str | None,
    role: str | None,
    memory_scope: str | None,
) -> dict | None:
    """Resolve a managed tool by name for *use* in a session (clearance-driven)."""
    if _is_unscoped_daemon_role(role, memory_scope):
        repo = await _get_definition_repository()
        return await repo.get_managed_tool_by_name(
            name,
            org_id,
            requester_user_id=user_id if user_id else None,
            requester_role=role,
        )
    repo = await _get_authorized_definition_repository(user_id, org_id)
    return await repo.get_usable_managed_tool_by_name(name, org_id)


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


def _parse_proposal_evidence(value: dict | str | None) -> dict:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"summary": value}
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("proposal_evidence must be a JSON object or plain text")


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
    """Return repository ownership kwargs for the current MCP auth context.

    Scoped contexts (task agents working for a specific user) pin the acting
    user as owner. Unscoped daemon contexts pass no owner kwargs and rely on
    the repository default, which attaches the org's owner user — every
    instance definition should end up owned, not org-shared-and-unowned
    (Kyle, 2026-10-01).
    """
    if user_role != "daemon" or memory_scope == "user":
        return {"owner_user_id": str(user_id)}
    return {}


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
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        result = await _usage_list_definitions(
            "agents",
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            role=role,
            memory_scope=memory_scope,
            status=status,
            limit=min(limit, 100),
            offset=offset,
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
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        agent = await _usage_get_definition_by_id(
            "agents",
            agent_id,
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            role=role,
            memory_scope=memory_scope,
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
    search: Optional search term matched against skill name, description, or content
    limit: Max results to return (default 25, max 100)
    offset: Pagination offset (default 0)

Returns: JSON with items array, total_count, and pagination info."""
    )
    async def list_skill_definitions(
        status: str | None = None,
        search: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        result = await _usage_list_definitions(
            "skills",
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            role=role,
            memory_scope=memory_scope,
            status=status,
            search=search,
            limit=min(limit, 100),
            offset=offset,
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
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        gate_error = await _capability_gate_error(
            kind="skills",
            resource_id=skill_id,
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            role=role,
            memory_scope=memory_scope,
        )
        if gate_error:
            return json.dumps({"error": gate_error, "code": 403})

        skill = await _usage_get_definition_by_id(
            "skills",
            skill_id,
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            role=role,
            memory_scope=memory_scope,
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
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_definition_repository()
        if role == "daemon" and memory_scope != "user":
            result = await repo.get_pending_proposals(str(org_id))
        else:
            result = await repo.get_pending_proposals(
                str(org_id),
                requester_user_id=str(user_id) if user_id else None,
                requester_role="member" if memory_scope == "user" else role,
            )

        # Include proposed sandbox templates so the planner sees what's
        # already been proposed (and won't duplicate) and admins can review
        # them in one place. Pending proposals are management data: admins/
        # owners see every pending proposal, other principals only their own
        # (proposals are owned by their proposer).
        try:
            from lucent.db import get_pool
            from lucent.db.sandbox_template import SandboxTemplateRepository

            pool = await get_pool()
            tpl_repo = SandboxTemplateRepository(pool)
            if role in ("admin", "owner") or (role == "daemon" and memory_scope != "user"):
                proposed_templates = await tpl_repo.list_proposed(str(org_id))
            else:
                proposed_templates = [
                    t
                    for t in await tpl_repo.list_proposed(str(org_id))
                    if str(t.get("proposed_by") or "") == str(user_id)
                ]
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
            evidence = _parse_proposal_evidence(proposal_evidence)
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
            evidence = _parse_proposal_evidence(proposal_evidence)
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
            evidence = _parse_proposal_evidence(proposal_evidence)
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

Managed tools are persistent, reviewable capabilities granted to agents. They
start in 'proposed' status and must be approved before agents can invoke them.
Default auth requires both user access to the tool and an explicit agent grant
when called from an agent context.

runtime_type decides how the tool actually runs:

  python   Sandboxed Python you write. Use for computation, parsing, or custom
           logic. Requires source_code; needs sandboxes enabled.
  http     One HTTPS request described declaratively. No code, no container:
           Lucent renders runtime_config url/query/body/headers from the
           arguments and sends it. Use for a JSON API with a stable endpoint.
  openapi  One operation from an OpenAPI 3.x or Swagger 2.0 document. Put the
           document in runtime_config.spec and select it with
           runtime_config.operation_id (or method + path). The input_schema is
           derived from the operation, and the spec is stored at approval time so
           an approved tool cannot change behaviour underneath the model.
  builtin  A reviewed Lucent-native handler. runtime_config.handler names it:
           web_search, web_fetch, weather_forecast, current_time,
           date_calculate, calculate, json_extract, regex_extract,
           text_transform, text_stats, unit_convert, csv_parse, uuid_generate,
           random_choice, sort_and_dedupe — plus pdf/docx/xlsx/image handlers when
           the image was built with the optional document extras. No code, no
           container, and the input_schema comes from Lucent.

For http and openapi, credentials must never be literal: put them in env_vars as
a secret:// or credential:// reference and interpolate with {{env:NAME}}. The
tool's network_policy must allow the hosts it contacts: 'none' blocks all
outbound traffic, 'allowlist' permits only allowed_hosts, and 'bridge' permits
any public host on ports 80/443. Private, loopback, link-local, and cloud-metadata
addresses are always blocked.

Args:
    name: Tool name (max 64 chars, e.g. 'lookup_customer')
    description: What this tool does
    runtime_type: python | http | openapi | builtin (default python)
    source_code: Python source code (python only). Must define the entrypoint.
    input_schema: JSON Schema object or JSON string for tool arguments. Ignored
        for builtin (the catalog is authoritative); derivable for openapi.
    output_schema: Optional JSON Schema object or JSON string for returned data
    entrypoint: Python function name (python) or catalog handler name (builtin)
    requirements: JSON array/list of pip requirements (python only)
    runtime_config: JSON object of per-kind configuration (url/method/query/body
        for http; spec/operation_id for openapi; handler for builtin)
    env_vars: JSON object of environment variables or credential references
    network_policy: JSON object, default {"network_mode":"none","allowed_hosts":[]}
    resource_limits: JSON object for memory/cpu/disk limits
    timeout_seconds: Per-call execution timeout

Returns: JSON with the created proposed tool including its ID and status."""
    )
    async def create_tool_definition(
        name: str,
        description: str = "",
        runtime_type: str = "python",
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
        from lucent.services.tool_runtimes import runtime_requires_sandbox, runtime_types
        from lucent.settings import custom_tooling_enabled

        kind = str(runtime_type or "python").strip().lower()
        if kind not in runtime_types():
            return json.dumps(
                {"error": f"runtime_type must be one of: {', '.join(runtime_types())}"}
            )
        # Only sandbox-backed kinds need the sandbox feature. A deployment with
        # containers disabled can still create declarative and builtin tools.
        if runtime_requires_sandbox(kind) and not custom_tooling_enabled(
            organization_id=org_id
        ):
            return json.dumps(
                {
                    "error": (
                        "Custom tooling is disabled for this organization because "
                        "features.sandboxes_enabled is off. Create an 'http', "
                        "'openapi', or 'builtin' tool instead, which needs no sandbox."
                    ),
                    "code": 409,
                    "custom_tooling_enabled": False,
                }
            )
        if not name or len(name) > 64:
            return json.dumps({"error": "name is required and must be <= 64 characters"})
        if kind == "python" and not source_code:
            return json.dumps({"error": "source_code is required for a python tool"})

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
            evidence = _parse_proposal_evidence(proposal_evidence)
        except (ValueError, json.JSONDecodeError) as exc:
            return json.dumps({"error": str(exc)})

        if kind == "builtin" and not parsed_runtime_config.get("handler") and entrypoint:
            # The handler is a builtin's entrypoint; accept either field rather
            # than making the model remember which one a kind uses.
            parsed_runtime_config["handler"] = entrypoint

        repo = await _get_definition_repository()
        try:
            tool = await repo.create_managed_tool(
                name=name,
                description=description,
                input_schema=parsed_input_schema,
                output_schema=parsed_output_schema,
                runtime_type=kind,
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
        if denied := await _capability_write_denied(role, memory_scope):
            return denied

        repo = await _get_definition_repository()
        outcome = await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=skill_id,
            org_id=str(org_id),
            actor_user_id=str(user_id),
        )
        return _attach_response(
            outcome, agent_id=agent_id, id_field="skill_id", capability_id=skill_id,
        )

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
        if denied := await _capability_write_denied(role, memory_scope):
            return denied

        repo = await _get_definition_repository()
        revoked = await repo.revoke_skill(
            agent_id, skill_id,
            org_id=str(org_id),
            actor_user_id=str(user_id),
            requester_role=role,
        )
        if not revoked:
            return json.dumps(
                {
                    "error": (
                        "Not permitted: only the agent owner, the skill owner, "
                        "or an organization admin can detach it."
                    ),
                    "code": 403,
                }
            )
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
        if denied := await _capability_write_denied(role, memory_scope):
            return denied

        repo = await _get_definition_repository()
        outcome = await repo.grant_capability(
            agent_id=agent_id,
            capability_type="mcp_server",
            capability_id=definition_id,
            org_id=str(org_id),
            actor_user_id=str(user_id),
        )
        return _attach_response(
            outcome,
            agent_id=agent_id,
            id_field="server_id",
            capability_id=definition_id,
        )

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
        if denied := await _capability_write_denied(role, memory_scope):
            return denied

        repo = await _get_definition_repository()
        outcome = await repo.grant_capability(
            agent_id=agent_id,
            capability_type="hook",
            capability_id=hook_id,
            org_id=str(org_id),
            actor_user_id=str(user_id),
        )
        return _attach_response(
            outcome, agent_id=agent_id, id_field="hook_id", capability_id=hook_id,
        )

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
        if denied := await _capability_write_denied(role, memory_scope):
            return denied

        repo = await _get_definition_repository()
        from lucent.services.entitlements import CapabilityDisabledError

        try:
            outcome = await repo.grant_capability(
                agent_id=agent_id,
                capability_type="managed_tool",
                capability_id=tool_id,
                org_id=str(org_id),
                actor_user_id=str(user_id),
            )
        except CapabilityDisabledError as exc:
            # The organization's entitlement does not cover managed tools;
            # refuse with the capability named, not a generic 403.
            return json.dumps({"error": str(exc), "code": 403})
        return _attach_response(
            outcome, agent_id=agent_id, id_field="tool_id", capability_id=tool_id,
        )

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
        if denied := await _capability_write_denied(role, memory_scope):
            return denied

        repo = await _get_definition_repository()
        revoked = await repo.revoke_mcp_server(
            agent_id, server_id,
            org_id=str(org_id),
            actor_user_id=str(user_id),
            requester_role=role,
        )
        if not revoked:
            return json.dumps(
                {
                    "error": (
                        "Not permitted: only the agent owner, the provider owner, "
                        "or an organization admin can detach it."
                    ),
                    "code": 403,
                }
            )
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
        if denied := await _capability_write_denied(role, memory_scope):
            return denied

        repo = await _get_definition_repository()
        revoked = await repo.revoke_hook(
            agent_id, hook_id,
            org_id=str(org_id),
            actor_user_id=str(user_id),
            requester_role=role,
        )
        if not revoked:
            return json.dumps(
                {
                    "error": (
                        "Not permitted: only the agent owner, the hook owner, "
                        "or an organization admin can detach it."
                    ),
                    "code": 403,
                }
            )
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
        if denied := await _capability_write_denied(role, memory_scope):
            return denied

        repo = await _get_definition_repository()
        revoked = await repo.revoke_managed_tool(
            agent_id, tool_id,
            org_id=str(org_id),
            actor_user_id=str(user_id),
            requester_role=role,
        )
        if not revoked:
            return json.dumps(
                {
                    "error": (
                        "Not permitted: only the agent owner, the tool owner, "
                        "or an organization admin can detach it."
                    ),
                    "code": 403,
                }
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
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        result = await _usage_list_definitions(
            "mcp_servers",
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            role=role,
            memory_scope=memory_scope,
            status=status,
            limit=min(limit, 100),
            offset=offset,
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
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        result = await _usage_list_definitions(
            "hooks",
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            role=role,
            memory_scope=memory_scope,
            status=status,
            limit=min(limit, 100),
            offset=offset,
        )
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Get full details of a hook definition by ID.

Args:
    hook_id: UUID of the hook definition

Returns: JSON with the hook details, or an error if not found."""
    )
    async def get_hook_definition(hook_id: str) -> str:
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        gate_error = await _capability_gate_error(
            kind="hooks",
            resource_id=hook_id,
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            role=role,
            memory_scope=memory_scope,
        )
        if gate_error:
            return json.dumps({"error": gate_error, "code": 403})

        hook = await _usage_get_definition_by_id(
            "hooks",
            hook_id,
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            role=role,
            memory_scope=memory_scope,
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
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        result = await _usage_list_definitions(
            "managed_tools",
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            role=role,
            memory_scope=memory_scope,
            status=status,
            limit=min(limit, 100),
            offset=offset,
        )
        from lucent.settings import custom_tooling_enabled

        result["custom_tooling_enabled"] = custom_tooling_enabled(organization_id=org_id)
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        annotations=READ_ONLY,
        description="""Get full details of a managed tool definition by ID or name.

Args:
    tool: UUID or name of the managed tool definition

Returns: JSON with the managed tool details, or an error if not found."""
    )
    async def get_tool_definition(tool: str) -> str:
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        context = dict(
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            role=role,
            memory_scope=memory_scope,
        )
        found = None
        try:
            UUID(tool)
            found = await _usage_get_definition_by_id("managed_tools", tool, **context)
        except (TypeError, ValueError):
            found = None
        if not found:
            found = await _usage_get_managed_tool_by_name(tool, **context)
        if not found:
            return json.dumps({"error": "Managed tool not found"})
        return json.dumps(found, default=_serialize)

    @mcp.tool(
        annotations=READ_ONLY,
        description="""List the managed-tool runtime kinds and built-in handlers.

Call this before creating a custom tool: it tells you which executor kinds this
build supports, whether sandboxes (and therefore the 'python' kind) are enabled
for your organization, and exactly which built-in handlers are available with
their argument schemas. A handler listed with available=false needs an optional
package that this image was not built with.

Returns: JSON with runtime_kinds (runtime_type, label, summary, requires_sandbox,
accepts_source, available), builtin_handlers (handler, label, description,
input_schema, available, unavailable_reason), and sandboxes_enabled."""
    )
    async def list_tool_runtime_kinds() -> str:
        _, org_id, _, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        from lucent.services.builtin_tool_catalog import available_builtin_tools
        from lucent.services.tool_runtimes import runtime_catalog
        from lucent.settings import custom_tooling_enabled

        sandboxes = custom_tooling_enabled(organization_id=org_id)
        kinds = runtime_catalog()
        for kind in kinds:
            kind["available"] = sandboxes or not kind["requires_sandbox"]
        return json.dumps(
            {
                "runtime_kinds": kinds,
                "builtin_handlers": available_builtin_tools(),
                "sandboxes_enabled": sandboxes,
            },
            default=_serialize,
        )

    @mcp.tool(
        description="""Run an approved managed tool.

The server enforces the default auth wrapper: the caller must be an
authenticated user with access to the managed tool. If the call comes from an
agent session, the tool must also be explicitly granted to that agent via the
trusted agent-definition header set by Lucent, not a model-supplied argument.

How the tool runs depends on its runtime_type, and is not something the caller
chooses or can influence: python tools execute in a sandbox container, http and
openapi tools send one pre-approved request, and builtin tools run a reviewed
Lucent-native handler in-process. Input and output are validated against the
tool's declared schemas either way.

Args:
    tool: UUID or name of the active managed tool definition
    arguments: JSON object or JSON string matching the tool's input_schema

Returns: JSON with ok/result/stdout/stderr/run_id metadata, or an error."""
    )
    async def run_managed_tool(tool: str, arguments: dict | str | None = None) -> str:
        user_id, org_id, role, memory_scope, _ = await _get_current_user_context()
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
        from lucent.services.managed_tools import (
            ManagedToolArgumentError,
            ManagedToolBlockedError,
            ManagedToolDefinitionError,
            ManagedToolExecutor,
        )

        context = dict(
            org_id=str(org_id),
            user_id=str(user_id),
            role=role,
            memory_scope=memory_scope,
        )
        found = None
        try:
            UUID(tool)
            found = await _usage_get_definition_by_id("managed_tools", tool, **context)
        except (TypeError, ValueError):
            found = None
        if not found:
            found = await _usage_get_managed_tool_by_name(tool, **context)
        if not found:
            return json.dumps({"error": "Managed tool not found", "code": 404})
        if found.get("status") != "active":
            return json.dumps({"error": "Managed tool is not active", "code": 409})

        # Consent gate: an agent context may only run a managed tool that this
        # user approved for that agent. The grant check further below only
        # proves the tool is *attached*; attachment is not consent.
        gate_error = await _capability_gate_error(
            kind="managed_tools",
            resource_id=str(found["id"]),
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            role=role,
            memory_scope=memory_scope,
        )
        if gate_error:
            return json.dumps({"error": gate_error, "code": 403})

        if _is_unscoped_daemon_role(role, memory_scope):
            # Legacy resolution above is org-shared for system actors — keep
            # the explicit can_access gate that user contexts no longer need
            # (their clearance lookup is the gate).
            acl = AccessControlService(pool)
            if not await acl.can_access(
                str(user_id), "managed_tool", str(found["id"]), str(org_id)
            ):
                return json.dumps({"error": "Managed tool not found", "code": 404})

        agent_id = get_llm_context().get("agent_definition_id")
        repo = DefinitionRepository(pool)
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
        except ManagedToolArgumentError as exc:
            return json.dumps({"error": str(exc), "code": 400})
        except ManagedToolDefinitionError as exc:
            logger.error("Managed tool %s has an unusable definition", tool, exc_info=True)
            return json.dumps(
                {
                    "error": str(exc),
                    "code": 500,
                    "error_type": "ManagedToolDefinitionError",
                }
            )
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
            evidence = _parse_proposal_evidence(proposal_evidence)
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

    # ── Capability consent ────────────────────────────────────────────────
    #
    # Consent is what stops one person's hook, tool, or skill from being
    # injected into another person's agent run without their knowledge. A
    # capability attached to an agent only reaches a user who can read it AND
    # has approved it (or is exempt: built-in, the attacher, or its owner).

    @mcp.tool(
        annotations=READ_ONLY,
        description="""List capabilities attached to agents that are waiting for
your approval, or that you declined.

A capability reaches you through an agent only when you approve it. Built-in
capabilities, ones you attached yourself, and ones you own never need approval.

Returns: JSON with a `pending` array (agent, capability, type) and a `declined`
array. Approve or decline with decide_capability_consent."""
    )
    async def list_pending_capability_consents() -> str:
        user_id, org_id, _, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        repo = await _get_definition_repository()
        pending = await repo.list_pending_capability_consents(
            str(org_id), str(user_id)
        )
        declined = await repo.list_declined_capability_consents(
            str(org_id), str(user_id)
        )
        return json.dumps(
            {
                "pending": pending,
                "declined": declined,
                "total": len(pending) + len(declined),
            },
            default=_serialize,
        )

    @mcp.tool(
        description="""Approve or decline a capability attached to an agent.

Approving makes the capability available to *you* in that agent. Declining
keeps it unavailable to you; the decision is sticky and never re-prompts, but
you can approve it later. Neither affects any other user.

Args:
    agent_id: UUID of the agent definition
    capability_type: 'skill', 'managed_tool', 'hook', or 'mcp_server'
    capability_id: UUID of the capability
    decision: 'approved' or 'declined'

Returns: JSON with the recorded decision, or an error."""
    )
    async def decide_capability_consent(
        agent_id: str,
        capability_type: str,
        capability_id: str,
        decision: str,
    ) -> str:
        user_id, org_id, _, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        if decision not in ("approved", "declined"):
            return json.dumps(
                {"error": "decision must be 'approved' or 'declined'"}
            )

        from lucent.db.definitions import (
            DefinitionRepository as _Repo,
            get_authorized_definitions_pool,
            normalize_capability_type,
        )

        try:
            canonical_type = normalize_capability_type(capability_type)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        pool = await get_pool()
        authorized = await get_authorized_definitions_pool(
            pool, {"id": str(user_id), "organization_id": str(org_id)}
        )
        # The agent must be usable by this user before they can have an opinion
        # about its composition.
        agent = await _Repo(authorized).get_usable_agent(agent_id)
        if not agent:
            return json.dumps({"error": "Agent not found", "code": 404})

        repo = _Repo(pool)
        recorded = await repo.upsert_capability_consent(
            org_id=str(org_id),
            agent_id=agent_id,
            user_id=str(user_id),
            capability_type=canonical_type,
            capability_id=capability_id,
            state=decision,
        )
        if not recorded:
            return json.dumps(
                {
                    "error": (
                        "That capability is not attached to this agent, so there "
                        "is nothing to decide."
                    ),
                    "code": 404,
                }
            )
        return json.dumps(
            {
                "status": decision,
                "agent_id": agent_id,
                "capability_type": canonical_type,
                "capability_id": capability_id,
            }
        )
