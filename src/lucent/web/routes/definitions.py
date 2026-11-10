"""Definition management routes — agents, skills, tools, hooks, providers."""

import json
from math import ceil
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from lucent.access_control import AccessControlService
from lucent.auth_providers import CSRF_COOKIE_NAME
from lucent.db import get_pool
from lucent.db.groups import GroupRepository
from lucent.db.user import UserRepository
from lucent.logging import get_logger
from lucent.rbac import Role, is_admin_role
from lucent.settings import (
    hook_admin_approval_required,
    managed_tool_admin_approval_required,
)

from ._shared import _check_csrf, _parse_env_vars, get_user_context, templates

logger = get_logger("web.routes.definitions")

router = APIRouter()

ALLOWED_PER_PAGE = {10, 25, 50, 100}
VALID_AGENT_KINDS = {"functional", "persona", "hybrid"}


async def _get_user_groups(pool, user_id: str, org_id: str) -> list[dict]:
    from lucent.db.groups import GroupRepository

    repo = GroupRepository(pool)
    return await repo.get_user_groups(user_id, org_id)


async def _resolve_owner_maps(user_id: str, org_id: str, pool, items: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    user_ids = {
        str(item.get(user_key))
        for item in items
        for user_key in ("owner_user_id", "owner_approved_by")
        if item.get(user_key)
    }
    group_ids = {str(item.get("owner_group_id")) for item in items if item.get("owner_group_id")}
    user_map: dict[str, str] = {}
    group_map: dict[str, str] = {}
    if not user_ids and not group_ids:
        return user_map, group_map

    if user_ids:
        user_map = await UserRepository(pool).get_display_names_by_ids(
            [UUID(uid) for uid in user_ids],
            organization_id=org_id,
        )
    if group_ids:
        group_map = await GroupRepository(pool).get_names_by_ids(
            [UUID(gid) for gid in group_ids],
            organization_id=org_id,
        )
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
        owner_approved_by = str(d.get("owner_approved_by")) if d.get("owner_approved_by") else None
        d["owner_approved_by_name"] = user_map.get(owner_approved_by) if owner_approved_by else None
        enriched.append(d)
    return enriched


def _normalize_agent_kind(value) -> str:
    kind = str(value or "functional").strip().lower()
    return kind if kind in VALID_AGENT_KINDS else "functional"


def _agent_kind_evidence(kind: str, source: str) -> dict[str, str]:
    return {
        "agent_kind": _normalize_agent_kind(kind),
        "classification_source": source,
    }


async def _find_agent_composer_agent(repo, org_id: str, user, role_value: str) -> dict | None:
    """Return the active definition-engineer agent if this user can access it."""
    result = await repo.list_agents(
        org_id,
        status="active",
        limit=1000,
        requester_user_id=str(user.id),
        requester_role=role_value,
    )
    for agent in result.get("items", []):
        if agent and agent.get("name") == "definition-engineer":
            return agent
    return None


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
    if not is_admin_role(role_value):
        raise HTTPException(status_code=403, detail="Permission denied")


def _is_admin_or_owner(user) -> bool:
    role_value = user.role if isinstance(user.role, str) else user.role.value
    return is_admin_role(role_value)


async def _require_definition_approval_access(
    pool,
    user,
    definition_type: str,
    definition_id: str,
) -> dict:
    """Authorize an owner approval or an administrator's final safety approval."""
    acl = AccessControlService(pool)
    org_id = str(user.organization_id)
    if not await acl.can_modify(str(user.id), definition_type, definition_id, org_id):
        raise HTTPException(status_code=404, detail="Definition not found")

    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    getters = {
        "agent": repo.get_agent,
        "skill": repo.get_skill,
        "hook": repo.get_hook,
        "managed_tool": repo.get_managed_tool,
    }
    definition = await getters[definition_type](definition_id, org_id)
    if not definition:
        raise HTTPException(status_code=404, detail="Definition not found")
    if (
        definition_type in {"hook", "managed_tool"}
        and definition.get("status") == "owner_approved"
        and not _is_admin_or_owner(user)
    ):
        raise HTTPException(status_code=403, detail="Administrative approval required")
    return definition


async def _approval_button_state(pool, user, definition_type: str, definition: dict) -> tuple[bool, str]:
    """Return whether the current user can act and the action label to present."""
    try:
        await _require_definition_approval_access(
            pool, user, definition_type, str(definition["id"])
        )
    except HTTPException:
        return False, "Approve"
    requires_admin = (
        definition_type == "hook"
        and hook_admin_approval_required(organization_id=user.organization_id)
    ) or (
        definition_type == "managed_tool"
        and managed_tool_admin_approval_required(organization_id=user.organization_id)
    )
    if requires_admin and definition.get("status") == "proposed":
        return True, "Approve and send to admin"
    if requires_admin and definition.get("status") == "owner_approved":
        return True, "Final approve"
    return True, "Approve"


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


def _parse_lines_or_json_array(value: str) -> list[str]:
    """Parse a textarea as JSON array or newline-separated strings."""
    raw = (value or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _json_text(value, default) -> str:
    """Render a JSON-ish value as pretty text for edit forms."""
    if value in (None, ""):
        value = default
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    return json.dumps(value, indent=2) if isinstance(value, (dict, list)) else str(value)


def _args_text(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value)


def _env_vars_text(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    if isinstance(value, dict):
        return "\n".join(f"{key}={val}" for key, val in value.items())
    return str(value)


# ── Managed tool authoring helpers ────────────────────────────────────────
#
# The create/update web routes accept kind-specific fields instead of a single
# freeform `runtime_config` blob. These helpers turn the form into the exact
# `runtime_config` each runtime documents, so the repository's per-kind
# validator (the write boundary) sees a config it can actually judge.

#: Cap on a posted OpenAPI document, matching the repository's stored-spec limit.
_MAX_OPENAPI_SPEC_BYTES = 2_000_000

_HTTP_FORM_FIELDS = (
    "http_url",
    "http_method",
    "http_query",
    "http_headers",
    "http_body",
    "http_form",
    "http_response_path",
    "http_response_format",
    "http_allowed_status",
    "http_content_type",
)

_OPENAPI_FORM_FIELDS = (
    "openapi_spec",
    "openapi_operation_id",
    "openapi_base_url",
    "openapi_response_path",
    "openapi_response_format",
    "openapi_allowed_status",
    "openapi_headers",
    "openapi_query",
)


def _tool_error_redirect(message: str, *, tool_id: str | None = None) -> RedirectResponse:
    """Bounce back to the authoring page with a readable error, never a 500.

    A malformed tool definition is a user mistake, so it should land on the form
    the user just submitted with an explanation rather than as an unhandled
    validation exception.
    """
    base = f"/definitions/tools/{tool_id}" if tool_id else "/definitions?tab=tools"
    separator = "&" if "?" in base else "?"
    return RedirectResponse(
        url=f"{base}{separator}error={quote(str(message), safe='')}",
        status_code=303,
    )


def _parse_key_value_map(value: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` / ``Header: value`` lines, or a JSON object.

    Query strings are usually written as ``k=v`` and headers as ``Header:
    value``, so both separators are accepted rather than making the author
    remember which field wants which. A JSON object is taken as-is because the
    API surface also accepts one and users paste between the two.
    """
    raw = (value or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            result: dict[str, str] = {}
            for key, item in parsed.items():
                if isinstance(item, (dict, list)):
                    result[str(key)] = json.dumps(item)
                elif item is None:
                    result[str(key)] = ""
                else:
                    result[str(key)] = str(item)
            return result
    result = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, item = line.split("=", 1)
        elif ":" in line:
            key, item = line.split(":", 1)
        else:
            continue
        key = key.strip()
        if key:
            result[key] = item.strip()
    return result


def _parse_status_codes(value: str) -> list[int] | None:
    """Parse a comma-separated list of HTTP status codes, or ``None``."""
    raw = (value or "").strip()
    if not raw:
        return None
    codes: list[int] = []
    for chunk in raw.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            codes.append(int(chunk))
        except ValueError:
            raise ValueError(f"{chunk!r} is not an HTTP status code") from None
    return codes or None


def _parse_body_value(value: str):
    """Parse a request body as JSON when possible, otherwise keep it raw.

    XML, GraphQL, and plain-text bodies are legitimate, so an unparseable body
    is stored as the raw string the runtime documents for non-JSON payloads
    instead of being rejected.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _runtime_config_is_complete(form, kind: str) -> bool:
    """Whether the form posted *every* field for *kind*.

    The detail page's edit form renders all of a kind's fields, so "all present"
    means the author saw and could clear each one — a cleared field should really
    be cleared. A form that posts only some fields (an API-style partial edit) is
    merged instead so nothing is silently dropped.
    """
    fields = {
        "http": _HTTP_FORM_FIELDS,
        "openapi": _OPENAPI_FORM_FIELDS,
        "builtin": ("builtin_handler",),
        "python": ("python_image",),
    }.get(kind, ())
    return bool(fields) and all(field in form for field in fields)


def _runtime_config_from_form(form, kind: str) -> dict | None:
    """Build the ``runtime_config`` for *kind* from the posted form.

    Returns ``None`` when the form carries none of that kind's fields. That is
    how an update distinguishes "the author edited this config" from "this
    form never touched it", so editing a name cannot wipe a stored spec.
    """
    if kind == "http":
        if not any(field in form for field in _HTTP_FORM_FIELDS):
            return None
        config: dict = {}
        url = str(form.get("http_url", "")).strip()
        if url:
            config["url"] = url
        method = str(form.get("http_method", "")).strip().upper()
        if method:
            config["method"] = method
        for field, key in (
            ("http_query", "query"),
            ("http_headers", "headers"),
            ("http_form", "form"),
        ):
            parsed = _parse_key_value_map(str(form.get(field, "")))
            if parsed:
                config[key] = parsed
        body = _parse_body_value(str(form.get("http_body", "")))
        if body is not None:
            config["body"] = body
        response_path = str(form.get("http_response_path", "")).strip()
        if response_path:
            config["response_path"] = response_path
        response_format = str(form.get("http_response_format", "")).strip()
        if response_format:
            config["response_format"] = response_format
        content_type = str(form.get("http_content_type", "")).strip()
        if content_type:
            config["content_type"] = content_type
        allowed_status = _parse_status_codes(str(form.get("http_allowed_status", "")))
        if allowed_status:
            config["allowed_status"] = allowed_status
        return config

    if kind == "openapi":
        if not any(field in form for field in _OPENAPI_FORM_FIELDS):
            return None
        config = {}
        spec = str(form.get("openapi_spec", "")).strip()
        if spec:
            config["spec"] = spec
        operation_id = str(form.get("openapi_operation_id", "")).strip()
        if operation_id:
            config["operation_id"] = operation_id
        base_url = str(form.get("openapi_base_url", "")).strip()
        if base_url:
            config["base_url"] = base_url
        response_path = str(form.get("openapi_response_path", "")).strip()
        if response_path:
            config["response_path"] = response_path
        response_format = str(form.get("openapi_response_format", "")).strip()
        if response_format:
            config["response_format"] = response_format
        allowed_status = _parse_status_codes(str(form.get("openapi_allowed_status", "")))
        if allowed_status:
            config["allowed_status"] = allowed_status
        for field, key in (("openapi_headers", "headers"), ("openapi_query", "query")):
            parsed = _parse_key_value_map(str(form.get(field, "")))
            if parsed:
                config[key] = parsed
        return config

    if kind == "builtin":
        if "builtin_handler" not in form:
            return None
        handler = str(form.get("builtin_handler", "")).strip()
        return {"handler": handler} if handler else {}

    if kind == "python":
        if "python_image" not in form:
            return None
        image = str(form.get("python_image", "")).strip()
        return {"image": image} if image else {}

    return None


def _sandbox_gate_message() -> str:
    return (
        "Sandboxed Python tools are disabled for this organization because "
        "features.sandboxes_enabled is off. HTTP, OpenAPI, and built-in tools "
        "still work without a sandbox."
    )


def _parse_timeout_field(value) -> int:
    """Parse the timeout input, defaulting a blank field to 300 seconds."""
    raw = str(value or "").strip()
    if not raw:
        return 300
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Timeout must be a whole number of seconds, not {raw!r}") from None


# =============================================================================
# Definitions Management
# =============================================================================


@router.get("/definitions", response_class=HTMLResponse)
async def definitions_page(
    request: Request,
    tab: str = "composer",
    page: int = 1,
    per_page: int = 25,
    search: str = "",
):
    """Agent and skill definitions management page."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    role_value = user.role if isinstance(user.role, str) else user.role.value
    if tab in {"agent-composer", "agent_composer", "wizard", "agent-wizard", "agent_wizard"}:
        tab = "composer"
    if tab in {"mcp", "mcp-servers", "mcp_servers"}:
        tab = "tools"

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
        search=search if tab == "skills" else None,
        limit=per_page if tab == "skills" else 1000,
        offset=offset if tab == "skills" else 0,
        requester_user_id=str(user.id),
        requester_role=role_value,
    )
    mcp_result = await repo.list_mcp_servers(
        org_id,
        limit=1000,
        offset=0,
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
    tools_result = await repo.list_managed_tools(
        org_id,
        limit=1000,
        offset=0,
        requester_user_id=str(user.id),
        requester_role=role_value,
    )
    # A retired built-in is archived rather than deleted so its run history stays
    # auditable, but it is not a tool anyone can use — listing it would offer a
    # capability that fails on every call.
    tools_result["items"] = [
        item for item in tools_result.get("items", []) if item.get("status") != "archived"
    ]
    from lucent.settings import custom_tooling_enabled, sandboxes_enabled

    org_sandboxes_enabled = sandboxes_enabled(organization_id=org_id)
    org_custom_tooling_enabled = custom_tooling_enabled(organization_id=org_id)
    # One entry per executor kind, with an availability verdict the form uses to
    # disable a choice instead of offering one that would fail on submit. Only
    # sandbox-backed kinds depend on `features.sandboxes_enabled`.
    from lucent.services.tool_runtimes import runtime_catalog

    tool_runtime_kinds = runtime_catalog()
    for runtime_kind in tool_runtime_kinds:
        runtime_kind["available"] = (
            org_sandboxes_enabled or not runtime_kind["requires_sandbox"]
        )
    from lucent.services.builtin_tool_catalog import available_builtin_tools

    builtin_handlers = available_builtin_tools()
    # Curated "connect your accounts" presets. These are verified vendor endpoints,
    # shown so a user does not have to know a service publishes an MCP server or
    # guess its URL.
    from lucent.services.connector_catalog import connector_catalog, connector_categories

    connector_presets = connector_catalog()
    connector_groups = connector_categories()
    proposals = await repo.get_pending_proposals(
        org_id,
        requester_user_id=str(user.id),
        requester_role=role_value,
    )
    for definition_type, proposal_key in (
        ("agent", "agents"),
        ("skill", "skills"),
        ("hook", "hooks"),
        ("managed_tool", "managed_tools"),
    ):
        for proposal in proposals[proposal_key]:
            can_approve, approval_label = await _approval_button_state(
                pool, user, definition_type, proposal
            )
            proposal["can_approve"] = can_approve
            proposal["approval_label"] = approval_label
    for proposal in proposals["mcp_servers"]:
        proposal["can_approve"] = _is_admin_or_owner(user)
        proposal["approval_label"] = "Approve"
    definition_engineer_agent = await _find_agent_composer_agent(
        repo, org_id, user, role_value,
    )
    all_items = [
        *agents_result["items"],
        *skills_result["items"],
        *mcp_result["items"],
        *hooks_result["items"],
        *tools_result["items"],
    ]
    user_map, group_map = await _resolve_owner_maps(str(user.id), org_id, pool, all_items)
    user_groups = await _get_user_groups(pool, str(user.id), org_id)
    agents = _attach_owner_names(agents_result["items"], user_map, group_map)
    skills = _attach_owner_names(skills_result["items"], user_map, group_map)
    mcp_servers = _attach_owner_names(mcp_result["items"], user_map, group_map)
    hooks = _attach_owner_names(hooks_result["items"], user_map, group_map)
    managed_tools = _attach_owner_names(tools_result["items"], user_map, group_map)

    # Determine total_count and total_pages based on active tab
    if tab == "composer":
        total_count = 0
    elif tab == "agents":
        total_count = agents_result["total_count"]
    elif tab == "skills":
        total_count = skills_result["total_count"]
    elif tab == "hooks":
        total_count = hooks_result["total_count"]
    elif tab == "tools":
        total_count = tools_result["total_count"] + mcp_result["total_count"]
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
            "managed_tools": managed_tools,
            "proposals": proposals,
            "definition_engineer_agent": definition_engineer_agent,
            "owner_groups": user_groups,
            "agents_total": agents_result["total_count"],
            "skills_total": skills_result["total_count"],
            "mcp_total": mcp_result["total_count"],
            "hooks_total": hooks_result["total_count"],
            "tools_total": len(tools_result["items"]),
            "custom_tooling_enabled": org_custom_tooling_enabled,
            "sandboxes_enabled": org_sandboxes_enabled,
            "tool_runtime_kinds": tool_runtime_kinds,
            "builtin_handlers": builtin_handlers,
            "connector_presets": connector_presets,
            "connector_groups": connector_groups,
            "tab": tab,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total_count": total_count,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
            "search": search,
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
    user_map, group_map = await _resolve_owner_maps(str(user.id), org_id, pool, [agent])
    agent = _attach_owner_names([agent], user_map, group_map)[0]
    user_groups = await _get_user_groups(pool, str(user.id), org_id)
    can_approve, approval_label = await _approval_button_state(pool, user, "agent", agent)

    # Get all skills, tools, hooks, and providers for the assignment dropdowns
    all_skills = (
        await repo.list_skills(
            org_id,
            status="active",
            limit=1000,
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
    all_tools = (
        await repo.list_managed_tools(
            org_id,
            status="active",
            requester_user_id=str(user.id),
            requester_role=role_value,
        )
    )["items"]
    assigned_skills = await repo.get_agent_skills(agent_id, org_id)
    assigned_mcp = await repo.get_agent_mcp_servers(agent_id, org_id)
    assigned_hooks = await repo.get_agent_hooks(agent_id, org_id)
    assigned_tools = await repo.get_agent_managed_tools(agent_id, org_id)

    # Get assigned skill/mcp IDs for easier template logic
    assigned_skill_ids = {str(s["id"]) for s in assigned_skills}
    assigned_mcp_ids = {str(s["id"]) for s in assigned_mcp}
    assigned_hook_ids = {str(h["id"]) for h in assigned_hooks}
    assigned_tool_ids = {str(t["id"]) for t in assigned_tools}

    # Per-user consent state. Pending items are attached to this agent but this
    # user has not approved them, so they are NOT active for their runs yet;
    # declined items stay visible so the decision can be reversed.
    pending_capabilities: list[dict] = []
    declined_capabilities: list[dict] = []
    try:
        all_pending = await repo.list_pending_capability_consents(org_id, str(user.id))
        pending_capabilities = [
            item for item in all_pending if str(item["agent_id"]) == str(agent_id)
        ]
        all_declined = await repo.list_declined_capability_consents(
            org_id, str(user.id)
        )
        declined_capabilities = [
            item for item in all_declined if str(item["agent_id"]) == str(agent_id)
        ]
    except Exception:
        logger.debug("Could not load capability consent state", exc_info=True)

    return templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition": agent,
            "definition_type": "agent",
            "can_approve": can_approve,
            "approval_label": approval_label,
            "owner_groups": user_groups,
            "all_skills": all_skills,
            "all_mcp": all_mcp,
            "all_hooks": all_hooks,
            "all_tools": all_tools,
            "skills": assigned_skills,
            "mcp_servers": assigned_mcp,
            "hooks": assigned_hooks,
            "managed_tools": assigned_tools,
            "granted_skill_ids": assigned_skill_ids,
            "granted_mcp_ids": assigned_mcp_ids,
            "granted_hook_ids": assigned_hook_ids,
            "granted_tool_ids": assigned_tool_ids,
            "pending_capabilities": pending_capabilities,
            "declined_capabilities": declined_capabilities,
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
    user_map, group_map = await _resolve_owner_maps(str(user.id), org_id, pool, [skill])
    skill = _attach_owner_names([skill], user_map, group_map)[0]
    user_groups = await _get_user_groups(pool, str(user.id), org_id)
    can_approve, approval_label = await _approval_button_state(pool, user, "skill", skill)

    return templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition": skill,
            "definition_type": "skill",
            "can_approve": can_approve,
            "approval_label": approval_label,
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
    user_map, group_map = await _resolve_owner_maps(str(user.id), org_id, pool, [server])
    server = _attach_owner_names([server], user_map, group_map)[0]
    server["args_text"] = _args_text(server.get("args"))
    server["headers_text"] = _json_text(server.get("headers"), {})
    server["env_vars_text"] = _env_vars_text(server.get("env_vars"))
    user_groups = await _get_user_groups(pool, str(user.id), org_id)
    can_approve = _is_admin_or_owner(user)
    approval_label = "Approve"

    # Which declared credential slots this *user* can already satisfy. The
    # endpoint is shared; the secret is personal, so "does this connector work for
    # me" is a per-viewer question and has to be answered on the page rather than
    # discovered when a tool call fails.
    credential_status = await _mcp_credential_status(
        pool, server, user, org_id
    )

    return templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition": server,
            "definition_type": "mcp-server",
            "can_approve": can_approve,
            "approval_label": approval_label,
            "owner_groups": user_groups,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
            "credential_status": credential_status,
        },
    )


async def _mcp_credential_status(
    pool, server: dict, user, org_id: str
) -> list[dict]:
    """Report, per declared credential slot, whether this user can resolve it.

    Uses the same clearance-driven resolution the runtime uses, so the page cannot
    claim a connector is ready when a call would still fail.
    """
    slots = server.get("credential_slots") or []
    if not slots:
        return []
    from lucent.secrets import SecretRegistry
    from lucent.secrets.utils import resolve_secret_reference

    try:
        provider = SecretRegistry.get()
    except KeyError:
        provider = None

    status: list[dict] = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        secret_key = str(slot.get("secret_key") or "").strip()
        entry = {
            "name": slot.get("name") or "",
            "label": slot.get("label") or slot.get("name") or "",
            "help": slot.get("help") or "",
            "secret_key": secret_key,
            "create_url": slot.get("create_url") or "",
            "required": bool(slot.get("required", True)),
            "resolved": False,
        }
        if secret_key and provider is not None:
            try:
                await resolve_secret_reference(f"secret://{secret_key}", provider)
                entry["resolved"] = True
            except (KeyError, ValueError):
                entry["resolved"] = False
        status.append(entry)
    return status


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
    user_map, group_map = await _resolve_owner_maps(str(user.id), org_id, pool, [hook])
    hook = _attach_owner_names([hook], user_map, group_map)[0]
    user_groups = await _get_user_groups(pool, str(user.id), org_id)
    can_approve, approval_label = await _approval_button_state(pool, user, "hook", hook)

    return templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition": hook,
            "definition_type": "hook",
            "can_approve": can_approve,
            "approval_label": approval_label,
            "owner_groups": user_groups,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.get("/definitions/tools/{tool_id}", response_class=HTMLResponse)
async def managed_tool_detail_page(request: Request, tool_id: str):
    """Managed tool definition detail page."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    role_value = user.role if isinstance(user.role, str) else user.role.value
    tool = await repo.get_managed_tool(
        tool_id, org_id, requester_user_id=str(user.id), requester_role=role_value
    )
    if not tool:
        raise HTTPException(status_code=404, detail="Managed tool not found")
    user_map, group_map = await _resolve_owner_maps(str(user.id), org_id, pool, [tool])
    tool = _attach_owner_names([tool], user_map, group_map)[0]
    tool["input_schema_text"] = _json_text(tool.get("input_schema"), {})
    tool["output_schema_text"] = _json_text(tool.get("output_schema"), {})
    tool["requirements_text"] = _args_text(tool.get("requirements"))
    tool["runtime_config_text"] = _json_text(tool.get("runtime_config"), {})
    tool["env_vars_text"] = _env_vars_text(tool.get("env_vars"))
    tool["auth_policy_text"] = _json_text(tool.get("auth_policy"), {})
    tool["network_policy_text"] = _json_text(tool.get("network_policy"), {})
    tool["resource_limits_text"] = _json_text(tool.get("resource_limits"), {})
    tool["content"] = tool.get("source_code") or ""
    user_groups = await _get_user_groups(pool, str(user.id), org_id)
    can_approve, approval_label = await _approval_button_state(pool, user, "managed_tool", tool)

    return templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition": tool,
            "definition_type": "tool",
            "tool_kind": _tool_kind_view(tool),
            "network_policy_summary": _network_policy_summary(
                tool.get("network_policy")
            ),
            "can_approve": can_approve,
            "approval_label": approval_label,
            "owner_groups": user_groups,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


def _network_policy_summary(policy) -> str:
    """Render a network policy as one plain-language sentence.

    An operator approving a tool should not have to decode `network_mode` to
    learn whether it can reach the internet.
    """
    if not isinstance(policy, dict):
        return "No outbound network"
    mode = str(policy.get("network_mode") or "none").strip().lower()
    hosts = [
        str(host).strip()
        for host in (policy.get("allowed_hosts") or [])
        if str(host).strip()
    ]
    if mode == "none":
        return "No outbound network"
    if mode == "allowlist":
        if not hosts:
            return "No outbound network (empty allowlist)"
        return "Only " + ", ".join(hosts)
    if mode == "bridge":
        return "Any public host on port 80/443"
    return f"Unrecognized network mode {mode!r}"


def _config_keys(config: dict, key: str) -> list[str]:
    """Sorted keys of a config mapping, tolerating a malformed stored value."""
    value = config.get(key)
    if isinstance(value, dict):
        return sorted(str(item) for item in value)
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _tool_kind_view(tool: dict) -> dict:
    """Resolve the presentation details for one tool's runtime kind.

    Built-in availability is checked live against this image so an approver can
    see that a selected handler will fail to run here (a missing optional
    dependency), rather than discovering it on the first agent call.
    """
    kind = str(tool.get("runtime_type") or "python").strip().lower()
    config = tool.get("runtime_config") or {}
    if not isinstance(config, dict):
        config = {}
    view: dict = {"kind": kind, "config": config, "handlers": [], "handler": None}

    if kind == "python":
        view["image"] = str(config.get("image") or "lucent-sandbox:base")
    elif kind == "http":
        view["url"] = config.get("url") or ""
        view["method"] = str(config.get("method") or "GET").upper()
        view["query_keys"] = _config_keys(config, "query")
        view["header_names"] = _config_keys(config, "headers")
        view["response_path"] = config.get("response_path") or ""
        view["response_format"] = config.get("response_format") or "json"
        view["allowed_status"] = config.get("allowed_status") or [200]
    elif kind == "openapi":
        from lucent.services.tool_runtimes.openapi_spec import (
            SpecError,
            list_operations,
            parse_spec,
            resolve_operation,
        )

        view["operation_id"] = config.get("operation_id") or ""
        view["base_url"] = config.get("base_url") or ""
        view["response_path"] = config.get("response_path") or ""
        view["response_format"] = config.get("response_format") or "json"
        view["allowed_status"] = config.get("allowed_status") or [200]
        view["header_names"] = _config_keys(config, "headers")
        view["query_keys"] = _config_keys(config, "query")
        spec_text = config.get("spec") or ""
        view["spec_text"] = spec_text
        view["spec_bytes"] = len(spec_text.encode("utf-8")) if spec_text else 0
        view["method"] = ""
        view["path"] = ""
        try:
            document = parse_spec(spec_text)
            operation = resolve_operation(
                document, operation_id=config.get("operation_id")
            )
            view["method"] = operation["method"]
            view["path"] = operation["path"]
            view["base_url"] = view["base_url"] or operation.get("base_url") or ""
            view["operation_count"] = len(list_operations(document))
        except SpecError as exc:
            view["spec_error"] = str(exc)
    elif kind == "builtin":
        from lucent.services.builtin_tool_catalog import (
            available_builtin_tools,
            get_builtin_tool,
        )

        handler = str(config.get("handler") or "").strip()
        view["handler"] = handler
        view["handlers"] = available_builtin_tools()
        view["descriptor"] = next(
            (item for item in view["handlers"] if item.get("handler") == handler), None
        )
        view["label"] = ""
        view["handler_description"] = ""
        view["handler_available"] = False
        view["handler_unavailable_reason"] = None
        view["handler_uses_network"] = False
        if handler:
            try:
                spec = get_builtin_tool(handler)
                view["label"] = spec.label
                view["handler_description"] = spec.description
                view["handler_uses_network"] = spec.uses_network
            except Exception:  # noqa: BLE001 — an unknown handler is shown as such
                view["handler_unavailable_reason"] = (
                    f"No handler named {handler!r} exists in this build"
                )
        if view["descriptor"]:
            view["handler_available"] = bool(view["descriptor"].get("available"))
            view["handler_unavailable_reason"] = view["descriptor"].get(
                "unavailable_reason"
            )
    return view


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
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await _require_definition_approval_access(pool, user, "agent", agent_id)
    await repo.approve_agent(agent_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/reject")
async def reject_agent_web(request: Request, agent_id: str):
    """Reject an agent definition."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await _require_definition_approval_access(pool, user, "agent", agent_id)
    await repo.reject_agent(agent_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/skills/{skill_id}/approve")
async def approve_skill_web(request: Request, skill_id: str):
    """Approve a skill definition."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await _require_definition_approval_access(pool, user, "skill", skill_id)
    await repo.approve_skill(skill_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/skills/{skill_id}", status_code=303)


@router.post("/definitions/skills/{skill_id}/reject")
async def reject_skill_web(request: Request, skill_id: str):
    """Reject a skill definition."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await _require_definition_approval_access(pool, user, "skill", skill_id)
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
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await _require_definition_approval_access(pool, user, "hook", hook_id)
    await repo.approve_hook(
        hook_id,
        str(user.organization_id),
        str(user.id),
        require_admin_approval=hook_admin_approval_required(
            organization_id=user.organization_id
        ),
    )
    return RedirectResponse(url=f"/definitions/hooks/{hook_id}", status_code=303)


@router.post("/definitions/hooks/{hook_id}/reject")
async def reject_hook_web(request: Request, hook_id: str):
    """Reject a hook definition."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await _require_definition_approval_access(pool, user, "hook", hook_id)
    await repo.reject_hook(hook_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/hooks/{hook_id}", status_code=303)


@router.post("/definitions/tools/{tool_id}/approve")
async def approve_managed_tool_web(request: Request, tool_id: str):
    """Approve a managed tool definition."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await _require_definition_approval_access(pool, user, "managed_tool", tool_id)
    await repo.approve_managed_tool(
        tool_id,
        str(user.organization_id),
        str(user.id),
        require_admin_approval=managed_tool_admin_approval_required(
            organization_id=user.organization_id
        ),
    )
    return RedirectResponse(url=f"/definitions/tools/{tool_id}", status_code=303)


@router.post("/definitions/tools/{tool_id}/reject")
async def reject_managed_tool_web(request: Request, tool_id: str):
    """Reject a managed tool definition."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await _require_definition_approval_access(pool, user, "managed_tool", tool_id)
    await repo.reject_managed_tool(tool_id, str(user.organization_id), str(user.id))
    return RedirectResponse(url=f"/definitions/tools/{tool_id}", status_code=303)


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
        proposal_evidence=_agent_kind_evidence(form.get("agent_kind"), "web_form"),
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

    server_type = str(form.get("server_type", "stdio")).strip() or "stdio"
    url = str(form.get("url", "")).strip() or None
    headers = _parse_json_object(str(form.get("headers", "")), {})

    # A curated preset fills in the endpoint and transport so the user only has to
    # confirm a name. The preset is looked up server-side and only used to *seed*
    # values the user could have typed themselves — it grants nothing, and the
    # normal proposed -> approved lifecycle still applies.
    preset_slug = str(form.get("connector_preset", "")).strip()
    if preset_slug:
        from lucent.services.connector_catalog import get_connector

        try:
            preset = get_connector(preset_slug)
        except KeyError:
            return _tool_error_redirect(
                f"Unknown connector preset {preset_slug!r}."
            )
        server_type = preset.transport
        url = (
            preset.readonly_url
            if preset.has_readonly_variant and str(form.get("readonly", "")).strip()
            else preset.url
        )
        if not str(form.get("name", "")).strip():
            form = {**form, "name": preset.name}
        if not str(form.get("description", "")).strip():
            form = {**form, "description": preset.summary}

    server = await repo.create_mcp_server(
        name=str(form.get("name", "")).strip(),
        org_id=org_id,
        description=str(form.get("description", "")).strip(),
        server_type=server_type,
        url=url,
        created_by=str(user.id),
        command=command,
        args=args,
        headers=headers,
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


@router.post("/definitions/tools/create")
async def create_managed_tool_web(request: Request):
    """Create a new managed tool definition of any supported runtime kind."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository
    from lucent.services.tool_runtimes import (
        runtime_requires_sandbox,
        runtime_types,
    )

    kind = str(form.get("runtime_type", "python")).strip().lower() or "python"
    if kind not in runtime_types():
        return _tool_error_redirect(
            f"Unknown runtime kind {kind!r}. Choose one of: "
            + ", ".join(runtime_types())
        )
    # Per-kind gate: a sandbox-less deployment may still author declarative and
    # built-in tools because nothing is provisioned to run them.
    if runtime_requires_sandbox(kind):
        from lucent.settings import custom_tooling_enabled

        if not custom_tooling_enabled(organization_id=user.organization_id):
            return _tool_error_redirect(_sandbox_gate_message())

    try:
        runtime_config = _runtime_config_from_form(form, kind) or {}
    except ValueError as exc:
        return _tool_error_redirect(str(exc))
    if kind == "openapi":
        # Surface a bad document or a missing operation as a banner instead of
        # letting the repository raise into a 500.
        from lucent.services.tool_runtimes.openapi_spec import (
            SpecError,
            list_operations,
            parse_spec,
            resolve_operation,
        )

        try:
            document = parse_spec(str(runtime_config.get("spec") or ""))
        except SpecError as exc:
            return _tool_error_redirect(f"OpenAPI document was rejected: {exc}")
        operation_id = str(runtime_config.get("operation_id") or "").strip()
        if not operation_id:
            count = len(list_operations(document))
            return _tool_error_redirect(
                "Choose one operation from the OpenAPI document; an OpenAPI tool "
                f"wraps exactly one operation (this document declares {count})."
            )
        try:
            resolve_operation(document, operation_id=operation_id)
        except SpecError as exc:
            return _tool_error_redirect(f"OpenAPI operation was rejected: {exc}")

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    owner_user_id, owner_group_id, shared_with_org = await _resolve_owner_scope(
        form, user, pool
    )

    input_schema = _parse_json_object(
        str(form.get("input_schema", "")), {"type": "object", "properties": {}}
    )
    if kind == "builtin":
        # The catalog owns a built-in tool's contract; the repository enforces
        # that, so never forward a schema the form happened to post.
        from lucent.services.builtin_tool_catalog import get_builtin_tool
        from lucent.services.tool_runtimes.base import ToolRuntimeError

        handler = str(runtime_config.get("handler") or "").strip()
        if not handler:
            return _tool_error_redirect(
                "Choose a built-in handler; a built-in tool is one reviewed "
                "catalog action."
            )
        try:
            input_schema = get_builtin_tool(handler).input_schema
        except ToolRuntimeError as exc:
            return _tool_error_redirect(str(exc))

    network_policy_text = str(form.get("network_policy", "")).strip()
    if network_policy_text:
        network_policy = _parse_json_object(network_policy_text, {})
    elif kind == "builtin":
        # Seed a workable policy from the handler's own host list so a
        # network-using handler is not approved with "no outbound network".
        from lucent.services.builtin_tool_catalog import get_builtin_tool

        spec = get_builtin_tool(str(runtime_config.get("handler") or "").strip())
        if spec.network_hosts:
            network_policy = {
                "network_mode": "allowlist",
                "allowed_hosts": list(spec.network_hosts),
            }
        elif spec.uses_network:
            network_policy = {"network_mode": "bridge", "allowed_hosts": []}
        else:
            network_policy = {"network_mode": "none", "allowed_hosts": []}
    else:
        network_policy = {"network_mode": "none", "allowed_hosts": []}

    try:
        tool = await repo.create_managed_tool(
            name=str(form.get("name", "")).strip(),
            org_id=org_id,
            description=str(form.get("description", "")).strip(),
            input_schema=input_schema,
            output_schema=_parse_json_object(str(form.get("output_schema", "")), {}) or None,
            runtime_type=kind,
            source_code=str(form.get("source_code", "")).strip() if kind == "python" else "",
            entrypoint=(
                (str(form.get("entrypoint", "handler")).strip() or "handler")
                if kind == "python"
                else "handler"
            ),
            requirements=(
                _parse_lines_or_json_array(str(form.get("requirements", "")))
                if kind == "python"
                else []
            ),
            runtime_config=runtime_config,
            env_vars=_parse_env_vars(str(form.get("env_vars", ""))),
            auth_policy=_parse_json_object(
                str(form.get("auth_policy", "")),
                {"mode": "agent_grant", "require_user_access": True},
            ),
            network_policy=network_policy,
            resource_limits=_parse_json_object(
                str(form.get("resource_limits", "")),
                {"memory_limit": "512m", "cpu_limit": 1.0, "disk_limit": "1g"},
            ),
            timeout_seconds=_parse_timeout_field(form.get("timeout_seconds")),
            created_by=str(user.id),
            owner_user_id=owner_user_id,
            owner_group_id=owner_group_id,
            shared_with_org=shared_with_org,
        )
    except ValueError as exc:
        return _tool_error_redirect(str(exc))
    return RedirectResponse(url=f"/definitions/tools/{tool['id']}", status_code=303)


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
    headers = _parse_json_object(str(form.get("headers", "")), {})
    server_type = str(form.get("server_type", "stdio")).strip() or "stdio"
    url = str(form.get("url", "")).strip() or None
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
        server_type=server_type,
        url=url,
        command=command,
        args=args,
        headers=headers,
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
    return RedirectResponse(url="/definitions?tab=tools", status_code=303)


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


@router.post("/definitions/tools/{tool_id}/update")
async def update_managed_tool_web(request: Request, tool_id: str):
    """Update a managed tool definition, preserving its runtime kind.

    Every field is optional: only the ones the form actually submitted are
    forwarded, so a form that edits a name (or a form for a different kind) can
    never silently rewrite `runtime_config` or flip an http/openapi/builtin tool
    back to `python`.
    """
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository
    from lucent.services.tool_runtimes import (
        runtime_requires_sandbox,
        runtime_types,
    )

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    org_id = str(user.organization_id)
    role_value = user.role if isinstance(user.role, str) else user.role.value
    current = await repo.get_managed_tool(
        tool_id,
        org_id,
        requester_user_id=str(user.id),
        requester_role=role_value,
    )
    if not current:
        raise HTTPException(status_code=404, detail="Managed tool not found")

    # Preserve the stored kind unless the form explicitly switches it.
    previous_kind = str(current.get("runtime_type") or "python").strip().lower()
    kind = str(form.get("runtime_type", "")).strip().lower() or previous_kind
    if kind not in runtime_types():
        return _tool_error_redirect(
            f"Unknown runtime kind {kind!r}. Choose one of: "
            + ", ".join(runtime_types()),
            tool_id=tool_id,
        )
    if kind != previous_kind and runtime_requires_sandbox(kind):
        from lucent.settings import custom_tooling_enabled

        if not custom_tooling_enabled(organization_id=user.organization_id):
            return _tool_error_redirect(_sandbox_gate_message(), tool_id=tool_id)

    previous_config = current.get("runtime_config") or {}
    if not isinstance(previous_config, dict):
        previous_config = {}
    try:
        form_config = _runtime_config_from_form(form, kind)
    except ValueError as exc:
        return _tool_error_redirect(str(exc), tool_id=tool_id)

    if kind == "openapi" and form_config is not None:
        from lucent.services.tool_runtimes.openapi_spec import (
            SpecError,
            list_operations,
            parse_spec,
            resolve_operation,
        )

        spec_text = str(form_config.get("spec") or previous_config.get("spec") or "")
        operation_id = form_config.get("operation_id") or previous_config.get("operation_id")
        try:
            document = parse_spec(spec_text)
            resolve_operation(document, operation_id=operation_id)
        except SpecError as exc:
            return _tool_error_redirect(
                f"OpenAPI document was rejected: {exc}", tool_id=tool_id
            )
        operations = list_operations(document)
        if operations and not operation_id:
            return _tool_error_redirect(
                "Choose one operation from the OpenAPI document; an OpenAPI tool "
                "wraps exactly one operation.",
                tool_id=tool_id,
            )

    if kind != previous_kind:
        # Switching kinds replaces the config wholesale: carrying a stored spec
        # into an http tool (or a URL into a builtin) would leave fields the new
        # kind silently ignores.
        runtime_config = dict(form_config or {})
    elif form_config is not None and _runtime_config_is_complete(form, kind):
        # The full form was rendered and submitted, so a blank field is a real
        # deletion rather than a field this form does not know about.
        runtime_config = dict(form_config)
    elif form_config is not None:
        # Partial form: merge so untouched fields keep their stored values.
        runtime_config = {**previous_config, **form_config}
    else:
        runtime_config = None  # untouched

    updates: dict = {
        "requester_role": role_value,
        "name": str(form.get("name", "")).strip() or current.get("name"),
        "description": form.get("description", current.get("description") or ""),
        "runtime_type": kind,
    }

    if kind == "python" and ("source_code" in form or previous_kind != kind):
        source_code = str(form.get("source_code", "")).strip()
        if source_code:
            updates["source_code"] = source_code
    if kind == "python" and ("entrypoint" in form or previous_kind != kind):
        updates["entrypoint"] = str(form.get("entrypoint", "handler")).strip() or "handler"
    if kind == "python" and ("requirements" in form or previous_kind != kind):
        updates["requirements"] = _parse_lines_or_json_array(
            str(form.get("requirements", ""))
        )
    if runtime_config is not None:
        updates["runtime_config"] = runtime_config

    if "input_schema" in form and kind != "builtin":
        updates["input_schema"] = _parse_json_object(
            str(form.get("input_schema", "")), {"type": "object", "properties": {}}
        )
    if "output_schema" in form:
        updates["output_schema"] = _parse_json_object(
            str(form.get("output_schema", "")), {}
        ) or None
    if kind == "builtin":  # the catalog owns the contract
        from lucent.services.builtin_tool_catalog import get_builtin_tool
        from lucent.services.tool_runtimes.base import ToolRuntimeError

        handler = str((runtime_config or previous_config).get("handler") or "").strip()
        if not handler:
            return _tool_error_redirect(
                "Choose a built-in handler; a built-in tool is one reviewed "
                "catalog action.",
                tool_id=tool_id,
            )
        try:
            updates["input_schema"] = get_builtin_tool(handler).input_schema
        except ToolRuntimeError as exc:
            return _tool_error_redirect(str(exc), tool_id=tool_id)

    if "env_vars" in form:
        updates["env_vars"] = _parse_env_vars(str(form.get("env_vars", "")))
    if "auth_policy" in form:
        updates["auth_policy"] = _parse_json_object(
            str(form.get("auth_policy", "")),
            current.get("auth_policy") or {"mode": "agent_grant", "require_user_access": True},
        )
    network_policy_text = str(form.get("network_policy", "")).strip()
    submitted_policy = _parse_json_object(network_policy_text, {}) if network_policy_text else {}
    current_policy = current.get("network_policy") or {}
    # Omitting an unchanged policy matters for `builtin`: the repository seeds the
    # handler's own host list when a handler changes, and passing the old value
    # back would suppress that.
    if submitted_policy and submitted_policy != current_policy:
        updates["network_policy"] = submitted_policy
    elif kind == "builtin":
        previous_handler = str(previous_config.get("handler") or "").strip()
        new_handler = str((runtime_config or previous_config).get("handler") or "").strip()
        if new_handler and (new_handler != previous_handler or kind != previous_kind):
            # Re-derive the policy for the *new* handler. A handler that makes no
            # outbound calls must not inherit `bridge` from the repository's
            # host-less-handler default.
            from lucent.services.builtin_tool_catalog import get_builtin_tool

            spec = get_builtin_tool(new_handler)
            if spec.network_hosts:
                seeded = {
                    "network_mode": "allowlist",
                    "allowed_hosts": list(spec.network_hosts),
                }
            elif spec.uses_network:
                seeded = {"network_mode": "bridge", "allowed_hosts": []}
            else:
                seeded = {"network_mode": "none", "allowed_hosts": []}
            if seeded != current_policy:
                updates["network_policy"] = seeded
    if "resource_limits" in form:
        updates["resource_limits"] = _parse_json_object(
            str(form.get("resource_limits", "")),
            current.get("resource_limits") or {
                "memory_limit": "512m", "cpu_limit": 1.0, "disk_limit": "1g",
            },
        )
    timeout_text = str(form.get("timeout_seconds", "")).strip()
    if timeout_text:
        try:
            updates["timeout_seconds"] = int(timeout_text)
        except ValueError:
            return _tool_error_redirect(
                f"Timeout must be a whole number of seconds, not {timeout_text!r}.",
                tool_id=tool_id,
            )

    owner_kwargs = {}
    if "owner_scope" in form:
        owner_user_id, owner_group_id, _shared_with_org = await _resolve_owner_scope(
            form, user, pool
        )
        owner_kwargs = {"owner_user_id": owner_user_id, "owner_group_id": owner_group_id}

    try:
        await repo.update_managed_tool(tool_id, org_id, **updates, **owner_kwargs)
    except ValueError as exc:
        return _tool_error_redirect(str(exc), tool_id=tool_id)
    return RedirectResponse(url=f"/definitions/tools/{tool_id}", status_code=303)


@router.post("/definitions/tools/openapi-operations")
async def openapi_operations_ajax(request: Request):
    """Return the operations of a posted OpenAPI document for the UI picker.

    Chosen over rendering the picker server-side because the author may paste a
    spec and want to see the operation list without committing a definition: a
    JSON round-trip keeps the create modal open and never writes a row for a
    half-filled form.

    CSRF is checked the same way as every other state-changing web route, and the
    document is size-capped before parsing so a hostile paste cannot exhaust the
    worker.
    """
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > (
        _MAX_OPENAPI_SPEC_BYTES + 65536
    ):
        return JSONResponse(
            {
                "operations": [],
                "error": (
                    f"Document exceeds the {_MAX_OPENAPI_SPEC_BYTES} byte limit; create "
                    "per-operation tools instead of storing one large document."
                ),
            },
            status_code=413,
        )

    form = await request.form()
    spec_text = str(form.get("spec", ""))
    if not spec_text.strip():
        return JSONResponse({"operations": [], "error": "Paste an OpenAPI document first."})
    if len(spec_text.encode("utf-8")) > _MAX_OPENAPI_SPEC_BYTES:
        return JSONResponse({
            "operations": [],
            "error": (
                f"Document exceeds the {_MAX_OPENAPI_SPEC_BYTES} byte limit; create "
                "per-operation tools instead of storing one large document."
            ),
        })
    from lucent.services.tool_runtimes.openapi_spec import (
        SpecError,
        list_operations,
        parse_spec,
    )

    try:
        document = parse_spec(spec_text)
        operations = list_operations(document)
    except SpecError as exc:
        return JSONResponse({"operations": [], "error": str(exc)})
    return JSONResponse({"operations": operations, "error": None})


@router.post("/definitions/tools/{tool_id}/discover-tools")
async def discover_managed_mcp_proxy_tools_web(request: Request, tool_id: str):
    """Warm an active managed MCP proxy and cache its native tool descriptors."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository
    from lucent.services.managed_tools import (
        ManagedToolBlockedError,
        ManagedToolError,
        ManagedToolExecutor,
    )

    org_id = str(user.organization_id)
    role_value = user.role if isinstance(user.role, str) else user.role.value
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    tool = await repo.get_managed_tool(
        tool_id,
        org_id,
        requester_user_id=str(user.id),
        requester_role=role_value,
    )
    if not tool:
        raise HTTPException(status_code=404, detail="Managed tool not found")
    if tool.get("status") != "active":
        raise HTTPException(status_code=409, detail="Managed tool must be active before discovery")

    try:
        tools = await ManagedToolExecutor(repo).discover_mcp_proxy_tools(
            tool=tool,
            org_id=org_id,
            user_id=str(user.id),
            user_role=role_value,
            enforce_agent_grant=False,
        )
    except ManagedToolBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ManagedToolError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await repo.save_managed_mcp_proxy_tools(tool_id, tools, org_id)
    return RedirectResponse(url=f"/definitions/tools/{tool_id}", status_code=303)


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


@router.post("/definitions/tools/{tool_id}/delete")
async def delete_managed_tool_web(request: Request, tool_id: str):
    """Delete a managed tool definition."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    await repo.delete_managed_tool(tool_id, str(user.organization_id))
    return RedirectResponse(url="/definitions?tab=tools", status_code=303)


async def _agent_capability_attach(
    request: Request,
    *,
    agent_id: str,
    capability_type: str,
    capability_id: str,
) -> RedirectResponse:
    """Shared attach handler for the four capability panels.

    Authorization lives in the repository (read clearance on both endpoints), so
    a member who can see the agent and the capability may attach it. There is no
    role check here on purpose: attaching no longer grants anyone else anything,
    because every other user must still approve it for themselves.
    """
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository, GrantOutcome

    destination = f"/definitions/agents/{agent_id}"
    if not capability_id:
        return RedirectResponse(url=destination, status_code=303)

    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    from lucent.services.entitlements import CapabilityDisabledError

    try:
        outcome = await repo.grant_capability(
            agent_id=agent_id,
            capability_type=capability_type,
            capability_id=capability_id,
            org_id=str(user.organization_id),
            actor_user_id=str(user.id),
        )
    except CapabilityDisabledError as exc:
        # The org's plan does not include this capability; say so explicitly
        # in the redirect the user sees rather than as a generic Forbidden.
        return RedirectResponse(
            url=f"{destination}?error={quote(str(exc))}", status_code=303
        )
    if outcome is GrantOutcome.ALREADY_ATTACHED:
        message = (
            "That capability is already attached to this agent. Approve it for "
            "yourself to use it — it cannot be attached twice."
        )
        return RedirectResponse(url=f"{destination}?warning={quote(message)}", status_code=303)
    if outcome is GrantOutcome.DENIED:
        message = "You can only attach capabilities you can see to agents you can see."
        return RedirectResponse(url=f"{destination}?error={quote(message)}", status_code=303)
    if outcome is GrantOutcome.FAILED:
        message = "Could not attach that capability. Please try again."
        return RedirectResponse(url=f"{destination}?error={quote(message)}", status_code=303)
    return RedirectResponse(url=destination, status_code=303)


async def _agent_capability_detach(
    request: Request,
    *,
    agent_id: str,
    capability_type: str,
    capability_id: str,
) -> RedirectResponse:
    """Shared detach handler. Restricted to the agent owner, capability owner,
    or an org admin/owner — declining is how a user removes it for themselves."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.audit import AuditRepository
    from lucent.db.definitions import DefinitionRepository

    destination = f"/definitions/agents/{agent_id}"
    role_value = user.role if isinstance(user.role, str) else user.role.value
    repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
    revoked = await repo.revoke_capability(
        agent_id=agent_id,
        capability_type=capability_type,
        capability_id=capability_id,
        org_id=str(user.organization_id),
        user_id=str(user.id),
        user_role=role_value,
    )
    if not revoked:
        message = (
            "Only the agent owner, the capability owner, or an organization "
            "admin can detach it. You can decline it for yourself instead."
        )
        return RedirectResponse(url=f"{destination}?error={quote(message)}", status_code=303)
    return RedirectResponse(url=destination, status_code=303)


@router.post("/definitions/agents/{agent_id}/grant-skill")
async def grant_skill_web(request: Request, agent_id: str):
    """Grant a skill to an agent."""
    form = await request.form()
    return await _agent_capability_attach(
        request,
        agent_id=agent_id,
        capability_type="skill",
        capability_id=str(form.get("skill_id", "")),
    )


@router.post("/definitions/agents/{agent_id}/revoke-skill/{skill_id}")
async def revoke_skill_web(request: Request, agent_id: str, skill_id: str):
    """Revoke a skill from an agent."""
    return await _agent_capability_detach(
        request, agent_id=agent_id, capability_type="skill", capability_id=skill_id,
    )


@router.post("/definitions/agents/{agent_id}/grant-mcp")
async def grant_mcp_web(request: Request, agent_id: str):
    """Grant an MCP server to an agent."""
    form = await request.form()
    return await _agent_capability_attach(
        request,
        agent_id=agent_id,
        capability_type="mcp_server",
        capability_id=str(form.get("mcp_server_id", "") or form.get("server_id", "")),
    )


@router.post("/definitions/agents/{agent_id}/revoke-mcp/{server_id}")
async def revoke_mcp_web(request: Request, agent_id: str, server_id: str):
    """Revoke an MCP server from an agent."""
    return await _agent_capability_detach(
        request, agent_id=agent_id, capability_type="mcp_server", capability_id=server_id,
    )


@router.post("/definitions/agents/{agent_id}/grant-hook")
async def grant_hook_web(request: Request, agent_id: str):
    """Grant a hook to an agent."""
    form = await request.form()
    return await _agent_capability_attach(
        request,
        agent_id=agent_id,
        capability_type="hook",
        capability_id=str(form.get("hook_id", "")),
    )


@router.post("/definitions/agents/{agent_id}/grant-tool")
async def grant_managed_tool_web(request: Request, agent_id: str):
    """Grant a managed tool to an agent."""
    form = await request.form()
    return await _agent_capability_attach(
        request,
        agent_id=agent_id,
        capability_type="managed_tool",
        capability_id=str(form.get("tool_id", "")),
    )


@router.post("/definitions/agents/{agent_id}/revoke-hook/{hook_id}")
async def revoke_hook_web(request: Request, agent_id: str, hook_id: str):
    """Revoke a hook from an agent."""
    return await _agent_capability_detach(
        request, agent_id=agent_id, capability_type="hook", capability_id=hook_id,
    )


@router.post("/definitions/agents/{agent_id}/revoke-tool/{tool_id}")
async def revoke_managed_tool_web(request: Request, agent_id: str, tool_id: str):
    """Revoke a managed tool from an agent."""
    return await _agent_capability_detach(
        request, agent_id=agent_id, capability_type="managed_tool", capability_id=tool_id,
    )


@router.post("/definitions/agents/{agent_id}/consent")
async def decide_capability_consent_web(request: Request, agent_id: str):
    """Approve or decline one capability for the current user."""
    user = await get_user_context(request)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    destination = f"/definitions/agents/{agent_id}"
    capability_type = str(form.get("capability_type", ""))
    capability_id = str(form.get("capability_id", ""))
    decision = str(form.get("decision", ""))
    if not capability_type or not capability_id or decision not in (
        "approved",
        "declined",
    ):
        message = "That approval request was incomplete."
        return RedirectResponse(url=f"{destination}?error={quote(message)}", status_code=303)

    repo = DefinitionRepository(pool)
    recorded = await repo.upsert_capability_consent(
        org_id=str(user.organization_id),
        agent_id=agent_id,
        user_id=str(user.id),
        capability_type=capability_type,
        capability_id=capability_id,
        state=decision,
    )
    if not recorded:
        message = "That capability is no longer attached to this agent."
        return RedirectResponse(url=f"{destination}?error={quote(message)}", status_code=303)
    return RedirectResponse(url=destination, status_code=303)


@router.post("/definitions/agents/{agent_id}/consent/bulk")
async def bulk_capability_consent_web(request: Request, agent_id: str):
    """Approve or decline every pending capability on one agent.

    Bulk is safe because the operation is strictly per-user: it cannot grant
    anyone else anything.
    """
    user = await get_user_context(request)
    await _check_csrf(request)
    form = await request.form()
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    destination = f"/definitions/agents/{agent_id}"
    decision = str(form.get("decision", ""))
    if decision not in ("approved", "declined"):
        message = "Choose approve or decline."
        return RedirectResponse(url=f"{destination}?error={quote(message)}", status_code=303)

    repo = DefinitionRepository(pool)
    pending = await repo.list_pending_capability_consents(
        str(user.organization_id), str(user.id)
    )
    applied = 0
    for item in pending:
        if str(item["agent_id"]) != str(agent_id):
            continue
        if await repo.upsert_capability_consent(
            org_id=str(user.organization_id),
            agent_id=agent_id,
            user_id=str(user.id),
            capability_type=str(item["capability_type"]),
            capability_id=str(item["capability_id"]),
            state=decision,
        ):
            applied += 1
    return RedirectResponse(url=destination, status_code=303)


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
