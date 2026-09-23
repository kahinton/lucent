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

    async def get_work_summary(
        self,
        org_id: str,
        *,
        statuses: list[str],
        approval_statuses: list[str],
    ) -> dict[str, Any] | None:
        async with scoped_acquire(organization_id=org_id) as conn:
            row = await conn.fetchrow(
                """WITH current_requests AS (
                       SELECT id, status
                       FROM requests
                       WHERE organization_id = $1
                         AND status = ANY($2::text[])
                         AND approval_status = ANY($3::text[])
                   )
                   SELECT
                     (SELECT COUNT(*) FROM current_requests) AS open_requests,
                     (SELECT COUNT(*) FROM current_requests
                      WHERE status IN ('pending', 'planned')) AS pending_requests,
                     (SELECT COUNT(*) FROM current_requests
                      WHERE status IN ('in_progress', 'review', 'needs_rework'))
                      AS active_requests,
                     COUNT(t.id) FILTER (WHERE t.status IN ('claimed', 'running'))
                      AS running_tasks,
                     COUNT(t.id) FILTER (WHERE t.status IN ('pending', 'planned'))
                      AS queued_tasks,
                     COUNT(t.id) FILTER (WHERE t.status = 'completed')
                      AS completed_tasks,
                     COUNT(t.id) FILTER (WHERE t.status = 'failed') AS failed_tasks
                   FROM current_requests cr
                   LEFT JOIN tasks t ON t.request_id = cr.id""",
                org_id,
                statuses,
                approval_statuses,
            )
        return dict(row) if row else None

    async def get_active_work(
        self,
        org_id: str,
        *,
        statuses: list[str],
        approval_statuses: list[str],
        limit: int = 5,
    ) -> dict[str, Any]:
        async with scoped_acquire(organization_id=org_id) as conn:
            count_row = await conn.fetchrow(
                """SELECT COUNT(*) AS total
                   FROM requests
                   WHERE organization_id = $1
                     AND status = ANY($2::text[])
                     AND approval_status = ANY($3::text[])""",
                org_id,
                statuses,
                approval_statuses,
            )
            rows = await conn.fetch(
                """SELECT r.id, r.title, r.description, r.status, r.priority,
                          r.source, r.created_at, r.updated_at,
                          COUNT(t.id) FILTER (WHERE t.status = 'pending')
                            AS tasks_pending,
                          COUNT(t.id) FILTER (WHERE t.status = 'planned')
                            AS tasks_planned,
                          COUNT(t.id) FILTER (WHERE t.status IN ('claimed', 'running'))
                            AS tasks_running,
                          COUNT(t.id) FILTER (WHERE t.status = 'completed')
                            AS tasks_completed,
                          COUNT(t.id) FILTER (WHERE t.status = 'failed') AS tasks_failed,
                          COUNT(t.id) AS tasks_total
                   FROM requests r
                   LEFT JOIN tasks t ON t.request_id = r.id
                   WHERE r.organization_id = $1
                     AND r.status = ANY($2::text[])
                     AND r.approval_status = ANY($3::text[])
                   GROUP BY r.id
                   ORDER BY
                     CASE r.status
                       WHEN 'in_progress' THEN 0
                       WHEN 'needs_rework' THEN 1
                       WHEN 'review' THEN 2
                       WHEN 'pending' THEN 3
                       WHEN 'planned' THEN 4
                       ELSE 5
                     END,
                     CASE r.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                     WHEN 'medium' THEN 2 ELSE 3 END,
                     r.updated_at DESC
                   LIMIT $4""",
                org_id,
                statuses,
                approval_statuses,
                limit,
            )
        return {
            "items": [dict(row) for row in rows],
            "total_count": count_row["total"] if count_row else 0,
        }

    async def get_pending_approvals(self, org_id: str, limit: int = 5) -> dict[str, Any]:
        async with scoped_acquire(organization_id=org_id) as conn:
            total = int(
                await conn.fetchval(
                    """SELECT COUNT(*)
                       FROM requests
                       WHERE organization_id = $1
                         AND approval_status = 'pending_approval'
                         AND status NOT IN ('cancelled', 'rejection_processing')""",
                    org_id,
                )
                or 0
            )
            rows = await conn.fetch(
                """SELECT r.id, r.title, r.description, r.source, r.priority,
                          r.created_at, r.updated_at,
                          (SELECT COUNT(*) FROM tasks t WHERE t.request_id = r.id)
                            AS task_count
                   FROM requests r
                   WHERE r.organization_id = $1
                     AND r.approval_status = 'pending_approval'
                     AND r.status NOT IN ('cancelled', 'rejection_processing')
                   ORDER BY
                     CASE r.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                     WHEN 'medium' THEN 2 ELSE 3 END,
                     r.created_at
                   LIMIT $2""",
                org_id,
                limit,
            )
        return {"items": [dict(row) for row in rows], "total_count": total}

    async def count_completed_last_24_hours(self, org_id: str) -> int:
        async with scoped_acquire(organization_id=org_id) as conn:
            return int(
                await conn.fetchval(
                    """SELECT COUNT(*) FROM requests
                       WHERE organization_id = $1
                         AND status = 'completed'
                         AND completed_at > NOW() - INTERVAL '24 hours'""",
                    org_id,
                )
                or 0
            )

    async def get_admin_summary(self, org_id: str) -> dict[str, Any] | None:
        async with scoped_acquire(organization_id=org_id, role="admin") as conn:
            row = await conn.fetchrow(
                """SELECT
                     (SELECT COUNT(*) FROM users
                      WHERE organization_id = $1 AND is_active = true)
                        AS active_users,
                     (SELECT COUNT(*) FROM requests
                      WHERE organization_id = $1
                        AND status = 'failed'
                        AND updated_at > NOW() - INTERVAL '7 days')
                        AS failed_requests_7d,
                     (SELECT COUNT(*) FROM tasks
                      WHERE organization_id = $1
                        AND status = 'failed'
                        AND updated_at > NOW() - INTERVAL '7 days')
                        AS failed_tasks_7d,
                     (SELECT COUNT(*) FROM schedules
                      WHERE organization_id = $1
                        AND enabled = true
                        AND status = 'active') AS active_schedules,
                     (SELECT COUNT(*) FROM sandboxes
                      WHERE organization_id = $1
                        AND status NOT IN ('destroyed', 'stopped'))
                        AS live_sandboxes""",
                org_id,
            )
        return dict(row) if row else None
