"""Repository for agent, skill, MCP server, hook, and managed tool definitions.

Handles CRUD, approval workflow, and access grants.

*Use* of definitions (pickers, task/schedule dropdowns, definition tools) is
clearance-driven and default-deny: a definition is usable only when the
principal has an explicit read clearance — its owner or owner-group, an
explicit user/group grant, a built-in's automatic org-wide grant (removable
by an org owner/admin), or a whole-org grant. Management surfaces (definitions
UI, approvals, grants) stay on the org-scoped legacy path, role-gated at the
routes.

Runtime composition follows the same rule, applied through the agent. A
capability (skill, managed tool, hook, MCP server) reaches a running agent only
when it is *attached* to that agent, the *requesting user* can read it, and
that user has *consented* (or is exempt: built-in, the attacher, or its owner).
The agent is the structural hub; see ``lucent.agent_capabilities``.
"""

import copy
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Final
from urllib.parse import urlsplit
from uuid import UUID

from asyncpg import Pool
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from lucent.db.admin_audit import (
    DEFINITION_APPROVE as ADMIN_DEFINITION_APPROVE,
)
from lucent.db.admin_audit import (
    DEFINITION_CREATE as ADMIN_DEFINITION_CREATE,
)
from lucent.db.admin_audit import (
    DEFINITION_DELETE as ADMIN_DEFINITION_DELETE,
)
from lucent.db.admin_audit import (
    DEFINITION_GRANT as ADMIN_DEFINITION_GRANT,
)
from lucent.db.admin_audit import (
    DEFINITION_REJECT as ADMIN_DEFINITION_REJECT,
)
from lucent.db.admin_audit import (
    DEFINITION_REVOKE as ADMIN_DEFINITION_REVOKE,
)
from lucent.db.admin_audit import (
    DEFINITION_UPDATE as ADMIN_DEFINITION_UPDATE,
)
from lucent.db.admin_audit import (
    AdminAuditRepository,
    resolve_audit_actor,
)
from lucent.db.audit import (
    DEFINITION_APPROVE,
    DEFINITION_CREATE,
    DEFINITION_DELETE,
    DEFINITION_GRANT,
    DEFINITION_REJECT,
    DEFINITION_REVOKE,
    DEFINITION_UPDATE,
    AuditRepository,
)
from lucent.db.pool import (
    AuthorizedDatabasePool,
    AuthTablePolicy,
    get_authorized_pool_for_user,
    scoped_acquire_on,
)

logger = logging.getLogger(__name__)

AGENT_AUTH_POLICY: Final[AuthTablePolicy] = AuthTablePolicy("agent_definitions", direct_columns=())
SKILL_AUTH_POLICY: Final[AuthTablePolicy] = AuthTablePolicy("skill_definitions", direct_columns=())
MCP_SERVER_AUTH_POLICY: Final[AuthTablePolicy] = AuthTablePolicy("mcp_server_configs", direct_columns=())
HOOK_AUTH_POLICY: Final[AuthTablePolicy] = AuthTablePolicy("hook_definitions", direct_columns=())
MANAGED_TOOL_AUTH_POLICY: Final[AuthTablePolicy] = AuthTablePolicy("managed_tool_definitions", direct_columns=())
DEFINITIONS_AUTH_POLICIES: Final[tuple[AuthTablePolicy, ...]] = (
    AGENT_AUTH_POLICY,
    SKILL_AUTH_POLICY,
    MCP_SERVER_AUTH_POLICY,
    HOOK_AUTH_POLICY,
    MANAGED_TOOL_AUTH_POLICY,
)


async def get_authorized_definitions_pool(
    pool: Pool,
    user: dict[str, Any],
    required_clearance: str = "read",
) -> AuthorizedDatabasePool:
    """Return a pool whose definition reads are clearance-gated (usage path).

    Default deny: only definitions the principal is explicitly cleared for
    resolve here — their own or their group's (via the migration-123 owner
    triggers), built-ins with their automatic org-wide grant (removable by an
    org owner/admin), and explicit user/group/org grants. Management surfaces
    stay on the org-scoped legacy path; they are role-gated at the routes.
    """
    return await get_authorized_pool_for_user(
        pool,
        user,
        DEFINITIONS_AUTH_POLICIES,
        required_clearance,
    )

BUILTIN_PROTECTION_MSG = (
    "Built-in definitions cannot be modified by the daemon. "
    "Update the on-disk source file instead."
)

VALID_HOOK_TRIGGER_EVENTS = frozenset({
    "tool_call",  # legacy alias for before_tool_call
    "before_model_call",
    "after_model_call",
    "before_tool_call",
    "after_tool_call",
})

_JSON_SCHEMA_TYPES = frozenset({
    "array", "boolean", "integer", "null", "number", "object", "string",
})


def _normalize_json_schema(value: Any) -> Any:
    if isinstance(value, dict):
        normalized = {key: _normalize_json_schema(item) for key, item in value.items()}
        schema_type = normalized.get("type")
        if isinstance(schema_type, str) and schema_type.lower() in _JSON_SCHEMA_TYPES:
            normalized["type"] = schema_type.lower()
        elif isinstance(schema_type, list):
            normalized["type"] = [
                item.lower()
                if isinstance(item, str) and item.lower() in _JSON_SCHEMA_TYPES
                else item
                for item in schema_type
            ]
        return normalized
    if isinstance(value, list):
        return [_normalize_json_schema(item) for item in value]
    return value


def _prepare_json_schema(schema: dict | None, field_name: str) -> dict | None:
    if schema is None:
        return None
    normalized = _normalize_json_schema(schema)
    try:
        validator_for(normalized).check_schema(normalized)
    except SchemaError as exc:
        raise ValueError(f"{field_name} is not valid JSON Schema: {exc.message}") from exc
    return normalized
VALID_HOOK_ACTION_TYPES = frozenset({"memory_lookup", "message_memory_lookup", "static_context", "command"})
DEFAULT_AGENT_HOOK_NAMES = ("file-memory-lookup", "message-memory-lookup")


def _parse_frontmatter_list(frontmatter: str, key: str) -> list[str]:
    """Parse one block-style YAML list from on-disk definition frontmatter.

    Deliberately not a YAML parser: built-in syncing must run before (and
    independently of) anything that imports a YAML dependency, and the shipped
    files use one simple shape — ``key:`` followed by ``  - value`` lines. This
    is the parsing rule ``skill_names`` has always used, factored out so
    ``tool_names`` binds through exactly the same path.
    """
    values: list[str] = []
    in_list = False
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if stripped in (f"{key}:", f"{key}: []"):
            in_list = stripped == f"{key}:"
            continue
        if not in_list:
            continue
        match = re.match(r"\s+-\s+(.+)", line)
        if match:
            values.append(match.group(1).strip())
        elif stripped:
            # Any other top-level (unindented or scalar) line ends the list.
            in_list = False
    return values


# ── per-runtime-kind tool validation ──────────────────────────────────────
#
# These run at the repository's write boundary so a definition that cannot work
# is rejected while its author is looking at the form, not on the first call.

_ALLOWED_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})
# A declarative call has no sandbox around it, so its ceiling is the outbound
# transport's own, not the 3600s a container may take.
_MAX_DECLARATIVE_TIMEOUT = 120
# Guard against a spec so large it costs more to parse than the tool is worth.
_MAX_SPEC_BYTES = 2_000_000


def _validate_python_tool(*, source_code: str, entrypoint: str, config: dict) -> None:
    if not source_code or not source_code.strip():
        raise ValueError("source_code is required for a python tool")
    if not entrypoint or not entrypoint.isidentifier():
        raise ValueError("entrypoint must be a Python identifier")
    image = config.get("image")
    if image is not None and not isinstance(image, str):
        raise ValueError("runtime_config.image must be a string")


def _validate_http_tool(*, config: dict) -> None:
    url = config.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError(
            "runtime_config.url is required for an http tool "
            "(for example https://api.example.com/v1/weather/{city})"
        )
    parsed = urlsplit(url.strip())
    # A relative URL has no scheme, so this check is also what stops a
    # scheme-relative `//host/path` from being sent to an arbitrary host.
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("runtime_config.url must be an absolute http(s) URL")
    if not parsed.netloc:
        raise ValueError("runtime_config.url must include a hostname")
    # Credentials must never be baked into a stored URL: they belong in env_vars
    # as a reference, which the runtime resolves just in time. (`urlsplit` has no
    # `userinfo` attribute; `username`/`password` are the public accessors.)
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise ValueError(
            "runtime_config.url must not contain credentials; put them in env_vars "
            "as a credential:// or secret:// reference and use {{env:NAME}} in the "
            "template"
        )

    method = str(config.get("method") or "GET").strip().upper()
    if method not in _ALLOWED_HTTP_METHODS:
        raise ValueError(
            "runtime_config.method must be one of: "
            + ", ".join(sorted(_ALLOWED_HTTP_METHODS))
        )
    for key in ("query", "headers", "body", "form"):
        value = config.get(key)
        if value is None:
            continue
        if key == "body" and isinstance(value, str):
            continue
        if not isinstance(value, dict):
            raise ValueError(f"runtime_config.{key} must be a JSON object")
    if config.get("body") is not None and config.get("form") is not None:
        raise ValueError("runtime_config may set 'body' or 'form', not both")
    response_format = config.get("response_format")
    if response_format is not None and response_format not in ("json", "text"):
        raise ValueError("runtime_config.response_format must be 'json' or 'text'")
    response_path = config.get("response_path")
    if response_path is not None and not isinstance(response_path, str):
        raise ValueError("runtime_config.response_path must be a string")
    _validate_allowed_status(config.get("allowed_status"))
    _validate_declarative_timeout(config.get("timeout_seconds"))
    _validate_credential_headers(config.get("credential_headers"))


def _validate_openapi_tool(*, config: dict) -> None:
    from lucent.services.tool_runtimes.openapi_spec import SpecError, parse_spec, resolve_operation

    spec = config.get("spec")
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError(
            "runtime_config.spec is required for an openapi tool. The document is "
            "stored at approval time so an approved tool cannot change behaviour "
            "underneath the model."
        )
    if len(spec.encode("utf-8")) > _MAX_SPEC_BYTES:
        raise ValueError(
            f"runtime_config.spec exceeds the {_MAX_SPEC_BYTES} byte limit; create "
            "per-operation tools for the ones you need instead of storing a large "
            "document"
        )
    try:
        document = parse_spec(spec)
        resolve_operation(
            document,
            operation_id=config.get("operation_id"),
            method=config.get("method"),
            path=config.get("path"),
        )
    except SpecError as exc:
        raise ValueError(str(exc)) from exc

    base_url = config.get("base_url")
    if base_url is not None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("runtime_config.base_url must be a non-empty string")
        parsed = urlsplit(base_url.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("runtime_config.base_url must be an absolute http(s) URL")
        if parsed.username or parsed.password or "@" in parsed.netloc:
            raise ValueError(
                "runtime_config.base_url must not contain credentials; use env_vars "
                "with a credential:// or secret:// reference instead"
            )
    response_format = config.get("response_format")
    if response_format is not None and response_format not in ("json", "text"):
        raise ValueError("runtime_config.response_format must be 'json' or 'text'")
    _validate_allowed_status(config.get("allowed_status"))
    _validate_declarative_timeout(config.get("timeout_seconds"))
    _validate_credential_headers(config.get("credential_headers"))


def _validate_builtin_tool(*, config: dict, input_schema: dict | None) -> None:
    from lucent.services.builtin_tool_catalog import get_builtin_tool

    handler = config.get("handler")
    if not isinstance(handler, str) or not handler.strip():
        raise ValueError(
            "runtime_config.handler is required for a builtin tool (see the "
            "built-in tool catalog for available handler names)"
        )
    try:
        get_builtin_tool(handler)
    except Exception as exc:
        from lucent.services.tool_runtimes.base import ToolRuntimeError

        if isinstance(exc, ToolRuntimeError):
            raise ValueError(str(exc)) from exc
        # A genuine internal fault must not be reported as a bad definition.
        raise
    # A builtin's schema comes from the catalog. Refuse a definition that tries to
    # widen it, or an agent could be granted capabilities the reviewer never saw.
    if input_schema is not None:
        from lucent.services.builtin_tool_catalog import BUILTIN_TOOL_CATALOG

        canonical = BUILTIN_TOOL_CATALOG[handler.strip()].input_schema
        if _normalize_json_schema(input_schema) != _normalize_json_schema(canonical):
            raise ValueError(
                f"input_schema must match the catalog schema for handler {handler!r}; "
                "a builtin tool's contract is fixed by Lucent, not by the definition"
            )


def _validate_allowed_status(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list) or not value:
        raise ValueError(
            "runtime_config.allowed_status must be a non-empty JSON array of status codes"
        )
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int):
            raise ValueError(
                "runtime_config.allowed_status entries must be integers"
            )
        if entry < 100 or entry > 599:
            raise ValueError(
                f"runtime_config.allowed_status entry {entry} is not an HTTP status code"
            )


def _validate_declarative_timeout(value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("runtime_config.timeout_seconds must be an integer")
    if value < 1 or value > _MAX_DECLARATIVE_TIMEOUT:
        raise ValueError(
            f"runtime_config.timeout_seconds must be between 1 and "
            f"{_MAX_DECLARATIVE_TIMEOUT} for a tool executed by the server"
        )


def _validate_credential_headers(value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        return
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(
            "runtime_config.credential_headers must be an array of header names"
        )


class BuiltInProtectionError(Exception):
    """Raised when a daemon tries to modify a built-in definition."""

    def __init__(self, msg: str = BUILTIN_PROTECTION_MSG):
        super().__init__(msg)


class GrantOutcome(str, Enum):
    """Result of attaching or detaching a capability on an agent.

    Three distinct outcomes instead of a bool, because callers must tell
    "you did it" from "someone already did it" from "you may not". Truthiness
    is preserved for the older ``if not await repo.grant_*()`` call sites:
    ``ATTACHED`` and ``ALREADY_ATTACHED`` are truthy, ``DENIED``/``FAILED``
    are falsy. An attachment that exists and that the caller was allowed to
    create is a success either way — the distinction is for messaging.
    """

    ATTACHED = "attached"
    ALREADY_ATTACHED = "already_attached"
    DENIED = "denied"
    FAILED = "failed"

    def __bool__(self) -> bool:
        return self in (GrantOutcome.ATTACHED, GrantOutcome.ALREADY_ATTACHED)

    @property
    def ok(self) -> bool:
        """True when the capability is attached to the agent after this call."""
        return bool(self)


@dataclass(frozen=True)
class CapabilitySpec:
    """One capability family's storage locations.

    ``capability_type`` is the value stored in
    ``agent_capability_consents.capability_type`` and the ``definition_type``
    recorded in the audit stream.
    """

    capability_type: str
    target_table: str
    junction_table: str
    fk_column: str      # column on the junction table that holds the capability
    agent_column: str   # column on the junction table that holds the agent
    consent_column: str  # column on agent_capability_consents for this family
    has_config_override: bool = False


CAPABILITY_SPECS: Final[tuple[CapabilitySpec, ...]] = (
    CapabilitySpec(
        capability_type="skill",
        target_table="skill_definitions",
        junction_table="agent_skills",
        fk_column="skill_id",
        agent_column="agent_id",
        consent_column="skill_id",
    ),
    CapabilitySpec(
        capability_type="managed_tool",
        target_table="managed_tool_definitions",
        junction_table="agent_managed_tools",
        fk_column="tool_id",
        agent_column="agent_id",
        consent_column="managed_tool_id",
        has_config_override=True,
    ),
    CapabilitySpec(
        capability_type="hook",
        target_table="hook_definitions",
        junction_table="agent_hooks",
        fk_column="hook_id",
        agent_column="agent_id",
        consent_column="hook_id",
        has_config_override=True,
    ),
    CapabilitySpec(
        capability_type="mcp_server",
        target_table="mcp_server_configs",
        junction_table="agent_mcp_servers",
        fk_column="mcp_server_id",
        agent_column="agent_id",
        consent_column="mcp_server_id",
    ),
)

CAPABILITY_SPEC_BY_TYPE: Final[dict[str, CapabilitySpec]] = {
    spec.capability_type: spec for spec in CAPABILITY_SPECS
}

# Aliases accepted from callers/UI. Kept narrow on purpose: anything else is a
# programming error and should raise rather than silently no-op.
_CAPABILITY_TYPE_ALIASES: Final[dict[str, str]] = {
    "skill": "skill",
    "skills": "skill",
    "managed_tool": "managed_tool",
    "managed_tools": "managed_tool",
    "tool": "managed_tool",
    "tools": "managed_tool",
    "hook": "hook",
    "hooks": "hook",
    "mcp_server": "mcp_server",
    "mcp_servers": "mcp_server",
    "mcp": "mcp_server",
}


def normalize_capability_type(value: str) -> str:
    """Map a caller-facing capability name onto its canonical type."""
    key = (value or "").strip().lower()
    try:
        return _CAPABILITY_TYPE_ALIASES[key]
    except KeyError:
        raise ValueError(f"Unsupported capability type: {value}") from None


CONSENT_STATES: Final[frozenset[str]] = frozenset({"approved", "declined"})


class DefinitionRepository:
    """Repository for managing agent, skill, and MCP server definitions."""

    # Map memory-audit definition event constants → admin-audit action names.
    # Keys are the action_type values carried by AuditRepository.log_definition_event.
    _ADMIN_ACTION_MAP = {
        DEFINITION_CREATE: ADMIN_DEFINITION_CREATE,
        DEFINITION_UPDATE: ADMIN_DEFINITION_UPDATE,
        DEFINITION_APPROVE: ADMIN_DEFINITION_APPROVE,
        DEFINITION_REJECT: ADMIN_DEFINITION_REJECT,
        DEFINITION_DELETE: ADMIN_DEFINITION_DELETE,
        DEFINITION_GRANT: ADMIN_DEFINITION_GRANT,
        DEFINITION_REVOKE: ADMIN_DEFINITION_REVOKE,
    }

    # Mechanical update paths that mutate DB rows but are not admin actions:
    # MCP tool-discovery caching refreshes discovered_tools on TTL expiry.
    _AUDIT_EXEMPT_CONTEXT_FIELDS = frozenset(
        {"discovered_tools", "tools_discovered_at"}
    )

    def __init__(
        self,
        pool: Pool,
        audit_repo: AuditRepository | None = None,
        *,
        admin_audit: bool = True,
    ):
        self.pool = pool
        self.audit_repo = audit_repo
        self._admin_audit = admin_audit
        self._admin_audit_repo: AdminAuditRepository | None = None

    @staticmethod
    def _role_value(role: str | None) -> str:
        return role or "member"

    @staticmethod
    def _definition_access_condition(
        alias: str,
        user_param: str,
        role_param: str,
    ) -> str:
        """Return the requester ACL for private and organization-shared definitions.

        This is the MANAGEMENT/legacy path (definitions CRUD, detail, grants,
        proposals): org admin/owner roles keep the legacy org-wide view over
        every instance definition in their org.
        Usage paths don't go through here — they are clearance-driven via the
        ``*_accessible_by`` listers on authorized pools (default-deny).
        """
        prefix = f"{alias}." if alias else ""
        return (
            f"({role_param} IN ('admin', 'owner') "
            f"OR {prefix}scope = 'built-in' "
            f"OR {prefix}owner_user_id = {user_param} "
            f"OR {prefix}owner_group_id IN ("
            f"SELECT group_id FROM user_groups WHERE user_id = {user_param}) "
            f"OR ({prefix}scope = 'instance' "
            f"AND {prefix}owner_user_id IS NULL "
            f"AND {prefix}owner_group_id IS NULL "
            f"AND ({prefix}status NOT IN ('proposed', 'owner_approved') "
            f"OR {role_param} IN ('admin', 'owner'))))"
        )

    async def _default_owner_user_id(
        self,
        *,
        created_by: str | None,
        org_id: str,
        scope: str,
        shared_with_org: bool = False,
    ) -> str | None:
        """Default ownership for new instance definitions.

        Human-created definitions default to the creator. Daemon-created
        instance definitions are owned by the org's owner user — the daemon
        is an actor, not a capability owner, but every instance row should
        end up with an owner attached so its clearance exists and admins
        have someone to grant from (Kyle, 2026-09-30).
        """
        if scope == "built-in" or shared_with_org or not created_by:
            return None
        try:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                role = await conn.fetchval(
                    "SELECT role FROM users WHERE id = $1 AND organization_id = $2",
                    UUID(created_by),
                    UUID(org_id),
                )
                if role == "daemon":
                    owner_row = await conn.fetchrow(
                        "SELECT id FROM users "
                        "WHERE organization_id = $1 AND role = 'owner' "
                        "ORDER BY created_at LIMIT 1",
                        UUID(org_id),
                    )
                    return str(owner_row["id"]) if owner_row else None
        except Exception:
            role = None
        return created_by

    @staticmethod
    def _execute_count(result: str) -> int:
        try:
            return int(result.rsplit(" ", 1)[-1])
        except (ValueError, AttributeError):
            return 0

    @staticmethod
    def _decode_json(value: Any, default: Any = None) -> Any:
        if value is None:
            return default
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                if isinstance(decoded, str):
                    try:
                        return json.loads(decoded)
                    except (TypeError, ValueError):
                        return decoded
                return decoded
            except (TypeError, ValueError):
                return default
        return value

    @classmethod
    def _normalize_definition_row(cls, row: Any | None) -> dict | None:
        if not row:
            return None
        data = dict(row)
        data["proposal_evidence"] = cls._decode_json(data.get("proposal_evidence"), {}) or {}
        return data

    @classmethod
    def _normalize_mcp_row(cls, row: Any | None) -> dict | None:
        data = cls._normalize_definition_row(row)
        if not data:
            return None
        for key, default in (
            ("headers", {}),
            ("env_vars", {}),
            ("args", []),
            ("discovered_tools", None),
            ("allowed_tools", None),
            # Declared per-user credential slots. Empty means "none declared",
            # which is the pre-migration behaviour for every existing row.
            ("credential_slots", []),
        ):
            if key in data:
                data[key] = cls._decode_json(data.get(key), default)
        return data

    @classmethod
    def _normalize_hook_row(cls, row: Any | None) -> dict | None:
        if not row:
            return None
        data = dict(row)
        data["config"] = cls._decode_json(data.get("config"), {}) or {}
        data["proposal_evidence"] = cls._decode_json(data.get("proposal_evidence"), {}) or {}
        if "config_override" in data:
            data["config_override"] = cls._decode_json(
                data.get("config_override"), None
            )
        return data

    @classmethod
    def _normalize_tool_row(cls, row: Any | None) -> dict | None:
        if not row:
            return None
        data = dict(row)
        for key, default in (
            ("input_schema", {"type": "object", "properties": {}}),
            ("output_schema", None),
            ("requirements", []),
            ("runtime_config", {}),
            ("discovered_tools", None),
            ("env_vars", {}),
            ("auth_policy", {"mode": "agent_grant", "require_user_access": True}),
            ("network_policy", {"network_mode": "none", "allowed_hosts": []}),
            ("resource_limits", {
                "memory_limit": "512m", "cpu_limit": 1.0,
                "disk_limit": "1g", "timeout_seconds": 300,
            }),
            ("proposal_evidence", {}),
        ):
            if key in data:
                data[key] = cls._decode_json(data.get(key), default)
        if "config_override" in data:
            data["config_override"] = cls._decode_json(data.get("config_override"), None)
        return data

    @staticmethod
    def _validate_hook_shape(trigger_event: str, action_type: str, config: dict | None) -> None:
        if trigger_event not in VALID_HOOK_TRIGGER_EVENTS:
            valid = ", ".join(sorted(VALID_HOOK_TRIGGER_EVENTS))
            raise ValueError(f"Invalid trigger_event '{trigger_event}'. Must be one of: {valid}")
        if action_type not in VALID_HOOK_ACTION_TYPES:
            valid = ", ".join(sorted(VALID_HOOK_ACTION_TYPES))
            raise ValueError(f"Invalid action_type '{action_type}'. Must be one of: {valid}")
        if config is not None and not isinstance(config, dict):
            raise ValueError("config must be a JSON object")

    @staticmethod
    def _validate_tool_shape(
        *,
        input_schema: dict | None,
        output_schema: dict | None,
        runtime_type: str,
        source_code: str,
        entrypoint: str,
        requirements: list | None,
        runtime_config: dict | None,
        env_vars: dict | None,
        auth_policy: dict | None,
        network_policy: dict | None,
        resource_limits: dict | None,
        timeout_seconds: int,
    ) -> None:
        """Validate a tool definition against its runtime kind's contract.

        Each kind has a different notion of "complete", and validating them
        together is how a `python` definition acquires an `http` field that is
        silently ignored (or an `http` definition is rejected for having no
        source code). The per-kind rules live here — the repository is the write
        boundary, so this is where a malformed definition must fail, not at call
        time.
        """
        from lucent.services.tool_runtimes import runtime_types

        if input_schema is not None and not isinstance(input_schema, dict):
            raise ValueError("input_schema must be a JSON object")
        if output_schema is not None and not isinstance(output_schema, dict):
            raise ValueError("output_schema must be a JSON object when provided")

        kind = str(runtime_type or "").strip().lower()
        if kind not in runtime_types():
            raise ValueError(
                f"runtime_type must be one of: {', '.join(runtime_types())}"
            )
        if requirements is not None and not isinstance(requirements, list):
            raise ValueError("requirements must be a JSON array")
        for name, value in (
            ("runtime_config", runtime_config),
            ("env_vars", env_vars),
            ("auth_policy", auth_policy),
            ("network_policy", network_policy),
            ("resource_limits", resource_limits),
        ):
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"{name} must be a JSON object")
        from lucent.secrets.utils import validate_env_var_references

        validate_env_var_references(env_vars)
        network_mode = (network_policy or {}).get("network_mode", "none")
        if network_mode not in {"none", "bridge", "allowlist"}:
            raise ValueError("network_policy.network_mode must be none, bridge, or allowlist")
        allowed_hosts = (network_policy or {}).get("allowed_hosts", [])
        if allowed_hosts is not None and not isinstance(allowed_hosts, list):
            raise ValueError("network_policy.allowed_hosts must be a JSON array")
        from lucent.sandbox.models import validate_extra_hosts

        try:
            validate_extra_hosts((network_policy or {}).get("extra_hosts") or {})
        except ValueError as exc:
            raise ValueError(f"network_policy.{exc}") from exc
        if timeout_seconds < 1 or timeout_seconds > 3600:
            raise ValueError("timeout_seconds must be between 1 and 3600")

        config = runtime_config or {}
        if kind == "python":
            _validate_python_tool(source_code=source_code, entrypoint=entrypoint, config=config)
        elif kind == "http":
            _validate_http_tool(config=config)
        elif kind == "openapi":
            _validate_openapi_tool(config=config)
        elif kind == "builtin":
            _validate_builtin_tool(config=config, input_schema=input_schema)

    async def _check_builtin_protection(
        self,
        table: str,
        item_id: str,
        org_id: str,
        requester_role: str | None,
    ) -> None:
        """Raise BuiltInProtectionError if a daemon tries to modify a built-in object.

        Built-in objects have their source of truth on disk (.github/agents/definitions/,
        .github/skills/, etc.).  DB-only updates are ephemeral — they get overwritten on
        server restart when files are reloaded.  The daemon should be blocked from making
        changes it cannot persist.

        Admin and owner roles are still allowed to update built-in objects.
        """
        if requester_role != "daemon":
            return
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            scope = await conn.fetchval(
                f"SELECT scope FROM {table} WHERE id = $1 AND organization_id = $2",
                item_id, org_id,
            )
        if scope == "built-in":
            raise BuiltInProtectionError()

    async def _audit(
        self,
        event_type: str,
        org_id: str,
        definition_type: str,
        definition_id: str,
        user_id: str | None = None,
        context: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> None:
        """Fire-and-forget audit logging on every definition mutation.

        Two independent surfaces, neither of which may break the mutation:

        * The memory-centric audit stream (``memory_audit_log``) via the
          optional ``audit_repo``. When none is wired, a lazily-constructed
          ``AuditRepository`` over the same pool is used so MCP-tool and
          daemon callers keep their historical audit coverage.
        * The admin/security audit (``admin_audit_log``), default-on. Actor
          attribution falls back to the request-scoped auth context
          (``lucent.auth.get_current_user``) so the MCP/daemon paths — which
          construct this repository without an explicit actor — still record
          who mutated what. Unauthenticated (system-originated) mutations are
          logged with a NULL actor and ``actor_source=system``.
        """
        audit = self.audit_repo
        if audit is None:
            try:
                audit = AuditRepository(self.pool)
            except Exception:
                logger.debug("Memory audit fallback unavailable", exc_info=True)
        if audit is not None:
            try:
                await audit.log_definition_event(
                    event_type=event_type,
                    organization_id=UUID(org_id),
                    user_id=UUID(user_id) if user_id else None,
                    definition_type=definition_type,
                    definition_id=UUID(definition_id),
                    context=context,
                    notes=notes,
                )
            except Exception:
                logger.warning(
                    "Audit log failed for %s on %s %s",
                    event_type,
                    definition_type,
                    definition_id,
                    exc_info=True,
                )

        # Admin audit: dual-fire alongside the memory audit stream (default-on).
        if self._admin_audit:
            await self._log_admin_audit(
                event_type, org_id, definition_type, definition_id,
                user_id=user_id, context=context, notes=notes,
            )

    async def _log_admin_audit(
        self,
        event_type: str,
        org_id: str,
        definition_type: str,
        definition_id: str,
        user_id: str | None = None,
        context: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> None:
        """Write the admin_audit_log row. Never raises.

        Actor resolution order: explicit ``user_id`` argument, then the
        request-scoped auth context, then a NULL (system) actor. Any
        attribution failure degrades to the system actor instead of
        breaking the mutation or dropping the row.
        """
        # Mechanical caching updates are not admin actions. The exemption
        # list describes updated-FIELD names carried by discovery-cache
        # writes; apply it to that list, not to the context keys themselves,
        # so a manual admin update that happens to set a cached field is
        # still audited.
        if isinstance(context, dict):
            updated = context.get("updated_fields")
            if (
                isinstance(updated, (list, tuple, set))
                and self._AUDIT_EXEMPT_CONTEXT_FIELDS.intersection(updated)
                and "tool_count" in context
            ):
                return
        ctx: dict[str, Any] = dict(context) if context else {}
        actor_user_id, impersonator_user_id, auth_ctx = resolve_audit_actor(user_id)
        ctx.update(auth_ctx)

        if self._admin_audit_repo is None:
            self._admin_audit_repo = AdminAuditRepository(self.pool)
        try:
            await self._admin_audit_repo.log_definition_event(
                event_type=self._ADMIN_ACTION_MAP[event_type],
                organization_id=UUID(org_id),
                user_id=actor_user_id,
                impersonator_user_id=impersonator_user_id,
                definition_type=definition_type,
                definition_id=UUID(definition_id),
                context=ctx,
                notes=notes,
            )
        except Exception:
            logger.warning(
                "Admin audit failed for %s on %s %s",
                event_type,
                definition_type,
                definition_id,
                exc_info=True,
            )

    # ── Agents ────────────────────────────────────────────────────────────

    async def list_agents(
        self,
        org_id: str,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict:
        base = """
            FROM agent_definitions
            WHERE organization_id = $1
        """
        params: list[Any] = [org_id]
        if requester_user_id:
            params.extend([requester_user_id, self._role_value(requester_role)])
            uid_idx = len(params) - 1
            role_idx = len(params)
            base += " AND " + self._definition_access_condition(
                "", f"${uid_idx}", f"${role_idx}"
            )
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"""
                 SELECT id, name, description, status, scope,
                     created_by, approved_by, approved_at,
                   owner_user_id, owner_group_id,
                     proposal_reason, proposal_evidence,
                   created_at, updated_at
            {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        params_with_page = [*params, limit, offset]

        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, *params_with_page)
        return {
            "items": [self._normalize_definition_row(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def get_agent(
        self,
        agent_id: str,
        org_id: str,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict | None:
        acl_sql = ""
        params: list[Any] = [agent_id, org_id]
        if requester_user_id:
            params.extend([requester_user_id, self._role_value(requester_role)])
            acl_sql = " AND " + self._definition_access_condition(
                "a", "$3", "$4"
            )
        query = """
            SELECT a.*,
                array_agg(DISTINCT s.name) FILTER (WHERE s.name IS NOT NULL) as skill_names,
                array_agg(DISTINCT m.name) FILTER (WHERE m.name IS NOT NULL) as mcp_server_names,
                array_agg(DISTINCT h.name) FILTER (WHERE h.name IS NOT NULL) as hook_names,
                array_agg(DISTINCT t.name) FILTER (WHERE t.name IS NOT NULL) as tool_names
            FROM agent_definitions a
            LEFT JOIN agent_skills ags ON a.id = ags.agent_id
            LEFT JOIN skill_definitions s ON ags.skill_id = s.id
            LEFT JOIN agent_mcp_servers agm ON a.id = agm.agent_id
            LEFT JOIN mcp_server_configs m ON agm.mcp_server_id = m.id
            LEFT JOIN agent_hooks ah ON a.id = ah.agent_id
            LEFT JOIN hook_definitions h ON ah.hook_id = h.id
            LEFT JOIN agent_managed_tools amt ON a.id = amt.agent_id
            LEFT JOIN managed_tool_definitions t ON amt.tool_id = t.id
            WHERE a.id = $1 AND a.organization_id = $2
        """ + acl_sql + """
            GROUP BY a.id
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, *params)
        return self._normalize_definition_row(row)

    async def create_agent(
        self,
        name: str,
        description: str,
        content: str,
        org_id: str,
        created_by: str,
        status: str = "proposed",
        scope: str = "instance",
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
        shared_with_org: bool = False,
        proposal_reason: str | None = None,
        proposal_evidence: dict | None = None,
    ) -> dict:
        # Default owner to creator when no explicit ownership is provided
        if owner_user_id is None and owner_group_id is None:
            owner_user_id = await self._default_owner_user_id(
                created_by=created_by,
                org_id=org_id,
                scope=scope,
                shared_with_org=shared_with_org,
            )
        query = """
            INSERT INTO agent_definitions (name, description, content, status, scope,
                created_by, organization_id, owner_user_id, owner_group_id,
                proposal_reason, proposal_evidence)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                query, name, description, content, status, scope, created_by, org_id,
                owner_user_id, owner_group_id, proposal_reason,
                json.dumps(proposal_evidence or {}),
            )
        result = self._normalize_definition_row(row)
        await self._audit(
            DEFINITION_CREATE, org_id, "agent", str(result["id"]),
            user_id=created_by, notes=f"Created agent '{name}'",
        )
        await self.grant_default_hooks_to_agent(str(result["id"]), org_id)
        return result

    async def update_agent(
        self, agent_id: str, org_id: str, *, requester_role: str | None = None, **kwargs,
    ) -> dict | None:
        await self._check_builtin_protection(
            "agent_definitions", agent_id, org_id, requester_role,
        )
        sets = []
        params: list[Any] = []
        for key in ("name", "description", "content", "status", "owner_user_id", "owner_group_id"):
            if key in kwargs:
                params.append(kwargs[key])
                sets.append(f"{key} = ${len(params)}")
        if not sets:
            return await self.get_agent(agent_id, org_id)
        params.append(datetime.now(timezone.utc))
        sets.append(f"updated_at = ${len(params)}")
        params.append(agent_id)
        params.append(org_id)
        query = f"""
            UPDATE agent_definitions SET {", ".join(sets)}
            WHERE id = ${len(params) - 1} AND organization_id = ${len(params)}
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, *params)
        result = self._normalize_definition_row(row)
        if result:
            await self._audit(
                DEFINITION_UPDATE, org_id, "agent", agent_id,
                context={
                    "updated_fields": [
                        k for k in ("name", "description", "content", "status",
                                     "owner_user_id", "owner_group_id")
                        if k in kwargs
                    ],
                },
                notes=f"Updated agent '{agent_id}'",
            )
        return result

    async def approve_agent(self, agent_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE agent_definitions
            SET status = 'active', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, agent_id, org_id, approved_by)
        result = self._normalize_definition_row(row)
        if result:
            await self._audit(
                DEFINITION_APPROVE, org_id, "agent", agent_id,
                user_id=approved_by, notes=f"Approved agent '{agent_id}'",
            )
        return result

    async def reject_agent(self, agent_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE agent_definitions
            SET status = 'rejected', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, agent_id, org_id, approved_by)
        result = self._normalize_definition_row(row)
        if result:
            await self._audit(
                DEFINITION_REJECT, org_id, "agent", agent_id,
                user_id=approved_by, notes=f"Rejected agent '{agent_id}'",
            )
        return result

    async def delete_agent(self, agent_id: str, org_id: str) -> bool:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            result = await conn.execute(
                "DELETE FROM agent_definitions WHERE id = $1 AND organization_id = $2",
                agent_id,
                org_id,
            )
        deleted = result == "DELETE 1"
        if deleted:
            await self._audit(
                DEFINITION_DELETE, org_id, "agent", agent_id,
                notes=f"Deleted agent '{agent_id}'",
            )
        return deleted

    # ── Skills ────────────────────────────────────────────────────────────

    async def list_skills(
        self,
        org_id: str,
        status: str | None = None,
        search: str | None = None,
        limit: int = 25,
        offset: int = 0,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict:
        base = """
            FROM skill_definitions
            WHERE organization_id = $1
        """
        params: list[Any] = [org_id]
        if requester_user_id:
            params.extend([requester_user_id, self._role_value(requester_role)])
            uid_idx = len(params) - 1
            role_idx = len(params)
            base += " AND " + self._definition_access_condition(
                "", f"${uid_idx}", f"${role_idx}"
            )
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"
        if search:
            term = search.strip()
            if term:
                params.append(f"%{term}%")
                base += (
                    f" AND (name ILIKE ${len(params)} "
                    f"OR description ILIKE ${len(params)} "
                    f"OR content ILIKE ${len(params)})"
                )

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"""
                 SELECT id, name, description, status, scope,
                     created_by, approved_by, approved_at,
                                     owner_user_id, owner_group_id,
                                     proposal_reason, proposal_evidence,
                   created_at, updated_at
            {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        params_with_page = [*params, limit, offset]

        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, *params_with_page)
        return {
            "items": [self._normalize_definition_row(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def get_skill(
        self,
        skill_id: str,
        org_id: str,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict | None:
        acl_sql = ""
        params: list[Any] = [skill_id, org_id]
        if requester_user_id:
            params.extend([requester_user_id, self._role_value(requester_role)])
            acl_sql = " AND " + self._definition_access_condition(
                "", "$3", "$4"
            )
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM skill_definitions WHERE id = $1 AND organization_id = $2" + acl_sql,
                *params,
            )
        return self._normalize_definition_row(row)

    async def create_skill(
        self,
        name: str,
        description: str,
        content: str,
        org_id: str,
        created_by: str,
        status: str = "proposed",
        scope: str = "instance",
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
        shared_with_org: bool = False,
        proposal_reason: str | None = None,
        proposal_evidence: dict | None = None,
    ) -> dict:
        # Default owner to creator when no explicit ownership is provided
        if owner_user_id is None and owner_group_id is None:
            owner_user_id = await self._default_owner_user_id(
                created_by=created_by,
                org_id=org_id,
                scope=scope,
                shared_with_org=shared_with_org,
            )
        query = """
            INSERT INTO skill_definitions (name, description, content, status, scope,
                created_by, organization_id, owner_user_id, owner_group_id,
                proposal_reason, proposal_evidence)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                query, name, description, content, status, scope, created_by, org_id,
                owner_user_id, owner_group_id, proposal_reason,
                json.dumps(proposal_evidence or {}),
            )
        result = self._normalize_definition_row(row)
        await self._audit(
            DEFINITION_CREATE, org_id, "skill", str(result["id"]),
            user_id=created_by, notes=f"Created skill '{name}'",
        )
        return result

    async def approve_skill(self, skill_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE skill_definitions
            SET status = 'active', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, skill_id, org_id, approved_by)
        result = self._normalize_definition_row(row)
        if result:
            await self._audit(
                DEFINITION_APPROVE, org_id, "skill", skill_id,
                user_id=approved_by, notes=f"Approved skill '{skill_id}'",
            )
        return result

    async def reject_skill(self, skill_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE skill_definitions
            SET status = 'rejected', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, skill_id, org_id, approved_by)
        result = self._normalize_definition_row(row)
        if result:
            await self._audit(
                DEFINITION_REJECT, org_id, "skill", skill_id,
                user_id=approved_by, notes=f"Rejected skill '{skill_id}'",
            )
        return result

    async def delete_skill(self, skill_id: str, org_id: str) -> bool:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            result = await conn.execute(
                "DELETE FROM skill_definitions WHERE id = $1 AND organization_id = $2",
                skill_id,
                org_id,
            )
        deleted = result == "DELETE 1"
        if deleted:
            await self._audit(
                DEFINITION_DELETE, org_id, "skill", skill_id,
                notes=f"Deleted skill '{skill_id}'",
            )
        return deleted

    # ── MCP Servers ───────────────────────────────────────────────────────

    async def list_mcp_servers(
        self,
        org_id: str,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict:
        base = """
            FROM mcp_server_configs
            WHERE organization_id = $1
        """
        params: list[Any] = [org_id]
        if requester_user_id:
            params.extend([requester_user_id, self._role_value(requester_role)])
            uid_idx = len(params) - 1
            role_idx = len(params)
            base += " AND " + self._definition_access_condition(
                "", f"${uid_idx}", f"${role_idx}"
            )
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"""
            SELECT id, name, description, server_type, url, status, scope,
                   created_by, approved_by, approved_at,
                   owner_user_id, owner_group_id,
                     proposal_reason, proposal_evidence,
                   created_at, updated_at
            {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        params_with_page = [*params, limit, offset]

        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, *params_with_page)
        return {
            "items": [self._normalize_mcp_row(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def get_mcp_server(
        self,
        server_id: str,
        org_id: str,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict | None:
        acl_sql = ""
        params: list[Any] = [server_id, org_id]
        if requester_user_id:
            params.extend([requester_user_id, self._role_value(requester_role)])
            acl_sql = " AND " + self._definition_access_condition(
                "", "$3", "$4"
            )
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM mcp_server_configs WHERE id = $1 AND organization_id = $2" + acl_sql,
                *params,
            )
        return self._normalize_mcp_row(row)

    # ── Hooks ─────────────────────────────────────────────────────────────

    async def list_hooks(
        self,
        org_id: str,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict:
        base = """
            FROM hook_definitions
            WHERE organization_id = $1
        """
        params: list[Any] = [org_id]
        if requester_user_id:
            params.extend([requester_user_id, self._role_value(requester_role)])
            uid_idx = len(params) - 1
            role_idx = len(params)
            base += " AND " + self._definition_access_condition(
                "", f"${uid_idx}", f"${role_idx}"
            )
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"""
                 SELECT id, name, description, trigger_event, action_type, status, scope,
                     created_by, approved_by, approved_at, owner_approved_by, owner_approved_at,
                   owner_user_id, owner_group_id,
                     proposal_reason, proposal_evidence,
                   created_at, updated_at
            {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        params_with_page = [*params, limit, offset]

        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, *params_with_page)
        return {
            "items": [self._normalize_hook_row(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def get_hook(
        self,
        hook_id: str,
        org_id: str,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict | None:
        acl_sql = ""
        params: list[Any] = [hook_id, org_id]
        if requester_user_id:
            params.extend([requester_user_id, self._role_value(requester_role)])
            acl_sql = " AND " + self._definition_access_condition(
                "", "$3", "$4"
            )
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM hook_definitions WHERE id = $1 AND organization_id = $2" + acl_sql,
                *params,
            )
        return self._normalize_hook_row(row)

    async def create_hook(
        self,
        name: str,
        description: str,
        trigger_event: str,
        action_type: str,
        config: dict | None,
        org_id: str,
        created_by: str,
        content: str = "",
        status: str = "proposed",
        scope: str = "instance",
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
        shared_with_org: bool = False,
        proposal_reason: str | None = None,
        proposal_evidence: dict | None = None,
    ) -> dict:
        self._validate_hook_shape(trigger_event, action_type, config)
        if owner_user_id is None and owner_group_id is None:
            owner_user_id = await self._default_owner_user_id(
                created_by=created_by,
                org_id=org_id,
                scope=scope,
                shared_with_org=shared_with_org,
            )
        query = """
            INSERT INTO hook_definitions
                (name, description, trigger_event, action_type, content, config,
                 status, scope, created_by, organization_id, owner_user_id, owner_group_id,
                 proposal_reason, proposal_evidence)
            VALUES ($1, $2, $3, $4, $5, $6::text::jsonb, $7, $8, $9, $10, $11, $12,
                    $13, $14::jsonb)
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                query,
                name,
                description,
                trigger_event,
                action_type,
                content,
                json.dumps(config or {}),
                status,
                scope,
                created_by,
                org_id,
                owner_user_id,
                owner_group_id,
                proposal_reason,
                json.dumps(proposal_evidence or {}),
            )
        result = self._normalize_hook_row(row)
        await self._audit(
            DEFINITION_CREATE, org_id, "hook", str(result["id"]),
            user_id=created_by, notes=f"Created hook '{name}'",
        )
        return result

    async def update_hook(
        self, hook_id: str, org_id: str, *, requester_role: str | None = None, **kwargs,
    ) -> dict | None:
        await self._check_builtin_protection(
            "hook_definitions", hook_id, org_id, requester_role,
        )
        current = await self.get_hook(hook_id, org_id)
        trigger_event = kwargs.get(
            "trigger_event", current.get("trigger_event") if current else None
        )
        action_type = kwargs.get("action_type", current.get("action_type") if current else None)
        config = kwargs.get("config", current.get("config") if current else None)
        if trigger_event and action_type:
            self._validate_hook_shape(trigger_event, action_type, config)

        sets = []
        params: list[Any] = []
        for key in (
            "name", "description", "trigger_event", "action_type", "content",
            "status", "owner_user_id", "owner_group_id",
        ):
            if key in kwargs:
                params.append(kwargs[key])
                sets.append(f"{key} = ${len(params)}")
        if "config" in kwargs:
            params.append(json.dumps(kwargs["config"] or {}))
            sets.append(f"config = ${len(params)}::text::jsonb")
        if not sets:
            return current
        params.append(datetime.now(timezone.utc))
        sets.append(f"updated_at = ${len(params)}")
        params.append(hook_id)
        params.append(org_id)
        query = f"""
            UPDATE hook_definitions SET {", ".join(sets)}
            WHERE id = ${len(params) - 1} AND organization_id = ${len(params)}
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, *params)
        result = self._normalize_hook_row(row)
        if result:
            await self._audit(
                DEFINITION_UPDATE, org_id, "hook", hook_id,
                context={"updated_fields": [k for k in kwargs.keys()]},
                notes=f"Updated hook '{hook_id}'",
            )
        return result

    async def approve_hook(
        self,
        hook_id: str,
        org_id: str,
        approved_by: str,
        *,
        require_admin_approval: bool = True,
    ) -> dict | None:
        if require_admin_approval:
            query = """
                UPDATE hook_definitions
                SET status = CASE WHEN status = 'proposed' THEN 'owner_approved' ELSE 'active' END,
                    owner_approved_by = CASE WHEN status = 'proposed' THEN $3 ELSE owner_approved_by END,
                    owner_approved_at = CASE WHEN status = 'proposed' THEN NOW() ELSE owner_approved_at END,
                    approved_by = CASE WHEN status = 'owner_approved' THEN $3 ELSE approved_by END,
                    approved_at = CASE WHEN status = 'owner_approved' THEN NOW() ELSE approved_at END,
                    updated_at = NOW()
                WHERE id = $1 AND organization_id = $2 AND status IN ('proposed', 'owner_approved')
                RETURNING *
            """
        else:
            query = """
                UPDATE hook_definitions
                SET status = 'active', approved_by = $3, approved_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
                RETURNING *
            """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, hook_id, org_id, approved_by)
        result = self._normalize_hook_row(row)
        if result:
            await self._audit(
                DEFINITION_APPROVE, org_id, "hook", hook_id,
                user_id=approved_by,
                context={"status": result["status"]},
                notes=f"Approved hook '{hook_id}'",
            )
        return result

    async def reject_hook(self, hook_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE hook_definitions
            SET status = 'rejected', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status IN ('proposed', 'owner_approved')
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, hook_id, org_id, approved_by)
        result = self._normalize_hook_row(row)
        if result:
            await self._audit(
                DEFINITION_REJECT, org_id, "hook", hook_id,
                user_id=approved_by, notes=f"Rejected hook '{hook_id}'",
            )
        return result

    async def delete_hook(self, hook_id: str, org_id: str) -> bool:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            result = await conn.execute(
                "DELETE FROM hook_definitions WHERE id = $1 AND organization_id = $2",
                hook_id,
                org_id,
            )
        deleted = result == "DELETE 1"
        if deleted:
            await self._audit(
                DEFINITION_DELETE, org_id, "hook", hook_id,
                notes=f"Deleted hook '{hook_id}'",
            )
        return deleted

    # ── Managed Tools ───────────────────────────────────────────────────

    async def list_mcp_server_configs_for_credential_migration(
        self, organization_id: str
    ) -> list[dict]:
        """List MCP server config env rows for credential migration."""
        async with scoped_acquire_on(
            self.pool, organization_id=organization_id
        ) as conn:
            rows = await conn.fetch(
                """
                SELECT id, organization_id, owner_user_id, owner_group_id, env_vars
                FROM mcp_server_configs
                WHERE organization_id = $1
                """,
                organization_id,
            )
        return [dict(row) for row in rows]

    async def update_mcp_server_config_env_vars(
        self, config_id: str, organization_id: str, env_vars: dict
    ) -> bool:
        """Replace an MCP server config's environment variables."""
        async with scoped_acquire_on(
            self.pool, organization_id=organization_id
        ) as conn:
            result = await conn.execute(
                """
                UPDATE mcp_server_configs
                SET env_vars = $2::jsonb, updated_at = NOW()
                WHERE id = $1 AND organization_id = $2
                """,
                config_id,
                organization_id,
                json.dumps(env_vars),
            )
        return result != "UPDATE 0"

    async def list_managed_tools(
        self,
        org_id: str,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict:
        base = """
            FROM managed_tool_definitions
            WHERE organization_id = $1
        """
        params: list[Any] = [org_id]
        if requester_user_id:
            params.extend([requester_user_id, self._role_value(requester_role)])
            uid_idx = len(params) - 1
            role_idx = len(params)
            base += " AND " + self._definition_access_condition(
                "", f"${uid_idx}", f"${role_idx}"
            )
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        # `runtime_config` is included minus `spec`: the list view needs the
        # built-in handler name to label a row, but an OpenAPI document can be
        # megabytes and is only meaningful on the detail page. Legacy rows may
        # hold a JSON *string* rather than an object (double-encoded), and the
        # `-` operator is only defined for objects, so guard on the type.
        query = f"""
                 SELECT id, name, description, runtime_type, entrypoint,
                     input_schema, output_schema, status, scope,
                     created_by, approved_by, approved_at, owner_approved_by, owner_approved_at,
                   owner_user_id, owner_group_id,
                   auth_policy, network_policy, resource_limits,
                   CASE WHEN jsonb_typeof(runtime_config) = 'object'
                        THEN runtime_config - 'spec'
                        ELSE runtime_config END AS runtime_config,
                   discovered_tools, tools_discovered_at,
                   proposal_reason, proposal_evidence,
                   created_at, updated_at
            {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        params_with_page = [*params, limit, offset]

        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, *params_with_page)
        return {
            "items": [self._normalize_tool_row(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
    }

    async def list_managed_tools_for_credential_migration(
        self, organization_id: str
    ) -> list[dict]:
        """List managed-tool env rows that may contain plaintext credentials."""
        async with scoped_acquire_on(
            self.pool, organization_id=organization_id
        ) as conn:
            rows = await conn.fetch(
                """
                SELECT id, organization_id, owner_user_id, owner_group_id, env_vars
                FROM managed_tool_definitions
                WHERE organization_id = $1
                """,
                organization_id,
            )
        return [dict(row) for row in rows]

    async def update_managed_tool_env_vars(
        self, tool_id: str, organization_id: str, env_vars: dict
    ) -> bool:
        """Replace a managed tool's environment variables after migration."""
        async with scoped_acquire_on(
            self.pool, organization_id=organization_id
        ) as conn:
            result = await conn.execute(
                """
                UPDATE managed_tool_definitions
                SET env_vars = $3::jsonb, updated_at = NOW()
                WHERE id = $1 AND organization_id = $2
                """,
                UUID(str(tool_id)),
                UUID(str(organization_id)),
                json.dumps(env_vars),
            )
        return result != "UPDATE 0"

    async def get_managed_tool(
        self,
        tool_id: str,
        org_id: str,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict | None:
        acl_sql = ""
        params: list[Any] = [tool_id, org_id]
        if requester_user_id:
            params.extend([requester_user_id, self._role_value(requester_role)])
            acl_sql = " AND " + self._definition_access_condition(
                "", "$3", "$4"
            )
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM managed_tool_definitions "
                "WHERE id = $1 AND organization_id = $2" + acl_sql,
                *params,
            )
        return self._normalize_tool_row(row)

    async def get_managed_tool_by_name(
        self,
        name: str,
        org_id: str,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict | None:
        acl_sql = ""
        params: list[Any] = [name, org_id]
        if requester_user_id:
            params.extend([requester_user_id, self._role_value(requester_role)])
            acl_sql = " AND " + self._definition_access_condition(
                "", "$3", "$4"
            )
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM managed_tool_definitions "
                "WHERE name = $1 AND organization_id = $2" + acl_sql,
                *params,
            )
        return self._normalize_tool_row(row)

    async def create_managed_tool(
        self,
        name: str,
        description: str,
        source_code: str = "",
        org_id: str = "",
        created_by: str = "",
        input_schema: dict | None = None,
        output_schema: dict | None = None,
        runtime_type: str = "python",
        entrypoint: str = "handler",
        requirements: list | None = None,
        runtime_config: dict | None = None,
        env_vars: dict | None = None,
        auth_policy: dict | None = None,
        network_policy: dict | None = None,
        resource_limits: dict | None = None,
        timeout_seconds: int = 300,
        status: str = "proposed",
        scope: str = "instance",
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
        shared_with_org: bool = False,
        proposal_reason: str | None = None,
        proposal_evidence: dict | None = None,
    ) -> dict:
        kind = str(runtime_type or "python").strip().lower()
        runtime_config = dict(runtime_config or {})

        if kind == "builtin":
            # A builtin's model-facing contract is the catalog's, not the
            # author's. Copying it here keeps the row self-describing while the
            # runtime still validates against the catalog on every call.
            from lucent.services.builtin_tool_catalog import get_builtin_tool

            handler = str(runtime_config.get("handler") or "").strip()
            if handler:
                spec = get_builtin_tool(handler)
                # Unconditional: callers send a placeholder `{"type":"object",
                # "properties":{}}` by default, so trusting the argument would
                # either reject every create or advertise an empty contract.
                input_schema = copy.deepcopy(spec.input_schema)
                if network_policy is None:
                    if spec.network_hosts:
                        # Narrowest policy that actually works: the handler's own
                        # known hosts.
                        network_policy = {
                            "network_mode": "allowlist",
                            "allowed_hosts": list(spec.network_hosts),
                        }
                    elif spec.uses_network:
                        # `web_fetch` reaches arbitrary public hosts by design; an
                        # empty allowlist would fail every call, so it needs bridge.
                        network_policy = {"network_mode": "bridge", "allowed_hosts": []}
                default_timeout = spec.default_config.get("timeout_seconds")
                if default_timeout and timeout_seconds == 300:
                    timeout_seconds = int(default_timeout)
            # Source code is meaningless for this kind; store none so an operator
            # reading the row is not misled into thinking code runs.
            source_code = ""
            entrypoint = handler or entrypoint

        elif kind in ("http", "openapi"):
            # Declarative kinds have no code, and their ceiling is the outbound
            # transport's, not a container's.
            source_code = ""
            entrypoint = "handler"
            if timeout_seconds > _MAX_DECLARATIVE_TIMEOUT:
                timeout_seconds = _MAX_DECLARATIVE_TIMEOUT

        input_schema = _prepare_json_schema(
            input_schema or {"type": "object", "properties": {}},
            "input_schema",
        )
        output_schema = _prepare_json_schema(output_schema, "output_schema")
        auth_policy = auth_policy or {"mode": "agent_grant", "require_user_access": True}
        network_policy = network_policy or {"network_mode": "none", "allowed_hosts": []}
        resource_limits = resource_limits or {
            "memory_limit": "512m", "cpu_limit": 1.0,
            "disk_limit": "1g", "timeout_seconds": timeout_seconds,
        }
        self._validate_tool_shape(
            input_schema=input_schema,
            output_schema=output_schema,
            runtime_type=kind,
            source_code=source_code,
            entrypoint=entrypoint,
            requirements=requirements,
            runtime_config=runtime_config,
            env_vars=env_vars,
            auth_policy=auth_policy,
            network_policy=network_policy,
            resource_limits=resource_limits,
            timeout_seconds=timeout_seconds,
        )
        if owner_user_id is None and owner_group_id is None:
            owner_user_id = await self._default_owner_user_id(
                created_by=created_by,
                org_id=org_id,
                scope=scope,
                shared_with_org=shared_with_org,
            )
        query = """
            INSERT INTO managed_tool_definitions
                (name, description, input_schema, output_schema, runtime_type,
                 source_code, entrypoint, requirements, runtime_config, env_vars,
                 auth_policy, network_policy, resource_limits, timeout_seconds,
                 status, scope, created_by, organization_id, owner_user_id, owner_group_id,
                 proposal_reason, proposal_evidence)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8::jsonb, $9::jsonb,
                    $10::jsonb, $11::jsonb, $12::jsonb, $13::jsonb, $14, $15, $16,
                    $17, $18, $19, $20, $21, $22::jsonb)
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                query,
                name,
                description,
                json.dumps(input_schema),
                json.dumps(output_schema) if output_schema is not None else None,
                kind,
                source_code,
                entrypoint,
                json.dumps(requirements or []),
                json.dumps(runtime_config or {}),
                json.dumps(env_vars or {}),
                json.dumps(auth_policy),
                json.dumps(network_policy),
                json.dumps(resource_limits),
                timeout_seconds,
                status,
                scope,
                created_by,
                org_id,
                owner_user_id,
                owner_group_id,
                proposal_reason,
                json.dumps(proposal_evidence or {}),
            )
        result = self._normalize_tool_row(row)
        await self._audit(
            DEFINITION_CREATE, org_id, "managed_tool", str(result["id"]),
            user_id=created_by, notes=f"Created managed tool '{name}'",
        )
        return result

    async def update_managed_tool(
        self, tool_id: str, org_id: str, *, requester_role: str | None = None, **kwargs,
    ) -> dict | None:
        await self._check_builtin_protection(
            "managed_tool_definitions", tool_id, org_id, requester_role,
        )
        current = await self.get_managed_tool(tool_id, org_id)
        if not current:
            return None
        if "input_schema" in kwargs:
            kwargs["input_schema"] = _prepare_json_schema(
                kwargs["input_schema"], "input_schema",
            )
        if "output_schema" in kwargs:
            kwargs["output_schema"] = _prepare_json_schema(
                kwargs["output_schema"], "output_schema",
            )
        kind = str(
            kwargs.get("runtime_type", current.get("runtime_type", "python"))
        ).strip().lower()
        kwargs["runtime_type"] = kind
        if "runtime_config" in kwargs:
            config = kwargs["runtime_config"] or {}
        else:
            config = current.get("runtime_config") or {}

        previous_kind = str(current.get("runtime_type") or "python").strip().lower()
        previous_handler = str((current.get("runtime_config") or {}).get("handler") or "").strip()
        handler_changed = False
        if kind == "builtin":
            handler = str(config.get("handler") or "").strip()
            handler_changed = handler != previous_handler
            if handler:
                from lucent.services.builtin_tool_catalog import get_builtin_tool

                spec = get_builtin_tool(handler)
                # A handler change invalidates the stored contract, the seeded
                # network policy, and the handler's own timeout. Re-deriving all
                # three together is what keeps the row truthful.
                if handler_changed or "input_schema" not in kwargs:
                    kwargs["input_schema"] = _prepare_json_schema(
                        copy.deepcopy(spec.input_schema), "input_schema",
                    )
                if handler_changed:
                    if "network_policy" not in kwargs:
                        kwargs["network_policy"] = (
                            {
                                "network_mode": "allowlist",
                                "allowed_hosts": list(spec.network_hosts),
                            }
                            if spec.network_hosts
                            else {"network_mode": "bridge", "allowed_hosts": []}
                        )
                    default_timeout = spec.default_config.get("timeout_seconds")
                    if default_timeout and "timeout_seconds" not in kwargs:
                        kwargs["timeout_seconds"] = int(default_timeout)
        elif kind in ("http", "openapi"):
            # Declarative kinds are executed by the server, so they inherit the
            # outbound transport's ceiling and carry no source code.
            if "source_code" in kwargs or previous_kind != kind:
                kwargs["source_code"] = ""
                kwargs["entrypoint"] = "handler"
                kwargs["requirements"] = []
            requested = kwargs.get("timeout_seconds", current.get("timeout_seconds", 300))
            if isinstance(requested, int) and requested > _MAX_DECLARATIVE_TIMEOUT:
                kwargs["timeout_seconds"] = _MAX_DECLARATIVE_TIMEOUT
        if kind != "python" and previous_kind == "python":
            # Leaving `python` means the code is no longer reachable. Keeping it
            # would contradict the migration's own statement that a non-python
            # tool stores no source.
            kwargs.setdefault("source_code", "")
            kwargs.setdefault("requirements", [])
        if kwargs.get("input_schema") is None and "input_schema" in kwargs:
            # An explicit None would fail the NOT NULL constraint and bypass the
            # builtin comparison; fall back to the kind's default contract.
            kwargs["input_schema"] = {"type": "object", "properties": {}}

        self._validate_tool_shape(
            input_schema=kwargs.get("input_schema", current.get("input_schema")),
            output_schema=kwargs.get("output_schema", current.get("output_schema")),
            runtime_type=kind,
            source_code=kwargs.get("source_code", current.get("source_code", "")),
            entrypoint=kwargs.get("entrypoint", current.get("entrypoint", "handler")),
            requirements=kwargs.get("requirements", current.get("requirements", [])),
            runtime_config=config,
            env_vars=kwargs.get("env_vars", current.get("env_vars", {})),
            auth_policy=kwargs.get("auth_policy", current.get("auth_policy", {})),
            network_policy=kwargs.get("network_policy", current.get("network_policy", {})),
            resource_limits=kwargs.get("resource_limits", current.get("resource_limits", {})),
            timeout_seconds=kwargs.get("timeout_seconds", current.get("timeout_seconds", 300)),
        )
        invalidate_discovery = bool({
            "source_code", "entrypoint", "requirements", "runtime_config",
            "env_vars", "network_policy", "resource_limits", "timeout_seconds",
        }.intersection(kwargs))
        sets = []
        params: list[Any] = []
        for key in (
            "name", "description", "runtime_type", "source_code", "entrypoint",
            "timeout_seconds", "status", "owner_user_id", "owner_group_id",
        ):
            if key in kwargs:
                params.append(kwargs[key])
                sets.append(f"{key} = ${len(params)}")
        for key in (
            "input_schema", "output_schema", "requirements", "runtime_config", "env_vars",
            "auth_policy", "network_policy", "resource_limits", "proposal_evidence",
        ):
            if key in kwargs:
                params.append(json.dumps(kwargs[key]) if kwargs[key] is not None else None)
                sets.append(f"{key} = ${len(params)}::jsonb")
        if "proposal_reason" in kwargs:
            params.append(kwargs["proposal_reason"])
            sets.append(f"proposal_reason = ${len(params)}")
        if invalidate_discovery:
            sets.extend(["discovered_tools = NULL", "tools_discovered_at = NULL"])
        if not sets:
            return current
        params.append(datetime.now(timezone.utc))
        sets.append(f"updated_at = ${len(params)}")
        params.append(tool_id)
        params.append(org_id)
        query = f"""
            UPDATE managed_tool_definitions SET {", ".join(sets)}
            WHERE id = ${len(params) - 1} AND organization_id = ${len(params)}
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, *params)
        result = self._normalize_tool_row(row)
        if result:
            await self._audit(
                DEFINITION_UPDATE, org_id, "managed_tool", tool_id,
                context={"updated_fields": [k for k in kwargs.keys()]},
                notes=f"Updated managed tool '{tool_id}'",
            )
        return result

    async def save_managed_mcp_proxy_tools(
        self,
        tool_id: str,
        tools_list: list[dict],
        org_id: str,
    ) -> dict | None:
        """Cache native tool descriptors discovered from a managed MCP proxy."""
        query = """
            UPDATE managed_tool_definitions
            SET discovered_tools = $1::jsonb, tools_discovered_at = NOW(), updated_at = NOW()
            WHERE id = $2 AND organization_id = $3
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, json.dumps(tools_list), tool_id, org_id)
        result = self._normalize_tool_row(row)
        if result:
            await self._audit(
                DEFINITION_UPDATE, org_id, "managed_tool", tool_id,
                context={"updated_fields": ["discovered_tools", "tools_discovered_at"],
                         "tool_count": len(tools_list)},
                notes=f"Saved {len(tools_list)} discovered tools for managed MCP proxy '{tool_id}'",
            )
        return result

    async def get_managed_mcp_proxy_tools(
        self,
        tool_id: str,
        org_id: str,
    ) -> dict | None:
        """Return cached native tool descriptors for a managed MCP proxy."""
        query = """
            SELECT discovered_tools, tools_discovered_at
            FROM managed_tool_definitions
            WHERE id = $1 AND organization_id = $2
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, tool_id, org_id)
        if row is None:
            return None
        return {
            "discovered_tools": self._decode_json(row["discovered_tools"], None),
            "tools_discovered_at": row["tools_discovered_at"],
        }

    async def approve_managed_tool(
        self,
        tool_id: str,
        org_id: str,
        approved_by: str,
        *,
        require_admin_approval: bool = True,
    ) -> dict | None:
        if require_admin_approval:
            query = """
                UPDATE managed_tool_definitions
                SET status = CASE WHEN status = 'proposed' THEN 'owner_approved' ELSE 'active' END,
                    owner_approved_by = CASE WHEN status = 'proposed' THEN $3 ELSE owner_approved_by END,
                    owner_approved_at = CASE WHEN status = 'proposed' THEN NOW() ELSE owner_approved_at END,
                    approved_by = CASE WHEN status = 'owner_approved' THEN $3 ELSE approved_by END,
                    approved_at = CASE WHEN status = 'owner_approved' THEN NOW() ELSE approved_at END,
                    updated_at = NOW()
                WHERE id = $1 AND organization_id = $2 AND status IN ('proposed', 'owner_approved')
                RETURNING *
            """
        else:
            query = """
                UPDATE managed_tool_definitions
                SET status = 'active', approved_by = $3, approved_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
                RETURNING *
            """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, tool_id, org_id, approved_by)
        result = self._normalize_tool_row(row)
        if result:
            await self._audit(
                DEFINITION_APPROVE, org_id, "managed_tool", tool_id,
                user_id=approved_by,
                context={"status": result["status"]},
                notes=f"Approved managed tool '{tool_id}'",
            )
        return result

    async def reject_managed_tool(
        self, tool_id: str, org_id: str, approved_by: str,
    ) -> dict | None:
        query = """
            UPDATE managed_tool_definitions
            SET status = 'rejected', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status IN ('proposed', 'owner_approved')
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, tool_id, org_id, approved_by)
        result = self._normalize_tool_row(row)
        if result:
            await self._audit(
                DEFINITION_REJECT, org_id, "managed_tool", tool_id,
                user_id=approved_by, notes=f"Rejected managed tool '{tool_id}'",
            )
        return result

    async def delete_managed_tool(self, tool_id: str, org_id: str) -> bool:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            result = await conn.execute(
                "DELETE FROM managed_tool_definitions WHERE id = $1 AND organization_id = $2",
                tool_id,
                org_id,
            )
        deleted = result == "DELETE 1"
        if deleted:
            await self._audit(
                DEFINITION_DELETE, org_id, "managed_tool", tool_id,
                notes=f"Deleted managed tool '{tool_id}'",
            )
        return deleted

    async def grant_managed_tool(
        self, agent_id: str, tool_id: str,
        org_id: str | None = None, user_id: str | None = None,
        config_override: dict | None = None,
        *, actor_user_id: str | None = None, system: bool = False,
    ) -> bool:
        outcome = await self.grant_capability(
            agent_id=agent_id,
            capability_type="managed_tool",
            capability_id=tool_id,
            org_id=org_id or "",
            actor_user_id=actor_user_id or user_id,
            system=system,
            config_override=config_override,
        )
        return outcome.ok

    async def revoke_managed_tool(
        self, agent_id: str, tool_id: str,
        org_id: str | None = None, user_id: str | None = None,
        *, actor_user_id: str | None = None, requester_role: str | None = None,
        system: bool = False,
    ) -> bool:
        return await self.revoke_capability(
            agent_id=agent_id,
            capability_type="managed_tool",
            capability_id=tool_id,
            org_id=org_id or "",
            user_id=actor_user_id or user_id,
            user_role=requester_role,
            system=system,
        )

    async def get_agent_managed_tools(self, agent_id: str, org_id: str | None = None) -> list[dict]:
        # The organization predicate is defense in depth: the junction table
        # has no organization column, so a cross-org pairing could only be
        # written by a bug. It must never be *read* as if it were valid.
        query = """
            SELECT t.*, amt.config_override, amt.granted_by FROM managed_tool_definitions t
            JOIN agent_managed_tools amt ON t.id = amt.tool_id
            WHERE amt.agent_id = $1 AND t.status = 'active'
              AND ($2::uuid IS NULL OR t.organization_id = $2::uuid)
            ORDER BY t.name
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id, role="system" if not org_id else None
        ) as conn:
            rows = await conn.fetch(query, agent_id, org_id)
        return [self._normalize_tool_row(r) for r in rows]

    async def is_managed_tool_granted_to_agent(self, agent_id: str, tool_id: str, org_id: str | None = None) -> bool:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id, role="system" if not org_id else None
        ) as conn:
            return bool(await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM agent_managed_tools "
                "WHERE agent_id = $1 AND tool_id = $2)",
                agent_id,
                tool_id,
            ))

    async def create_managed_tool_run(
        self,
        *,
        tool_id: str,
        org_id: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        input_payload: dict | None = None,
    ) -> dict:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO managed_tool_runs
                    (tool_id, organization_id, user_id, agent_id, input_payload)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                RETURNING *
                """,
                tool_id,
                org_id,
                user_id,
                agent_id,
                json.dumps(input_payload) if input_payload is not None else None,
            )
        return dict(row)

    async def complete_managed_tool_run(
        self,
        run_id: str,
        *,
        status: str,
        output_payload: dict | None = None,
        error: str | None = None,
        sandbox_id: str | None = None,
        duration_ms: int | None = None,
        org_id: str | None = None,
    ) -> dict | None:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id, role="system" if not org_id else None
        ) as conn:
            row = await conn.fetchrow(
                """
                UPDATE managed_tool_runs
                SET status = $2,
                    output_payload = $3::jsonb,
                    error = $4,
                    sandbox_id = $5,
                    duration_ms = $6,
                    finished_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                run_id,
                status,
                json.dumps(output_payload) if output_payload is not None else None,
                error,
                sandbox_id,
                duration_ms,
            )
        return dict(row) if row else None

    async def create_mcp_server(
        self,
        name: str,
        description: str,
        server_type: str,
        url: str | None,
        org_id: str,
        created_by: str,
        headers: dict | None = None,
        command: str | None = None,
        args: list | None = None,
        env_vars: dict | None = None,
        status: str = "proposed",
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
        shared_with_org: bool = False,
        proposal_reason: str | None = None,
        proposal_evidence: dict | None = None,
    ) -> dict:
        # Default owner to creator when no explicit ownership is provided
        if owner_user_id is None and owner_group_id is None:
            owner_user_id = await self._default_owner_user_id(
                created_by=created_by,
                org_id=org_id,
                scope="instance",
                shared_with_org=shared_with_org,
            )
        query = """
            INSERT INTO mcp_server_configs (name, description, server_type, url,
                command, args, headers, env_vars, status, created_by, organization_id,
                owner_user_id, owner_group_id, proposal_reason, proposal_evidence)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15::jsonb)
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                query,
                name,
                description,
                server_type,
                url,
                command,
                json.dumps(args or []),
                json.dumps(headers or {}),
                json.dumps(env_vars or {}),
                status,
                created_by,
                org_id,
                owner_user_id,
                owner_group_id,
                proposal_reason,
                json.dumps(proposal_evidence or {}),
            )
        result = self._normalize_mcp_row(row)
        await self._audit(
            DEFINITION_CREATE, org_id, "mcp_server", str(result["id"]),
            user_id=created_by, notes=f"Created MCP server '{name}'",
        )
        return result

    async def approve_mcp_server(
        self,
        server_id: str,
        org_id: str,
        approved_by: str,
    ) -> dict | None:
        query = """
            UPDATE mcp_server_configs
            SET status = 'active', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, server_id, org_id, approved_by)
        result = self._normalize_mcp_row(row)
        if result:
            await self._audit(
                DEFINITION_APPROVE, org_id, "mcp_server", server_id,
                user_id=approved_by, notes=f"Approved MCP server '{server_id}'",
            )
        return result

    async def reject_mcp_server(self, server_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE mcp_server_configs
            SET status = 'rejected', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, server_id, org_id, approved_by)
        result = self._normalize_mcp_row(row)
        if result:
            await self._audit(
                DEFINITION_REJECT, org_id, "mcp_server", server_id,
                user_id=approved_by, notes=f"Rejected MCP server '{server_id}'",
            )
        return result

    # ── Access Grants ─────────────────────────────────────────────────────

    async def _assert_capability_attach_allowed(
        self,
        *,
        agent_id: str,
        capability_id: str,
        spec: CapabilitySpec,
        org_id: str,
        actor_user_id: str | None,
        system: bool,
    ) -> GrantOutcome:
        """Authorize attaching one capability to one agent.

        Fail-closed chokepoint shared by every attach surface (web, REST, MCP,
        built-in sync). Attaching requires *read* clearance on both endpoints:
        an agent a user cannot see is not one they may modify, and a capability
        a user cannot see is not one they may hand to an agent.

        ``system=True`` is the seed path (built-in sync, default hooks). It
        skips the read checks because there is no principal, and it is only
        reachable from code that runs as the instance. Everything else must
        name an actor.
        """
        if not org_id:
            # Tenant-scoped writes are fail-closed: no organization, no write.
            return GrantOutcome.DENIED
        if system:
            return GrantOutcome.ATTACHED
        if not actor_user_id:
            return GrantOutcome.DENIED

        from lucent.db.pool import get_authorized_pool_for_user

        try:
            authorized_pool = await get_authorized_pool_for_user(
                self.pool,
                {"id": actor_user_id, "organization_id": org_id},
                DEFINITIONS_AUTH_POLICIES,
                "read",
            )
        except (KeyError, TypeError, ValueError):
            return GrantOutcome.DENIED

        repository = DefinitionRepository(authorized_pool)
        agent = await repository.get_usable_agent(agent_id)
        if not agent or str(agent.get("organization_id")) != str(org_id):
            return GrantOutcome.DENIED

        capability = await self._get_usable_capability(repository, spec, capability_id)
        if not capability:
            return GrantOutcome.DENIED

        # A capability attached to an agent must belong to the same
        # organization. The junction tables carry no organization column, so
        # this is the only place the cross-tenant pairing can be rejected.
        if str(capability.get("organization_id")) != str(org_id):
            return GrantOutcome.DENIED
        return GrantOutcome.ATTACHED

    @staticmethod
    async def _get_usable_capability(
        repository: "DefinitionRepository",
        spec: CapabilitySpec,
        capability_id: str,
    ) -> dict | None:
        """Clearance-gated lookup for one capability family.

        ``repository`` must be bound to an authorized pool, so an unreadable
        capability is simply absent rather than merely unvalidated.
        """
        getter = {
            "skill": repository.get_usable_skill,
            "managed_tool": repository.get_usable_managed_tool,
            "hook": repository.get_usable_hook,
            "mcp_server": repository.get_usable_mcp_server,
        }[spec.capability_type]
        return await getter(capability_id)

    async def can_attach_capability(
        self,
        *,
        agent_id: str,
        capability_type: str,
        capability_id: str,
        org_id: str,
        actor_user_id: str,
    ) -> bool:
        """Public read-only check for UI affordances (no writes)."""
        spec = CAPABILITY_SPEC_BY_TYPE[normalize_capability_type(capability_type)]
        outcome = await self._assert_capability_attach_allowed(
            agent_id=agent_id,
            capability_id=capability_id,
            spec=spec,
            org_id=org_id,
            actor_user_id=actor_user_id,
            system=False,
        )
        return outcome.ok

    async def _capability_is_attached(
        self, spec: CapabilitySpec, agent_id: str, capability_id: str, org_id: str,
    ) -> bool:
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            return bool(
                await conn.fetchval(
                    f"SELECT EXISTS(SELECT 1 FROM {spec.junction_table} "
                    f"WHERE {spec.agent_column} = $1 AND {spec.fk_column} = $2)",
                    agent_id,
                    capability_id,
                )
            )

    async def _get_junction_granted_by(
        self, spec: CapabilitySpec, agent_id: str, capability_id: str, org_id: str,
    ) -> str | None:
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            value = await conn.fetchval(
                f"SELECT granted_by FROM {spec.junction_table} "
                f"WHERE {spec.agent_column} = $1 AND {spec.fk_column} = $2",
                agent_id,
                capability_id,
            )
        return str(value) if value else None

    async def _audit_capability_attach(
        self,
        *,
        spec: CapabilitySpec,
        agent_id: str,
        capability_id: str,
        org_id: str | None,
        user_id: str | None,
        event_type: str,
        verb: str,
    ) -> None:
        if not org_id:
            return
        await self._audit(
            event_type, org_id, spec.capability_type, capability_id,
            user_id=user_id,
            context={"agent_id": agent_id},
            notes=f"{verb} {spec.capability_type} '{capability_id}' "
                  f"{'to' if verb == 'Granted' else 'from'} agent '{agent_id}'",
        )

    async def _insert_capability_grant(
        self,
        *,
        spec: CapabilitySpec,
        agent_id: str,
        capability_id: str,
        org_id: str,
        actor_user_id: str | None,
        config_override: dict | None = None,
    ) -> bool:
        columns = [spec.agent_column, spec.fk_column, "granted_by"]
        values = ["$1", "$2", "$3"]
        params: list[Any] = [agent_id, capability_id, actor_user_id]
        updates = []
        if spec.has_config_override:
            columns.append("config_override")
            values.append(f"${len(params) + 1}::text::jsonb")
            params.append(
                json.dumps(config_override) if config_override is not None else None
            )
            updates.append("config_override = EXCLUDED.config_override")

        # granted_by is only recorded on first attach. A second attach by a
        # different user is refused up front (ALREADY_ATTACHED), so the only
        # way to reach the conflict path is a retry by the same actor.
        conflict_target = f"({spec.agent_column}, {spec.fk_column})"
        conflict_action = (
            "DO UPDATE SET " + ", ".join(updates) if updates else "DO NOTHING"
        )
        query = (
            f"INSERT INTO {spec.junction_table} ({', '.join(columns)}) "
            f"VALUES ({', '.join(values)}) "
            f"ON CONFLICT {conflict_target} {conflict_action}"
        )
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            await conn.execute(query, *params)
        return True

    async def grant_capability(
        self,
        *,
        agent_id: str,
        capability_type: str,
        capability_id: str,
        org_id: str,
        actor_user_id: str | None,
        system: bool = False,
        config_override: dict | None = None,
    ) -> GrantOutcome:
        """Generic attach path: authorize, insert, and report the outcome."""
        spec = CAPABILITY_SPEC_BY_TYPE[normalize_capability_type(capability_type)]
        if not org_id:
            return GrantOutcome.FAILED
        allowed = await self._assert_capability_attach_allowed(
            agent_id=agent_id,
            capability_id=capability_id,
            spec=spec,
            org_id=org_id,
            actor_user_id=actor_user_id,
            system=system,
        )
        if not allowed.ok:
            return GrantOutcome.DENIED
        try:
            if await self._capability_is_attached(spec, agent_id, capability_id, org_id):
                return GrantOutcome.ALREADY_ATTACHED
            await self._insert_capability_grant(
                spec=spec,
                agent_id=agent_id,
                capability_id=capability_id,
                org_id=org_id,
                actor_user_id=None if system else actor_user_id,
                config_override=config_override,
            )
        except Exception:
            logger.error(
                "Failed to attach %s %s to agent %s",
                spec.capability_type, capability_id, agent_id, exc_info=True,
            )
            return GrantOutcome.FAILED
        await self._audit_capability_attach(
            spec=spec, agent_id=agent_id, capability_id=capability_id,
            org_id=org_id, user_id=actor_user_id,
            event_type=DEFINITION_GRANT, verb="Granted",
        )
        return GrantOutcome.ATTACHED

    async def can_revoke_capability(
        self,
        *,
        agent_id: str,
        capability_type: str,
        capability_id: str,
        org_id: str,
        user_id: str,
        user_role: str | None = None,
    ) -> bool:
        """May this user detach a capability from this agent?

        Detaching is destructive for everyone using the agent, so it is
        restricted to the people responsible for the two ends: the agent's
        owner, the capability's owner, or an org admin/owner. A user who simply
        does not want the capability declines it for themselves instead, which
        never affects anyone else.
        """
        spec = CAPABILITY_SPEC_BY_TYPE[normalize_capability_type(capability_type)]
        if not org_id:
            return False
        role = self._role_value(user_role)
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            agent_row = await conn.fetchrow(
                "SELECT owner_user_id, owner_group_id, scope FROM agent_definitions "
                "WHERE id = $1 AND organization_id = $2",
                agent_id, org_id,
            )
            capability_row = await conn.fetchrow(
                f"SELECT owner_user_id, owner_group_id, scope FROM {spec.target_table} "
                "WHERE id = $1 AND organization_id = $2",
                capability_id, org_id,
            )
            group_ids = [
                str(row["group_id"])
                for row in await conn.fetch(
                    "SELECT group_id FROM user_groups WHERE user_id = $1",
                    UUID(user_id),
                )
            ]
        if not agent_row or not capability_row:
            return False
        if role in ("admin", "owner"):
            return True
        if agent_row["owner_user_id"] and str(agent_row["owner_user_id"]) == str(user_id):
            return True
        if capability_row["owner_user_id"] and str(capability_row["owner_user_id"]) == str(user_id):
            return True
        for owner_group_id in (
            agent_row["owner_group_id"],
            capability_row["owner_group_id"],
        ):
            if owner_group_id and str(owner_group_id) in group_ids:
                return True
        return False

    async def revoke_capability(
        self,
        *,
        agent_id: str,
        capability_type: str,
        capability_id: str,
        org_id: str,
        user_id: str | None,
        user_role: str | None = None,
        system: bool = False,
    ) -> bool:
        """Generic detach path: authorize, delete, and audit."""
        spec = CAPABILITY_SPEC_BY_TYPE[normalize_capability_type(capability_type)]
        if not org_id:
            return False
        if not system:
            if not user_id:
                return False
            if not await self.can_revoke_capability(
                agent_id=agent_id,
                capability_type=spec.capability_type,
                capability_id=capability_id,
                org_id=org_id,
                user_id=user_id,
                user_role=user_role,
            ):
                return False
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            result = await conn.execute(
                f"DELETE FROM {spec.junction_table} "
                f"WHERE {spec.agent_column} = $1 AND {spec.fk_column} = $2",
                agent_id,
                capability_id,
            )
            # Detaching removes the ask: a stale approval for a capability that
            # is no longer attached would silently apply if it were re-added.
            await conn.execute(
                f"DELETE FROM agent_capability_consents "
                f"WHERE agent_id = $1 AND capability_type = $2 "
                f"AND {spec.consent_column} = $3",
                agent_id,
                spec.capability_type,
                capability_id,
            )
        revoked = result == "DELETE 1"
        if revoked:
            await self._audit_capability_attach(
                spec=spec, agent_id=agent_id, capability_id=capability_id,
                org_id=org_id, user_id=user_id,
                event_type=DEFINITION_REVOKE, verb="Revoked",
            )
        return revoked

    async def grant_skill(
        self, agent_id: str, skill_id: str,
        org_id: str | None = None, user_id: str | None = None,
        *, actor_user_id: str | None = None, system: bool = False,
    ) -> bool:
        outcome = await self.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=skill_id,
            org_id=org_id or "",
            actor_user_id=actor_user_id or user_id,
            system=system,
        )
        return outcome.ok

    async def revoke_skill(
        self, agent_id: str, skill_id: str,
        org_id: str | None = None, user_id: str | None = None,
        *, actor_user_id: str | None = None, requester_role: str | None = None,
        system: bool = False,
    ) -> bool:
        return await self.revoke_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=skill_id,
            org_id=org_id or "",
            user_id=actor_user_id or user_id,
            user_role=requester_role,
            system=system,
        )

    async def grant_mcp_server(
        self, agent_id: str, mcp_server_id: str,
        org_id: str | None = None, user_id: str | None = None,
        *, actor_user_id: str | None = None, system: bool = False,
    ) -> bool:
        outcome = await self.grant_capability(
            agent_id=agent_id,
            capability_type="mcp_server",
            capability_id=mcp_server_id,
            org_id=org_id or "",
            actor_user_id=actor_user_id or user_id,
            system=system,
        )
        return outcome.ok

    async def revoke_mcp_server(
        self, agent_id: str, mcp_server_id: str,
        org_id: str | None = None, user_id: str | None = None,
        *, actor_user_id: str | None = None, requester_role: str | None = None,
        system: bool = False,
    ) -> bool:
        return await self.revoke_capability(
            agent_id=agent_id,
            capability_type="mcp_server",
            capability_id=mcp_server_id,
            org_id=org_id or "",
            user_id=actor_user_id or user_id,
            user_role=requester_role,
            system=system,
        )

    async def grant_hook(
        self, agent_id: str, hook_id: str,
        org_id: str | None = None, user_id: str | None = None,
        config_override: dict | None = None,
        *, actor_user_id: str | None = None, system: bool = False,
    ) -> bool:
        outcome = await self.grant_capability(
            agent_id=agent_id,
            capability_type="hook",
            capability_id=hook_id,
            org_id=org_id or "",
            actor_user_id=actor_user_id or user_id,
            system=system,
            config_override=config_override,
        )
        return outcome.ok

    async def revoke_hook(
        self, agent_id: str, hook_id: str,
        org_id: str | None = None, user_id: str | None = None,
        *, actor_user_id: str | None = None, requester_role: str | None = None,
        system: bool = False,
    ) -> bool:
        return await self.revoke_capability(
            agent_id=agent_id,
            capability_type="hook",
            capability_id=hook_id,
            org_id=org_id or "",
            user_id=actor_user_id or user_id,
            user_role=requester_role,
            system=system,
        )

    # ── Capability consent ────────────────────────────────────────────────

    async def upsert_capability_consent(
        self,
        *,
        org_id: str,
        agent_id: str,
        user_id: str,
        capability_type: str,
        capability_id: str,
        state: str,
    ) -> bool:
        """Record one user's approve/decline decision for a capability.

        Consent is only recorded against a capability that is actually
        attached to the agent; otherwise a stale decision would silently apply
        the next time somebody attached it.
        """
        spec = CAPABILITY_SPEC_BY_TYPE[normalize_capability_type(capability_type)]
        if state not in CONSENT_STATES:
            raise ValueError(f"Unsupported consent state: {state}")
        if not org_id:
            return False
        if not await self._capability_is_attached(spec, agent_id, capability_id, org_id):
            return False
        columns = [
            "organization_id", "agent_id", "capability_type",
            spec.consent_column, "user_id", "state",
        ]
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            await conn.execute(
                f"INSERT INTO agent_capability_consents ({', '.join(columns)}) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                f"ON CONFLICT (agent_id, user_id, {spec.consent_column}) "
                f"WHERE capability_type = '{spec.capability_type}' "
                "DO UPDATE SET state = EXCLUDED.state, decided_at = NOW()",
                org_id, agent_id, spec.capability_type, capability_id, user_id, state,
            )
        await self._audit(
            DEFINITION_UPDATE, org_id, spec.capability_type, capability_id,
            user_id=user_id,
            context={"agent_id": agent_id, "consent_state": state},
            notes=(
                f"{'Approved' if state == 'approved' else 'Declined'} "
                f"{spec.capability_type} '{capability_id}' for agent '{agent_id}'"
            ),
        )
        return True

    async def list_capability_consents(
        self, agent_id: str, user_id: str, org_id: str,
    ) -> dict[str, dict[str, str]]:
        """Return this user's recorded decisions for one agent, by type → id."""
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            rows = await conn.fetch(
                "SELECT capability_type, skill_id, managed_tool_id, hook_id, "
                "mcp_server_id, state FROM agent_capability_consents "
                "WHERE agent_id = $1 AND user_id = $2 AND organization_id = $3",
                agent_id, user_id, org_id,
            )
        decisions: dict[str, dict[str, str]] = {spec.capability_type: {} for spec in CAPABILITY_SPECS}
        for row in rows:
            spec = CAPABILITY_SPEC_BY_TYPE[str(row["capability_type"])]
            target_id = row[spec.consent_column]
            if target_id:
                decisions[spec.capability_type][str(target_id)] = str(row["state"])
        return decisions

    # Shared consent predicate: $2 = user_id, $4 = the user's group ids.
    # Kept in one place so the pending listing, the badge count, and the runtime
    # resolver's exemptions cannot drift apart.
    _CONSENT_CANDIDATE_PREDICATE = (
        "(j.granted_by IS NULL OR j.granted_by <> $2) "
        "AND COALESCE(c.scope, 'instance') <> 'built-in' "
        "AND (c.owner_user_id IS NULL OR c.owner_user_id <> $2) "
        "AND (c.owner_group_id IS NULL OR NOT (c.owner_group_id = ANY($4::uuid[])))"
    )

    # Readability gate, mirroring ``_authorized_table_sql``: a capability the
    # user cannot read must never be surfaced here. Without this the approvals
    # panel would disclose the name and description of capabilities the user
    # has no clearance for, and would ask them to approve something that could
    # never take effect (the runtime resolver already skips unreadable
    # capabilities, so the two views would disagree).
    _CONSENT_READABLE_PREDICATE = """
        EXISTS (
            SELECT 1 FROM auth_clearances ac
            WHERE ac.auth_id = c.auth_id
              AND (
                    (ac.principal_type = 'user'  AND ac.principal_id = $2::uuid)
                 OR (ac.principal_type = 'group' AND ac.principal_id = ANY($4::uuid[]))
                 OR (ac.principal_type = 'org'   AND ac.principal_id = $1::uuid)
              )
        )
    """

    @classmethod
    def _consent_candidate_predicate(cls) -> str:
        return cls._CONSENT_CANDIDATE_PREDICATE

    @classmethod
    def _consent_readable_predicate(cls) -> str:
        return cls._CONSENT_READABLE_PREDICATE

    async def _list_capability_consent_candidates(
        self,
        *,
        org_id: str,
        user_id: str,
        state: str | None,
        limit: int,
    ) -> list[dict]:
        """Attached capabilities in a given consent state for one user.

        ``state=None`` means "no recorded decision yet" (pending). The
        exemption predicates mirror :func:`lucent.agent_capabilities._is_exempt`
        so the listing and the runtime resolver can never disagree about who
        needs to approve what.
        """
        results: list[dict] = []
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            group_ids = [
                UUID(str(row["group_id"]))
                for row in await conn.fetch(
                    "SELECT group_id FROM user_groups WHERE user_id = $1",
                    UUID(user_id),
                )
            ]
            for spec in CAPABILITY_SPECS:
                state_clause = (
                    "AND consent.id IS NULL"
                    if state is None
                    else "AND consent.state = $6"
                )
                params: list[Any] = [
                    UUID(org_id),
                    UUID(user_id),
                    spec.capability_type,
                    group_ids,
                    limit,
                ]
                if state is not None:
                    params.append(state)
                rows = await conn.fetch(
                    f"""
                    SELECT c.id AS capability_id, c.name AS capability_name,
                           c.description AS capability_description,
                           a.id AS agent_id, a.name AS agent_name
                    FROM {spec.junction_table} j
                    JOIN agent_definitions a
                      ON a.id = j.{spec.agent_column}
                     AND a.organization_id = $1
                     AND a.status = 'active'
                    JOIN {spec.target_table} c
                      ON c.id = j.{spec.fk_column}
                     AND c.organization_id = $1
                     AND c.status = 'active'
                    LEFT JOIN agent_capability_consents consent
                      ON consent.agent_id = a.id
                     AND consent.user_id = $2
                     AND consent.capability_type = $3
                     AND consent.{spec.consent_column} = c.id
                    WHERE {self._consent_candidate_predicate()}
                      AND {self._consent_readable_predicate()}
                      {state_clause}
                    ORDER BY a.name, c.name
                    LIMIT $5
                    """,
                    *params,
                )
                for row in rows:
                    results.append(
                        {
                            "capability_type": spec.capability_type,
                            "capability_id": str(row["capability_id"]),
                            "capability_name": row["capability_name"],
                            "capability_description": row["capability_description"],
                            "agent_id": str(row["agent_id"]),
                            "agent_name": row["agent_name"],
                        }
                    )
        results.sort(key=lambda item: (item["agent_name"], item["capability_name"]))
        return results

    async def list_pending_capability_consents(
        self, org_id: str, user_id: str, *, limit: int = 100,
    ) -> list[dict]:
        """Capabilities attached to an agent this user has not decided on yet."""
        return await self._list_capability_consent_candidates(
            org_id=org_id, user_id=user_id, state=None, limit=limit,
        )

    async def list_declined_capability_consents(
        self, org_id: str, user_id: str, *, limit: int = 100,
    ) -> list[dict]:
        """Capabilities this user declined, kept visible for re-approval."""
        return await self._list_capability_consent_candidates(
            org_id=org_id, user_id=user_id, state="declined", limit=limit,
        )


    async def count_pending_capability_consents(self, org_id: str, user_id: str) -> int:
        """Cheap navigation-badge count (same predicate as the full listing)."""
        total = 0
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            group_ids = [
                UUID(str(row["group_id"]))
                for row in await conn.fetch(
                    "SELECT group_id FROM user_groups WHERE user_id = $1",
                    UUID(user_id),
                )
            ]
            for spec in CAPABILITY_SPECS:
                count = await conn.fetchval(
                    f"""
                    SELECT COUNT(*)
                    FROM {spec.junction_table} j
                    JOIN agent_definitions a
                      ON a.id = j.{spec.agent_column}
                     AND a.organization_id = $1
                     AND a.status = 'active'
                    JOIN {spec.target_table} c
                      ON c.id = j.{spec.fk_column}
                     AND c.organization_id = $1
                     AND c.status = 'active'
                    LEFT JOIN agent_capability_consents consent
                      ON consent.agent_id = a.id
                     AND consent.user_id = $2
                     AND consent.capability_type = $3
                     AND consent.{spec.consent_column} = c.id
                    WHERE {self._CONSENT_CANDIDATE_PREDICATE}
                      AND {self._CONSENT_READABLE_PREDICATE}
                      AND consent.id IS NULL
                    """,
                    UUID(org_id),
                    UUID(user_id),
                    spec.capability_type,
                    group_ids,
                )
                total += int(count or 0)
        return total

    async def get_accessible_capability_ids(
        self, capability_type: str, org_id: str,
    ) -> set[str]:
        """IDs of one capability family the authorized pool's principal can read.

        Requires an authorized pool: the pool rewrite injects the clearance
        filter, so an unreadable capability is absent rather than merely
        unvalidated. Raises ``TypeError`` on a plain pool — fail closed.
        """
        spec = CAPABILITY_SPEC_BY_TYPE[normalize_capability_type(capability_type)]
        if not isinstance(self.pool, AuthorizedDatabasePool):
            raise TypeError(
                "Capability usage reads require an AuthorizedDatabasePool "
                "(capability visibility is clearance-driven)"
            )
        rows = await self._authorized_conn_fetch(
            f"SELECT id FROM {spec.target_table} WHERE organization_id = $1",
            UUID(org_id),
        )
        return {str(row["id"]) for row in rows}

    async def get_agent_skills(self, agent_id: str, org_id: str | None = None) -> list[dict]:
        query = """
            SELECT s.*, ags.granted_by FROM skill_definitions s
            JOIN agent_skills ags ON s.id = ags.skill_id
            WHERE ags.agent_id = $1 AND s.status = 'active'
              AND ($2::uuid IS NULL OR s.organization_id = $2::uuid)
            ORDER BY s.name
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id, role="system" if not org_id else None
        ) as conn:
            rows = await conn.fetch(query, agent_id, org_id)
        return [self._normalize_definition_row(r) for r in rows]

    async def get_agent_mcp_servers(self, agent_id: str, org_id: str | None = None) -> list[dict]:
        query = """
            SELECT m.*, agm.allowed_tools, agm.granted_by FROM mcp_server_configs m
            JOIN agent_mcp_servers agm ON m.id = agm.mcp_server_id
            WHERE agm.agent_id = $1 AND m.status = 'active'
              AND ($2::uuid IS NULL OR m.organization_id = $2::uuid)
            ORDER BY m.name
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id, role="system" if not org_id else None
        ) as conn:
            rows = await conn.fetch(query, agent_id, org_id)
        return [self._normalize_mcp_row(r) for r in rows]

    async def get_agent_hooks(self, agent_id: str, org_id: str | None = None) -> list[dict]:
        query = """
            SELECT h.*, ah.config_override, ah.granted_by FROM hook_definitions h
            JOIN agent_hooks ah ON h.id = ah.hook_id
            WHERE ah.agent_id = $1 AND h.status = 'active'
              AND ($2::uuid IS NULL OR h.organization_id = $2::uuid)
            ORDER BY h.name
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id, role="system" if not org_id else None
        ) as conn:
            rows = await conn.fetch(query, agent_id, org_id)
        return [self._normalize_hook_row(r) for r in rows]

    async def grant_default_hooks_to_agent(self, agent_id: str, org_id: str) -> int:
        """Grant built-in default hooks to one agent without overriding explicit config."""
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            result = await conn.execute(
                """
                INSERT INTO agent_hooks (agent_id, hook_id, granted_by)
                SELECT $1, h.id, NULL
                FROM hook_definitions h
                WHERE h.organization_id = $2
                    AND h.status = 'active'
                    AND h.scope = 'built-in'
                    AND h.name = ANY($3::text[])
                ON CONFLICT DO NOTHING
                """,
                agent_id,
                org_id,
                list(DEFAULT_AGENT_HOOK_NAMES),
            )
        return self._execute_count(result)

    async def grant_default_hooks_to_all_agents(self, org_id: str) -> int:
        """Ensure every agent in an organization has the built-in default hooks."""
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            result = await conn.execute(
                """
                INSERT INTO agent_hooks (agent_id, hook_id, granted_by)
                SELECT a.id, h.id, NULL
                FROM agent_definitions a
                JOIN hook_definitions h ON h.organization_id = a.organization_id
                WHERE a.organization_id = $1
                    AND h.status = 'active'
                    AND h.scope = 'built-in'
                    AND h.name = ANY($2::text[])
                ON CONFLICT DO NOTHING
                """,
                org_id,
                list(DEFAULT_AGENT_HOOK_NAMES),
            )
        return self._execute_count(result)

    async def update_mcp_tool_grants(
        self,
        agent_id: str,
        mcp_server_id: str,
        allowed_tools: list[str] | None,
        org_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        """Set which tools an agent can use from an MCP server. None = all."""
        val = json.dumps(allowed_tools) if allowed_tools is not None else None
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            result = await conn.execute(
                "UPDATE agent_mcp_servers SET allowed_tools = $3 "
                "WHERE agent_id = $1 AND mcp_server_id = $2",
                agent_id,
                mcp_server_id,
                val,
            )
        updated = result == "UPDATE 1"
        if updated and org_id:
            await self._audit(
                DEFINITION_UPDATE, org_id, "mcp_server", mcp_server_id,
                user_id=user_id,
                context={"agent_id": agent_id, "allowed_tools": allowed_tools},
                notes=f"Updated tool grants for MCP server '{mcp_server_id}' on agent '{agent_id}'",
            )
        return updated

    async def update_skill(
        self, skill_id: str, org_id: str, *, requester_role: str | None = None, **kwargs,
    ) -> dict | None:
        await self._check_builtin_protection(
            "skill_definitions", skill_id, org_id, requester_role,
        )
        sets = []
        params: list[Any] = []
        for key in ("name", "description", "content", "status", "owner_user_id", "owner_group_id"):
            if key in kwargs:
                params.append(kwargs[key])
                sets.append(f"{key} = ${len(params)}")
        if not sets:
            return await self.get_skill(skill_id, org_id)
        params.append(datetime.now(timezone.utc))
        sets.append(f"updated_at = ${len(params)}")
        params.append(skill_id)
        params.append(org_id)
        query = f"""
            UPDATE skill_definitions SET {", ".join(sets)}
            WHERE id = ${len(params) - 1} AND organization_id = ${len(params)}
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, *params)
        result = self._normalize_definition_row(row)
        if result:
            await self._audit(
                DEFINITION_UPDATE, org_id, "skill", skill_id,
                context={
                    "updated_fields": [
                        k for k in ("name", "description", "content", "status",
                                     "owner_user_id", "owner_group_id")
                        if k in kwargs
                    ],
                },
                notes=f"Updated skill '{skill_id}'",
            )
        return result

    async def update_mcp_server(
        self, server_id: str, org_id: str, *, requester_role: str | None = None, **kwargs,
    ) -> dict | None:
        await self._check_builtin_protection(
            "mcp_server_configs", server_id, org_id, requester_role,
        )
        sets = []
        params: list[Any] = []
        for key in ("name", "description", "server_type", "url", "command",
                     "owner_user_id", "owner_group_id"):
            if key in kwargs:
                params.append(kwargs[key])
                sets.append(f"{key} = ${len(params)}")
        for key in ("headers", "args", "env_vars"):
            if key in kwargs:
                params.append(json.dumps(kwargs[key]) if kwargs[key] else None)
                sets.append(f"{key} = ${len(params)}")
        if not sets:
            return await self.get_mcp_server(server_id, org_id)
        params.append(datetime.now(timezone.utc))
        sets.append(f"updated_at = ${len(params)}")
        params.append(server_id)
        params.append(org_id)
        query = f"""
            UPDATE mcp_server_configs SET {", ".join(sets)}
            WHERE id = ${len(params) - 1} AND organization_id = ${len(params)}
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, *params)
        result = self._normalize_mcp_row(row)
        if result:
            all_keys = (
                "name", "description", "server_type", "url",
                "command", "headers", "args", "env_vars",
                "owner_user_id", "owner_group_id",
            )
            await self._audit(
                DEFINITION_UPDATE, org_id, "mcp_server", server_id,
                context={"updated_fields": [k for k in all_keys if k in kwargs]},
                notes=f"Updated MCP server '{server_id}'",
            )
        return result

    async def delete_mcp_server(self, server_id: str, org_id: str) -> bool:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            result = await conn.execute(
                "DELETE FROM mcp_server_configs WHERE id = $1 AND organization_id = $2",
                server_id,
                org_id,
            )
        deleted = result == "DELETE 1"
        if deleted:
            await self._audit(
                DEFINITION_DELETE, org_id, "mcp_server", server_id,
                notes=f"Deleted MCP server '{server_id}'",
            )
        return deleted

    # ── Tool Discovery Cache ─────────────────────────────────────────────

    async def save_discovered_tools(
        self,
        server_id: str,
        tools_list: list[dict],
        org_id: str,
    ) -> dict | None:
        """Cache discovered tools for an MCP server."""
        query = """
            UPDATE mcp_server_configs
            SET discovered_tools = $1, tools_discovered_at = NOW(), updated_at = NOW()
            WHERE id = $2 AND organization_id = $3
            RETURNING *
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                query,
                tools_list,
                server_id,
                org_id,
            )
        result = self._normalize_mcp_row(row)
        if result:
            await self._audit(
                DEFINITION_UPDATE, org_id, "mcp_server", server_id,
                context={"updated_fields": ["discovered_tools", "tools_discovered_at"],
                         "tool_count": len(tools_list)},
                notes=f"Saved {len(tools_list)} discovered tools for MCP server '{server_id}'",
            )
        return result

    async def get_discovered_tools(
        self,
        server_id: str,
        org_id: str,
    ) -> dict | None:
        """Return cached tools and discovery timestamp for TTL checks."""
        query = """
            SELECT discovered_tools, tools_discovered_at
            FROM mcp_server_configs
            WHERE id = $1 AND organization_id = $2
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, server_id, org_id)
        if row is None:
            return None
        return {
            "discovered_tools": self._decode_json(row["discovered_tools"], None),
            "tools_discovered_at": row["tools_discovered_at"],
        }

    async def clear_discovered_tools(
        self,
        server_id: str,
        org_id: str,
    ) -> bool:
        """Clear the cached tools for an MCP server."""
        query = """
            UPDATE mcp_server_configs
            SET discovered_tools = NULL, tools_discovered_at = NULL, updated_at = NOW()
            WHERE id = $1 AND organization_id = $2
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            result = await conn.execute(query, server_id, org_id)
        return result == "UPDATE 1"

    # ── Access Control Queries ───────────────────────────────────────────

    # ── Clearance-driven usage reads ──────────────────────────────────────

    async def _accessible_definitions(
        self,
        *,
        table: str,
        alias: str,
        org_id: str,
        status: str | None = "active",
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
        normalize,
    ) -> dict:
        """List definitions cleared for *use* by the pool's principal.

        Shared implementation for the five definition families. On an
        authorized pool this is clearance-driven and default-deny — the
        ownership (123), group (126) and built-in org grants (126) plus
        explicit grants all live in auth_clearances. A plain pool is
        fail-closed. The status filter and org predicate stay in the caller's
        SQL: status is the caller's lifecycle filter, org is defense in depth.
        """
        if not isinstance(self.pool, AuthorizedDatabasePool):
            raise TypeError(
                "Definition usage reads require an AuthorizedDatabasePool "
                "built for the definitions policies (usage is "
                "clearance-driven and default-deny)"
            )
        params: list[Any] = [UUID(org_id)]
        status_clause = ""
        if status:
            status_clause = f" AND {alias}.status = ${len(params) + 1}"
            params.append(status)
        search_clause = ""
        term = (search or "").strip()
        if term:
            params.append(f"%{term}%")
            search_clause = (
                f" AND ({alias}.name ILIKE ${len(params)} "
                f"OR {alias}.description ILIKE ${len(params)} "
                f"OR {alias}.content ILIKE ${len(params)})"
            )
        count_query = (
            f"SELECT COUNT(*) AS total FROM {table} {alias} "
            f"WHERE {alias}.organization_id = $1{status_clause}{search_clause}"
        )
        query = (
            f"SELECT {alias}.* FROM {table} {alias} "
            f"WHERE {alias}.organization_id = $1{status_clause}{search_clause} "
            f"ORDER BY {alias}.name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
        params_with_page = [*params, limit, offset]

        count_row = await self._authorized_conn_fetchrow(count_query, *params)
        total_count = count_row["total"] if count_row else 0
        rows = await self._authorized_conn_fetch(query, *params_with_page)
        return {
            "items": [normalize(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def _authorized_conn_fetchrow(self, query: str, *params: object):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *params)

    async def _authorized_conn_fetch(self, query: str, *params: object):
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *params)

    async def _usable_by_id(self, table: str, resource_id: str, normalize) -> dict | None:
        """By-ID *usage* lookup: the row must be cleared for this principal.

        Requires an authorized pool — the clearance subquery decides, and an
        ungranted definition is simply absent. Management callers use the
        org-scoped get_* instead.
        """
        if not isinstance(self.pool, AuthorizedDatabasePool):
            raise TypeError("Definition usage reads require an AuthorizedDatabasePool")
        row = await self._authorized_conn_fetchrow(
            f"SELECT * FROM {table} WHERE id = $1", resource_id
        )
        return normalize(row) if row else None

    async def _usable_by_name(
        self, table: str, name: str, org_id: str, normalize,
    ) -> dict | None:
        """By-name *usage* lookup within the org; clearance decides."""
        if not isinstance(self.pool, AuthorizedDatabasePool):
            raise TypeError("Definition usage reads require an AuthorizedDatabasePool")
        row = await self._authorized_conn_fetchrow(
            f"SELECT * FROM {table} WHERE organization_id = $1 AND name = $2",
            UUID(org_id),
            name,
        )
        return normalize(row) if row else None

    async def list_agents_accessible_by(
        self,
        user_id: str,
        org_id: str,
        *,
        status: str | None = "active",
        limit: int = 100,
        offset: int = 0,
        requester_role: str | None = None,
    ) -> dict:
        """List agents cleared for *use* by the principal.

        ``user_id``/``requester_role`` are accepted for compatibility but a
        role grants nothing — owners/admins get agents only via explicit
        grants, like everyone else.
        """
        return await self._accessible_definitions(
            table="agent_definitions",
            alias="a",
            org_id=org_id,
            status=status,
            limit=limit,
            offset=offset,
            normalize=self._normalize_definition_row,
        )

    async def list_skills_accessible_by(
        self,
        user_id: str,
        org_id: str,
        *,
        status: str | None = "active",
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
        requester_role: str | None = None,
    ) -> dict:
        """List skills cleared for *use* by the principal (see agents)."""
        return await self._accessible_definitions(
            table="skill_definitions",
            alias="s",
            org_id=org_id,
            status=status,
            search=search,
            limit=limit,
            offset=offset,
            normalize=self._normalize_definition_row,
        )

    async def list_mcp_servers_accessible_by(
        self,
        user_id: str,
        org_id: str,
        *,
        status: str | None = "active",
        limit: int = 100,
        offset: int = 0,
        requester_role: str | None = None,
    ) -> dict:
        """List MCP servers cleared for *use* by the principal (see agents)."""
        return await self._accessible_definitions(
            table="mcp_server_configs",
            alias="ms",
            org_id=org_id,
            status=status,
            limit=limit,
            offset=offset,
            normalize=self._normalize_definition_row,
        )

    async def list_hooks_accessible_by(
        self,
        user_id: str,
        org_id: str,
        *,
        status: str | None = "active",
        limit: int = 100,
        offset: int = 0,
        requester_role: str | None = None,
    ) -> dict:
        """List hooks cleared for *use* by the principal (see agents)."""
        return await self._accessible_definitions(
            table="hook_definitions",
            alias="h",
            org_id=org_id,
            status=status,
            limit=limit,
            offset=offset,
            normalize=self._normalize_hook_row,
        )

    async def list_managed_tools_accessible_by(
        self,
        user_id: str,
        org_id: str,
        *,
        status: str | None = "active",
        limit: int = 100,
        offset: int = 0,
        requester_role: str | None = None,
    ) -> dict:
        """List managed tools cleared for *use* by the principal (see agents)."""
        return await self._accessible_definitions(
            table="managed_tool_definitions",
            alias="mt",
            org_id=org_id,
            status=status,
            limit=limit,
            offset=offset,
            normalize=self._normalize_tool_row,
        )

    async def get_usable_agent(self, agent_id: str) -> dict | None:
        """By-ID *usage* lookup for agents; clearance decides."""
        return await self._usable_by_id(
            "agent_definitions", agent_id, self._normalize_definition_row
        )

    async def get_usable_skill(self, skill_id: str) -> dict | None:
        return await self._usable_by_id(
            "skill_definitions", skill_id, self._normalize_definition_row
        )

    async def get_usable_mcp_server(self, config_id: str) -> dict | None:
        return await self._usable_by_id(
            "mcp_server_configs", config_id, self._normalize_definition_row
        )

    async def get_usable_hook(self, hook_id: str) -> dict | None:
        return await self._usable_by_id("hook_definitions", hook_id, self._normalize_hook_row)

    async def get_usable_managed_tool(self, tool_id: str) -> dict | None:
        return await self._usable_by_id(
            "managed_tool_definitions", tool_id, self._normalize_tool_row
        )

    async def get_usable_skill_by_name(self, name: str, org_id: str) -> dict | None:
        return await self._usable_by_name(
            "skill_definitions", name, org_id, self._normalize_definition_row
        )

    async def get_usable_hook_by_name(self, name: str, org_id: str) -> dict | None:
        return await self._usable_by_name(
            "hook_definitions", name, org_id, self._normalize_hook_row
        )

    async def get_usable_managed_tool_by_name(self, name: str, org_id: str) -> dict | None:
        return await self._usable_by_name(
            "managed_tool_definitions", name, org_id, self._normalize_tool_row
        )

    # ── Bulk/convenience ──────────────────────────────────────────────────

    async def get_pending_proposals(
        self,
        org_id: str,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict:
        """Get proposed definitions visible to the requester."""
        visibility = {
            "requester_user_id": requester_user_id,
            "requester_role": requester_role,
        }
        agent_result = await self.list_agents(
            org_id, status="proposed", limit=1000, **visibility
        )
        skill_result = await self.list_skills(
            org_id, status="proposed", limit=1000, **visibility
        )
        mcp_result = await self.list_mcp_servers(
            org_id, status="proposed", limit=1000, **visibility
        )
        hook_result = await self.list_hooks(
            org_id, status="proposed", limit=1000, **visibility
        )
        hook_owner_approved_result = await self.list_hooks(
            org_id, status="owner_approved", limit=1000, **visibility
        )
        tool_result = await self.list_managed_tools(
            org_id, status="proposed", limit=1000, **visibility
        )
        tool_owner_approved_result = await self.list_managed_tools(
            org_id, status="owner_approved", limit=1000, **visibility
        )
        agents = agent_result["items"]
        skills = skill_result["items"]
        mcp_servers = mcp_result["items"]
        hooks = [*hook_result["items"], *hook_owner_approved_result["items"]]
        managed_tools = [*tool_result["items"], *tool_owner_approved_result["items"]]
        return {
            "agents": agents,
            "skills": skills,
            "mcp_servers": mcp_servers,
            "hooks": hooks,
            "managed_tools": managed_tools,
            "total": sum(
                result["total_count"]
                for result in (
                    agent_result,
                    skill_result,
                    mcp_result,
                    hook_result,
                    hook_owner_approved_result,
                    tool_result,
                    tool_owner_approved_result,
                )
            ),
        }

    async def count_pending_proposals(
        self,
        org_id: str,
        requester_user_id: str,
        requester_role: str | None = None,
    ) -> int:
        """Count proposed definitions visible to the requester."""
        visibility = self._definition_access_condition("d", "$2", "$3")
        tables = (
            "agent_definitions",
            "skill_definitions",
            "mcp_server_configs",
            "hook_definitions",
            "managed_tool_definitions",
        )
        counts = " UNION ALL ".join(
            f"SELECT COUNT(*) AS total FROM {table} d "
            f"WHERE d.organization_id = $1 AND d.status IN ('proposed', 'owner_approved') "
            f"AND {visibility}"
            for table in tables
        )
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            total = await conn.fetchval(
                f"SELECT COALESCE(SUM(total), 0) FROM ({counts}) proposal_counts",
                org_id,
                requester_user_id,
                self._role_value(requester_role),
            )
        return int(total or 0)

    async def get_active_agent_with_grants(self, agent_name: str, org_id: str) -> dict | None:
        """Get an active agent by name with its skills and MCP servers loaded."""
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            agent = await conn.fetchrow(
                "SELECT * FROM agent_definitions "
                "WHERE name = $1 AND organization_id = $2 AND status = 'active'",
                agent_name,
                org_id,
            )
        if not agent:
            return None
        agent_dict = dict(agent)
        agent_dict["skills"] = await self.get_agent_skills(str(agent["id"]), org_id)
        agent_dict["mcp_servers"] = await self.get_agent_mcp_servers(str(agent["id"]), org_id)
        agent_dict["hooks"] = await self.get_agent_hooks(str(agent["id"]), org_id)
        agent_dict["managed_tools"] = await self.get_agent_managed_tools(str(agent["id"]), org_id)
        return agent_dict

    async def list_agents_with_grants(
        self,
        org_id: str,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
    ) -> dict:
        """List agents with their granted skills and MCP servers."""
        result = await self.list_agents(
            org_id, status=status, limit=limit, offset=offset,
            requester_user_id=requester_user_id, requester_role=requester_role,
        )
        for agent in result["items"]:
            agent["skills"] = await self.get_agent_skills(str(agent["id"]), org_id)
            agent["mcp_servers"] = await self.get_agent_mcp_servers(str(agent["id"]), org_id)
            agent["hooks"] = await self.get_agent_hooks(str(agent["id"]), org_id)
            agent["managed_tools"] = await self.get_agent_managed_tools(str(agent["id"]), org_id)
        return result

    async def sync_built_in_skills(self, org_id: str, skills_dir: str) -> int:
        """Sync .github/skills/ into the DB as built-in, active definitions.

        Upserts on (name, org_id). Returns count of synced skills.
        """
        import pathlib

        synced = 0
        skills_path = pathlib.Path(skills_dir)
        if not skills_path.is_dir():
            return 0
        for skill_dir in sorted(skills_path.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            raw = skill_file.read_text()
            # Parse YAML frontmatter
            name = skill_dir.name
            description = ""
            fm_match = re.match(r"^---\n(.*?)\n---", raw, re.DOTALL)
            if fm_match:
                for line in fm_match.group(1).splitlines():
                    if line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip("'\"")
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                # Update-then-insert (a plain ON CONFLICT DO UPDATE would fire
                # the BEFORE INSERT auth_id trigger on the conflict path,
                # leaking an unused auth_ids row every sync).
                await conn.execute(
                    """
                    UPDATE skill_definitions
                    SET content = $3, description = $2, scope = 'built-in',
                        updated_at = NOW()
                    WHERE (name, organization_id) = ($1, $4)
                      AND scope = 'built-in'
                      AND origin = 'disk'
                """,
                    name,
                    description,
                    raw,
                    org_id,
                )
                await conn.execute(
                    """
                    INSERT INTO skill_definitions
                        (name, description, content, status, scope, organization_id)
                    SELECT $1::varchar, $2::varchar, $3, 'active', 'built-in', $4::uuid
                    WHERE NOT EXISTS (
                        SELECT 1 FROM skill_definitions
                        WHERE name = $1 AND organization_id = $4
                    )
                """,
                    name,
                    description,
                    raw,
                    org_id,
                )
            synced += 1
        return synced

    async def sync_built_in_agents(self, org_id: str, agents_dir: str) -> int:
        """Sync .github/agents/definitions/ into the DB as built-in agents.

        Reads AGENT.md files from subdirectories. Upserts on (name, org_id).
        Parses ``skill_names`` and ``tool_names`` from YAML frontmatter and syncs
        the ``agent_skills`` and ``agent_managed_tools`` junction tables so the
        mappings always reflect the source files.

        Returns count of synced agents.
        """
        import pathlib

        synced = 0
        agents_path = pathlib.Path(agents_dir)
        if not agents_path.is_dir():
            return 0
        for agent_dir in sorted(agents_path.iterdir()):
            agent_file = agent_dir / "AGENT.md"
            if not agent_file.is_file():
                continue
            raw = agent_file.read_text()
            name = agent_dir.name
            description = ""
            skill_names: list[str] = []
            tool_names: list[str] = []
            fm_match = re.match(r"^---\n(.*?)\n---", raw, re.DOTALL)
            if fm_match:
                frontmatter = fm_match.group(1)
                for line in frontmatter.splitlines():
                    if line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip("'\"")
                skill_names = _parse_frontmatter_list(frontmatter, "skill_names")
                tool_names = _parse_frontmatter_list(frontmatter, "tool_names")
                # Shipped connectors this agent may use. A grant makes the
                # connector reachable; the credential is supplied per requesting
                # user, so one grant serves the whole organization.
                mcp_server_names = _parse_frontmatter_list(
                    frontmatter, "mcp_server_names"
                )
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                # Upsert the agent definition (update-then-insert: a plain
                # ON CONFLICT DO UPDATE fires the BEFORE INSERT auth_id
                # trigger on the conflict path, leaking an unused auth_ids row
                # every sync).
                agent_row = await conn.fetchrow(
                    """
                    UPDATE agent_definitions
                    SET content = $3, description = $2, scope = 'built-in',
                        updated_at = NOW()
                    WHERE (name, organization_id) = ($1, $4)
                      AND scope = 'built-in'
                      AND origin = 'disk'
                    RETURNING id
                """,
                    name,
                    description,
                    raw,
                    org_id,
                )
                if not agent_row:
                    agent_row = await conn.fetchrow(
                        """
                        INSERT INTO agent_definitions
                            (name, description, content, status, scope, organization_id)
                        SELECT $1::varchar, $2::varchar, $3, 'active', 'built-in', $4::uuid
                        WHERE NOT EXISTS (
                            SELECT 1 FROM agent_definitions
                            WHERE name = $1 AND organization_id = $4
                        )
                        RETURNING id
                    """,
                        name,
                        description,
                        raw,
                        org_id,
                    )
                if not agent_row:
                    # Agent exists but is not built-in scope (user-created) —
                    # don't touch its skill mappings.
                    synced += 1
                    continue
                agent_id = str(agent_row["id"])

                # Resolve declared skill names → skill IDs
                if skill_names:
                    skill_rows = await conn.fetch(
                        """
                        SELECT id, name FROM skill_definitions
                        WHERE name = ANY($1) AND organization_id = $2
                            AND status = 'active'
                        """,
                        skill_names,
                        org_id,
                    )
                    declared_skill_ids = {str(r["id"]) for r in skill_rows}
                else:
                    declared_skill_ids = set()

                # Current skill grants for this agent
                current_rows = await conn.fetch(
                    "SELECT skill_id FROM agent_skills WHERE agent_id = $1",
                    agent_id,
                )
                current_skill_ids = {str(r["skill_id"]) for r in current_rows}

                # Add missing grants. System-originated (granted_by stays NULL):
                # built-in skills are consent-exempt by scope, and a NULL
                # grantor must never look like "somebody else attached this".
                to_add = declared_skill_ids - current_skill_ids
                for sid in to_add:
                    await conn.execute(
                        "INSERT INTO agent_skills (agent_id, skill_id, granted_by) "
                        "VALUES ($1, $2, NULL) ON CONFLICT DO NOTHING",
                        agent_id,
                        sid,
                    )

                # Remove stale grants (only for built-in agents)
                to_remove = current_skill_ids - declared_skill_ids
                if to_remove:
                    await conn.execute(
                        "DELETE FROM agent_skills "
                        "WHERE agent_id = $1 AND skill_id = ANY($2)",
                        agent_id,
                        list(to_remove),
                    )

                if to_add or to_remove:
                    logger.info(
                        "Agent '%s': synced skills (+%d/-%d → %d total)",
                        name, len(to_add), len(to_remove),
                        len(declared_skill_ids),
                    )

                # Managed tools declared in frontmatter bind through the same
                # reconciliation, with one deliberate asymmetry: declared grants
                # are added but non-declared grants are never removed. Skills'
                # frontmatter is the complete binding for that agent, whereas tool
                # grants are also created by admins and owners in the UI and by
                # the daemon from other policies — a restart must not silently
                # strip a grant the frontmatter never knew about.
                if tool_names:
                    tool_rows = await conn.fetch(
                        """
                        SELECT id, name FROM managed_tool_definitions
                        WHERE name = ANY($1) AND organization_id = $2
                            AND status = 'active' AND scope = 'built-in'
                            AND origin = 'disk'
                        """,
                        tool_names,
                        org_id,
                    )
                    declared_tool_ids = {str(r["id"]) for r in tool_rows}
                else:
                    declared_tool_ids = set()

                current_tool_rows = await conn.fetch(
                    "SELECT tool_id FROM agent_managed_tools WHERE agent_id = $1",
                    agent_id,
                )
                current_tool_ids = {str(r["tool_id"]) for r in current_tool_rows}
                tools_added = declared_tool_ids - current_tool_ids
                for tid in tools_added:
                    # System-originated (granted_by stays NULL) and no
                    # config_override: built-in tools carry their own fixed
                    # contract, and a NULL grantor must never look like
                    # "somebody else attached this".
                    await conn.execute(
                        "INSERT INTO agent_managed_tools "
                        "(agent_id, tool_id, config_override, granted_by) "
                        "VALUES ($1, $2, NULL, NULL) ON CONFLICT DO NOTHING",
                        agent_id,
                        tid,
                    )
                if tools_added:
                    logger.info(
                        "Agent '%s': synced managed tools (+%d → %d total)",
                        name, len(tools_added), len(current_tool_ids | tools_added),
                    )

                # Shipped connectors declared in frontmatter bind the same way and
                # with the same asymmetry as managed tools: declared grants are
                # added, non-declared ones are never removed, because an admin may
                # have granted a connector the file does not know about.
                #
                # The grant is what makes the connector *reachable* by this agent.
                # It is not what makes it *work* — the credential resolves per
                # requesting user, so the same grant serves everyone in the org
                # without an admin creating one definition per person.
                if mcp_server_names:
                    server_rows = await conn.fetch(
                        """
                        SELECT id FROM mcp_server_configs
                        WHERE name = ANY($1) AND organization_id = $2
                            AND status = 'active' AND scope = 'built-in'
                            AND origin = 'disk'
                        """,
                        mcp_server_names,
                        org_id,
                    )
                    declared_server_ids = {str(r["id"]) for r in server_rows}
                else:
                    declared_server_ids = set()

                if declared_server_ids:
                    current_server_rows = await conn.fetch(
                        "SELECT mcp_server_id FROM agent_mcp_servers WHERE agent_id = $1",
                        agent_id,
                    )
                    current_server_ids = {str(r["mcp_server_id"]) for r in current_server_rows}
                    servers_added = declared_server_ids - current_server_ids
                    for sid in servers_added:
                        await conn.execute(
                            "INSERT INTO agent_mcp_servers "
                            "(agent_id, mcp_server_id, granted_by) "
                            "VALUES ($1, $2, NULL) ON CONFLICT DO NOTHING",
                            agent_id,
                            sid,
                        )
                    if servers_added:
                        logger.info(
                            "Agent '%s': synced MCP connectors (+%d → %d total)",
                            name,
                            len(servers_added),
                            len(current_server_ids | servers_added),
                        )
            synced += 1
        return synced

    async def sync_built_in_hooks(self, org_id: str) -> int:
        """Ensure built-in hook definitions exist for an organization."""
        builtins = [
            {
                "name": "file-memory-lookup",
                "description": (
                    "Looks up accessible memories for files referenced by tool calls "
                    "and injects them into the model context."
                ),
                "trigger_event": "tool_call",
                "action_type": "memory_lookup",
                "content": "",
                "config": {
                    "tool_names": ["*"],
                    "max_memories": 3,
                    "memory_type": "technical",
                    "include_archived": False,
                },
            },
            {
                "name": "message-memory-lookup",
                "description": (
                    "Injects memories relevant to the user's latest message "
                    "before the model is invoked (proactive injection pipeline)."
                ),
                "trigger_event": "before_model_call",
                "action_type": "message_memory_lookup",
                "content": "",
                "config": {
                    "max_memories": 3,
                    "include_archived": False,
                },
            },
        ]
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            for hook in builtins:
                # Update-then-insert (a plain ON CONFLICT DO UPDATE would fire
                # the BEFORE INSERT auth_id trigger on the conflict path,
                # leaking an unused auth_ids row every sync).
                await conn.execute(
                    """
                    UPDATE hook_definitions
                    SET description = $2, trigger_event = $3, action_type = $4,
                        content = $5, config = $6::text::jsonb,
                        status = 'active', scope = 'built-in', updated_at = NOW()
                    WHERE (name, organization_id) = ($1, $7)
                      AND scope = 'built-in'
                    """,
                    hook["name"],
                    hook["description"],
                    hook["trigger_event"],
                    hook["action_type"],
                    hook["content"],
                    json.dumps(hook["config"]),
                    org_id,
                )
                await conn.execute(
                    """
                    INSERT INTO hook_definitions
                        (name, description, trigger_event, action_type, content,
                         config, status, scope, organization_id)
                    SELECT $1::varchar, $2::varchar, $3, $4, $5,
                           $6::text::jsonb, 'active', 'built-in', $7
                    WHERE NOT EXISTS (
                        SELECT 1 FROM hook_definitions
                        WHERE name = $1 AND organization_id = $7
                    )
                    """,
                    hook["name"],
                    hook["description"],
                    hook["trigger_event"],
                    hook["action_type"],
                    hook["content"],
                    json.dumps(hook["config"]),
                    org_id,
                )
                grant_count = await self.grant_default_hooks_to_all_agents(org_id)
                if grant_count:
                    logger.info("Granted default hooks to %d agent(s)", grant_count)
        return len(builtins)

    async def sync_built_in_managed_tools(self, org_id: str) -> int:
        """Sync the Lucent-native tool catalog into an org as built-in tools.

        Every handler in :data:`~lucent.services.builtin_tool_catalog.BUILTIN_TOOL_CATALOG`
        becomes one ``runtime_type='builtin'`` row with ``scope='built-in'``, so a
        fresh deployment ships a usable toolset without an admin authoring
        anything. Built-in scope is consent-exempt (see
        ``lucent.agent_capabilities``), but a tool still has to be granted to an
        agent before that agent can call it.

        Returns the number of handlers synced. A handler that collides with a
        user's instance tool of the same name is skipped and logged rather than
        aborting the sync — one collision must not cost a deployment its whole
        toolset.
        """
        from lucent.services.builtin_tool_catalog import BUILTIN_TOOL_CATALOG

        synced = 0
        skipped = 0
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            for name, spec in BUILTIN_TOOL_CATALOG.items():
                try:
                    # The column is VARCHAR(64), and a truncation would be a
                    # silent contract change rather than a visible failure.
                    if len(name) > 64:
                        raise ValueError(
                            f"handler name {name!r} exceeds the 64-character "
                            "managed_tool_definitions.name limit"
                        )

                    # Deep copy: the catalog is module-level state. Handing its
                    # dict to a driver (or to any caller inspecting the row)
                    # must not make later mutations of that dict alter the
                    # reviewed schema every future sync copies.
                    input_schema = copy.deepcopy(spec.input_schema)
                    if len(spec.network_hosts):
                        # Narrowest policy that actually works: the handler's
                        # own known hosts.
                        network_policy = {
                            "network_mode": "allowlist",
                            "allowed_hosts": list(spec.network_hosts),
                        }
                    elif spec.uses_network:
                        # `web_fetch` reaches arbitrary public hosts by design;
                        # an empty allowlist would fail every call.
                        network_policy = {"network_mode": "bridge", "allowed_hosts": []}
                    else:
                        network_policy = {"network_mode": "none", "allowed_hosts": []}

                    timeout_seconds = int(
                        spec.default_config.get("timeout_seconds", 60)
                    )
                    resource_limits = {
                        "memory_limit": "512m",
                        "cpu_limit": 1.0,
                        "disk_limit": "1g",
                        "timeout_seconds": timeout_seconds,
                    }
                    # No options: `runtime_config` names the handler and nothing
                    # else, so a catalog change cannot leave a stale option that
                    # the executor silently ignores.
                    runtime_config = {"handler": spec.name}
                    auth_policy = {
                        "mode": "agent_grant",
                        "require_user_access": True,
                    }
                    env_vars: dict[str, str] = {}

                    self._validate_tool_shape(
                        input_schema=input_schema,
                        output_schema=None,
                        runtime_type="builtin",
                        source_code="",
                        entrypoint=spec.name,
                        requirements=[],
                        runtime_config=runtime_config,
                        env_vars=env_vars,
                        auth_policy=auth_policy,
                        network_policy=network_policy,
                        resource_limits=resource_limits,
                        timeout_seconds=timeout_seconds,
                    )

                    # Update-then-insert (a plain ON CONFLICT DO UPDATE would
                    # fire the BEFORE INSERT auth_id trigger on the conflict
                    # path, leaking an unused auth_ids row every sync).
                    update_result = await conn.execute(
                        """
                        UPDATE managed_tool_definitions
                        SET description = $2, input_schema = $3::text::jsonb,
                            runtime_type = 'builtin',
                            source_code = '', entrypoint = $4,
                            requirements = '[]'::jsonb,
                            runtime_config = $5::text::jsonb,
                            env_vars = '{}'::jsonb,
                            auth_policy = $6::text::jsonb,
                            network_policy = $7::text::jsonb,
                            resource_limits = $8::text::jsonb,
                            timeout_seconds = $9,
                            status = 'active', scope = 'built-in',
                            updated_at = NOW()
                        WHERE (name, organization_id) = ($1, $10)
                          AND scope = 'built-in'
                          AND origin = 'disk'
                        """,
                        name,
                        spec.description,
                        json.dumps(input_schema),
                        spec.name,
                        json.dumps(runtime_config),
                        json.dumps(auth_policy),
                        json.dumps(network_policy),
                        json.dumps(resource_limits),
                        timeout_seconds,
                        org_id,
                    )
                    inserted_id = await conn.fetchval(
                        """
                        INSERT INTO managed_tool_definitions
                            (name, description, input_schema, runtime_type,
                             source_code, entrypoint, requirements, runtime_config,
                             env_vars, auth_policy, network_policy,
                             resource_limits, timeout_seconds, status, scope,
                             organization_id)
                        SELECT $1::varchar, $2, $3::text::jsonb, 'builtin',
                               '', $4, '[]'::jsonb, $5::text::jsonb,
                               '{}'::jsonb, $6::text::jsonb, $7::text::jsonb,
                               $8::text::jsonb, $9, 'active', 'built-in', $10::uuid
                        WHERE NOT EXISTS (
                            SELECT 1 FROM managed_tool_definitions
                            WHERE name = $1 AND organization_id = $10
                        )
                        RETURNING id
                        """,
                        name,
                        spec.description,
                        json.dumps(input_schema),
                        spec.name,
                        json.dumps(runtime_config),
                        json.dumps(auth_policy),
                        json.dumps(network_policy),
                        json.dumps(resource_limits),
                        timeout_seconds,
                        org_id,
                    )
                    if inserted_id is None and update_result == "UPDATE 0":
                        # The name is taken by a row this sync is not allowed to
                        # touch (a user's instance tool). The unique constraint
                        # makes coexistence impossible, so skip this handler
                        # instead of failing every other tool.
                        existing_scope = await conn.fetchval(
                            "SELECT scope FROM managed_tool_definitions "
                            "WHERE name = $1 AND organization_id = $2",
                            name,
                            org_id,
                        )
                        logger.warning(
                            "Built-in tool %r skipped for org %s: the name is "
                            "already used by a %s-scoped tool",
                            name,
                            org_id,
                            existing_scope or "unknown",
                        )
                        skipped += 1
                        continue
                    synced += 1
                except Exception:
                    # Tolerant by design: one malformed handler (or a transient
                    # write failure) must not cost the org the rest of the
                    # catalog.
                    logger.warning(
                        "Failed to sync built-in tool %r for org %s",
                        name,
                        org_id,
                        exc_info=True,
                    )
                    skipped += 1

        if skipped:
            logger.warning(
                "Built-in tool sync for org %s: %d synced, %d skipped",
                org_id,
                synced,
                skipped,
            )
        await self._prune_retired_builtin_tools(org_id, set(BUILTIN_TOOL_CATALOG))
        return synced

    async def _prune_retired_builtin_tools(
        self, org_id: str, live_handlers: set[str]
    ) -> int:
        """Archive built-in tools whose handler no longer exists in the catalog.

        Without this, removing a handler leaves its row behind forever: the row
        is `active`, still granted to agents, and still resolvable by
        `run_managed_tool` — where it fails at call time with "no built-in tool
        named ...". A catalog entry that was cut for being redundant becomes a
        permanently broken tool on every existing deployment.

        Retired rows are *archived*, not deleted: the definition and its run
        history stay auditable, and `status != 'active'` already blocks execution.
        A user's instance-scoped tool of the same name is never touched.
        """
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            rows = await conn.fetch(
                "SELECT id, name, runtime_type, runtime_config "
                "FROM managed_tool_definitions "
                "WHERE organization_id = $1 AND scope = 'built-in' "
                "AND origin = 'disk' AND status <> 'archived'",
                org_id,
            )
            retired: list[str] = []
            for row in rows:
                if row["runtime_type"] != "builtin":
                    continue
                config = self._decode_json(row["runtime_config"], {}) or {}
                handler = str(config.get("handler") or row["name"] or "").strip()
                if handler and handler not in live_handlers:
                    retired.append(str(row["id"]))
            if retired:
                await conn.execute(
                    "UPDATE managed_tool_definitions "
                    "SET status = 'archived', updated_at = NOW() "
                    "WHERE id = ANY($1::uuid[]) AND organization_id = $2",
                    retired,
                    org_id,
                )
                logger.info(
                    "Archived %d retired built-in tool(s) for org %s",
                    len(retired),
                    org_id,
                )
            return len(retired)

    # ── Instance catalog: operator-provisioned built-ins ──────────────────
    #
    # A hyperadmin provisions a tool *into a specific organization* rather than
    # creating one shared NULL-org row. That costs one row per tenant and buys an
    # explicit security boundary on every row: no cross-tenant read, no NULL-org
    # special case in any ACL, and a per-tenant revoke that is a plain DELETE.
    #
    # The row is `scope='built-in'` (so it inherits consent-exemption, org-wide
    # readability, daemon protection) but `origin='operator'` (so the disk sync
    # never adopts, overwrites, or prunes it). Both facts are needed; see
    # migration 160.

    async def provision_catalog_mcp_server(
        self,
        *,
        org_id: str,
        name: str,
        description: str,
        server_type: str,
        url: str | None,
        created_by: str,
        headers: dict | None = None,
        command: str | None = None,
        args: list | None = None,
        env_vars: dict | None = None,
        credential_slots: list | None = None,
        status: str = "active",
    ) -> dict:
        """Create or replace an operator-provisioned MCP connector for one org.

        Upsert on ``(name, org_id)`` restricted to operator rows, so re-running a
        provisioning script is idempotent but never overwrites a disk-shipped
        connector or a tenant's own definition of the same name — the caller gets
        a clear error instead.
        """
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            existing = await conn.fetchrow(
                "SELECT id, scope, origin FROM mcp_server_configs "
                "WHERE name = $1 AND organization_id = $2",
                name,
                org_id,
            )
            if existing is not None and existing["origin"] != "operator":
                raise BuiltInProtectionError(
                    f"'{name}' already exists in this organization as a "
                    f"{existing['scope']}-scoped {existing['origin']} definition. "
                    "Choose a different catalog name."
                )
            if existing is not None:
                row = await conn.fetchrow(
                    """
                    UPDATE mcp_server_configs
                    SET description = $2, server_type = $3, url = $4,
                        headers = $5::text::jsonb, command = $6,
                        args = $7::text::jsonb, env_vars = $8::text::jsonb,
                        credential_slots = $9::text::jsonb,
                        status = $10, scope = 'built-in', origin = 'operator',
                        updated_at = NOW()
                    WHERE id = $1 AND organization_id = $11
                    RETURNING *
                    """,
                    existing["id"],
                    description,
                    server_type,
                    url,
                    json.dumps(headers or {}),
                    command,
                    json.dumps(args or []),
                    json.dumps(env_vars or {}),
                    json.dumps(credential_slots or []),
                    status,
                    org_id,
                )
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO mcp_server_configs
                        (name, description, server_type, url, headers, command,
                         args, env_vars, credential_slots, status, scope, origin,
                         organization_id, created_by)
                    VALUES ($1, $2, $3, $4, $5::text::jsonb, $6, $7::text::jsonb,
                            $8::text::jsonb, $9::text::jsonb, $10, 'built-in',
                            'operator', $11, $12)
                    RETURNING *
                    """,
                    name,
                    description,
                    server_type,
                    url,
                    json.dumps(headers or {}),
                    command,
                    json.dumps(args or []),
                    json.dumps(env_vars or {}),
                    json.dumps(credential_slots or []),
                    status,
                    org_id,
                    created_by,
                )
        result = self._normalize_mcp_row(row)
        await self._audit(
            DEFINITION_CREATE,
            org_id,
            "mcp_server",
            str(result["id"]),
            user_id=created_by,
            notes=(
                f"Operator provisioned connector '{name}' "
                f"({'replaced' if existing is not None else 'created'})"
            ),
        )
        return result

    async def revoke_catalog_mcp_server(
        self, *, org_id: str, name: str, actor_id: str
    ) -> bool:
        """Remove a single operator-provisioned connector from one organization.

        Refuses anything it did not create: a disk-shipped built-in must be
        changed on disk, and a tenant's own definition belongs to that tenant.
        """
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            deleted = await conn.fetchval(
                "DELETE FROM mcp_server_configs "
                "WHERE name = $1 AND organization_id = $2 "
                "AND scope = 'built-in' AND origin = 'operator' "
                "RETURNING id",
                name,
                org_id,
            )
        if not deleted:
            return False
        await self._audit(
            DEFINITION_DELETE,
            org_id,
            "mcp_server",
            str(deleted),
            user_id=actor_id,
            notes=f"Operator revoked connector '{name}'",
        )
        return True

    async def list_catalog_definitions(
        self, org_id: str, definition_type: str
    ) -> list[dict]:
        """Operator-provisioned definitions currently live in one organization."""
        table, normalizer = {
            "mcp_server": ("mcp_server_configs", self._normalize_mcp_row),
            "managed_tool": ("managed_tool_definitions", self._normalize_tool_row),
            "skill": ("skill_definitions", self._normalize_definition_row),
            "agent": ("agent_definitions", self._normalize_definition_row),
        }[definition_type]
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {table} "
                "WHERE organization_id = $1 AND origin = 'operator' "
                "ORDER BY name",
                org_id,
            )
        return [normalizer(row) for row in rows if row]

    async def list_catalog_organizations(self, definition_type: str) -> list[dict]:
        """Every organization that has at least one operator-provisioned row.

        This is the operator's fleet view: what did I push, and where. Grouped in
        SQL so it stays one query regardless of tenant count.
        """
        table = {
            "mcp_server": "mcp_server_configs",
            "managed_tool": "managed_tool_definitions",
            "skill": "skill_definitions",
            "agent": "agent_definitions",
        }[definition_type]
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT organization_id, count(*) AS definition_count, "
                f"   array_agg(name ORDER BY name) AS names "
                f"FROM {table} WHERE origin = 'operator' "
                f"GROUP BY organization_id ORDER BY organization_id"
            )
        return [
            {
                "organization_id": str(row["organization_id"]),
                "definition_count": int(row["definition_count"]),
                "names": list(row["names"] or []),
            }
            for row in rows
        ]

    async def sync_built_in_mcp_connectors(self, org_id: str) -> int:
        """Seed the shipped hosted MCP connectors into an organization.

        A built-in connector is created ``scope='built-in'`` and ``status='active'``
        with **no owner**, so it is readable org-wide and consent-exempt exactly like
        a built-in skill or managed tool. The point is that no administrator has to
        author one per person: the endpoint is shared, and each user supplies their
        own credential through the declared ``credential_slots``.

        Credentials are stored as ``secret://`` references in ``headers``, which
        resolve per *calling* user (own secret first, then a group share, then an
        explicit grant). Nothing here holds a token.

        Idempotent, and skips a connector whose name is already taken by a
        user-authored definition rather than failing the whole sync.
        """
        from lucent.services.builtin_connectors import BUILTIN_CONNECTORS

        synced = 0
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            for connector in BUILTIN_CONNECTORS:
                try:
                    if len(connector.name) > 64:
                        raise ValueError(
                            f"connector name {connector.name!r} exceeds the "
                            "64-character mcp_server_configs.name limit"
                        )
                    headers = connector.header_templates()
                    env_vars = connector.env_vars()
                    slots = json.dumps(connector.descriptor()["credential_slots"])

                    # Update-then-insert, matching the other built-in syncs: an
                    # ON CONFLICT DO UPDATE would fire the BEFORE INSERT auth_id
                    # trigger on the conflict path and leak a row each sync.
                    updated = await conn.execute(
                        """
                        UPDATE mcp_server_configs
                        SET description = $3, server_type = $4, url = $5,
                            headers = $6::text::jsonb, env_vars = $7::text::jsonb,
                            credential_slots = $8::text::jsonb,
                            status = 'active', scope = 'built-in',
                            updated_at = NOW()
                        WHERE (name, organization_id) = ($1, $2)
                          AND scope = 'built-in'
                          AND origin = 'disk'
                        """,
                        connector.name,
                        org_id,
                        connector.summary,
                        connector.transport,
                        connector.effective_url,
                        json.dumps(headers),
                        json.dumps(env_vars),
                        slots,
                    )
                    if updated != "UPDATE 0":
                        synced += 1
                        continue

                    inserted = await conn.fetchval(
                        """
                        INSERT INTO mcp_server_configs
                            (name, description, server_type, url, args, headers,
                             env_vars, credential_slots, status, scope,
                             organization_id)
                        SELECT $1::varchar, $2, $3, $4, '[]'::jsonb,
                               $5::text::jsonb, $6::text::jsonb, $7::text::jsonb,
                               'active', 'built-in', $8::uuid
                        WHERE NOT EXISTS (
                            SELECT 1 FROM mcp_server_configs
                            WHERE name = $1 AND organization_id = $8
                        )
                        RETURNING id
                        """,
                        connector.name,
                        connector.summary,
                        connector.transport,
                        connector.effective_url,
                        json.dumps(headers),
                        json.dumps(env_vars),
                        slots,
                        org_id,
                    )
                    if inserted is None:
                        logger.warning(
                            "Built-in connector %r skipped for org %s: the name is "
                            "already used by a non-built-in definition",
                            connector.name,
                            org_id,
                        )
                        continue
                    synced += 1
                except Exception:
                    # One bad connector must not cost the org the rest.
                    logger.warning(
                        "Failed to sync built-in MCP connector %r for org %s",
                        connector.name,
                        org_id,
                        exc_info=True,
                    )
        if synced:
            logger.info(
                "Seeded %d built-in MCP connector(s) for org %s", synced, org_id
            )
        return synced
