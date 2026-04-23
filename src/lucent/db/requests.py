"""Repository for request tracking and task queue.

Full lineage: request → tasks → events → memory links.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
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

DEFAULT_TASK_LEASE_SECONDS = int(os.environ.get("LUCENT_TASK_LEASE_SECONDS", "1800"))
DEFAULT_INSTANCE_STALE_SECONDS = int(os.environ.get("LUCENT_INSTANCE_STALE_SECONDS", "1800"))

# Advisory lock namespace for serializing memory-link operations on the same
# memory_id across concurrent transactions. Keyed alongside the memory UUID.
# Distinct from DECOMPOSITION_LOCK_NAMESPACE in daemon.py.
MEMORY_LINK_LOCK_NAMESPACE = 0x4C4D454D  # "LMEM" — Lucent MEMory link

# How recently a completed/cancelled request must have happened for a
# normalized-title match to be considered a duplicate of new work for the
# same goal. The cognitive planner runs periodically; if a request finished
# in the last day and another one with the same normalized title comes in,
# it's almost certainly the planner re-proposing work because the goal's
# milestone state hasn't been updated.
RECENT_COMPLETION_WINDOW_HOURS = 24


def _normalize_title_for_dedup(title: str | None) -> str:
    """Normalize a request title for duplicate detection.

    Lowercases, replaces all runs of non-alphanumeric characters with a
    single space, and trims. This collapses superficial differences like
    hyphen-vs-space, em-dash, or extra punctuation that the planner often
    introduces between cycles.

    Examples:
        'Native forgetting M2: Propose strategy candidates'
        'Native-forgetting M2 — propose strategy candidates'
        → both normalize to 'native forgetting m2 propose strategy candidates'
    """
    if not title:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


# Patterns the planner uses to label which milestone a request targets.
# Order matters: more specific patterns come first so 'Milestone 3' isn't
# captured as just 'M3' (the integer would still match but the more specific
# label is more readable in error output).
_MILESTONE_REF_PATTERNS = (
    re.compile(r"\bMilestone\s+(\d+)\b", re.IGNORECASE),
    re.compile(r"\bPhase\s+(\d+)\b", re.IGNORECASE),
    # Stand-alone M<N> or M<N>: at the start of a token.
    # Anchored so we don't grab the M out of words like 'memo' or 'M3 file'.
    re.compile(r"(?<![A-Za-z])M(\d+)(?=[\s:,.\-—–_]|$)"),
)


def _extract_milestone_number(title: str | None) -> int | None:
    """Extract the milestone index a request title is targeting.

    The cognitive planner is instructed to label requests with the milestone
    they target — 'Foo M3: ...', 'Foo Milestone 3 ...', 'Foo Phase 3 ...'.
    We extract that integer so the create-request guard can validate it
    against the goal's milestone array.

    Returns the 1-based milestone number, or None if the title doesn't
    reference a specific milestone (in which case we can't validate, and
    the request is allowed through).
    """
    if not title:
        return None
    for pat in _MILESTONE_REF_PATTERNS:
        m = pat.search(title)
        if m:
            try:
                n = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if n >= 1:
                return n
    return None

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

    # ── Daemon Instance Registry ───────────────────────────────────────────

    async def register_instance(
        self,
        *,
        org_id: str,
        instance_id: str,
        hostname: str | None = None,
        pid: int | None = None,
        roles: list[str] | None = None,
        metadata: dict | None = None,
        status: str = "active",
    ) -> dict:
        """Register or refresh a daemon instance row."""
        now = datetime.now(timezone.utc)
        roles = roles or []
        metadata_json = json.dumps(metadata or {})
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO daemon_instances
                   (instance_id, organization_id, hostname, pid, roles, status,
                    started_at, last_seen_at, metadata, created_at, updated_at)
                   VALUES ($1, $2, $3, $4, $5::text[], $6, $7, $7, $8::jsonb, $7, $7)
                   ON CONFLICT (instance_id, organization_id) DO UPDATE
                   SET hostname = EXCLUDED.hostname,
                       pid = EXCLUDED.pid,
                       roles = EXCLUDED.roles,
                       status = EXCLUDED.status,
                       metadata = EXCLUDED.metadata,
                       last_seen_at = EXCLUDED.last_seen_at,
                       updated_at = EXCLUDED.updated_at
                   RETURNING *""",
                instance_id,
                UUID(org_id),
                hostname,
                pid,
                roles,
                status,
                now,
                metadata_json,
            )
        return dict(row)

    async def heartbeat_instance(
        self,
        *,
        org_id: str,
        instance_id: str,
        metadata: dict | None = None,
        lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS,
    ) -> dict | None:
        """Update daemon last_seen and renew leases on claimed/running tasks."""
        now = datetime.now(timezone.utc)
        metadata_json = json.dumps(metadata or {})
        expires_at = now + timedelta(seconds=lease_seconds)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """UPDATE daemon_instances
                       SET last_seen_at = $3,
                           status = 'active',
                           metadata = CASE
                               WHEN $4::jsonb = '{}'::jsonb THEN metadata
                               ELSE $4::jsonb
                           END,
                           updated_at = $3
                       WHERE organization_id = $1
                         AND instance_id = $2
                       RETURNING *""",
                    UUID(org_id),
                    instance_id,
                    now,
                    metadata_json,
                )
                if not row:
                    return None
                await conn.execute(
                    """UPDATE tasks
                       SET last_heartbeat_at = $3,
                           claim_expires_at = $4,
                           updated_at = $3
                       WHERE organization_id = $1
                          AND claimed_by = $2
                          AND status IN ('claimed', 'running')""",
                    UUID(org_id),
                    instance_id,
                    now,
                    expires_at,
                )
        return dict(row)

    async def mark_instance_stopped(self, *, org_id: str, instance_id: str) -> dict | None:
        """Mark daemon instance as stopped."""
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE daemon_instances
                   SET status = 'stopped',
                       updated_at = $3
                   WHERE organization_id = $1
                     AND instance_id = $2
                   RETURNING *""",
                UUID(org_id),
                instance_id,
                now,
            )
        return dict(row) if row else None

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
        target_repo: str | None = None,
        target_paths: list[str] | None = None,
        force_pending_approval: bool = False,
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
        approval = (
            APPROVAL_PENDING
            if force_pending_approval or _requires_approval(source)
            else APPROVAL_AUTO
        )
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

                    # Milestone-state guard. The cognitive planner has been
                    # observed picking the wrong milestone (proposing M1 work
                    # when M1 is already completed and M5 is the active one).
                    # Title-normalization dedup doesn't catch this because
                    # each generated description is novel — it's not the same
                    # work being re-proposed, it's plausible work for the
                    # wrong phase. The structural fix is to verify the
                    # milestone the title references is actually active in
                    # the goal's metadata. If the title doesn't reference a
                    # milestone, we can't validate and let it through. If the
                    # goal has no milestones, we let it through. Otherwise:
                    #   - referenced milestone must exist
                    #   - and have status='active'
                    # Also: if every milestone is completed/abandoned but the
                    # goal status is still 'active', refuse and tell the
                    # planner to mark the goal complete instead of inventing
                    # phantom work.
                    if source == "cognitive":
                        goal_rows = await conn.fetch(
                            """SELECT id, content, metadata
                               FROM memories
                               WHERE id = ANY($1)
                                 AND type = 'goal'
                                 AND deleted_at IS NULL""",
                            mem_ids,
                        )
                        for goal_row in goal_rows:
                            metadata = goal_row["metadata"]
                            if isinstance(metadata, str):
                                try:
                                    metadata = json.loads(metadata)
                                except (TypeError, ValueError):
                                    metadata = None
                            if not isinstance(metadata, dict):
                                continue
                            milestones = metadata.get("milestones") or []
                            if not isinstance(milestones, list) or not milestones:
                                continue

                            active_indexes = [
                                i for i, m in enumerate(milestones, start=1)
                                if isinstance(m, dict) and m.get("status") == "active"
                            ]
                            # Goal has milestones but none are active anymore —
                            # the planner is making up work. Refuse and tell
                            # it to mark the goal completed.
                            if not active_indexes:
                                return {
                                    "id": str(goal_row["id"]),
                                    "title": title,
                                    "status": "skipped",
                                    "reason": "goal_milestones_all_done",
                                    "detail": (
                                        f"Goal {goal_row['id']} has "
                                        f"{len(milestones)} milestones and none "
                                        "are active anymore. Do NOT create new "
                                        "work for this goal — call update_memory "
                                        "to set metadata.status='completed'."
                                    ),
                                }

                            referenced_n = _extract_milestone_number(title)
                            if referenced_n is None:
                                # No milestone label in the title. The planner
                                # is supposed to use the canonical 'M<N>:'
                                # format so future cycles can recognize prior
                                # work; refuse here and force the convention.
                                return {
                                    "id": str(goal_row["id"]),
                                    "title": title,
                                    "status": "skipped",
                                    "reason": "milestone_not_referenced",
                                    "detail": (
                                        f"Goal {goal_row['id']} has milestones, "
                                        "but the request title does not "
                                        "reference a specific milestone (e.g. "
                                        "'M3', 'Phase 3', 'Milestone 3'). "
                                        f"Active milestones: {active_indexes}. "
                                        "Re-issue the request with the next "
                                        "active milestone in the title."
                                    ),
                                }

                            if referenced_n > len(milestones):
                                return {
                                    "id": str(goal_row["id"]),
                                    "title": title,
                                    "status": "skipped",
                                    "reason": "milestone_out_of_range",
                                    "detail": (
                                        f"Title references M{referenced_n} but "
                                        f"goal {goal_row['id']} only has "
                                        f"{len(milestones)} milestones. Active "
                                        f"milestones: {active_indexes}."
                                    ),
                                }

                            milestone = milestones[referenced_n - 1]
                            current_status = (
                                milestone.get("status")
                                if isinstance(milestone, dict)
                                else None
                            )
                            if current_status != "active":
                                return {
                                    "id": str(goal_row["id"]),
                                    "title": title,
                                    "status": "skipped",
                                    "reason": "milestone_not_active",
                                    "detail": (
                                        f"Title references M{referenced_n} but "
                                        f"that milestone of goal "
                                        f"{goal_row['id']} has status "
                                        f"{current_status!r}, not 'active'. "
                                        f"Active milestones: {active_indexes}. "
                                        "Pick the next active milestone or "
                                        "update the goal's milestone state if "
                                        "you believe this one was completed by "
                                        "prior work."
                                    ),
                                }

                    # Dedup: if any linked memory already has an active request, return it.
                    # 'failed' counts as active because the user/operator may want
                    # to edit and retry the failed request rather than silently
                    # spawning a duplicate. Only 'completed' and 'cancelled'
                    # (terminal by design) free a goal up for a fresh request.
                    existing = await conn.fetchrow(
                        """SELECT r.* FROM requests r
                           JOIN request_memories rm ON r.id = rm.request_id
                           WHERE r.organization_id = $1
                             AND rm.memory_id = ANY($2)
                             AND r.status NOT IN ('completed', 'cancelled')
                           ORDER BY r.created_at DESC LIMIT 1""",
                        UUID(org_id),
                        mem_ids,
                    )
                    if existing:
                        return dict(existing)

                    # Stale-progress guard: even if no active request exists,
                    # refuse to create a new cognitive request whose normalized
                    # title matches a recently completed (or cancelled)
                    # request linked to the same goal. This catches the case
                    # where a request finished but the goal's milestone state
                    # wasn't updated, so the planner re-proposes the same
                    # work next cycle.
                    #
                    # The previous guard compared `r.completed_at > m.updated_at`
                    # which was defeated by ANY touch to the goal memory
                    # (e.g. an unrelated progress note). Normalized-title
                    # matching is a much stronger signal because it answers
                    # the actual question: "did we just do this exact work?"
                    if source == "cognitive":
                        normalized = _normalize_title_for_dedup(title)
                        if normalized:
                            recent = await conn.fetchrow(
                                """SELECT r.id, r.title, r.status, r.completed_at
                                   FROM requests r
                                   JOIN request_memories rm ON rm.request_id = r.id
                                   WHERE r.organization_id = $1
                                     AND rm.memory_id = ANY($2)
                                     AND r.status IN ('completed', 'cancelled')
                                     AND COALESCE(r.completed_at, r.updated_at)
                                         > NOW() - make_interval(hours => $3)
                                     AND regexp_replace(
                                             lower(r.title),
                                             '[^a-z0-9]+', ' ', 'g'
                                         ) = $4
                                   ORDER BY COALESCE(r.completed_at, r.updated_at) DESC
                                   LIMIT 1""",
                                UUID(org_id),
                                mem_ids,
                                RECENT_COMPLETION_WINDOW_HOURS,
                                normalized,
                            )
                            if recent:
                                return {
                                    "id": str(recent["id"]),
                                    "title": title,
                                    "status": "skipped",
                                    "reason": "duplicate_of_recent_completion",
                                    "existing_request_id": str(recent["id"]),
                                    "existing_status": recent["status"],
                                    "detail": (
                                        f"A request with the same normalized "
                                        f"title was already "
                                        f"{recent['status']} for this goal at "
                                        f"{recent['completed_at']}. Refusing "
                                        "to create a duplicate. If the goal's "
                                        "milestone state hasn't been updated "
                                        "to reflect the completed work, update "
                                        "the goal first; otherwise re-think "
                                        "whether new work is actually needed."
                                    ),
                                }

            row = await conn.fetchrow(
                """INSERT INTO requests
                   (title, description, source, priority, created_by,
                    organization_id, dependency_policy,
                    approval_status, approved_at,
                    target_repo, target_paths)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
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
                target_repo,
                target_paths,
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
        block_duplicates: bool = True,
    ) -> dict | None:
        """Link a memory to a request.

        Returns one of:
            - the inserted link row dict on success
            - ``None`` if the request doesn't exist (or doesn't belong to org)
            - a dict with ``{"duplicate_of": <existing_request_id>, ...}`` when
              ``block_duplicates`` is True (default), ``relation == 'goal'``,
              and another active or recently-completed request is already
              linked to the same memory. The link is NOT inserted in this case.
            - ``None`` if the link already existed (insert was a no-op)

        The duplicate check is the structural backstop for the
        create-request-then-link-later pattern: callers (REST API,
        daemon's RequestAPI HTTP client, MCP tools called without
        ``goal_id``) all create requests without dedup running, then add
        memory links afterward. Without dedup here, those paths can produce
        duplicate goal-linked requests every cognitive cycle.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Serialize concurrent linkers for the same memory so two
                # parallel callers can't both pass the dedup check before
                # either commits.
                if block_duplicates and relation == "goal":
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock($1, hashtext($2)::int)",
                        MEMORY_LINK_LOCK_NAMESPACE,
                        str(memory_id),
                    )

                # Verify the request exists and belongs to the org.
                req_row = await conn.fetchrow(
                    "SELECT id, title, organization_id FROM requests "
                    "WHERE id = $1::uuid"
                    + (" AND organization_id = $2::uuid" if org_id else ""),
                    *((request_id, UUID(org_id)) if org_id else (request_id,)),
                )
                if not req_row:
                    return None
                req_org_id = req_row["organization_id"]

                if block_duplicates and relation == "goal":
                    # 1) Any OTHER active goal-linked request for this memory?
                    active_dup = await conn.fetchrow(
                        """SELECT r.id, r.title, r.status
                           FROM requests r
                           JOIN request_memories rm ON rm.request_id = r.id
                           WHERE rm.memory_id = $1::uuid
                             AND r.organization_id = $2
                             AND r.id != $3::uuid
                             AND rm.relation = 'goal'
                             AND r.status NOT IN ('completed', 'cancelled')
                           ORDER BY r.created_at
                           LIMIT 1""",
                        memory_id,
                        req_org_id,
                        request_id,
                    )
                    if active_dup:
                        return {
                            "duplicate_of": str(active_dup["id"]),
                            "existing_title": active_dup["title"],
                            "existing_status": active_dup["status"],
                            "reason": "active_request_for_goal",
                        }

                    # 2) Any RECENTLY completed/cancelled request for this
                    # memory whose normalized title matches the linking
                    # request's title? Mirrors the create_request guard so
                    # the late-link path is just as protected.
                    normalized = _normalize_title_for_dedup(req_row["title"])
                    if normalized:
                        recent_dup = await conn.fetchrow(
                            """SELECT r.id, r.title, r.status, r.completed_at
                               FROM requests r
                               JOIN request_memories rm ON rm.request_id = r.id
                               WHERE rm.memory_id = $1::uuid
                                 AND r.organization_id = $2
                                 AND r.id != $3::uuid
                                 AND rm.relation = 'goal'
                                 AND r.status IN ('completed', 'cancelled')
                                 AND COALESCE(r.completed_at, r.updated_at)
                                     > NOW() - make_interval(hours => $4)
                                 AND regexp_replace(
                                         lower(r.title),
                                         '[^a-z0-9]+', ' ', 'g'
                                     ) = $5
                               ORDER BY COALESCE(r.completed_at, r.updated_at) DESC
                               LIMIT 1""",
                            memory_id,
                            req_org_id,
                            request_id,
                            RECENT_COMPLETION_WINDOW_HOURS,
                            normalized,
                        )
                        if recent_dup:
                            return {
                                "duplicate_of": str(recent_dup["id"]),
                                "existing_title": recent_dup["title"],
                                "existing_status": recent_dup["status"],
                                "reason": "duplicate_of_recent_completion",
                            }

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

        # asyncpg returns jsonb columns as strings unless a codec is registered
        # at the connection level. Parse the columns the UI cares about so
        # templates can index into them as dicts/lists.
        import json as _json

        def _parse_jsonb(row: dict, *cols: str) -> None:
            for col in cols:
                val = row.get(col)
                if isinstance(val, str):
                    try:
                        row[col] = _json.loads(val)
                    except (ValueError, TypeError):
                        # Leave the original string if it isn't valid JSON.
                        pass

        for task in req["tasks"]:
            _parse_jsonb(
                task,
                "sandbox_config",
                "output_contract",
                "result_structured",
                "validation_errors",
            )

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
                event = dict(row)
                _parse_jsonb(event, "metadata")
                events_by_task.setdefault(tid, []).append(event)

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
                (json.dumps(sandbox_config) if isinstance(sandbox_config, dict)
                 else sandbox_config if isinstance(sandbox_config, str) and sandbox_config
                 else None),
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
        """Get all non-terminal requests with task status summaries.

        Returns requests that are pending/in_progress/planned/needs_rework/review
        AND requests in 'failed' state (because failed work is still active from
        the planner's perspective — it needs to be fixed/retried, not duplicated).
        Only 'completed' and 'cancelled' are excluded as truly terminal.
        """
        base = """FROM requests r LEFT JOIN tasks t ON t.request_id = r.id
                   WHERE r.organization_id = $1
                     AND r.status NOT IN ('completed', 'cancelled')"""
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
                     AND r.status NOT IN ('cancelled', 'completed', 'failed')
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
        self,
        task_id: str,
        instance_id: str,
        org_id: str | None = None,
        lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS,
    ) -> dict | None:
        """Atomically claim a pending task. Returns None if already claimed.

        Refuses to claim a task whose parent request has already reached a
        terminal state (cancelled, completed, failed). The dispatch query
        filters these out, but cancellation can land between dispatch
        selection and claim — we belt-and-suspender here so a stuck
        cancellation race doesn't run an LLM session for nothing.
        """
        now = datetime.now(timezone.utc)
        claim_expires_at = now + timedelta(seconds=lease_seconds)
        if org_id:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'claimed', claimed_by = $2,
                       claimed_at = $3, last_heartbeat_at = $3,
                       claim_expires_at = $4, claim_version = claim_version + 1,
                       updated_at = $3
                       WHERE id = $1 AND status IN ('pending', 'planned')
                       AND organization_id = $5
                       AND EXISTS (
                           SELECT 1 FROM requests r
                           WHERE r.id = tasks.request_id
                             AND r.status NOT IN ('cancelled', 'completed', 'failed')
                       )
                       RETURNING *""",
                    UUID(task_id),
                    instance_id,
                    now,
                    claim_expires_at,
                    UUID(org_id),
                )
        else:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'claimed', claimed_by = $2,
                       claimed_at = $3, last_heartbeat_at = $3,
                       claim_expires_at = $4, claim_version = claim_version + 1,
                       updated_at = $3
                       WHERE id = $1 AND status IN ('pending', 'planned')
                       AND EXISTS (
                           SELECT 1 FROM requests r
                           WHERE r.id = tasks.request_id
                             AND r.status NOT IN ('cancelled', 'completed', 'failed')
                       )
                       RETURNING *""",
                    UUID(task_id),
                    instance_id,
                    now,
                    claim_expires_at,
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

    # Statuses where the task is in flight or already finalized successfully.
    # Anything else (pending, planned, failed, cancelled, needs_review, etc.)
    # is safe to edit because the daemon won't be actively executing it.
    _NON_EDITABLE_TASK_STATUSES = ('claimed', 'running', 'completed')

    async def update_pending_task(
        self,
        task_id: str,
        org_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        model: str | None = None,
        agent_type: str | None = None,
        sandbox_template_id: str | None = None,
        clear_sandbox_template: bool = False,
    ) -> dict | None:
        """Edit a task in place. Returns the updated task or None.

        Succeeds for any task that is NOT currently being executed and has
        not already completed successfully. That means: pending, planned,
        failed, cancelled, needs_review, etc. are all editable. Tasks in
        ``claimed``, ``running``, or ``completed`` cannot be modified.
        Pass ``clear_sandbox_template=True`` to explicitly null out the
        sandbox template (distinguishes "no change" from "remove").
        """
        sets: list[str] = []
        params: list = []

        if title is not None:
            params.append(title)
            sets.append(f"title = ${len(params)}")
        if description is not None:
            params.append(description)
            sets.append(f"description = ${len(params)}")
        if model is not None:
            params.append(model)
            sets.append(f"model = ${len(params)}")
        if agent_type is not None:
            params.append(agent_type)
            sets.append(f"agent_type = ${len(params)}")
        if clear_sandbox_template:
            sets.append("sandbox_template_id = NULL")
        elif sandbox_template_id is not None:
            params.append(UUID(sandbox_template_id))
            sets.append(f"sandbox_template_id = ${len(params)}")

        if not sets:
            return await self.get_task(task_id, org_id=org_id)

        sets.append(f"updated_at = ${len(params) + 1}")
        params.append(datetime.now(timezone.utc))

        params.append(UUID(task_id))
        params.append(UUID(org_id))

        non_editable = ', '.join(f"'{s}'" for s in self._NON_EDITABLE_TASK_STATUSES)
        sql = (
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ${len(params) - 1} "
            f"  AND organization_id = ${len(params)} "
            f"  AND status NOT IN ({non_editable}) "
            "RETURNING *"
        )
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(sql, *params)
        if row:
            await self.add_task_event(
                task_id,
                "edited",
                f"Task edited: {', '.join(sorted(s.split(' = ')[0] for s in sets if 'updated_at' not in s))}",
            )
        return dict(row) if row else None

    async def start_task(
        self,
        task_id: str,
        org_id: str | None = None,
        instance_id: str | None = None,
    ) -> dict | None:
        """Mark a claimed task as running."""
        now = datetime.now(timezone.utc)
        if org_id:
            async with self.pool.acquire() as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'running', updated_at = $2
                           WHERE id = $1 AND status = 'claimed'
                           AND organization_id = $3
                           AND claimed_by = $4
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        UUID(org_id),
                        instance_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'running', updated_at = $2
                           WHERE id = $1 AND status = 'claimed'
                           AND organization_id = $3
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        UUID(org_id),
                    )
        else:
            async with self.pool.acquire() as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'running', updated_at = $2
                           WHERE id = $1 AND status = 'claimed'
                           AND claimed_by = $3
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        instance_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'running', updated_at = $2
                           WHERE id = $1 AND status = 'claimed'
                           RETURNING *""",
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
        instance_id: str | None = None,
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
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'completed', result = $2,
                           result_structured = $3,
                           result_summary = $4,
                           validation_status = $5,
                           validation_errors = $6,
                           error = NULL,
                           completed_at = $7, updated_at = $7
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND organization_id = $8
                           AND claimed_by = $9
                           RETURNING *""",
                        UUID(task_id),
                        result,
                        json.dumps(result_structured) if result_structured is not None else None,
                        result_summary,
                        validation_status,
                        json.dumps(validation_errors) if validation_errors is not None else None,
                        now,
                        UUID(org_id),
                        instance_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'completed', result = $2,
                           result_structured = $3,
                           result_summary = $4,
                           validation_status = $5,
                           validation_errors = $6,
                           error = NULL,
                           completed_at = $7, updated_at = $7
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND organization_id = $8
                           RETURNING *""",
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
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'completed', result = $2,
                           result_structured = $3,
                           result_summary = $4,
                           validation_status = $5,
                           validation_errors = $6,
                           error = NULL,
                           completed_at = $7, updated_at = $7
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND claimed_by = $8
                           RETURNING *""",
                        UUID(task_id),
                        result,
                        json.dumps(result_structured) if result_structured is not None else None,
                        result_summary,
                        validation_status,
                        json.dumps(validation_errors) if validation_errors is not None else None,
                        now,
                        instance_id,
                    )
                else:
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
        self,
        task_id: str,
        error: str,
        org_id: str | None = None,
        instance_id: str | None = None,
    ) -> dict | None:
        """Mark task as failed.

        Only tasks in 'claimed' or 'running' state can be failed
        (workflow-audit/phase-4: status transition guard).
        """
        now = datetime.now(timezone.utc)
        if org_id:
            async with self.pool.acquire() as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'failed', error = $2,
                           completed_at = $3, updated_at = $3
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND organization_id = $4
                           AND claimed_by = $5
                           RETURNING *""",
                        UUID(task_id),
                        error,
                        now,
                        UUID(org_id),
                        instance_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'failed', error = $2,
                           completed_at = $3, updated_at = $3
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND organization_id = $4
                           RETURNING *""",
                        UUID(task_id),
                        error,
                        now,
                        UUID(org_id),
                    )
        else:
            async with self.pool.acquire() as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'failed', error = $2,
                           completed_at = $3, updated_at = $3
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND claimed_by = $4
                           RETURNING *""",
                        UUID(task_id),
                        error,
                        now,
                        instance_id,
                    )
                else:
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

    async def release_task(
        self,
        task_id: str,
        org_id: str | None = None,
        instance_id: str | None = None,
    ) -> dict | None:
        """Release a claimed/running task back to pending (for retry/stale recovery)."""
        now = datetime.now(timezone.utc)
        if org_id:
            async with self.pool.acquire() as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                           claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                           updated_at = $2
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND organization_id = $3
                           AND claimed_by = $4
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        UUID(org_id),
                        instance_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                           claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                           updated_at = $2
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND organization_id = $3
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        UUID(org_id),
                    )
        else:
            async with self.pool.acquire() as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                           claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                           updated_at = $2
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND claimed_by = $3
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        instance_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                           claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                           updated_at = $2
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           RETURNING *""",
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
                       claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                       completed_at = NULL, result = NULL,
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
                       claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                       completed_at = NULL, result = NULL,
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
        self,
        stale_minutes: int = 30,
        org_id: str | None = None,
        instance_stale_seconds: int = DEFAULT_INSTANCE_STALE_SECONDS,
    ) -> int:
        """Release tasks from stale instances or expired leases."""
        stale_seconds = max(60, int(stale_minutes * 60))
        if org_id:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """WITH stale AS (
                           SELECT t.id, t.claimed_by
                           FROM tasks t
                           WHERE t.status IN ('claimed', 'running')
                             AND t.organization_id = $2
                             AND (
                                 t.claim_expires_at < NOW()
                                 OR (
                                     t.claimed_by IS NOT NULL
                                     AND EXISTS (
                                         SELECT 1 FROM daemon_instances di
                                         WHERE di.organization_id = t.organization_id
                                           AND di.instance_id = t.claimed_by
                                            AND (
                                                di.status <> 'active'
                                                OR di.last_seen_at < NOW()
                                                    - make_interval(secs := $3)
                                            )
                                     )
                                 )
                                 OR (
                                     t.claimed_at IS NOT NULL
                                     AND t.claimed_at < NOW() - make_interval(secs := $1)
                                 )
                             )
                           FOR UPDATE
                       ),
                       released AS (
                           UPDATE tasks t
                           SET status = 'pending',
                               claimed_by = NULL,
                               claimed_at = NULL,
                               claim_expires_at = NULL,
                               last_heartbeat_at = NULL,
                               updated_at = NOW()
                           FROM stale s
                           WHERE t.id = s.id
                           RETURNING t.id, s.claimed_by AS previous_claimed_by
                       )
                       SELECT id, previous_claimed_by FROM released""",
                    stale_seconds,
                    UUID(org_id),
                    instance_stale_seconds,
                )
        else:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """WITH stale AS (
                           SELECT t.id, t.claimed_by
                           FROM tasks t
                           WHERE t.status IN ('claimed', 'running')
                             AND (
                                 t.claim_expires_at < NOW()
                                 OR (
                                     t.claimed_by IS NOT NULL
                                     AND EXISTS (
                                         SELECT 1 FROM daemon_instances di
                                         WHERE di.organization_id = t.organization_id
                                           AND di.instance_id = t.claimed_by
                                            AND (
                                                di.status <> 'active'
                                                OR di.last_seen_at < NOW()
                                                    - make_interval(secs := $2)
                                            )
                                     )
                                 )
                                 OR (
                                     t.claimed_at IS NOT NULL
                                     AND t.claimed_at < NOW() - make_interval(secs := $1)
                                 )
                             )
                           FOR UPDATE
                       ),
                       released AS (
                           UPDATE tasks t
                           SET status = 'pending',
                               claimed_by = NULL,
                               claimed_at = NULL,
                               claim_expires_at = NULL,
                               last_heartbeat_at = NULL,
                               updated_at = NOW()
                           FROM stale s
                           WHERE t.id = s.id
                           RETURNING t.id, s.claimed_by AS previous_claimed_by
                       )
                       SELECT id, previous_claimed_by FROM released""",
                    stale_seconds,
                    instance_stale_seconds,
                )
        for row in rows:
            claimed_by = row["previous_claimed_by"] or "unknown"
            await self.add_task_event(
                str(row["id"]),
                "reaper",
                f"Claim expired (was claimed by {claimed_by}), task requeued to pending",
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

        Excludes request-review meta-tasks from the completion check. We
        identify them by EITHER agent_type='request-review' OR the canonical
        title 'Post-completion review' — the daemon's review-agent fallback
        path may create review tasks under a different agent_type (e.g.
        'code') when the dedicated review agent isn't accessible. Without
        the title-based exclusion those fallback reviews would count as
        work tasks and re-trigger another review task on completion,
        producing an infinite loop.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT
                     COUNT(*) as total,
                     COUNT(*) FILTER (WHERE status IN ('completed', 'failed', 'cancelled')) as done
                   FROM tasks
                   WHERE request_id = $1
                     AND agent_type IS DISTINCT FROM 'request-review'
                     AND title IS DISTINCT FROM 'Post-completion review'""",
                UUID(request_id),
            )
        if row and row["total"] > 0 and row["total"] == row["done"]:
            # Check if any failed
            async with self.pool.acquire() as conn:
                failed = await conn.fetchval(
                    """SELECT COUNT(*) FROM tasks
                       WHERE request_id = $1 AND status = 'failed'
                         AND agent_type IS DISTINCT FROM 'request-review'
                         AND title IS DISTINCT FROM 'Post-completion review'""",
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
