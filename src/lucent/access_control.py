"""Access control service for ownership and group-based resource visibility."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

RESOURCE_TABLE_MAP: dict[str, str] = {
    "agent": "agent_definitions",
    "agents": "agent_definitions",
    "skill": "skill_definitions",
    "skills": "skill_definitions",
    "mcp_server": "mcp_server_configs",
    "mcp_servers": "mcp_server_configs",
    "mcp": "mcp_server_configs",
    "sandbox_template": "sandbox_templates",
    "sandbox_templates": "sandbox_templates",
    "secret": "secrets",
    "secrets": "secrets",
}

# Tables that have a 'scope' column (supports built-in detection)
_TABLES_WITH_SCOPE = {
    "agent_definitions", "skill_definitions", "mcp_server_configs", "sandbox_templates",
}


def normalize_resource_type(resource_type: str) -> str:
    key = (resource_type or "").strip().lower()
    if key not in RESOURCE_TABLE_MAP:
        raise ValueError(f"Unsupported resource_type: {resource_type}")
    return key


class AccessControlService:
    """Resolve resource access by built-in, ownership, group, then admin override."""

    _GROUP_CACHE_TTL = timedelta(seconds=5)
    _group_cache: dict[str, tuple[datetime, list[str]]] = {}

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    def invalidate_user_groups(cls, user_id: str) -> None:
        cls._group_cache.pop(user_id, None)

    async def _get_user_role(self, user_id: str, org_id: str) -> str | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role FROM users WHERE id = $1 AND organization_id = $2",
                UUID(user_id),
                UUID(org_id),
            )
        return str(row["role"]) if row else None

    async def get_user_group_ids(self, user_id: str) -> list[str]:
        now = datetime.now(UTC)
        cached = self._group_cache.get(user_id)
        if cached and cached[0] > now:
            return list(cached[1])

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_id FROM user_groups WHERE user_id = $1",
                UUID(user_id),
            )
        group_ids = [str(r["group_id"]) for r in rows]
        self._group_cache[user_id] = (now + self._GROUP_CACHE_TTL, group_ids)
        return group_ids

    async def can_access(
        self, user_id: str, resource_type: str, resource_id: str, org_id: str
    ) -> bool:
        """Resolve access: built-in → owner → group owner → admin/owner → deny."""
        normalized = normalize_resource_type(resource_type)
        table = RESOURCE_TABLE_MAP[normalized]
        role = await self._get_user_role(user_id, org_id)
        if role is None:
            return False

        group_ids = [UUID(g) for g in await self.get_user_group_ids(user_id)]
        builtin_clause = "scope = 'built-in' OR " if table in _TABLES_WITH_SCOPE else ""
        query = f"""
            SELECT EXISTS(
                SELECT 1
                FROM {table}
                WHERE id = $1
                  AND organization_id = $2
                  AND (
                      {builtin_clause}owner_user_id = $3
                      OR owner_group_id = ANY($4::uuid[])
                      OR $5 IN ('admin', 'owner')
                  )
            ) AS allowed
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                UUID(resource_id),
                UUID(org_id),
                UUID(user_id),
                group_ids,
                role,
            )
        return bool(row["allowed"]) if row else False

    async def can_modify(
        self, user_id: str, resource_type: str, resource_id: str, org_id: str
    ) -> bool:
        """Check write access: only direct owner or admin/owner role can modify."""
        normalized = normalize_resource_type(resource_type)
        table = RESOURCE_TABLE_MAP[normalized]
        role = await self._get_user_role(user_id, org_id)
        if role is None:
            return False
        if role in ("admin", "owner"):
            # Admin/owner can modify any resource in their org (verify it exists)
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT EXISTS("
                    f"SELECT 1 FROM {table} WHERE id = $1 AND organization_id = $2"
                    f") AS e",
                    UUID(resource_id),
                    UUID(org_id),
                )
            return bool(row["e"]) if row else False
        # Members can only modify resources they directly own
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT EXISTS("
                f"SELECT 1 FROM {table}"
                f" WHERE id = $1 AND organization_id = $2 AND owner_user_id = $3"
                f") AS e",
                UUID(resource_id),
                UUID(org_id),
                UUID(user_id),
            )
        return bool(row["e"]) if row else False

    async def list_accessible(self, user_id: str, resource_type: str, org_id: str) -> list[str]:
        """Return IDs of all resources of this type the user can access."""
        normalized = normalize_resource_type(resource_type)
        table = RESOURCE_TABLE_MAP[normalized]
        role = await self._get_user_role(user_id, org_id)
        if role is None:
            return []

        group_ids = [UUID(g) for g in await self.get_user_group_ids(user_id)]
        builtin_clause = "scope = 'built-in' OR " if table in _TABLES_WITH_SCOPE else ""
        query = f"""
            SELECT id
            FROM {table}
            WHERE organization_id = $1
              AND (
                  {builtin_clause}owner_user_id = $2
                  OR owner_group_id = ANY($3::uuid[])
                  OR $4 IN ('admin', 'owner')
              )
            ORDER BY id
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, UUID(org_id), UUID(user_id), group_ids, role)
        return [str(r["id"]) for r in rows]
