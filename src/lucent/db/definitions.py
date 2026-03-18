"""Repository for agent, skill, and MCP server definitions.

Handles CRUD, approval workflow, and access grants (agent↔skill, agent↔MCP).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from asyncpg import Pool

logger = logging.getLogger(__name__)


class DefinitionRepository:
    """Repository for managing agent, skill, and MCP server definitions."""

    def __init__(self, pool: Pool):
        self.pool = pool

    # ── Agents ────────────────────────────────────────────────────────────

    async def list_agents(self, org_id: str, status: str | None = None) -> list[dict]:
        query = """
            SELECT id, name, description, status, scope,
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
        self,
        name: str,
        description: str,
        content: str,
        org_id: str,
        created_by: str,
        status: str = "proposed",
    ) -> dict:
        query = """
            INSERT INTO agent_definitions (name, description, content, status,
                created_by, organization_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, name, description, content, status, created_by, org_id)
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
            UPDATE agent_definitions SET {", ".join(sets)}
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
                agent_id,
                org_id,
            )
        return result == "DELETE 1"

    # ── Skills ────────────────────────────────────────────────────────────

    async def list_skills(self, org_id: str, status: str | None = None) -> list[dict]:
        query = """
            SELECT id, name, description, status, scope,
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
                skill_id,
                org_id,
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
    ) -> dict:
        query = """
            INSERT INTO skill_definitions (name, description, content, status,
                created_by, organization_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, name, description, content, status, created_by, org_id)
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
                skill_id,
                org_id,
            )
        return result == "DELETE 1"

    # ── MCP Servers ───────────────────────────────────────────────────────

    async def list_mcp_servers(self, org_id: str, status: str | None = None) -> list[dict]:
        query = """
            SELECT id, name, description, server_type, url, status, scope,
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
                server_id,
                org_id,
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
    ) -> dict:
        query = """
            INSERT INTO mcp_server_configs (name, description, server_type, url,
                command, args, headers, env_vars, status, created_by, organization_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
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
            )
        return dict(row)

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
                    agent_id,
                    skill_id,
                )
            return True
        except Exception:
            logger.error("Failed to grant skill %s to agent %s", skill_id, agent_id, exc_info=True)
            return False

    async def revoke_skill(self, agent_id: str, skill_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agent_skills WHERE agent_id = $1 AND skill_id = $2",
                agent_id,
                skill_id,
            )
        return result == "DELETE 1"

    async def grant_mcp_server(self, agent_id: str, mcp_server_id: str) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_mcp_servers (agent_id, mcp_server_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    agent_id,
                    mcp_server_id,
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

    async def revoke_mcp_server(self, agent_id: str, mcp_server_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agent_mcp_servers WHERE agent_id = $1 AND mcp_server_id = $2",
                agent_id,
                mcp_server_id,
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
        return result == "UPDATE 1"

    async def update_skill(self, skill_id: str, org_id: str, **kwargs) -> dict | None:
        sets = []
        params: list[Any] = []
        for key in ("name", "description", "content", "status"):
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
        return dict(row) if row else None

    async def update_mcp_server(self, server_id: str, org_id: str, **kwargs) -> dict | None:
        sets = []
        params: list[Any] = []
        for key in ("name", "description", "server_type", "url", "command"):
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
        return dict(row) if row else None

    async def delete_mcp_server(self, server_id: str, org_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM mcp_server_configs WHERE id = $1 AND organization_id = $2",
                server_id,
                org_id,
            )
        return result == "DELETE 1"

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
                agent_name,
                org_id,
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
