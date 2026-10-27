"""Repository for dashboard read models."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from asyncpg import Pool

from lucent.db.pool import scoped_acquire


class DashboardRepository:
    """Aggregate dashboard operational queries."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def list_goal_requests(
        self, org_id: str | UUID, goal_ids: list[UUID]
    ) -> dict[str, list[dict[str, Any]]]:
        requests_by_goal = {str(goal_id): [] for goal_id in goal_ids}
        if not goal_ids:
            return requests_by_goal
        async with scoped_acquire(organization_id=org_id) as conn:
            rows = await conn.fetch(
                """SELECT DISTINCT ON (goal_link.linked_goal_id, r.id)
                          goal_link.linked_goal_id,
                          r.id,
                          r.title,
                          r.status,
                          r.approval_status,
                          r.priority,
                          r.source,
                          r.goal_milestone_index,
                          r.created_at,
                          r.updated_at,
                          r.completed_at
                   FROM requests r
                   LEFT JOIN request_memories rm
                     ON rm.request_id = r.id AND rm.relation = 'goal'
                   CROSS JOIN LATERAL (
                     SELECT COALESCE(r.goal_memory_id, rm.memory_id) AS linked_goal_id
                   ) goal_link
                   WHERE r.organization_id = $1
                     AND goal_link.linked_goal_id = ANY($2::uuid[])
                   ORDER BY goal_link.linked_goal_id, r.id, r.updated_at DESC""",
                org_id,
                goal_ids,
            )
        for row in rows:
            requests_by_goal.setdefault(str(row["linked_goal_id"]), []).append(dict(row))
        return requests_by_goal

    async def get_latest_heartbeat(
        self,
        org_id: str | UUID,
        *,
        fields: str = "last_seen_at",
    ) -> dict[str, Any] | None:
        async with scoped_acquire(organization_id=org_id) as conn:
            row = await conn.fetchrow(
                f"""SELECT {fields}
                    FROM daemon_instances
                    WHERE organization_id = $1::uuid
                    ORDER BY last_seen_at DESC
                    LIMIT 1""",
                org_id,
            )
        return dict(row) if row else None
