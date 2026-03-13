"""Repository for request tracking and task queue.

Full lineage: request → tasks → events → memory links.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from asyncpg import Pool


class RequestRepository:
    """Manages requests, tasks, events, and memory links."""

    def __init__(self, pool: Pool):
        self.pool = pool

    # ── Requests ──────────────────────────────────────────────────────────

    async def create_request(
        self,
        title: str,
        org_id: str,
        description: str | None = None,
        source: str = "user",
        priority: str = "medium",
        created_by: str | None = None,
    ) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO requests (title, description, source, priority, created_by, organization_id)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   RETURNING *""",
                title, description, source, priority,
                UUID(created_by) if created_by else None, UUID(org_id),
            )
        return dict(row)

    async def get_request(self, request_id: str, org_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM requests WHERE id = $1 AND organization_id = $2",
                UUID(request_id), UUID(org_id),
            )
        return dict(row) if row else None

    async def list_requests(
        self, org_id: str, status: str | None = None, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        query = "SELECT * FROM requests WHERE organization_id = $1"
        params: list[Any] = [UUID(org_id)]
        if status:
            params.append(status)
            query += f" AND status = ${len(params)}"
        query += f" ORDER BY created_at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        params.extend([limit, offset])
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

    async def update_request_status(self, request_id: str, status: str) -> dict | None:
        now = datetime.now(timezone.utc)
        completed_at = now if status in ("completed", "failed", "cancelled") else None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE requests SET status = $2, updated_at = $3, completed_at = COALESCE($4, completed_at)
                   WHERE id = $1 RETURNING *""",
                UUID(request_id), status, now, completed_at,
            )
        return dict(row) if row else None

    async def get_request_with_tasks(self, request_id: str, org_id: str) -> dict | None:
        """Load a request with its full task tree, events, and memory links."""
        req = await self.get_request(request_id, org_id)
        if not req:
            return None
        req["tasks"] = await self.list_tasks(request_id)
        # Load events and memory links for each task
        for task in req["tasks"]:
            tid = str(task["id"])
            task["events"] = await self.list_task_events(tid)
            task["memories"] = await self.list_task_memories(tid)
        # Build task tree (nest sub-tasks under parents)
        task_map = {str(t["id"]): t for t in req["tasks"]}
        root_tasks = []
        for t in req["tasks"]:
            t["sub_tasks"] = []
        for t in req["tasks"]:
            parent_id = str(t["parent_task_id"]) if t.get("parent_task_id") else None
            if parent_id and parent_id in task_map:
                task_map[parent_id]["sub_tasks"].append(t)
            else:
                root_tasks.append(t)
        req["task_tree"] = root_tasks

        # Compute summary stats
        statuses = [t["status"] for t in req["tasks"]]
        req["stats"] = {
            "total": len(statuses),
            "pending": statuses.count("pending") + statuses.count("planned"),
            "running": statuses.count("claimed") + statuses.count("running"),
            "completed": statuses.count("completed"),
            "failed": statuses.count("failed"),
        }
        return req

    # ── Tasks ─────────────────────────────────────────────────────────────

    async def create_task(
        self,
        request_id: str,
        title: str,
        org_id: str,
        description: str | None = None,
        agent_type: str | None = None,
        agent_definition_id: str | None = None,
        parent_task_id: str | None = None,
        priority: str = "medium",
        sequence_order: int = 0,
        model: str | None = None,
    ) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO tasks
                   (request_id, parent_task_id, title, description, agent_type,
                    agent_definition_id, priority, sequence_order, organization_id, model)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                   RETURNING *""",
                UUID(request_id),
                UUID(parent_task_id) if parent_task_id else None,
                title, description, agent_type,
                UUID(agent_definition_id) if agent_definition_id else None,
                priority, sequence_order, UUID(org_id), model,
            )
        task = dict(row)
        # Log creation event
        await self.add_task_event(str(task["id"]), "created", f"Task created: {title}")
        return task

    async def get_task(self, task_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tasks WHERE id = $1", UUID(task_id))
        return dict(row) if row else None

    async def list_tasks(
        self, request_id: str, status: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM tasks WHERE request_id = $1"
        params: list[Any] = [UUID(request_id)]
        if status:
            params.append(status)
            query += f" AND status = ${len(params)}"
        query += " ORDER BY sequence_order, created_at"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

    async def list_pending_tasks(self, org_id: str) -> list[dict]:
        """Get all tasks ready to be claimed.

        Respects sequence_order as a dependency gate: a task is only
        dispatchable when all lower-sequence tasks in the same request
        have completed (or been cancelled/failed).  Tasks at the same
        sequence_order can run in parallel.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT t.*, r.title as request_title
                   FROM tasks t JOIN requests r ON t.request_id = r.id
                   WHERE t.organization_id = $1
                     AND t.status IN ('pending', 'planned')
                     AND NOT EXISTS (
                       SELECT 1 FROM tasks earlier
                       WHERE earlier.request_id = t.request_id
                         AND earlier.sequence_order < t.sequence_order
                         AND earlier.status NOT IN ('completed', 'failed', 'cancelled')
                     )
                   ORDER BY
                     CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                     WHEN 'medium' THEN 2 ELSE 3 END,
                     t.sequence_order, t.created_at""",
                UUID(org_id),
            )
        return [dict(r) for r in rows]

    async def claim_task(self, task_id: str, instance_id: str) -> dict | None:
        """Atomically claim a pending task. Returns None if already claimed."""
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE tasks SET status = 'claimed', claimed_by = $2,
                   claimed_at = $3, updated_at = $3
                   WHERE id = $1 AND status IN ('pending', 'planned')
                   RETURNING *""",
                UUID(task_id), instance_id, now,
            )
        if row:
            task = dict(row)
            await self.add_task_event(
                task_id, "claimed", f"Claimed by {instance_id}",
                metadata={"instance_id": instance_id},
            )
            # Update parent request to in_progress if still pending/planning
            await self._ensure_request_in_progress(str(task["request_id"]))
            return task
        return None

    async def start_task(self, task_id: str) -> dict | None:
        """Mark a claimed task as running."""
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE tasks SET status = 'running', updated_at = $2
                   WHERE id = $1 AND status = 'claimed' RETURNING *""",
                UUID(task_id), now,
            )
        if row:
            await self.add_task_event(task_id, "running", "Agent started execution")
            return dict(row)
        return None

    async def complete_task(self, task_id: str, result: str) -> dict | None:
        """Mark task as completed with result."""
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE tasks SET status = 'completed', result = $2,
                   completed_at = $3, updated_at = $3
                   WHERE id = $1 RETURNING *""",
                UUID(task_id), result, now,
            )
        if row:
            task = dict(row)
            await self.add_task_event(
                task_id, "completed", f"Completed ({len(result)} chars output)",
            )
            # Check if all tasks in request are done
            await self._check_request_completion(str(task["request_id"]))
            return task
        return None

    async def fail_task(self, task_id: str, error: str) -> dict | None:
        """Mark task as failed."""
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE tasks SET status = 'failed', error = $2,
                   completed_at = $3, updated_at = $3
                   WHERE id = $1 RETURNING *""",
                UUID(task_id), error, now,
            )
        if row:
            task = dict(row)
            await self.add_task_event(task_id, "failed", f"Failed: {error[:200]}")
            return task
        return None

    async def release_task(self, task_id: str) -> dict | None:
        """Release a claimed/running task back to pending (for retry/stale recovery)."""
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                   claimed_at = NULL, updated_at = $2
                   WHERE id = $1 AND status IN ('claimed', 'running') RETURNING *""",
                UUID(task_id), now,
            )
        if row:
            await self.add_task_event(task_id, "released", "Task released back to pending")
            return dict(row)
        return None

    async def release_stale_tasks(self, stale_minutes: int = 30) -> int:
        """Release tasks stuck in claimed/running state past the timeout."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                   claimed_at = NULL, updated_at = NOW()
                   WHERE status IN ('claimed', 'running')
                   AND claimed_at < NOW() - make_interval(mins := $1)
                   RETURNING id""",
                stale_minutes,
            )
        for row in rows:
            await self.add_task_event(
                str(row["id"]), "released",
                f"Auto-released: stale for >{stale_minutes}min",
            )
        return len(rows)

    # ── Task Events ───────────────────────────────────────────────────────

    async def add_task_event(
        self,
        task_id: str,
        event_type: str,
        detail: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        import json
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO task_events (task_id, event_type, detail, metadata)
                   VALUES ($1, $2, $3, $4) RETURNING *""",
                UUID(task_id), event_type, detail,
                json.dumps(metadata) if metadata else "{}",
            )
        return dict(row)

    async def list_task_events(self, task_id: str, limit: int = 100) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM task_events WHERE task_id = $1 ORDER BY created_at LIMIT $2",
                UUID(task_id), limit,
            )
        return [dict(r) for r in rows]

    # ── Task ↔ Memory Links ──────────────────────────────────────────────

    async def link_memory(
        self, task_id: str, memory_id: str, relation: str = "created",
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO task_memories (task_id, memory_id, relation)
                   VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                UUID(task_id), UUID(memory_id), relation,
            )
        await self.add_task_event(
            task_id, f"memory_{relation}",
            f"Memory {relation}: {memory_id[:8]}...",
            metadata={"memory_id": memory_id, "relation": relation},
        )

    async def list_task_memories(self, task_id: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT tm.*, m.content, m.type as memory_type, m.tags
                   FROM task_memories tm
                   JOIN memories m ON tm.memory_id = m.id
                   WHERE tm.task_id = $1
                   ORDER BY tm.created_at""",
                UUID(task_id),
            )
        return [dict(r) for r in rows]

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _ensure_request_in_progress(self, request_id: str) -> None:
        """Move request to in_progress if it's still pending/planning."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE requests SET status = 'in_progress', updated_at = NOW()
                   WHERE id = $1 AND status IN ('pending', 'planning')""",
                UUID(request_id),
            )

    async def _check_request_completion(self, request_id: str) -> None:
        """If all tasks are done (completed/failed/cancelled), complete the request."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT
                     COUNT(*) as total,
                     COUNT(*) FILTER (WHERE status IN ('completed', 'failed', 'cancelled')) as done
                   FROM tasks WHERE request_id = $1""",
                UUID(request_id),
            )
        if row and row["total"] > 0 and row["total"] == row["done"]:
            # Check if any failed
            async with self.pool.acquire() as conn:
                failed = await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE request_id = $1 AND status = 'failed'",
                    UUID(request_id),
                )
            status = "failed" if failed > 0 else "completed"
            await self.update_request_status(request_id, status)

    # ── Dashboard queries ─────────────────────────────────────────────────

    async def get_active_summary(self, org_id: str) -> dict:
        """Quick dashboard stats."""
        async with self.pool.acquire() as conn:
            req_stats = await conn.fetchrow(
                """SELECT
                     COUNT(*) as total,
                     COUNT(*) FILTER (WHERE status = 'in_progress') as active,
                     COUNT(*) FILTER (WHERE status = 'pending') as pending,
                     COUNT(*) FILTER (WHERE status = 'completed') as completed
                   FROM requests WHERE organization_id = $1""",
                UUID(org_id),
            )
            task_stats = await conn.fetchrow(
                """SELECT
                     COUNT(*) as total,
                     COUNT(*) FILTER (WHERE status IN ('claimed', 'running')) as running,
                     COUNT(*) FILTER (WHERE status IN ('pending', 'planned')) as queued,
                     COUNT(*) FILTER (WHERE status = 'completed') as completed
                   FROM tasks WHERE organization_id = $1""",
                UUID(org_id),
            )
        return {
            "requests": dict(req_stats) if req_stats else {},
            "tasks": dict(task_stats) if task_stats else {},
        }

    async def get_recent_events(self, org_id: str, limit: int = 50) -> list[dict]:
        """Get recent events across all tasks for the activity feed."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT te.*, t.title as task_title, t.agent_type,
                          r.title as request_title, r.id as request_id
                   FROM task_events te
                   JOIN tasks t ON te.task_id = t.id
                   JOIN requests r ON t.request_id = r.id
                   WHERE t.organization_id = $1
                   ORDER BY te.created_at DESC LIMIT $2""",
                UUID(org_id), limit,
            )
        return [dict(r) for r in rows]
