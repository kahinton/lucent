"""Repository for request tracking and task queue.

Full lineage: request → tasks → events → memory links.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from asyncpg import Pool
from jsonschema import SchemaError
from jsonschema.validators import validator_for

from lucent.constants import (
    REQUEST_STATUS_CANCELLED,
    REQUEST_STATUS_COMPLETED,
    REQUEST_STATUS_FAILED,
    REQUEST_STATUS_IN_PROGRESS,
    REQUEST_STATUS_NEEDS_REWORK,
    REQUEST_STATUS_REVIEW,
    VALID_REQUEST_SOURCES,
    VALID_REQUEST_STATUSES,
)


def _request_fingerprint(title: str) -> str:
    """Compute a deduplication fingerprint from a request title."""
    return hashlib.md5(title.lower().strip().encode()).hexdigest()


_VALID_OUTPUT_FAILURE_POLICIES = {"fail", "fallback", "retry_then_fallback"}
_VALID_VALIDATION_STATUSES = {
    "not_applicable",
    "valid",
    "invalid",
    "extraction_failed",
    "fallback_used",
    "repair_succeeded",
}


def _validate_output_contract(output_contract: dict | None) -> None:
    """Validate output_contract shape and JSON Schema structure.

    Contract format:
      {
        "json_schema": {...},
        "on_failure": "fail|fallback|retry_then_fallback",  # optional
        "max_retries": 1,                                   # optional
      }
    """
    if output_contract is None:
        return
    if not isinstance(output_contract, dict):
        raise ValueError("output_contract must be an object")

    json_schema = output_contract.get("json_schema")
    if json_schema is None:
        raise ValueError("output_contract must include 'json_schema'")
    if not isinstance(json_schema, dict):
        raise ValueError("output_contract.json_schema must be an object")

    try:
        validator_cls = validator_for(json_schema)
        validator_cls.check_schema(json_schema)
    except SchemaError as exc:
        raise ValueError(f"Invalid output_contract.json_schema: {exc.message}") from exc

    on_failure = output_contract.get("on_failure", "fallback")
    if on_failure not in _VALID_OUTPUT_FAILURE_POLICIES:
        valid = ", ".join(sorted(_VALID_OUTPUT_FAILURE_POLICIES))
        raise ValueError(
            f"Invalid output_contract.on_failure '{on_failure}'. "
            f"Must be one of: {valid}"
        )

    max_retries = output_contract.get("max_retries", 1)
    if not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("output_contract.max_retries must be an integer >= 0")


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
        dependency_policy: str = "strict",
    ) -> dict:
        if source not in VALID_REQUEST_SOURCES:
            valid_sources = ", ".join(sorted(VALID_REQUEST_SOURCES))
            raise ValueError(
                f"Invalid source '{source}'. Must be one of: {valid_sources}"
            )
        if dependency_policy not in ("strict", "permissive"):
            raise ValueError(
                "Invalid dependency_policy "
                f"'{dependency_policy}'. Must be 'strict' or 'permissive'."
            )
        fingerprint = _request_fingerprint(title)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO requests
                   (title, description, source, priority, created_by,
                    organization_id, fingerprint, dependency_policy)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   ON CONFLICT (organization_id, fingerprint)
                       WHERE status IN ('pending','planned','in_progress','review','needs_rework')
                    DO UPDATE SET updated_at = NOW()
                    RETURNING *""",
                title,
                description,
                source,
                priority,
                UUID(created_by) if created_by else None,
                UUID(org_id),
                fingerprint,
                dependency_policy,
            )
        return dict(row)

    async def get_request(self, request_id: str, org_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM requests WHERE id = $1 AND organization_id = $2",
                UUID(request_id),
                UUID(org_id),
            )
        return dict(row) if row else None

    async def list_requests(
        self,
        org_id: str,
        status: str | None = None,
        source: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        base = "FROM requests WHERE organization_id = $1"
        params: list[Any] = [UUID(org_id)]
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"
        if source:
            sources = [s.strip() for s in source.split(",") if s.strip()]
            if len(sources) == 1:
                params.append(sources[0])
                base += f" AND source = ${len(params)}"
            else:
                placeholders = ", ".join(f"${len(params) + i + 1}" for i in range(len(sources)))
                params.extend(sources)
                base += f" AND source IN ({placeholders})"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = (
            f"SELECT * {base} ORDER BY created_at DESC "
            f"LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
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

    async def update_request_status(
        self, request_id: str, status: str, org_id: str | None = None
    ) -> dict | None:
        if status not in VALID_REQUEST_STATUSES:
            valid = ", ".join(sorted(VALID_REQUEST_STATUSES))
            raise ValueError(f"Invalid status '{status}'. Must be one of: {valid}")
        now = datetime.now(timezone.utc)
        completed_at = (
            now
            if status
            in (REQUEST_STATUS_COMPLETED, REQUEST_STATUS_FAILED, REQUEST_STATUS_CANCELLED)
            else None
        )
        reviewed_at = (
            now if status in (REQUEST_STATUS_REVIEW, REQUEST_STATUS_NEEDS_REWORK) else None
        )
        if org_id:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE requests
                       SET status = $2, updated_at = $3,
                           completed_at = COALESCE($4, completed_at),
                           reviewed_at = COALESCE($5, reviewed_at)
                       WHERE id = $1 AND organization_id = $6 RETURNING *""",
                    UUID(request_id),
                    status,
                    now,
                    completed_at,
                    reviewed_at,
                    UUID(org_id),
                )
        else:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE requests
                       SET status = $2, updated_at = $3,
                           completed_at = COALESCE($4, completed_at),
                           reviewed_at = COALESCE($5, reviewed_at)
                       WHERE id = $1 RETURNING *""",
                    UUID(request_id),
                    status,
                    now,
                    completed_at,
                    reviewed_at,
                )
        return dict(row) if row else None

    async def get_requests_in_review(
        self, org_id: str, limit: int = 25, offset: int = 0
    ) -> dict:
        """List requests currently awaiting or undergoing review."""
        base = """FROM requests
                   WHERE organization_id = $1
                     AND status IN ('review', 'needs_rework')"""
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) AS total {base}",
                UUID(org_id),
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                f"""SELECT * {base}
                    ORDER BY
                      CASE status WHEN 'review' THEN 0 ELSE 1 END,
                      updated_at DESC
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

    async def get_request_with_tasks(self, request_id: str, org_id: str) -> dict | None:
        """Load a request with its full task tree, events, memory links, and reviews."""
        req = await self.get_request(request_id, org_id)
        if not req:
            return None
        req["tasks"] = (await self.list_tasks(request_id))["items"]

        # Batch-load events and memory links for ALL tasks (avoids N+1)
        task_ids = [task["id"] for task in req["tasks"]]
        if task_ids:
            async with self.pool.acquire() as conn:
                event_rows = await conn.fetch(
                    "SELECT * FROM task_events WHERE task_id = ANY($1) ORDER BY created_at",
                    task_ids,
                )
                memory_rows = await conn.fetch(
                    """SELECT tm.*, m.content, m.type as memory_type, m.tags
                       FROM task_memories tm
                       JOIN memories m ON tm.memory_id = m.id
                       WHERE tm.task_id = ANY($1)
                       ORDER BY tm.created_at""",
                    task_ids,
                )

            # Group by task_id
            events_by_task: dict[str, list[dict]] = {}
            for row in event_rows:
                tid = str(row["task_id"])
                events_by_task.setdefault(tid, []).append(dict(row))

            memories_by_task: dict[str, list[dict]] = {}
            for row in memory_rows:
                tid = str(row["task_id"])
                memories_by_task.setdefault(tid, []).append(dict(row))

            for task in req["tasks"]:
                tid = str(task["id"])
                task["events"] = events_by_task.get(tid, [])
                task["memories"] = memories_by_task.get(tid, [])
        else:
            for task in req["tasks"]:
                task["events"] = []
                task["memories"] = []

        # Load reviews for this request (batch, no N+1)
        async with self.pool.acquire() as conn:
            review_rows = await conn.fetch(
                """SELECT * FROM reviews
                   WHERE request_id = $1 AND organization_id = $2
                   ORDER BY created_at DESC""",
                UUID(request_id),
                UUID(org_id),
            )
        req["reviews"] = [dict(r) for r in review_rows]

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
        sandbox_template_id: str | None = None,
        sandbox_config: dict | None = None,
        requesting_user_id: str | None = None,
        output_contract: dict | None = None,
    ) -> dict:
        _validate_output_contract(output_contract)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO tasks
                   (request_id, parent_task_id, title, description, agent_type,
                     agent_definition_id, priority, sequence_order, organization_id,
                     model, sandbox_template_id, sandbox_config, requesting_user_id,
                     output_contract)
                   VALUES (
                     $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                     COALESCE($13, (SELECT created_by FROM requests WHERE id = $1)),
                     $14
                    )
                    RETURNING *""",
                UUID(request_id),
                UUID(parent_task_id) if parent_task_id else None,
                title,
                description,
                agent_type,
                UUID(agent_definition_id) if agent_definition_id else None,
                priority,
                sequence_order,
                UUID(org_id),
                model,
                UUID(sandbox_template_id) if sandbox_template_id else None,
                json.dumps(sandbox_config) if sandbox_config else None,
                UUID(requesting_user_id) if requesting_user_id else None,
                json.dumps(output_contract) if output_contract else None,
            )
        task = dict(row)
        # Log creation event
        await self.add_task_event(str(task["id"]), "created", f"Task created: {title}")
        return task

    async def get_task(self, task_id: str, org_id: str | None = None) -> dict | None:
        if org_id:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM tasks WHERE id = $1 AND organization_id = $2",
                    UUID(task_id),
                    UUID(org_id),
                )
        else:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM tasks WHERE id = $1", UUID(task_id)
                )
        return dict(row) if row else None

    async def list_tasks(
        self,
        request_id: str,
        status: str | None = None,
        org_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        base = "FROM tasks WHERE request_id = $1"
        params: list[Any] = [UUID(request_id)]
        if org_id:
            params.append(UUID(org_id))
            base += f" AND organization_id = ${len(params)}"
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = (
            f"SELECT * {base} ORDER BY sequence_order, created_at "
            f"LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
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

    async def list_pending_requests(self, org_id: str, limit: int = 25, offset: int = 0) -> dict:
        """Get pending requests, including those with no tasks yet."""
        base = """FROM requests r LEFT JOIN tasks t ON t.request_id = r.id
                   WHERE r.organization_id = $1
                     AND r.status = 'pending'"""
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(DISTINCT r.id) AS total {base}",
                UUID(org_id),
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                f"""SELECT r.*, count(t.id) as task_count
                   {base}
                   GROUP BY r.id
                   ORDER BY
                     CASE r.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                     WHEN 'medium' THEN 2 ELSE 3 END,
                     r.created_at
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

    async def list_active_work(self, org_id: str, limit: int = 25, offset: int = 0) -> dict:
        """Get all non-completed requests with task status summaries.

        Returns requests in pending/in_progress/planned status along with
        counts of tasks by status, so the cognitive loop can see what's
        already being worked on and avoid creating duplicate work.
        """
        base = """FROM requests r LEFT JOIN tasks t ON t.request_id = r.id
                   WHERE r.organization_id = $1
                     AND r.status NOT IN ('completed', 'failed', 'cancelled')"""
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(DISTINCT r.id) AS total {base}",
                UUID(org_id),
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                f"""SELECT r.id, r.title, r.description, r.status, r.priority,
                          r.source, r.created_at,
                          count(t.id) FILTER (WHERE t.status = 'pending') AS tasks_pending,
                          count(t.id) FILTER (WHERE t.status = 'planned') AS tasks_planned,
                          count(t.id) FILTER (
                              WHERE t.status IN ('claimed', 'running')
                          ) AS tasks_running,
                          count(t.id) FILTER (WHERE t.status = 'completed') AS tasks_completed,
                          count(t.id) FILTER (WHERE t.status = 'failed') AS tasks_failed,
                          count(t.id) AS tasks_total
                   {base}
                   GROUP BY r.id
                   ORDER BY
                     CASE r.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                     WHEN 'medium' THEN 2 ELSE 3 END,
                     r.created_at
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

    async def list_pending_tasks(self, org_id: str, limit: int = 25, offset: int = 0) -> dict:
        """Get all tasks ready to be claimed.

        Respects sequence_order as a dependency gate: a task is only
        dispatchable when all lower-sequence tasks in the same request
        have completed.

        The request's dependency_policy controls what happens when a
        predecessor fails or is cancelled:
          - 'strict' (default): only 'completed' unblocks — failed/cancelled
            predecessors block all subsequent tasks.
          - 'permissive': completed, failed, and cancelled all unblock.
        """
        base = """FROM tasks t JOIN requests r ON t.request_id = r.id
                   WHERE t.organization_id = $1
                     AND t.status IN ('pending', 'planned')
                     AND NOT EXISTS (
                       SELECT 1 FROM tasks earlier
                       WHERE earlier.request_id = t.request_id
                         AND earlier.sequence_order < t.sequence_order
                         AND CASE COALESCE(r.dependency_policy, 'strict')
                             WHEN 'permissive'
                               THEN earlier.status NOT IN ('completed', 'failed', 'cancelled')
                             ELSE earlier.status != 'completed'
                             END
                     )"""
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) AS total {base}",
                UUID(org_id),
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                f"""SELECT t.*, r.title as request_title
                   {base}
                   ORDER BY
                     CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                     WHEN 'medium' THEN 2 ELSE 3 END,
                     t.sequence_order, t.created_at
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

    async def claim_task(
        self, task_id: str, instance_id: str, org_id: str | None = None
    ) -> dict | None:
        """Atomically claim a pending task. Returns None if already claimed."""
        now = datetime.now(timezone.utc)
        if org_id:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'claimed', claimed_by = $2,
                       claimed_at = $3, updated_at = $3
                       WHERE id = $1 AND status IN ('pending', 'planned')
                       AND organization_id = $4
                       RETURNING *""",
                    UUID(task_id),
                    instance_id,
                    now,
                    UUID(org_id),
                )
        else:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'claimed', claimed_by = $2,
                       claimed_at = $3, updated_at = $3
                       WHERE id = $1 AND status IN ('pending', 'planned')
                       RETURNING *""",
                    UUID(task_id),
                    instance_id,
                    now,
                )
        if row:
            task = dict(row)
            await self.add_task_event(
                task_id,
                "claimed",
                f"Claimed by {instance_id}",
                metadata={"instance_id": instance_id},
            )
            # Update parent request to in_progress if still pending/planned
            await self._ensure_request_in_progress(str(task["request_id"]))
            return task
        return None

    async def update_task_model(self, task_id: str, model: str) -> dict | None:
        """Write the resolved model back to the task record."""
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE tasks SET model = $1, updated_at = $2 WHERE id = $3 RETURNING *",
                model,
                now,
                UUID(task_id),
            )
        return dict(row) if row else None

    async def start_task(self, task_id: str, org_id: str | None = None) -> dict | None:
        """Mark a claimed task as running."""
        now = datetime.now(timezone.utc)
        if org_id:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'running', updated_at = $2
                       WHERE id = $1 AND status = 'claimed'
                       AND organization_id = $3 RETURNING *""",
                    UUID(task_id),
                    now,
                    UUID(org_id),
                )
        else:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'running', updated_at = $2
                       WHERE id = $1 AND status = 'claimed' RETURNING *""",
                    UUID(task_id),
                    now,
                )
        if row:
            await self.add_task_event(task_id, "running", "Agent started execution")
            return dict(row)
        return None

    async def complete_task(
        self,
        task_id: str,
        result: str,
        org_id: str | None = None,
        result_structured: dict | None = None,
        result_summary: str | None = None,
        validation_status: str = "not_applicable",
        validation_errors: list | None = None,
    ) -> dict | None:
        """Mark task as completed with result.

        Only tasks in 'claimed' or 'running' state can be completed
        (workflow-audit/phase-4: status transition guard).
        """
        if validation_status not in _VALID_VALIDATION_STATUSES:
            valid = ", ".join(sorted(_VALID_VALIDATION_STATUSES))
            raise ValueError(
                f"Invalid validation_status '{validation_status}'. Must be one of: {valid}"
            )
        now = datetime.now(timezone.utc)
        if org_id:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'completed', result = $2,
                       result_structured = $3,
                       result_summary = $4,
                       validation_status = $5,
                       validation_errors = $6,
                       completed_at = $7, updated_at = $7
                       WHERE id = $1 AND status IN ('claimed', 'running')
                       AND organization_id = $8 RETURNING *""",
                    UUID(task_id),
                    result,
                    json.dumps(result_structured) if result_structured is not None else None,
                    result_summary,
                    validation_status,
                    json.dumps(validation_errors) if validation_errors is not None else None,
                    now,
                    UUID(org_id),
                )
        else:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'completed', result = $2,
                       result_structured = $3,
                       result_summary = $4,
                       validation_status = $5,
                       validation_errors = $6,
                       completed_at = $7, updated_at = $7
                       WHERE id = $1 AND status IN ('claimed', 'running')
                       RETURNING *""",
                    UUID(task_id),
                    result,
                    json.dumps(result_structured) if result_structured is not None else None,
                    result_summary,
                    validation_status,
                    json.dumps(validation_errors) if validation_errors is not None else None,
                    now,
                )
        if row:
            task = dict(row)
            await self.add_task_event(
                task_id,
                "completed",
                f"Completed ({len(result)} chars output)",
            )
            # Check if all tasks in request are done
            await self._check_request_completion(str(task["request_id"]))
            return task
        return None

    async def fail_task(
        self, task_id: str, error: str, org_id: str | None = None
    ) -> dict | None:
        """Mark task as failed.

        Only tasks in 'claimed' or 'running' state can be failed
        (workflow-audit/phase-4: status transition guard).
        """
        now = datetime.now(timezone.utc)
        if org_id:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'failed', error = $2,
                       completed_at = $3, updated_at = $3
                       WHERE id = $1 AND status IN ('claimed', 'running')
                       AND organization_id = $4 RETURNING *""",
                    UUID(task_id),
                    error,
                    now,
                    UUID(org_id),
                )
        else:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'failed', error = $2,
                       completed_at = $3, updated_at = $3
                       WHERE id = $1 AND status IN ('claimed', 'running')
                       RETURNING *""",
                    UUID(task_id),
                    error,
                    now,
                )
        if row:
            task = dict(row)
            await self.add_task_event(task_id, "failed", f"Failed: {error[:200]}")
            await self._check_request_completion(str(task["request_id"]))
            return task
        return None

    async def release_task(self, task_id: str, org_id: str | None = None) -> dict | None:
        """Release a claimed/running task back to pending (for retry/stale recovery)."""
        now = datetime.now(timezone.utc)
        if org_id:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                       claimed_at = NULL, updated_at = $2
                       WHERE id = $1 AND status IN ('claimed', 'running')
                       AND organization_id = $3 RETURNING *""",
                    UUID(task_id),
                    now,
                    UUID(org_id),
                )
        else:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                       claimed_at = NULL, updated_at = $2
                       WHERE id = $1 AND status IN ('claimed', 'running') RETURNING *""",
                    UUID(task_id),
                    now,
                )
        if row:
            await self.add_task_event(task_id, "released", "Task released back to pending")
            return dict(row)
        return None

    async def retry_task(self, task_id: str, org_id: str | None = None) -> dict | None:
        """Reset a failed task back to pending for retry."""
        now = datetime.now(timezone.utc)
        if org_id:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                       claimed_at = NULL, completed_at = NULL, result = NULL,
                       error = NULL, updated_at = $2
                       WHERE id = $1 AND status = 'failed'
                       AND organization_id = $3 RETURNING *""",
                    UUID(task_id),
                    now,
                    UUID(org_id),
                )
        else:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                       claimed_at = NULL, completed_at = NULL, result = NULL,
                       error = NULL, updated_at = $2
                       WHERE id = $1 AND status = 'failed' RETURNING *""",
                    UUID(task_id),
                    now,
                )
        if row:
            task = dict(row)
            await self.add_task_event(task_id, "retried", "Task queued for retry")
            # If parent request was marked failed, set it back to in_progress
            await self._ensure_request_in_progress(str(task["request_id"]))
            return task
        return None

    async def retry_task_with_feedback(
        self, task_id: str, feedback: str, org_id: str | None = None
    ) -> dict | None:
        """Retry a failed task and persist corrective feedback on the parent request."""
        task = await self.retry_task(task_id, org_id=org_id)
        if not task:
            return None

        now = datetime.now(timezone.utc)
        request_id = str(task["request_id"])
        async with self.pool.acquire() as conn:
            if org_id:
                await conn.execute(
                    """UPDATE requests
                       SET status = $2,
                           review_feedback = $3,
                           review_count = review_count + 1,
                           updated_at = $4
                       WHERE id = $1 AND organization_id = $5""",
                    UUID(request_id),
                    REQUEST_STATUS_IN_PROGRESS,
                    feedback,
                    now,
                    UUID(org_id),
                )
            else:
                await conn.execute(
                    """UPDATE requests
                       SET status = $2,
                           review_feedback = $3,
                           review_count = review_count + 1,
                           updated_at = $4
                       WHERE id = $1""",
                    UUID(request_id),
                    REQUEST_STATUS_IN_PROGRESS,
                    feedback,
                    now,
                )

        await self.add_task_event(
            task_id,
            "review_feedback",
            "Retry queued with review feedback",
            metadata={"feedback": feedback},
        )
        refreshed = await self.get_task(task_id, org_id=org_id)
        return refreshed

    async def release_stale_tasks(
        self, stale_minutes: int = 30, org_id: str | None = None
    ) -> int:
        """Release tasks stuck in claimed/running state past the timeout."""
        if org_id:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                       claimed_at = NULL, updated_at = NOW()
                       WHERE status IN ('claimed', 'running')
                       AND claimed_at < NOW() - make_interval(mins := $1)
                       AND organization_id = $2
                       RETURNING id""",
                    stale_minutes,
                    UUID(org_id),
                )
        else:
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
                str(row["id"]),
                "released",
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
                UUID(task_id),
                event_type,
                detail,
                json.dumps(metadata) if metadata else "{}",
            )
        return dict(row)

    async def list_task_events(
        self,
        task_id: str,
        limit: int = 25,
        offset: int = 0,
        org_id: str | None = None,
    ) -> dict:
        async with self.pool.acquire() as conn:
            if org_id:
                count_row = await conn.fetchrow(
                    """SELECT COUNT(*) AS total FROM task_events te
                       JOIN tasks t ON te.task_id = t.id
                       WHERE te.task_id = $1 AND t.organization_id = $2""",
                    UUID(task_id),
                    UUID(org_id),
                )
                total_count = count_row["total"] if count_row else 0
                rows = await conn.fetch(
                    """SELECT te.* FROM task_events te
                       JOIN tasks t ON te.task_id = t.id
                       WHERE te.task_id = $1 AND t.organization_id = $2
                       ORDER BY te.created_at LIMIT $3 OFFSET $4""",
                    UUID(task_id),
                    UUID(org_id),
                    limit,
                    offset,
                )
            else:
                count_row = await conn.fetchrow(
                    "SELECT COUNT(*) AS total FROM task_events WHERE task_id = $1",
                    UUID(task_id),
                )
                total_count = count_row["total"] if count_row else 0
                rows = await conn.fetch(
                    "SELECT * FROM task_events WHERE task_id = $1 "
                    "ORDER BY created_at LIMIT $2 OFFSET $3",
                    UUID(task_id),
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

    # ── Task ↔ Memory Links ──────────────────────────────────────────────

    async def link_memory(
        self,
        task_id: str,
        memory_id: str,
        relation: str = "created",
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO task_memories (task_id, memory_id, relation)
                   VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                UUID(task_id),
                UUID(memory_id),
                relation,
            )
        await self.add_task_event(
            task_id,
            f"memory_{relation}",
            f"Memory {relation}: {memory_id[:8]}...",
            metadata={"memory_id": memory_id, "relation": relation},
        )

    async def list_task_memories(
        self,
        task_id: str,
        org_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        async with self.pool.acquire() as conn:
            if org_id:
                count_row = await conn.fetchrow(
                    """SELECT COUNT(*) AS total FROM task_memories tm
                       JOIN tasks t ON tm.task_id = t.id
                       WHERE tm.task_id = $1 AND t.organization_id = $2""",
                    UUID(task_id),
                    UUID(org_id),
                )
                total_count = count_row["total"] if count_row else 0
                rows = await conn.fetch(
                    """SELECT tm.*, m.content, m.type as memory_type, m.tags
                       FROM task_memories tm
                       JOIN memories m ON tm.memory_id = m.id
                       JOIN tasks t ON tm.task_id = t.id
                       WHERE tm.task_id = $1 AND t.organization_id = $2
                       ORDER BY tm.created_at LIMIT $3 OFFSET $4""",
                    UUID(task_id),
                    UUID(org_id),
                    limit,
                    offset,
                )
            else:
                count_row = await conn.fetchrow(
                    "SELECT COUNT(*) AS total FROM task_memories WHERE task_id = $1",
                    UUID(task_id),
                )
                total_count = count_row["total"] if count_row else 0
                rows = await conn.fetch(
                    """SELECT tm.*, m.content, m.type as memory_type, m.tags
                       FROM task_memories tm
                       JOIN memories m ON tm.memory_id = m.id
                       WHERE tm.task_id = $1
                       ORDER BY tm.created_at LIMIT $2 OFFSET $3""",
                    UUID(task_id),
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

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _ensure_request_in_progress(self, request_id: str) -> None:
        """Move request to in_progress if it's not already active.

        Handles pending/planned states AND failed (for retry recovery).
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE requests SET status = 'in_progress', updated_at = NOW()
                   WHERE id = $1 AND status IN ('pending', 'planned', 'failed', 'needs_rework')""",
                UUID(request_id),
            )

    async def _check_request_completion(self, request_id: str) -> None:
        """If all tasks are done, move request to review (or failed if task failed)."""
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
            status = REQUEST_STATUS_FAILED if failed > 0 else REQUEST_STATUS_REVIEW
            await self.update_request_status(request_id, status)

    async def reconcile_request_statuses(self, org_id: str | None = None) -> int:
        """Fix request statuses that got out of sync with their tasks.

        Handles two cases:
        1. Request is 'in_progress' but all tasks are terminal → complete/fail it
        2. Request is 'pending' but has running/completed tasks → mark in_progress

        Returns the number of requests fixed.
        """
        fixed = 0
        org_filter = "AND r.organization_id = $1" if org_id else ""
        params: list = [UUID(org_id)] if org_id else []

        async with self.pool.acquire() as conn:
            # Case 1: in_progress requests where all tasks are done
            rows = await conn.fetch(
                f"""SELECT r.id FROM requests r
                   WHERE r.status IN ('in_progress', 'needs_rework') {org_filter}
                   AND NOT EXISTS (
                       SELECT 1 FROM tasks t
                       WHERE t.request_id = r.id
                       AND t.status NOT IN ('completed', 'failed', 'cancelled')
                   )
                   AND EXISTS (SELECT 1 FROM tasks t WHERE t.request_id = r.id)""",
                *params,
            )
            for row in rows:
                await self._check_request_completion(str(row["id"]))
                fixed += 1

            # Case 2: pending requests with active/completed tasks
            rows = await conn.fetch(
                f"""SELECT DISTINCT r.id FROM requests r
                   JOIN tasks t ON t.request_id = r.id
                   WHERE r.status IN ('pending', 'planned', 'needs_rework') {org_filter}
                   AND t.status IN ('claimed', 'running', 'completed')""",
                *params,
            )
            for row in rows:
                await self._ensure_request_in_progress(str(row["id"]))
                fixed += 1

        return fixed

    # ── Dashboard queries ─────────────────────────────────────────────────

    async def get_active_summary(self, org_id: str) -> dict:
        """Quick dashboard stats."""
        async with self.pool.acquire() as conn:
            req_stats = await conn.fetchrow(
                """SELECT
                     COUNT(*) as total,
                     COUNT(*) FILTER (
                         WHERE status IN ('in_progress', 'review', 'needs_rework')
                     ) as active,
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
                UUID(org_id),
                limit,
            )
        return [dict(r) for r in rows]
