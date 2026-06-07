"""Repository for agent, skill, MCP server, hook, and managed tool definitions.

Handles CRUD, approval workflow, and access grants.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from asyncpg import Pool

from lucent.access_control import build_access_clause
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

logger = logging.getLogger(__name__)

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
VALID_HOOK_ACTION_TYPES = frozenset({"memory_lookup", "static_context", "command"})
DEFAULT_AGENT_HOOK_NAMES = ("file-memory-lookup",)


class BuiltInProtectionError(Exception):
    """Raised when a daemon tries to modify a built-in definition."""

    def __init__(self, msg: str = BUILTIN_PROTECTION_MSG):
        super().__init__(msg)


class DefinitionRepository:
    """Repository for managing agent, skill, and MCP server definitions."""

    def __init__(self, pool: Pool, audit_repo: AuditRepository | None = None):
        self.pool = pool
        self.audit_repo = audit_repo

    @staticmethod
    def _role_value(role: str | None) -> str:
        return role or "member"

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
        instance definitions default to org-shared because the daemon is an
        actor, not a useful capability owner in the UI/ACL model.
        """
        if scope == "built-in" or shared_with_org or not created_by:
            return None
        try:
            async with self.pool.acquire() as conn:
                role = await conn.fetchval(
                    "SELECT role FROM users WHERE id = $1 AND organization_id = $2",
                    UUID(created_by),
                    UUID(org_id),
                )
        except Exception:
            role = None
        if role == "daemon":
            return None
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
        if input_schema is not None and not isinstance(input_schema, dict):
            raise ValueError("input_schema must be a JSON object")
        if output_schema is not None and not isinstance(output_schema, dict):
            raise ValueError("output_schema must be a JSON object when provided")
        if runtime_type != "python":
            raise ValueError("runtime_type must be 'python'")
        if not source_code or not source_code.strip():
            raise ValueError("source_code is required")
        if not entrypoint or not entrypoint.isidentifier():
            raise ValueError("entrypoint must be a Python identifier")
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
        network_mode = (network_policy or {}).get("network_mode", "none")
        if network_mode not in {"none", "bridge", "allowlist"}:
            raise ValueError("network_policy.network_mode must be none, bridge, or allowlist")
        allowed_hosts = (network_policy or {}).get("allowed_hosts", [])
        if allowed_hosts is not None and not isinstance(allowed_hosts, list):
            raise ValueError("network_policy.allowed_hosts must be a JSON array")
        if timeout_seconds < 1 or timeout_seconds > 3600:
            raise ValueError("timeout_seconds must be between 1 and 3600")

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
        async with self.pool.acquire() as conn:
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
        """Fire-and-forget audit log. Never raises."""
        if self.audit_repo is None:
            return
        try:
            await self.audit_repo.log_definition_event(
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
            base += " AND " + build_access_clause(
                resource_type="agent", uid_param=len(params) - 1, role_param=len(params),
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

        async with self.pool.acquire() as conn:
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
            acl_sql = " AND " + build_access_clause(
                resource_type="agent", uid_param=3, role_param=4, alias="a",
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, agent_id, org_id, approved_by)
        result = self._normalize_definition_row(row)
        if result:
            await self._audit(
                DEFINITION_REJECT, org_id, "agent", agent_id,
                user_id=approved_by, notes=f"Rejected agent '{agent_id}'",
            )
        return result

    async def delete_agent(self, agent_id: str, org_id: str) -> bool:
        async with self.pool.acquire() as conn:
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
            base += " AND " + build_access_clause(
                resource_type="skill", uid_param=len(params) - 1, role_param=len(params),
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

        async with self.pool.acquire() as conn:
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
            acl_sql = " AND " + build_access_clause(resource_type="skill", uid_param=3, role_param=4)
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, skill_id, org_id, approved_by)
        result = self._normalize_definition_row(row)
        if result:
            await self._audit(
                DEFINITION_REJECT, org_id, "skill", skill_id,
                user_id=approved_by, notes=f"Rejected skill '{skill_id}'",
            )
        return result

    async def delete_skill(self, skill_id: str, org_id: str) -> bool:
        async with self.pool.acquire() as conn:
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
            base += " AND " + build_access_clause(
                resource_type="mcp_server", uid_param=len(params) - 1, role_param=len(params),
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

        async with self.pool.acquire() as conn:
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
            acl_sql = " AND " + build_access_clause(resource_type="mcp_server", uid_param=3, role_param=4)
        async with self.pool.acquire() as conn:
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
            base += " AND " + build_access_clause(
                resource_type="hook", uid_param=len(params) - 1, role_param=len(params),
            )
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"""
            SELECT id, name, description, trigger_event, action_type, status, scope,
                   created_by, approved_by, approved_at,
                   owner_user_id, owner_group_id,
                     proposal_reason, proposal_evidence,
                   created_at, updated_at
            {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        params_with_page = [*params, limit, offset]

        async with self.pool.acquire() as conn:
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
            acl_sql = " AND " + build_access_clause(resource_type="hook", uid_param=3, role_param=4)
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        result = self._normalize_hook_row(row)
        if result:
            await self._audit(
                DEFINITION_UPDATE, org_id, "hook", hook_id,
                context={"updated_fields": [k for k in kwargs.keys()]},
                notes=f"Updated hook '{hook_id}'",
            )
        return result

    async def approve_hook(self, hook_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE hook_definitions
            SET status = 'active', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, hook_id, org_id, approved_by)
        result = self._normalize_hook_row(row)
        if result:
            await self._audit(
                DEFINITION_APPROVE, org_id, "hook", hook_id,
                user_id=approved_by, notes=f"Approved hook '{hook_id}'",
            )
        return result

    async def reject_hook(self, hook_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE hook_definitions
            SET status = 'rejected', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, hook_id, org_id, approved_by)
        result = self._normalize_hook_row(row)
        if result:
            await self._audit(
                DEFINITION_REJECT, org_id, "hook", hook_id,
                user_id=approved_by, notes=f"Rejected hook '{hook_id}'",
            )
        return result

    async def delete_hook(self, hook_id: str, org_id: str) -> bool:
        async with self.pool.acquire() as conn:
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
            base += " AND " + build_access_clause(
                resource_type="managed_tool", uid_param=len(params) - 1, role_param=len(params),
            )
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"""
            SELECT id, name, description, runtime_type, entrypoint,
                   input_schema, output_schema, status, scope,
                   created_by, approved_by, approved_at,
                   owner_user_id, owner_group_id,
                   auth_policy, network_policy, resource_limits,
                   proposal_reason, proposal_evidence,
                   created_at, updated_at
            {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        params_with_page = [*params, limit, offset]

        async with self.pool.acquire() as conn:
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
            acl_sql = " AND " + build_access_clause(resource_type="managed_tool", uid_param=3, role_param=4)
        async with self.pool.acquire() as conn:
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
            acl_sql = " AND " + build_access_clause(resource_type="managed_tool", uid_param=3, role_param=4)
        async with self.pool.acquire() as conn:
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
        source_code: str,
        org_id: str,
        created_by: str,
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
        input_schema = input_schema or {"type": "object", "properties": {}}
        auth_policy = auth_policy or {"mode": "agent_grant", "require_user_access": True}
        network_policy = network_policy or {"network_mode": "none", "allowed_hosts": []}
        resource_limits = resource_limits or {
            "memory_limit": "512m", "cpu_limit": 1.0,
            "disk_limit": "1g", "timeout_seconds": timeout_seconds,
        }
        self._validate_tool_shape(
            input_schema=input_schema,
            output_schema=output_schema,
            runtime_type=runtime_type,
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
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                name,
                description,
                json.dumps(input_schema),
                json.dumps(output_schema) if output_schema is not None else None,
                runtime_type,
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
        self._validate_tool_shape(
            input_schema=kwargs.get("input_schema", current.get("input_schema")),
            output_schema=kwargs.get("output_schema", current.get("output_schema")),
            runtime_type=kwargs.get("runtime_type", current.get("runtime_type", "python")),
            source_code=kwargs.get("source_code", current.get("source_code", "")),
            entrypoint=kwargs.get("entrypoint", current.get("entrypoint", "handler")),
            requirements=kwargs.get("requirements", current.get("requirements", [])),
            runtime_config=kwargs.get("runtime_config", current.get("runtime_config", {})),
            env_vars=kwargs.get("env_vars", current.get("env_vars", {})),
            auth_policy=kwargs.get("auth_policy", current.get("auth_policy", {})),
            network_policy=kwargs.get("network_policy", current.get("network_policy", {})),
            resource_limits=kwargs.get("resource_limits", current.get("resource_limits", {})),
            timeout_seconds=kwargs.get("timeout_seconds", current.get("timeout_seconds", 300)),
        )
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
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        result = self._normalize_tool_row(row)
        if result:
            await self._audit(
                DEFINITION_UPDATE, org_id, "managed_tool", tool_id,
                context={"updated_fields": [k for k in kwargs.keys()]},
                notes=f"Updated managed tool '{tool_id}'",
            )
        return result

    async def approve_managed_tool(
        self, tool_id: str, org_id: str, approved_by: str,
    ) -> dict | None:
        query = """
            UPDATE managed_tool_definitions
            SET status = 'active', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, tool_id, org_id, approved_by)
        result = self._normalize_tool_row(row)
        if result:
            await self._audit(
                DEFINITION_APPROVE, org_id, "managed_tool", tool_id,
                user_id=approved_by, notes=f"Approved managed tool '{tool_id}'",
            )
        return result

    async def reject_managed_tool(
        self, tool_id: str, org_id: str, approved_by: str,
    ) -> dict | None:
        query = """
            UPDATE managed_tool_definitions
            SET status = 'rejected', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, tool_id, org_id, approved_by)
        result = self._normalize_tool_row(row)
        if result:
            await self._audit(
                DEFINITION_REJECT, org_id, "managed_tool", tool_id,
                user_id=approved_by, notes=f"Rejected managed tool '{tool_id}'",
            )
        return result

    async def delete_managed_tool(self, tool_id: str, org_id: str) -> bool:
        async with self.pool.acquire() as conn:
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
        granted_by: str | None = None,
        grant_reason: str | None = None,
        grant_override: bool = False,
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_managed_tools "
                    "(agent_id, tool_id, config_override, granted_by, "
                    "grant_reason, grant_override) "
                    "VALUES ($1, $2, $3::text::jsonb, $4, $5, $6) "
                    "ON CONFLICT (agent_id, tool_id) DO UPDATE "
                    "SET config_override = EXCLUDED.config_override, "
                    "granted_by = EXCLUDED.granted_by, granted_at = NOW(), "
                    "grant_reason = EXCLUDED.grant_reason, "
                    "grant_override = EXCLUDED.grant_override",
                    agent_id,
                    tool_id,
                    json.dumps(config_override) if config_override is not None else None,
                    granted_by or user_id,
                    grant_reason,
                    grant_override,
                )
            if org_id:
                await self._audit(
                    DEFINITION_GRANT, org_id, "managed_tool", tool_id,
                    user_id=user_id,
                    context={"agent_id": agent_id},
                    notes=f"Granted managed tool '{tool_id}' to agent '{agent_id}'",
                )
            return True
        except Exception:
            logger.error("Failed to grant managed tool %s to agent %s", tool_id, agent_id,
                         exc_info=True)
            return False

    async def revoke_managed_tool(
        self, agent_id: str, tool_id: str,
        org_id: str | None = None, user_id: str | None = None,
    ) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agent_managed_tools WHERE agent_id = $1 AND tool_id = $2",
                agent_id,
                tool_id,
            )
        revoked = result == "DELETE 1"
        if revoked and org_id:
            await self._audit(
                DEFINITION_REVOKE, org_id, "managed_tool", tool_id,
                user_id=user_id,
                context={"agent_id": agent_id},
                notes=f"Revoked managed tool '{tool_id}' from agent '{agent_id}'",
            )
        return revoked

    async def get_agent_managed_tools(self, agent_id: str) -> list[dict]:
        query = """
            SELECT t.*, amt.config_override FROM managed_tool_definitions t
            JOIN agent_managed_tools amt ON t.id = amt.tool_id
            WHERE amt.agent_id = $1 AND t.status = 'active'
            ORDER BY t.name
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, agent_id)
        return [self._normalize_tool_row(r) for r in rows]

    async def is_managed_tool_granted_to_agent(self, agent_id: str, tool_id: str) -> bool:
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
    ) -> dict | None:
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, server_id, org_id, approved_by)
        result = self._normalize_mcp_row(row)
        if result:
            await self._audit(
                DEFINITION_REJECT, org_id, "mcp_server", server_id,
                user_id=approved_by, notes=f"Rejected MCP server '{server_id}'",
            )
        return result

    # ── Access Grants ─────────────────────────────────────────────────────

    async def grant_skill(
        self, agent_id: str, skill_id: str,
        org_id: str | None = None, user_id: str | None = None,
        granted_by: str | None = None,
        grant_reason: str | None = None,
        grant_override: bool = False,
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_skills "
                    "(agent_id, skill_id, granted_by, grant_reason, grant_override) "
                    "VALUES ($1, $2, $3, $4, $5) "
                    "ON CONFLICT (agent_id, skill_id) DO UPDATE SET "
                    "granted_by = EXCLUDED.granted_by, granted_at = NOW(), "
                    "grant_reason = EXCLUDED.grant_reason, "
                    "grant_override = EXCLUDED.grant_override",
                    agent_id,
                    skill_id,
                    granted_by or user_id,
                    grant_reason,
                    grant_override,
                )
            if org_id:
                await self._audit(
                    DEFINITION_GRANT, org_id, "skill", skill_id,
                    user_id=user_id,
                    context={"agent_id": agent_id},
                    notes=f"Granted skill '{skill_id}' to agent '{agent_id}'",
                )
            return True
        except Exception:
            logger.error("Failed to grant skill %s to agent %s", skill_id, agent_id, exc_info=True)
            return False

    async def revoke_skill(
        self, agent_id: str, skill_id: str,
        org_id: str | None = None, user_id: str | None = None,
    ) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agent_skills WHERE agent_id = $1 AND skill_id = $2",
                agent_id,
                skill_id,
            )
        revoked = result == "DELETE 1"
        if revoked and org_id:
            await self._audit(
                DEFINITION_REVOKE, org_id, "skill", skill_id,
                user_id=user_id,
                context={"agent_id": agent_id},
                notes=f"Revoked skill '{skill_id}' from agent '{agent_id}'",
            )
        return revoked

    async def grant_mcp_server(
        self, agent_id: str, mcp_server_id: str,
        org_id: str | None = None, user_id: str | None = None,
        granted_by: str | None = None,
        grant_reason: str | None = None,
        grant_override: bool = False,
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_mcp_servers "
                    "(agent_id, mcp_server_id, granted_by, grant_reason, grant_override) "
                    "VALUES ($1, $2, $3, $4, $5) "
                    "ON CONFLICT (agent_id, mcp_server_id) DO UPDATE SET "
                    "granted_by = EXCLUDED.granted_by, granted_at = NOW(), "
                    "grant_reason = EXCLUDED.grant_reason, "
                    "grant_override = EXCLUDED.grant_override",
                    agent_id,
                    mcp_server_id,
                    granted_by or user_id,
                    grant_reason,
                    grant_override,
                )
            if org_id:
                await self._audit(
                    DEFINITION_GRANT, org_id, "mcp_server", mcp_server_id,
                    user_id=user_id,
                    context={"agent_id": agent_id},
                    notes=f"Granted MCP server '{mcp_server_id}' to agent '{agent_id}'",
                )
            return True
        except Exception:
            logger.error(
                "Failed to grant MCP server %s to agent %s",
                mcp_server_id,
                agent_id,
                exc_info=True,
            )
            return False

    async def revoke_mcp_server(
        self, agent_id: str, mcp_server_id: str,
        org_id: str | None = None, user_id: str | None = None,
    ) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agent_mcp_servers WHERE agent_id = $1 AND mcp_server_id = $2",
                agent_id,
                mcp_server_id,
            )
        revoked = result == "DELETE 1"
        if revoked and org_id:
            await self._audit(
                DEFINITION_REVOKE, org_id, "mcp_server", mcp_server_id,
                user_id=user_id,
                context={"agent_id": agent_id},
                notes=f"Revoked MCP server '{mcp_server_id}' from agent '{agent_id}'",
            )
        return revoked

    async def grant_hook(
        self, agent_id: str, hook_id: str,
        org_id: str | None = None, user_id: str | None = None,
        config_override: dict | None = None,
        granted_by: str | None = None,
        grant_reason: str | None = None,
        grant_override: bool = False,
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_hooks "
                    "(agent_id, hook_id, config_override, granted_by, "
                    "grant_reason, grant_override) "
                    "VALUES ($1, $2, $3::text::jsonb, $4, $5, $6) "
                    "ON CONFLICT (agent_id, hook_id) DO UPDATE "
                    "SET config_override = EXCLUDED.config_override, "
                    "granted_by = EXCLUDED.granted_by, granted_at = NOW(), "
                    "grant_reason = EXCLUDED.grant_reason, "
                    "grant_override = EXCLUDED.grant_override",
                    agent_id,
                    hook_id,
                    json.dumps(config_override) if config_override is not None else None,
                    granted_by or user_id,
                    grant_reason,
                    grant_override,
                )
            if org_id:
                await self._audit(
                    DEFINITION_GRANT, org_id, "hook", hook_id,
                    user_id=user_id,
                    context={"agent_id": agent_id},
                    notes=f"Granted hook '{hook_id}' to agent '{agent_id}'",
                )
            return True
        except Exception:
            logger.error("Failed to grant hook %s to agent %s", hook_id, agent_id, exc_info=True)
            return False

    async def revoke_hook(
        self, agent_id: str, hook_id: str,
        org_id: str | None = None, user_id: str | None = None,
    ) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agent_hooks WHERE agent_id = $1 AND hook_id = $2",
                agent_id,
                hook_id,
            )
        revoked = result == "DELETE 1"
        if revoked and org_id:
            await self._audit(
                DEFINITION_REVOKE, org_id, "hook", hook_id,
                user_id=user_id,
                context={"agent_id": agent_id},
                notes=f"Revoked hook '{hook_id}' from agent '{agent_id}'",
            )
        return revoked

    async def get_agent_skills(self, agent_id: str) -> list[dict]:
        query = """
            SELECT s.* FROM skill_definitions s
            JOIN agent_skills ags ON s.id = ags.skill_id
            WHERE ags.agent_id = $1 AND s.status = 'active'
            ORDER BY s.name
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, agent_id)
        return [self._normalize_definition_row(r) for r in rows]

    async def get_agent_mcp_servers(self, agent_id: str) -> list[dict]:
        query = """
            SELECT m.*, agm.allowed_tools FROM mcp_server_configs m
            JOIN agent_mcp_servers agm ON m.id = agm.mcp_server_id
            WHERE agm.agent_id = $1 AND m.status = 'active'
            ORDER BY m.name
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, agent_id)
        return [self._normalize_mcp_row(r) for r in rows]

    async def get_agent_hooks(self, agent_id: str) -> list[dict]:
        query = """
            SELECT h.*, ah.config_override FROM hook_definitions h
            JOIN agent_hooks ah ON h.id = ah.hook_id
            WHERE ah.agent_id = $1 AND h.status = 'active'
            ORDER BY h.name
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, agent_id)
        return [self._normalize_hook_row(r) for r in rows]

    async def grant_default_hooks_to_agent(self, agent_id: str, org_id: str) -> int:
        """Grant built-in default hooks to one agent without overriding explicit config."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO agent_hooks (agent_id, hook_id)
                SELECT $1, h.id
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
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO agent_hooks (agent_id, hook_id)
                SELECT a.id, h.id
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
            result = await conn.execute(query, server_id, org_id)
        return result == "UPDATE 1"

    # ── Access Control Queries ───────────────────────────────────────────

    async def list_agents_accessible_by(
        self, user_id: str, org_id: str,
        status: str | None = "active",
        limit: int = 100,
        offset: int = 0,
        requester_role: str | None = None,
    ) -> dict:
        """List agents accessible to a user: owned, group-owned, or built-in."""
        role = self._role_value(requester_role)
        base = """
            FROM agent_definitions
            WHERE organization_id = $1
            AND """ + build_access_clause(resource_type="agent", uid_param=2, role_param=3) + "\n"
        params: list[Any] = [org_id, user_id, role]
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"SELECT * {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        params_with_page = [*params, limit, offset]

        async with self.pool.acquire() as conn:
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

    async def list_skills_accessible_by(
        self, user_id: str, org_id: str,
        status: str | None = "active",
        limit: int = 100,
        offset: int = 0,
        requester_role: str | None = None,
    ) -> dict:
        """List skills accessible to a user: owned, group-owned, or built-in."""
        role = self._role_value(requester_role)
        base = """
            FROM skill_definitions
            WHERE organization_id = $1
            AND """ + build_access_clause(resource_type="skill", uid_param=2, role_param=3) + "\n"
        params: list[Any] = [org_id, user_id, role]
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"SELECT * {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        params_with_page = [*params, limit, offset]

        async with self.pool.acquire() as conn:
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

    async def list_mcp_servers_accessible_by(
        self, user_id: str, org_id: str,
        status: str | None = "active",
        limit: int = 100,
        offset: int = 0,
        requester_role: str | None = None,
    ) -> dict:
        """List MCP servers accessible to a user: owned, group-owned, or built-in."""
        role = self._role_value(requester_role)
        base = """
            FROM mcp_server_configs
            WHERE organization_id = $1
            AND """ + build_access_clause(resource_type="mcp_server", uid_param=2, role_param=3) + "\n"
        params: list[Any] = [org_id, user_id, role]
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"SELECT * {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        params_with_page = [*params, limit, offset]

        async with self.pool.acquire() as conn:
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

    async def list_hooks_accessible_by(
        self, user_id: str, org_id: str,
        status: str | None = "active",
        limit: int = 100,
        offset: int = 0,
        requester_role: str | None = None,
    ) -> dict:
        """List hooks accessible to a user: owned, group-owned, or built-in."""
        role = self._role_value(requester_role)
        base = """
            FROM hook_definitions
            WHERE organization_id = $1
            AND """ + build_access_clause(resource_type="hook", uid_param=2, role_param=3) + "\n"
        params: list[Any] = [org_id, user_id, role]
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"SELECT * {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        params_with_page = [*params, limit, offset]

        async with self.pool.acquire() as conn:
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

    # ── Bulk/convenience ──────────────────────────────────────────────────

    async def get_pending_proposals(self, org_id: str) -> dict:
        """Get all proposed (pending approval) definitions."""
        agents = (await self.list_agents(org_id, status="proposed"))["items"]
        skills = (await self.list_skills(org_id, status="proposed"))["items"]
        mcp_servers = (await self.list_mcp_servers(org_id, status="proposed"))["items"]
        hooks = (await self.list_hooks(org_id, status="proposed"))["items"]
        managed_tools = (await self.list_managed_tools(org_id, status="proposed"))["items"]
        return {
            "agents": agents,
            "skills": skills,
            "mcp_servers": mcp_servers,
            "hooks": hooks,
            "managed_tools": managed_tools,
            "total": (
                len(agents) + len(skills) + len(mcp_servers)
                + len(hooks) + len(managed_tools)
            ),
        }

    async def get_active_agent_with_grants(self, agent_name: str, org_id: str) -> dict | None:
        """Get an active agent by name with its skills and MCP servers loaded."""
        async with self.pool.acquire() as conn:
            agent = await conn.fetchrow(
                "SELECT * FROM agent_definitions "
                "WHERE name = $1 AND organization_id = $2 AND status = 'active'",
                agent_name,
                org_id,
            )
        if not agent:
            return None
        agent_dict = dict(agent)
        agent_dict["skills"] = await self.get_agent_skills(str(agent["id"]))
        agent_dict["mcp_servers"] = await self.get_agent_mcp_servers(str(agent["id"]))
        agent_dict["hooks"] = await self.get_agent_hooks(str(agent["id"]))
        agent_dict["managed_tools"] = await self.get_agent_managed_tools(str(agent["id"]))
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
            agent["skills"] = await self.get_agent_skills(str(agent["id"]))
            agent["mcp_servers"] = await self.get_agent_mcp_servers(str(agent["id"]))
            agent["hooks"] = await self.get_agent_hooks(str(agent["id"]))
            agent["managed_tools"] = await self.get_agent_managed_tools(str(agent["id"]))
        return result

    async def sync_built_in_skills(self, org_id: str, skills_dir: str) -> int:
        """Sync .github/skills/ into the DB as built-in, active definitions.

        Upserts on (name, org_id). Returns count of synced skills.
        """
        import pathlib
        import re

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
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO skill_definitions
                        (name, description, content, status, scope, organization_id)
                    VALUES ($1, $2, $3, 'active', 'built-in', $4)
                    ON CONFLICT (name, organization_id) DO UPDATE
                        SET content = EXCLUDED.content,
                            description = EXCLUDED.description,
                            scope = 'built-in',
                            updated_at = NOW()
                        WHERE skill_definitions.scope = 'built-in'
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
        Parses ``skill_names`` from YAML frontmatter and syncs the
        ``agent_skills`` junction table so skill mappings always reflect the
        source files.

        Returns count of synced agents.
        """
        import pathlib
        import re

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
            fm_match = re.match(r"^---\n(.*?)\n---", raw, re.DOTALL)
            if fm_match:
                in_skill_names = False
                for line in fm_match.group(1).splitlines():
                    if line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip("'\"")
                        in_skill_names = False
                    elif line.strip() == "skill_names:":
                        in_skill_names = True
                    elif in_skill_names:
                        m = re.match(r"\s+-\s+(.+)", line)
                        if m:
                            skill_names.append(m.group(1).strip())
                        elif line.strip() and not line.startswith(" "):
                            in_skill_names = False
            async with self.pool.acquire() as conn:
                # Upsert the agent definition
                agent_row = await conn.fetchrow(
                    """
                    INSERT INTO agent_definitions
                        (name, description, content, status, scope, organization_id)
                    VALUES ($1, $2, $3, 'active', 'built-in', $4)
                    ON CONFLICT (name, organization_id) DO UPDATE
                        SET content = EXCLUDED.content,
                            description = EXCLUDED.description,
                            scope = 'built-in',
                            updated_at = NOW()
                        WHERE agent_definitions.scope = 'built-in'
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

                # Add missing grants
                to_add = declared_skill_ids - current_skill_ids
                for sid in to_add:
                    await conn.execute(
                        "INSERT INTO agent_skills (agent_id, skill_id) "
                        "VALUES ($1, $2) ON CONFLICT DO NOTHING",
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
            }
        ]
        async with self.pool.acquire() as conn:
            for hook in builtins:
                await conn.execute(
                    """
                    INSERT INTO hook_definitions
                        (name, description, trigger_event, action_type, content,
                         config, status, scope, organization_id)
                    VALUES ($1, $2, $3, $4, $5, $6::text::jsonb, 'active', 'built-in', $7)
                    ON CONFLICT (name, organization_id) DO UPDATE
                        SET description = EXCLUDED.description,
                            trigger_event = EXCLUDED.trigger_event,
                            action_type = EXCLUDED.action_type,
                            content = EXCLUDED.content,
                            config = EXCLUDED.config,
                            status = 'active',
                            scope = 'built-in',
                            updated_at = NOW()
                        WHERE hook_definitions.scope = 'built-in'
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
