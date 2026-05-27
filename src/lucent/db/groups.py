"""Repository for group management and user-group membership.

Handles group CRUD, membership management, and group-based lookups.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from asyncpg import Pool

from lucent.access_control import AccessControlService

logger = logging.getLogger(__name__)


class GroupRepository:
    """Repository for managing groups and user-group membership."""

    def __init__(self, pool: Pool):
        self.pool = pool

    # ── Groups ────────────────────────────────────────────────────────────

    async def create_group(
        self,
        name: str,
        org_id: str,
        description: str = "",
        created_by: str | None = None,
    ) -> dict:
        """Create a new group within an organization."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO groups (name, description, organization_id, created_by)
                   VALUES ($1, $2, $3, $4)
                   RETURNING *""",
                name,
                description,
                UUID(org_id),
                UUID(created_by) if created_by else None,
            )
        return dict(row)

    async def get_group(self, group_id: str, org_id: str) -> dict | None:
        """Get a group by ID, scoped to organization."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM groups WHERE id = $1 AND organization_id = $2",
                UUID(group_id),
                UUID(org_id),
            )
        return dict(row) if row else None

    async def list_groups(
        self, org_id: str, limit: int = 25, offset: int = 0
    ) -> dict:
        """List groups in an organization with pagination."""
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(
                "SELECT COUNT(*) AS total FROM groups WHERE organization_id = $1",
                UUID(org_id),
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                """SELECT * FROM groups
                   WHERE organization_id = $1
                   ORDER BY name
                   LIMIT $2 OFFSET $3""",
                UUID(org_id),
                limit,
                offset,
            )
        return {
            "items": [dict(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def update_group(
        self,
        group_id: str,
        org_id: str,
        **kwargs: Any,
    ) -> dict | None:
        """Update a group's name and/or description. Returns None if not found."""
        sets: list[str] = []
        params: list[Any] = []
        for key in ("name", "description"):
            if key in kwargs:
                params.append(kwargs[key])
                sets.append(f"{key} = ${len(params)}")
        if not sets:
            return await self.get_group(group_id, org_id)
        params.append(datetime.now(timezone.utc))
        sets.append(f"updated_at = ${len(params)}")
        params.append(UUID(group_id))
        params.append(UUID(org_id))
        query = f"""
            UPDATE groups SET {", ".join(sets)}
            WHERE id = ${len(params) - 1} AND organization_id = ${len(params)}
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        return dict(row) if row else None

    async def delete_group(self, group_id: str, org_id: str) -> bool:
        """Delete a group. Returns True if deleted, False if not found."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM groups WHERE id = $1 AND organization_id = $2",
                UUID(group_id),
                UUID(org_id),
            )
        return result == "DELETE 1"

    # ── Membership ────────────────────────────────────────────────────────

    async def add_member(
        self, group_id: str, user_id: str, role: str = "member"
    ) -> dict:
        """Add a user to a group. Raises on duplicate or invalid role."""
        if role not in ("member", "admin"):
            raise ValueError(f"Invalid role '{role}'. Must be 'member' or 'admin'.")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO user_groups (user_id, group_id, role)
                   VALUES ($1, $2, $3)
                   RETURNING *""",
                UUID(user_id),
                UUID(group_id),
                role,
            )
        AccessControlService.invalidate_user_groups(user_id)
        return dict(row)

    async def remove_member(self, group_id: str, user_id: str) -> bool:
        """Remove a user from a group. Returns True if removed."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM user_groups WHERE group_id = $1 AND user_id = $2",
                UUID(group_id),
                UUID(user_id),
            )
        if result == "DELETE 1":
            AccessControlService.invalidate_user_groups(user_id)
        return result == "DELETE 1"

    async def update_member_role(
        self, group_id: str, user_id: str, role: str
    ) -> dict | None:
        """Update a member's role in a group."""
        if role not in ("member", "admin"):
            raise ValueError(f"Invalid role '{role}'. Must be 'member' or 'admin'.")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE user_groups SET role = $1
                   WHERE group_id = $2 AND user_id = $3
                   RETURNING *""",
                role,
                UUID(group_id),
                UUID(user_id),
            )
        if row:
            AccessControlService.invalidate_user_groups(user_id)
        return dict(row) if row else None

    async def list_members(self, group_id: str, org_id: str) -> list[dict]:
        """List members of a group with user details."""
        async with self.pool.acquire() as conn:
            # Verify group belongs to org
            group = await conn.fetchrow(
                "SELECT id FROM groups WHERE id = $1 AND organization_id = $2",
                UUID(group_id),
                UUID(org_id),
            )
            if not group:
                return []
            rows = await conn.fetch(
                """SELECT ug.user_id, ug.group_id, ug.role, ug.created_at,
                          u.display_name, u.email
                   FROM user_groups ug
                   JOIN users u ON ug.user_id = u.id
                   WHERE ug.group_id = $1
                   ORDER BY u.display_name""",
                UUID(group_id),
            )
        return [dict(r) for r in rows]

    async def get_user_groups(self, user_id: str, org_id: str) -> list[dict]:
        """Get all groups a user belongs to within an organization."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT g.id, g.name, g.description, ug.role, ug.created_at AS joined_at
                   FROM user_groups ug
                   JOIN groups g ON ug.group_id = g.id
                   WHERE ug.user_id = $1 AND g.organization_id = $2
                   ORDER BY g.name""",
                UUID(user_id),
                UUID(org_id),
            )
        return [dict(r) for r in rows]

    async def get_user_group_ids(self, user_id: str) -> list[str]:
        """Get all group IDs a user belongs to (across all orgs)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_id FROM user_groups WHERE user_id = $1",
                UUID(user_id),
            )
        return [str(r["group_id"]) for r in rows]

    async def is_member(self, user_id: str, group_id: str) -> bool:
        """Check if a user is a member of a group."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM user_groups WHERE user_id = $1 AND group_id = $2",
                UUID(user_id),
                UUID(group_id),
            )
        return row is not None

    async def is_group_admin(self, user_id: str, group_id: str) -> bool:
        """Check if a user is an admin of a group."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM user_groups WHERE user_id = $1 AND group_id = $2 AND role = 'admin'",
                UUID(user_id),
                UUID(group_id),
            )
        return row is not None
