"""Repository for request tracking and task queue.

Full lineage: request → tasks → events → memory links.
"""

import json
import logging
import os
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

logger = logging.getLogger(__name__)

# Approval statuses for the pre-work gate
APPROVAL_AUTO = "auto_approved"
APPROVAL_PENDING = "pending_approval"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"

# Sources subject to the auto-approve toggle.
# Schedule is excluded — scheduled requests are always auto-approved because
# schedules are either user-created or built-in system tasks.
_DAEMON_SOURCES = frozenset({"cognitive", "daemon"})


def _requires_approval(source: str) -> bool:
    """Check if a request from this source needs human approval.

    User/API/schedule requests are always auto-approved.
    Cognitive/daemon requests require approval unless
    LUCENT_AUTO_APPROVE is set to true (default: false).
    """
    if source not in _DAEMON_SOURCES:
        return False
    auto_approve = os.environ.get("LUCENT_AUTO_APPROVE", "false").lower()
    return auto_approve not in ("true", "1", "yes")


def _requires_post_completion_review() -> bool:
    """Check if completed requests should go through internal review.

    The daemon's post-completion review task is an automatic quality check
    (did the work accomplish what was requested?).  It always runs by default
    because it auto-approves or sends work back for rework — no human needed.

    Set LUCENT_SKIP_POST_REVIEW=true to bypass the automatic review task
    and send completed requests straight to 'completed' status.
    """
    val = os.environ.get("LUCENT_SKIP_POST_REVIEW", "false").lower()
    return val not in ("true", "1", "yes")


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
        memory_ids: list[dict] | None = None,
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
        approval = APPROVAL_PENDING if _requires_approval(source) else APPROVAL_AUTO
        now = datetime.now(timezone.utc) if approval == APPROVAL_AUTO else None
        async with self.pool.acquire() as conn:
            if memory_ids:
                mem_ids = [UUID(m["id"]) for m in memory_ids]
                if mem_ids:
                    # Check if any linked goal memory is already completed.
                    # Completed goals should not spawn new requests.
                    completed_goal = await conn.fetchval(
                        """SELECT m.id FROM memories m
                           WHERE m.id = ANY($1)
                             AND m.type = 'goal'
                             AND m.metadata->>'status' IN ('completed', 'abandoned')
                           LIMIT 1""",
                        mem_ids,
                    )
                    if completed_goal:
                        # Return a synthetic response indicating the goal is done
                        return {
                            "id": completed_goal,
                            "title": title,
                            "status": "skipped",
                            "reason": "goal_completed",
                        }

                    # Dedup: if any linked memory already has an active request, return it.
                    existing = await conn.fetchrow(
                        """SELECT r.* FROM requests r
                           JOIN request_memories rm ON r.id = rm.request_id
                           WHERE r.organization_id = $1
                             AND rm.memory_id = ANY($2)
                             AND r.status NOT IN ('completed', 'failed', 'cancelled')
                           ORDER BY r.created_at DESC LIMIT 1""",
                        UUID(org_id),
                        mem_ids,
                    )
                    if existing:
                        return dict(existing)

            row = await conn.fetchrow(
                """INSERT INTO requests
                   (title, description, source, priority, created_by,
                    organization_id, dependency_policy,
                    approval_status, approved_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                   RETURNING *""",
                title,
                description,
                source,
                priority,
                UUID(created_by) if created_by else None,
                UUID(org_id),
                dependency_policy,
                approval,
                now,
            )

            # Link memories to the new request
            if memory_ids and row:
                for m in memory_ids:
                    try:
                        await conn.execute(
                            """INSERT INTO request_memories (request_id, memory_id, relation)
                               VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                            row["id"],
                            UUID(m["id"]),
                            m.get("relation", "goal"),
                        )
                    except Exception:
                        pass  # Best-effort — don't fail request creation on link errors

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
        exclude_status: str | None = None,
    ) -> dict:
        base = "FROM requests WHERE organization_id = $1"
        params: list[Any] = [UUID(org_id)]
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"
        elif exclude_status:
            excluded = [s.strip() for s in exclude_status.split(",") if s.strip()]
            if excluded:
                placeholders = ", ".join(f"${len(params) + i + 1}" for i in range(len(excluded)))
                params.extend(excluded)
                base += f" AND status NOT IN ({placeholders})"
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

        # When a request reaches a terminal state, close any linked schedule run.
        # This runs in a separate connection from the request status update above,
        # so there is a small inconsistency window where the request is terminal
        # but the schedule_run remains "running". We intentionally keep this
        # best-effort/non-fatal to avoid rolling back the primary request update.
        # The schedule_run query targets only status='running', making retries safe
        # and preventing terminal schedule_runs from being overwritten.
        if row and status in (
            REQUEST_STATUS_COMPLETED,
            REQUEST_STATUS_FAILED,
            REQUEST_STATUS_CANCELLED,
        ):
            try:
                async with self.pool.acquire() as conn:
                    if status == REQUEST_STATUS_COMPLETED:
                        await conn.execute(
                            """UPDATE schedule_runs
                               SET status = 'completed', completed_at = now()
                               WHERE request_id = $1::uuid AND status = 'running'""",
                            request_id,
                        )
                    else:
                        await conn.execute(
                            """UPDATE schedule_runs
                               SET status = 'failed', completed_at = now(),
                                   error = $2
                               WHERE request_id = $1::uuid AND status = 'running'""",
                            request_id,
                            f"Request {status}",
                        )
            except Exception as e:
                logger.warning(
                    "Failed to close schedule run for request %s: %s",
                    request_id,
                    e,
                )

        return dict(row) if row else None

    async def link_request_memory(
        self,
        request_id: str,
        memory_id: str,
        relation: str = "goal",
        org_id: str | None = None,
    ) -> dict | None:
        """Link a memory to a request."""
        async with self.pool.acquire() as conn:
            # Verify the request exists and belongs to the org
            if org_id:
                req = await conn.fetchval(
                    "SELECT id FROM requests WHERE id = $1::uuid AND organization_id = $2::uuid",
                    request_id,
                    UUID(org_id),
                )
                if not req:
                    return None
            row = await conn.fetchrow(
                """INSERT INTO request_memories (request_id, memory_id, relation)
                   VALUES ($1::uuid, $2::uuid, $3)
                   ON CONFLICT DO NOTHING
                   RETURNING *""",
                request_id,
                memory_id,
                relation,
            )
        return dict(row) if row else None

    async def approve_request(
        self,
        request_id: str,
        org_id: str,
        approved_by: str,
        comment: str | None = None,
    ) -> dict | None:
        """Approve a pending_approval request so work can begin."""
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE requests
                   SET approval_status = 'approved',
                       approved_by = $3::uuid,
                       approved_at = $4,
                       approval_comment = $5,
                       updated_at = $4
                   WHERE id = $1::uuid
                     AND organization_id = $2::uuid
                     AND approval_status = 'pending_approval'
                   RETURNING *""",
                request_id,
                org_id,
                approved_by,
                now,
                comment,
            )
        return dict(row) if row else None

    async def reject_request(
        self,
        request_id: str,
        org_id: str,
        rejected_by: str,
        comment: str,
    ) -> dict | None:
        """Reject a pending_approval request — enters rejection_processing for daemon feedback loop."""
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE requests
                   SET approval_status = 'rejected',
                       approved_by = $3::uuid,
                       approved_at = $4,
                       approval_comment = $5,
                       status = 'rejection_processing',
                       updated_at = $4
                   WHERE id = $1::uuid
                     AND organization_id = $2::uuid
                     AND approval_status = 'pending_approval'
                   RETURNING *""",
                request_id,
                org_id,
                rejected_by,
                now,
                comment,
            )
        return dict(row) if row else None

    async def list_pending_approvals(
        self, org_id: str, limit: int = 25, offset: int = 0
    ) -> dict:
        """List requests awaiting human approval."""
        base = """FROM requests
                   WHERE organization_id = $1
                     AND approval_status = 'pending_approval'"""
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) AS total {base}",
                UUID(org_id),
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                f"""SELECT * {base}
                   ORDER BY
                     CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                   WHEN 'medium' THEN 2 ELSE 3 END,
                     created_at
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

        # Load request-level memory links
        async with self.pool.acquire() as conn:
            mem_rows = await conn.fetch(
                """SELECT rm.memory_id, rm.relation, rm.created_at,
                          m.content, m.type AS memory_type, m.tags,
                          m.metadata
                   FROM request_memories rm
                   JOIN memories m ON rm.memory_id = m.id
                   WHERE rm.request_id = $1
                   ORDER BY rm.created_at""",
                UUID(request_id),
            )
        req["memories"] = [dict(r) for r in mem_rows]

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

    async def list_recently_completed(
        self, org_id: str, hours: int = 2, limit: int = 25,
    ) -> list[dict]:
        """Get requests completed within the last N hours.

        Used by the cognitive loop to avoid re-creating work that was
        just finished. Without this, the window between a request completing
        and the goal memory being updated leaves a gap where duplicates
        can be created.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT r.id, r.title, r.source, r.status, r.completed_at
                   FROM requests r
                   WHERE r.organization_id = $1
                     AND r.status IN ('completed', 'review')
                     AND r.completed_at > NOW() - make_interval(hours => $2)
                   ORDER BY r.completed_at DESC
                   LIMIT $3""",
                UUID(org_id),
                hours,
                limit,
            )
        return [dict(r) for r in rows]

    async def list_pending_tasks(self, org_id: str, limit: int = 25, offset: int = 0) -> dict:
        """Get all tasks ready to be claimed.

        Respects sequence_order as a dependency gate: a task is only
        dispatchable when every earlier sequence level in the same request
        has at least one task in an acceptable terminal state.

        This correctly handles retries — if a task at sequence 0 fails but
        a retry task at the same sequence 0 completes, subsequent tasks
        are unblocked.

        The request's dependency_policy controls what happens when a
        predecessor fails or is cancelled:
          - 'strict' (default): at least one task at each earlier level must
            be 'completed' — failed/cancelled predecessors block unless a
            retry completed.
          - 'permissive': completed, failed, and cancelled all count as
            acceptable terminal states.
        """
        base = """FROM tasks t JOIN requests r ON t.request_id = r.id
                   WHERE t.organization_id = $1
                     AND t.status IN ('pending', 'planned')
                     AND r.approval_status IN ('auto_approved', 'approved')
                     AND NOT EXISTS (
                       SELECT 1 FROM (
                           SELECT DISTINCT sequence_order AS seq
                           FROM tasks
                           WHERE request_id = t.request_id
                             AND sequence_order < t.sequence_order
                       ) earlier_seqs
                       WHERE NOT EXISTS (
                           SELECT 1 FROM tasks t2
                           WHERE t2.request_id = t.request_id
                             AND t2.sequence_order = earlier_seqs.seq
                             AND CASE COALESCE(r.dependency_policy, 'strict')
                                 WHEN 'permissive'
                                   THEN t2.status IN ('completed', 'failed', 'cancelled')
                                 ELSE t2.status = 'completed'
                                 END
                       )
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
                       error = NULL,
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
                       error = NULL,
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
        org_id: str | None = None,
    ) -> dict:
        import json

        async with self.pool.acquire() as conn:
            if org_id:
                task_exists = await conn.fetchval(
                    "SELECT 1 FROM tasks WHERE id = $1 AND organization_id = $2",
                    UUID(task_id),
                    UUID(org_id),
                )
                if not task_exists:
                    raise ValueError("Task not found")
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
        org_id: str | None = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            if org_id:
                task_exists = await conn.fetchval(
                    "SELECT 1 FROM tasks WHERE id = $1 AND organization_id = $2",
                    UUID(task_id),
                    UUID(org_id),
                )
                if not task_exists:
                    raise ValueError("Task not found")
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
            org_id=org_id,
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
        """If all work tasks are done, move request to review (or failed).

        Excludes request-review tasks from the completion check — they are
        meta-tasks that validate work, not work tasks themselves.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT
                     COUNT(*) as total,
                     COUNT(*) FILTER (WHERE status IN ('completed', 'failed', 'cancelled')) as done
                   FROM tasks
                   WHERE request_id = $1
                     AND agent_type IS DISTINCT FROM 'request-review'""",
                UUID(request_id),
            )
        if row and row["total"] > 0 and row["total"] == row["done"]:
            # Check if any failed
            async with self.pool.acquire() as conn:
                failed = await conn.fetchval(
                    """SELECT COUNT(*) FROM tasks
                       WHERE request_id = $1 AND status = 'failed'
                         AND agent_type IS DISTINCT FROM 'request-review'""",
                    UUID(request_id),
                )
            status = REQUEST_STATUS_FAILED if failed > 0 else (
                REQUEST_STATUS_REVIEW if _requires_post_completion_review()
                else REQUEST_STATUS_COMPLETED
            )
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
