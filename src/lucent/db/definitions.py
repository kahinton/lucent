"""Repository for agent, skill, and MCP server definitions.

Handles CRUD, approval workflow, and access grants (agent↔skill, agent↔MCP).
"""

from datetime import datetime, timezone
from typing import Any

from asyncpg import Pool


class DefinitionRepository:
    """Repository for managing agent, skill, and MCP server definitions."""

    def __init__(self, pool: Pool):
        self.pool = pool

    # ── Agents ────────────────────────────────────────────────────────────

    async def list_agents(
        self, org_id: str, status: str | None = None
    ) -> list[dict]:
        query = """
            SELECT id, name, description, status,
                   created_by, approved_by, approved_at,
                   created_at, updated_at
            FROM agent_definitions
            WHERE organization_id = $1
        """
        params: list[Any] = [org_id]
        if status:
            params.append(status)
            query += f" AND status = ${len(params)}"
        query += " ORDER BY name"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

    async def get_agent(self, agent_id: str, org_id: str) -> dict | None:
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
            GROUP BY a.id
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, agent_id, org_id)
        return dict(row) if row else None

    async def create_agent(
        self, name: str, description: str, content: str,
        org_id: str, created_by: str,
        status: str = "proposed",
    ) -> dict:
        query = """
            INSERT INTO agent_definitions (name, description, content, status,
                created_by, organization_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query, name, description, content, status, created_by, org_id
            )
        return dict(row)

    async def update_agent(self, agent_id: str, org_id: str, **kwargs) -> dict | None:
        sets = []
        params: list[Any] = []
        for key in ("name", "description", "content", "status"):
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
            UPDATE agent_definitions SET {', '.join(sets)}
            WHERE id = ${len(params) - 1} AND organization_id = ${len(params)}
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        return dict(row) if row else None

    async def approve_agent(self, agent_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE agent_definitions
            SET status = 'active', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, agent_id, org_id, approved_by)
        return dict(row) if row else None

    async def reject_agent(self, agent_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE agent_definitions
            SET status = 'rejected', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, agent_id, org_id, approved_by)
        return dict(row) if row else None

    async def delete_agent(self, agent_id: str, org_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agent_definitions WHERE id = $1 AND organization_id = $2",
                agent_id, org_id,
            )
        return result == "DELETE 1"

    # ── Skills ────────────────────────────────────────────────────────────

    async def list_skills(
        self, org_id: str, status: str | None = None
    ) -> list[dict]:
        query = """
            SELECT id, name, description, status,
                   created_by, approved_by, approved_at,
                   created_at, updated_at
            FROM skill_definitions
            WHERE organization_id = $1
        """
        params: list[Any] = [org_id]
        if status:
            params.append(status)
            query += f" AND status = ${len(params)}"
        query += " ORDER BY name"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

    async def get_skill(self, skill_id: str, org_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM skill_definitions WHERE id = $1 AND organization_id = $2",
                skill_id, org_id,
            )
        return dict(row) if row else None

    async def create_skill(
        self, name: str, description: str, content: str,
        org_id: str, created_by: str,
        status: str = "proposed",
    ) -> dict:
        query = """
            INSERT INTO skill_definitions (name, description, content, status,
                created_by, organization_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query, name, description, content, status, created_by, org_id
            )
        return dict(row)

    async def approve_skill(self, skill_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE skill_definitions
            SET status = 'active', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, skill_id, org_id, approved_by)
        return dict(row) if row else None

    async def reject_skill(self, skill_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE skill_definitions
            SET status = 'rejected', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, skill_id, org_id, approved_by)
        return dict(row) if row else None

    async def delete_skill(self, skill_id: str, org_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM skill_definitions WHERE id = $1 AND organization_id = $2",
                skill_id, org_id,
            )
        return result == "DELETE 1"

    # ── MCP Servers ───────────────────────────────────────────────────────

    async def list_mcp_servers(
        self, org_id: str, status: str | None = None
    ) -> list[dict]:
        query = """
            SELECT id, name, description, server_type, url, status,
                   created_by, approved_by, approved_at,
                   created_at, updated_at
            FROM mcp_server_configs
            WHERE organization_id = $1
        """
        params: list[Any] = [org_id]
        if status:
            params.append(status)
            query += f" AND status = ${len(params)}"
        query += " ORDER BY name"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

    async def get_mcp_server(self, server_id: str, org_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM mcp_server_configs WHERE id = $1 AND organization_id = $2",
                server_id, org_id,
            )
        return dict(row) if row else None

    async def create_mcp_server(
        self, name: str, description: str, server_type: str, url: str | None,
        org_id: str, created_by: str, headers: dict | None = None,
        command: str | None = None, args: list | None = None,
        status: str = "proposed",
    ) -> dict:
        import json
        query = """
            INSERT INTO mcp_server_configs (name, description, server_type, url,
                command, args, headers, status, created_by, organization_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query, name, description, server_type, url,
                command, json.dumps(args or []), json.dumps(headers or {}),
                status, created_by, org_id,
            )
        return dict(row)

    async def approve_mcp_server(
        self, server_id: str, org_id: str, approved_by: str,
    ) -> dict | None:
        query = """
            UPDATE mcp_server_configs
            SET status = 'active', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, server_id, org_id, approved_by)
        return dict(row) if row else None

    async def reject_mcp_server(self, server_id: str, org_id: str, approved_by: str) -> dict | None:
        query = """
            UPDATE mcp_server_configs
            SET status = 'rejected', approved_by = $3, approved_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND organization_id = $2 AND status = 'proposed'
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, server_id, org_id, approved_by)
        return dict(row) if row else None

    # ── Access Grants ─────────────────────────────────────────────────────

    async def grant_skill(self, agent_id: str, skill_id: str) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_skills (agent_id, skill_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    agent_id, skill_id,
                )
            return True
        except Exception:
            return False

    async def revoke_skill(self, agent_id: str, skill_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agent_skills WHERE agent_id = $1 AND skill_id = $2",
                agent_id, skill_id,
            )
        return result == "DELETE 1"

    async def grant_mcp_server(self, agent_id: str, mcp_server_id: str) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_mcp_servers (agent_id, mcp_server_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    agent_id, mcp_server_id,
                )
            return True
        except Exception:
            return False

    async def revoke_mcp_server(self, agent_id: str, mcp_server_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agent_mcp_servers WHERE agent_id = $1 AND mcp_server_id = $2",
                agent_id, mcp_server_id,
            )
        return result == "DELETE 1"

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
            SELECT m.* FROM mcp_server_configs m
            JOIN agent_mcp_servers agm ON m.id = agm.mcp_server_id
            WHERE agm.agent_id = $1 AND m.status = 'active'
            ORDER BY m.name
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, agent_id)
        return [dict(r) for r in rows]

    # ── Bulk/convenience ──────────────────────────────────────────────────

    async def get_pending_proposals(self, org_id: str) -> dict:
        """Get all proposed (pending approval) definitions."""
        agents = await self.list_agents(org_id, status="proposed")
        skills = await self.list_skills(org_id, status="proposed")
        mcp_servers = await self.list_mcp_servers(org_id, status="proposed")
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
                agent_name, org_id,
            )
        if not agent:
            return None
        agent_dict = dict(agent)
        agent_dict["skills"] = await self.get_agent_skills(str(agent["id"]))
        agent_dict["mcp_servers"] = await self.get_agent_mcp_servers(str(agent["id"]))
        return agent_dict

    async def list_agents_with_grants(self, org_id: str, status: str | None = None) -> list[dict]:
        """List agents with their granted skills and MCP servers."""
        agents = await self.list_agents(org_id, status=status)
        for agent in agents:
            agent["skills"] = await self.get_agent_skills(str(agent["id"]))
            agent["mcp_servers"] = await self.get_agent_mcp_servers(str(agent["id"]))
        return agents
