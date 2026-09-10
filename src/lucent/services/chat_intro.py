"""Grounded work-state service for the LLM-generated chat intro summary.

Gathers real tracked-work data (active requests, task statuses, recently
completed work, goal milestones) via DB-only reads and renders a bounded
prompt context for the summarizer. This module never calls an LLM — it only
produces the grounding payload, the fingerprint used for cache/refresh
decisions, and the small structured snapshot cached alongside the summary.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from lucent.logging import get_logger

logger = get_logger("chat.intro")

# Hard context budget: the summarizer prompt is clipped to these bounds so a
# busy workspace cannot balloon the payload (token discipline).
MAX_ACTIVE_REQUESTS = 5
MAX_RECENT_COMPLETED = 5
MAX_GOALS = 4
MAX_FIELD_CHARS = 120

# A workspace is "quiet" when nothing is in flight and nothing completed
# recently — the summary should degrade to the default intro instead of
# rendering an empty "all caught up" hero.
QUIET_REQUEST_WINDOW_DAYS = 3

CURRENT_REQUEST_STATUSES = ("pending", "planned", "in_progress", "review", "needs_rework")


def _clean(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    """Collapse whitespace in a DB string and clip to the field budget."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _request_age_days(value: Any) -> float | None:
    stamp = _as_datetime(value)
    if stamp is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 86400)


def _request_line(req: dict[str, Any]) -> str:
    title = _clean(req.get("title"))
    status = _clean(req.get("status") or "unknown")
    approval = _clean(req.get("approval_status") or "")
    approval_part = f", approval={approval}" if approval else ""
    tasks = _clean(
        f"{req.get('tasks_completed', 0)}/{req.get('task_count', 0)} tasks done",
        limit=40,
    )
    running = int(req.get("tasks_running") or 0)
    if running:
        tasks += f", {running} running"
    return f"- {title or 'Untitled request'} [id={req.get('id')}] (status={status}{approval_part}, {tasks})"


def _goal_line(goal: dict[str, Any]) -> str:
    title = _clean(goal.get("title"))
    total = goal.get("milestone_total") or 0
    completed = goal.get("milestone_completed") or 0
    current = goal.get("current_milestone_label")
    parts = [
        f"- {title or 'Untitled goal'} [id={goal.get('id')}]",
        f"milestones {completed}/{total} completed",
    ]
    if current:
        parts.append(f"current: {_clean(current, limit=80)}")
    current_req = goal.get("current_request") or {}
    if current_req.get("title"):
        parts.append(f"current request: {_clean(current_req.get('title'), limit=80)}")
    return f"{parts[0]} ({'; '.join(parts[1:])})"


def _fingerprint_component(label: str, *values: Any) -> str:
    return f"{label}=" + ",".join(str(v) for v in values)


def _goal_rollup_sql(memory_access: str) -> str:
    """Goal-milestone rollup SQL composed with the requester's access condition.

    MemoryRepository's condition builders hard-code the bare ``memories``
    table name, so this query must not alias the table. Parameter layout is
    the repo's composition convention (web/routes/memories.py): $1 = user_id,
    $2 = organization_id, $3 = limit.
    """
    return f"""SELECT memories.id,
                      COALESCE(memories.content, '') AS title,
                      COALESCE(memories.metadata->>'status', 'active') AS status,
                      CASE
                          WHEN jsonb_typeof(memories.metadata) = 'object'
                               AND jsonb_typeof(memories.metadata->'milestones') = 'array'
                          THEN memories.metadata->'milestones'
                          ELSE '[]'::jsonb
                      END AS milestones
               FROM memories
               WHERE memories.type = 'goal'
                 AND memories.deleted_at IS NULL
                 AND memories.organization_id = $2::uuid
                 AND {memory_access}
               ORDER BY COALESCE(memories.updated_at, memories.created_at) DESC
               LIMIT $3"""


async def gather_work_context(user) -> dict[str, Any]:
    """Gather grounded tracked-work data for the intro summary.

    DB-only reads (no LLM calls). Returns None when the workspace is quiet —
    nothing in flight and nothing completed within the recency window — so the
    caller can fall back to the default intro instead of an empty summary.
    """
    from lucent.db import get_pool
    from lucent.db.requests import RequestRepository

    pool = await get_pool()
    org_id = str(user["organization_id"])
    user_id = str(user["id"])
    role = user.get("role", "member")
    if hasattr(role, "value"):
        role = role.value
    is_admin_or_owner = role in {"admin", "owner"}
    visibility_kwargs = {
        "requester_user_id": user_id,
        "include_system": is_admin_or_owner,
    }

    repo = RequestRepository(pool)
    active_result = await repo.list_requests(
        org_id,
        status=",".join(CURRENT_REQUEST_STATUSES),
        limit=MAX_ACTIVE_REQUESTS * 2,
        **visibility_kwargs,
    )
    active_requests = [
        req for req in active_result["items"] if req.get("approval_status") != "pending_approval"
    ][:MAX_ACTIVE_REQUESTS]

    completed_result = await repo.list_requests(
        org_id,
        status="completed",
        limit=MAX_RECENT_COMPLETED * 2,
        **visibility_kwargs,
    )
    recent_completed = [
        req
        for req in completed_result["items"]
        if (_age := _request_age_days(req.get("completed_at") or req.get("updated_at")))
        is not None
        and _age <= QUIET_REQUEST_WINDOW_DAYS
    ][:MAX_RECENT_COMPLETED]

    # Lightweight goal-milestone rollup via SQL (JSONB path), bounded to
    # recently-updated goals. DB-only; no LLM calls to gather context.
    # Scoped to the requesting user's memory boundary (own + org-granted +
    # daemon-owner-visible) — NOT org-wide, or one member's private goals
    # would ground another member's summary.
    from lucent.db.memory import MemoryRepository

    memory_access = MemoryRepository.user_memory_access_condition("$2", "$1")
    async with pool.acquire() as conn:
        goal_rows = await conn.fetch(
            _goal_rollup_sql(memory_access),
            UUID(user_id),
            UUID(org_id),
            MAX_GOALS * 4,
        )

    goals: list[dict[str, Any]] = []
    for row in goal_rows:
        status = str(row["status"] or "active").lower()
        if status not in {"active", ""}:
            continue
        milestones = row["milestones"]
        total = len(milestones) if isinstance(milestones, list) else 0
        completed = 0
        current_label = None
        for milestone in milestones if isinstance(milestones, list) else []:
            if not isinstance(milestone, dict):
                continue
            milestone_status = str(milestone.get("status") or "active").lower()
            if milestone_status in {"completed", "done"}:
                completed += 1
            elif milestone_status not in {"abandoned", "cancelled", "skipped"} and current_label is None:
                current_label = next(
                    (
                        milestone[key]
                        for key in ("description", "title", "name")
                        if isinstance(milestone.get(key), str) and milestone[key].strip()
                    ),
                    None,
                )
        title = _clean(next(
            (line for line in str(row["title"]).splitlines() if line.strip()),
            "Untitled goal",
        ), limit=80)
        goals.append(
            {
                "id": str(row["id"]),
                "title": title,
                "milestone_total": total,
                "milestone_completed": completed,
                "current_milestone_label": current_label,
            }
        )
        if len(goals) >= MAX_GOALS:
            break

    pending_count = sum(
        1
        for req in active_result["items"]
        if req.get("approval_status") == "pending_approval"
        and req.get("status") not in {"cancelled", "rejection_processing"}
    )

    is_quiet = not active_requests and not recent_completed
    snapshot = {
        "active_requests": [
            {
                "id": str(req.get("id")),
                "title": _clean(req.get("title")),
                "status": _clean(req.get("status")),
                "approval_status": _clean(req.get("approval_status")),
                "task_count": int(req.get("task_count") or 0),
                "tasks_completed": int(req.get("tasks_completed") or 0),
                "tasks_running": int(req.get("tasks_running") or 0),
                "tasks_failed": int(req.get("tasks_failed") or 0),
            }
            for req in active_requests
        ],
        "recently_completed": [
            {
                "id": str(req.get("id")),
                "title": _clean(req.get("title")),
                "completed_at": str(req.get("completed_at") or req.get("updated_at") or ""),
            }
            for req in recent_completed
        ],
        "goals": goals,
        "pending_approval_count": pending_count,
    }
    fingerprint = compute_fingerprint(snapshot)

    context_lines: list[str] = [
        "## Active requests (real tracked-work data)",
    ]
    context_lines.extend(_request_line(req) for req in active_requests)
    if pending_count:
        context_lines.append(f"- (+{pending_count} more awaiting approval, not shown)")
    context_lines.extend(["", "## Recently completed work (last 3 days)"])
    context_lines.extend(_request_line(req) for req in recent_completed)
    context_lines.extend(["", "## Goal milestones"])
    context_lines.extend(_goal_line(goal) for goal in goals)

    return {
        "is_quiet": is_quiet,
        "fingerprint": fingerprint,
        "context_text": "\n".join(context_lines),
        "snapshot": snapshot,
    }


def compute_fingerprint(snapshot: dict[str, Any]) -> str:
    """Stable content hash of the grounded data for cache/refresh decisions.

    Captures the work-state shape the summary is grounded in: request ids,
    statuses, task counters, completion recency, and goal milestone states.
    """
    parts = [
        _fingerprint_component(
            "active",
            *[
                (
                    req["id"],
                    req["status"],
                    req["approval_status"],
                    req["task_count"],
                    req["tasks_completed"],
                    req["tasks_running"],
                    req["tasks_failed"],
                )
                for req in snapshot.get("active_requests", [])
            ],
        ),
        _fingerprint_component(
            "completed",
            *[(req["id"], str(req.get("completed_at") or "")) for req in snapshot.get("recently_completed", [])],
        ),
        _fingerprint_component(
            "goals",
            *[
                (
                    goal["id"],
                    goal["milestone_total"],
                    goal["milestone_completed"],
                    goal.get("current_milestone_label") or "",
                )
                for goal in snapshot.get("goals", [])
            ],
        ),
        _fingerprint_component("pending_approvals", snapshot.get("pending_approval_count", 0)),
    ]
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]