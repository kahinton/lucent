"""Repository for agent, skill, and MCP server definitions.

Handles CRUD, approval workflow, and access grants (agent↔skill, agent↔MCP).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from asyncpg import Pool

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


class DefinitionRepository:
    """Repository for managing agent, skill, and MCP server definitions."""

    def __init__(self, pool: Pool, audit_repo: AuditRepository | None = None):
        self.pool = pool
        self.audit_repo = audit_repo

    @staticmethod
    def _role_value(role: str | None) -> str:
        return role or "member"

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
            uid_idx = len(params) - 1
            role_idx = len(params)
            base += (
                f" AND (scope = 'built-in' OR owner_user_id = ${uid_idx} "
                "OR owner_group_id IN ("
                f"SELECT group_id FROM user_groups WHERE user_id = ${uid_idx}"
                ") "
                f"OR ${role_idx} IN ('admin', 'owner'))"
            )
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"""
            SELECT id, name, description, status, scope,
                   created_by, approved_by, approved_at,
                   owner_user_id, owner_group_id,
                   created_at, updated_at
            {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        params_with_page = [*params, limit, offset]

        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, *params_with_page)
        return {
            "items": [dict(r) for r in rows],
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
            acl_sql = (
                " AND (a.scope = 'built-in' OR a.owner_user_id = $3 "
                "OR a.owner_group_id IN (SELECT group_id FROM user_groups WHERE user_id = $3) "
                "OR $4 IN ('admin', 'owner'))"
            )
        query = """
            SELECT a.*,
                array_agg(DISTINCT s.name) FILTER (WHERE s.name IS NOT NULL) as skill_names,
                array_agg(DISTINCT m.name) FILTER (WHERE m.name IS NOT NULL) as mcp_server_names
            FROM agent_definitions a
            LEFT JOIN agent_skills ags ON a.id = ags.agent_id
            LEFT JOIN skill_definitions s ON ags.skill_id = s.id
            LEFT JOIN agent_mcp_servers agm ON a.id = agm.agent_id
            LEFT JOIN mcp_server_configs m ON agm.mcp_server_id = m.id
            WHERE a.id = $1 AND a.organization_id = $2
        """ + acl_sql + """
            GROUP BY a.id
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        return dict(row) if row else None

    async def create_agent(
        self,
        name: str,
        description: str,
        content: str,
        org_id: str,
        created_by: str,
        status: str = "proposed",
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
    ) -> dict:
        # Default owner to creator when no explicit ownership is provided
        if owner_user_id is None and owner_group_id is None:
            owner_user_id = created_by
        query = """
            INSERT INTO agent_definitions (name, description, content, status,
                created_by, organization_id, owner_user_id, owner_group_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query, name, description, content, status, created_by, org_id,
                owner_user_id, owner_group_id,
            )
        result = dict(row)
        await self._audit(
            DEFINITION_CREATE, org_id, "agent", str(result["id"]),
            user_id=created_by, notes=f"Created agent '{name}'",
        )
        return result

    async def update_agent(self, agent_id: str, org_id: str, **kwargs) -> dict | None:
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
        result = dict(row) if row else None
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
        result = dict(row) if row else None
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
        result = dict(row) if row else None
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
            uid_idx = len(params) - 1
            role_idx = len(params)
            base += (
                f" AND (scope = 'built-in' OR owner_user_id = ${uid_idx} "
                "OR owner_group_id IN ("
                f"SELECT group_id FROM user_groups WHERE user_id = ${uid_idx}"
                ") "
                f"OR ${role_idx} IN ('admin', 'owner'))"
            )
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"""
            SELECT id, name, description, status, scope,
                   created_by, approved_by, approved_at,
                   owner_user_id, owner_group_id,
                   created_at, updated_at
            {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        params_with_page = [*params, limit, offset]

        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, *params_with_page)
        return {
            "items": [dict(r) for r in rows],
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
            acl_sql = (
                " AND (scope = 'built-in' OR owner_user_id = $3 "
                "OR owner_group_id IN (SELECT group_id FROM user_groups WHERE user_id = $3) "
                "OR $4 IN ('admin', 'owner'))"
            )
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM skill_definitions WHERE id = $1 AND organization_id = $2" + acl_sql,
                *params,
            )
        return dict(row) if row else None

    async def create_skill(
        self,
        name: str,
        description: str,
        content: str,
        org_id: str,
        created_by: str,
        status: str = "proposed",
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
    ) -> dict:
        # Default owner to creator when no explicit ownership is provided
        if owner_user_id is None and owner_group_id is None:
            owner_user_id = created_by
        query = """
            INSERT INTO skill_definitions (name, description, content, status,
                created_by, organization_id, owner_user_id, owner_group_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query, name, description, content, status, created_by, org_id,
                owner_user_id, owner_group_id,
            )
        result = dict(row)
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
        result = dict(row) if row else None
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
        result = dict(row) if row else None
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
            uid_idx = len(params) - 1
            role_idx = len(params)
            base += (
                f" AND (scope = 'built-in' OR owner_user_id = ${uid_idx} "
                "OR owner_group_id IN ("
                f"SELECT group_id FROM user_groups WHERE user_id = ${uid_idx}"
                ") "
                f"OR ${role_idx} IN ('admin', 'owner'))"
            )
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = f"""
            SELECT id, name, description, server_type, url, status, scope,
                   created_by, approved_by, approved_at,
                   owner_user_id, owner_group_id,
                   created_at, updated_at
            {base} ORDER BY name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        params_with_page = [*params, limit, offset]

        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, *params_with_page)
        return {
            "items": [dict(r) for r in rows],
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
            acl_sql = (
                " AND (scope = 'built-in' OR owner_user_id = $3 "
                "OR owner_group_id IN (SELECT group_id FROM user_groups WHERE user_id = $3) "
                "OR $4 IN ('admin', 'owner'))"
            )
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM mcp_server_configs WHERE id = $1 AND organization_id = $2" + acl_sql,
                *params,
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
    ) -> dict:
        # Default owner to creator when no explicit ownership is provided
        if owner_user_id is None and owner_group_id is None:
            owner_user_id = created_by
        query = """
            INSERT INTO mcp_server_configs (name, description, server_type, url,
                command, args, headers, env_vars, status, created_by, organization_id,
                owner_user_id, owner_group_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
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
            )
        result = dict(row)
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
        result = dict(row) if row else None
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
        result = dict(row) if row else None
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
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_skills (agent_id, skill_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    agent_id,
                    skill_id,
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
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_mcp_servers (agent_id, mcp_server_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    agent_id,
                    mcp_server_id,
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

    async def get_agent_skills(self, agent_id: str) -> list[dict]:
        query = """
            SELECT s.* FROM skill_definitions s
            JOIN agent_skills ags ON s.id = ags.skill_id
            WHERE ags.agent_id = $1 AND s.status = 'active'
            ORDER BY s.name
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, agent_id)
        return [dict(r) for r in rows]

    async def get_agent_mcp_servers(self, agent_id: str) -> list[dict]:
        query = """
            SELECT m.*, agm.allowed_tools FROM mcp_server_configs m
            JOIN agent_mcp_servers agm ON m.id = agm.mcp_server_id
            WHERE agm.agent_id = $1 AND m.status = 'active'
            ORDER BY m.name
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, agent_id)
        return [dict(r) for r in rows]

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

    async def update_skill(self, skill_id: str, org_id: str, **kwargs) -> dict | None:
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
        result = dict(row) if row else None
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

    async def update_mcp_server(self, server_id: str, org_id: str, **kwargs) -> dict | None:
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
        result = dict(row) if row else None
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
                json.dumps(tools_list),
                server_id,
                org_id,
            )
        result = dict(row) if row else None
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
            "discovered_tools": json.loads(row["discovered_tools"]) if row["discovered_tools"] else None,
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
            AND (
                scope = 'built-in'
                OR owner_user_id = $2
                OR owner_group_id IN (SELECT group_id FROM user_groups WHERE user_id = $2)
                OR $3 IN ('admin', 'owner')
            )
        """
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
            "items": [dict(r) for r in rows],
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
            AND (
                scope = 'built-in'
                OR owner_user_id = $2
                OR owner_group_id IN (SELECT group_id FROM user_groups WHERE user_id = $2)
                OR $3 IN ('admin', 'owner')
            )
        """
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
            "items": [dict(r) for r in rows],
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
            AND (
                scope = 'built-in'
                OR owner_user_id = $2
                OR owner_group_id IN (SELECT group_id FROM user_groups WHERE user_id = $2)
                OR $3 IN ('admin', 'owner')
            )
        """
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
            "items": [dict(r) for r in rows],
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
        return {
            "agents": agents,
            "skills": skills,
            "mcp_servers": mcp_servers,
            "total": len(agents) + len(skills) + len(mcp_servers),
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
            fm_match = re.match(r"^---\n(.*?)\n---", raw, re.DOTALL)
            if fm_match:
                for line in fm_match.group(1).splitlines():
                    if line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip("'\"")
            async with self.pool.acquire() as conn:
                await conn.execute(
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
                """,
                    name,
                    description,
                    raw,
                    org_id,
                )
            synced += 1
        return synced
