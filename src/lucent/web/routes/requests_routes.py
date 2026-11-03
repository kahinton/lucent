"""Request tracking and activity routes."""

import logging
from datetime import datetime, timezone
from hashlib import sha256
from math import ceil

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from lucent.auth_providers import CSRF_COOKIE_NAME
from lucent.db import get_pool
from lucent.rbac import Role, is_admin_role

from ._shared import _check_csrf, get_user_context, templates

logger = logging.getLogger(__name__)

router = APIRouter()

ALLOWED_PER_PAGE = {10, 25, 50, 100}


def request_visibility_context(user) -> tuple[str, bool]:
    """Return requester identity and whether daemon system work is visible."""
    role = user.role if isinstance(user.role, Role) else Role.from_string(str(user.role))
    return str(user.id), role in (Role.ADMIN, Role.OWNER, Role.DAEMON)


def can_review_request(user, req: dict) -> bool:
    """Allow owners of a request or privileged system actors to review it."""
    role = user.role if isinstance(user.role, Role) else Role.from_string(str(user.role))
    return str(req.get("created_by")) == str(user.id) or (
        role >= Role.ADMIN
        or role == Role.DAEMON
        or getattr(user, "is_daemon_service", False)
    )


def _request_live_revision(req: dict, reviews: list[dict]) -> str:
    """Return a revision token for request-detail data rendered in live updates."""
    parts = [
        str(req.get("id", "")),
        str(req.get("status", "")),
        str(req.get("approval_status", "")),
        str(req.get("updated_at", "")),
    ]
    for task in req.get("tasks", []):
        parts.extend(
            (
                str(task.get("id", "")),
                str(task.get("status", "")),
                str(task.get("updated_at", "")),
            )
        )
        for event in task.get("events", []):
            parts.extend((str(event.get("id", "")), str(event.get("created_at", ""))))
    for review in reviews:
        parts.extend(
            (
                str(review.get("id", "")),
                str(review.get("status", "")),
                str(review.get("updated_at", "")),
            )
        )
    return sha256("\x1f".join(parts).encode()).hexdigest()[:16]


async def _get_mutable_task_request(repo, task_id: str, user) -> tuple[dict, dict]:
    """Load a task and require visibility plus mutation rights on its parent request."""
    org_id = str(user.organization_id)
    task = await repo.get_task(task_id, org_id=org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    requester_user_id, include_system = request_visibility_context(user)
    req = await repo.get_request(
        str(task["request_id"]),
        org_id,
        requester_user_id=requester_user_id,
        include_system=include_system,
    )
    if not req or not can_review_request(user, req):
        raise HTTPException(404, "Task not found")
    return task, req


async def _notify_request_ready(pool, *, request_id: str, org_id: str, action: str) -> None:
    """Best-effort wake notification for daemon request/review state changes."""
    try:
        from lucent.db.requests import RequestRepository

        await RequestRepository(pool).notify_ready(
            org_id=org_id,
            payload=f'{{"type": "approval", "action": "{action}", "request_id": "{request_id}"}}',
        )
    except Exception:
        pass


async def _record_rejection_lesson(pool, *, user, req: dict, request_id: str, comment: str) -> None:
    """Capture rejection feedback as a learning memory for future planning."""
    from lucent.db import MemoryRepository

    memo_repo = MemoryRepository(pool)
    await memo_repo.create(
        username=user.display_name or user.email or "reviewer",
        type="experience",
        content=(
            f"Request rejected before work began: '{req.get('title', '')}'\n"
            f"Reason: {comment}\n"
            f"Description: {req.get('description', 'N/A')}"
        ),
        tags=[
            "rejection-lesson",
            "approval-rejected",
            "feedback-rejected",
            "learning-extraction",
        ],
        metadata={
            "request_id": request_id,
            "reviewer": user.display_name or user.email,
            "source": req.get("source", "unknown"),
        },
        user_id=user.id,
        organization_id=user.organization_id,
       )


def _needs_prework_approval(req: dict) -> bool:
    """Return whether a request is waiting at the pre-work approval gate."""
    return (
        req.get("approval_status") == "pending_approval"
        and req.get("status") not in ("cancelled", "rejection_processing")
    )


# Agent types Lucent uses for its own bookkeeping rather than work the user
# asked for. The detail page keeps them out of the main step list — a human
# reading "what happened to my request" should not have to parse
# "Post-completion review" #10000001 — and offers them under Details instead.
INTERNAL_AGENT_TYPES = frozenset({"request-review", "decision-step"})

# Plain-language progress for the "what's happening right now" line.
#
# Task events are developer-facing ("composition_surface", "mcp_tool_usage"),
# which is fine in a technical log and useless to anyone else. Only the events
# that genuinely describe a phase change are translated; everything else is
# skipped so the line stays a description of the current state rather than a
# replay of the machine's internal steps.
_ACTIVITY_LABELS: dict[str, str] = {
    "agent_dispatched": "Lucent is working on this",
    "claimed": "Lucent picked this up",
    "sandbox_created": "Setting up a fresh workspace",
    "sandbox_reused": "Picking up its workspace again",
    "sandbox_output_processed": "Collecting the results",
    "tool_requirement_repair": "Double-checking its work",
    "session_interrupted": "Resuming after an interruption",
    "session_requeued": "Resuming after an interruption",
    "needs_review": "Waiting for a quick check",
    "completed": "Finished",
    "failed": "Something went wrong",
    "cancelled": "Cancelled",
    "cancelled_by_user": "Cancelled",
    "review_approved": "Approved",
    "review_rejected": "Sent back for changes",
}


def _task_activity_line(task: dict) -> str:
    """Return a one-line, non-technical description of what a task is doing."""
    status = task.get("status")
    if status in ("completed", "failed", "cancelled", "needs_review"):
        return _ACTIVITY_LABELS.get(status, "")

    for event in reversed(task.get("events") or []):
        label = _ACTIVITY_LABELS.get(str(event.get("event_type") or ""))
        if label:
            return label

    if status == "claimed":
        return "Starting up"
    if status == "running":
        return "Working on it"
    return "Waiting for its turn"


# Fusion tasks do not run on one model, so `tasks.model` (the author's pick,
# which becomes the first candidate) is a half-truth on the detail page. These
# build the panel's view of what actually ran: the mode, every candidate that
# produced output, the arbiter that scored them, and any extra synthesised or
# refined session. The event log is the source of truth because it survives a
# failed dispatch — `result_structured` only exists when the task completed.
_FUSION_MODE_LABELS: dict[str, str] = {
    "best_of_n": "Best of N",
    "synthesize": "Synthesis",
}

_FUSION_FALLBACK_LABELS: dict[str, str] = {
    "fusion_disabled": "Fusion was off when this ran, so one model answered",
    "fusion_unsupported": "This step cannot use fusion, so one model answered",
}


def _fusion_is_configured(task: dict) -> bool:
    """Whether this task was authored as a fusion task."""
    if str(task.get("task_kind") or "") == "fusion":
        return True
    return _as_dict(task.get("fusion_config")) is not None


def _as_dict(value) -> dict | None:
    """Coerce a jsonb column to a dict, tolerating a JSON string.

    The connection pool registers a ``jsonb`` codec whose encoder is
    ``json.dumps``, while several write paths also serialise the value before
    handing it to asyncpg. The result is a jsonb *string* wrapping the object.
    Consumers across the codebase already ``json.loads`` defensively; this is
    the same guard so the detail page reads either shape.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        import json as _json

        try:
            decoded = _json.loads(value)
        except (TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _fusion_view(task: dict) -> dict | None:
    """Describe how a fusion task was configured and how it actually ran.

    Returns ``None`` for non-fusion tasks so the template can fall back to the
    plain single-model row.
    """
    if not _fusion_is_configured(task):
        return None

    config = _as_dict(task.get("fusion_config")) or _as_dict(
        task.get("execution_config")
    ) or {}

    mode = str(config.get("mode") or "")
    candidates_cfg = _as_dict(config.get("candidates")) or {}
    arbiter_cfg = _as_dict(config.get("arbiter")) or {}
    synthesis_cfg = _as_dict(config.get("synthesis")) or {}
    refine_cfg = _as_dict(config.get("refine")) or {}

    view: dict = {
        "configured": bool(config),
        "mode": mode or None,
        "mode_label": _FUSION_MODE_LABELS.get(mode, "Fusion"),
        "configured_models": [
            str(model) for model in (candidates_cfg.get("models") or []) if model
        ],
        "configured_count": candidates_cfg.get("count"),
        "arbiter_model": arbiter_cfg.get("model") or None,
        "arbiter_question_count": len(arbiter_cfg.get("questions") or []),
        "synthesis_model": synthesis_cfg.get("model") or None,
        "refine_model": refine_cfg.get("model") or None,
        "ran": False,
        "fallback": None,
        "fallback_label": None,
        "winner_model": None,
        "winner_index": None,
        "candidates": [],
        "candidate_count": 0,
        "degraded": False,
        "threshold_failed": False,
        "refine_taken": None,
        "total_tokens": None,
    }

    # A task can be retried or reworked many times, and each attempt records
    # its own events. Only the most recent attempt describes what the step
    # actually did, so any run-marker event resets the picture: a task whose
    # first attempt fell back to one model and whose last attempt ran fusion
    # must not show "fusion was off" next to a scored candidate table.
    last_completed: dict | None = None
    for event in task.get("events") or []:
        event_type = str(event.get("event_type") or "")
        metadata = _as_dict(event.get("metadata")) or {}

        if event_type == "fusion_dispatched":
            view.update(
                ran=True,
                fallback=None,
                fallback_label=None,
                winner_model=None,
                winner_index=None,
                candidates=[],
                candidate_count=0,
                degraded=False,
                threshold_failed=False,
            )
            last_completed = None
        elif event_type in _FUSION_FALLBACK_LABELS:
            view.update(
                ran=False,
                fallback=event_type,
                fallback_label=_FUSION_FALLBACK_LABELS[event_type],
                winner_model=None,
                winner_index=None,
                candidates=[],
                candidate_count=0,
                degraded=False,
                threshold_failed=False,
            )
            last_completed = None
        elif event_type == "fusion_completed":
            view["ran"] = True
            last_completed = metadata

    if last_completed:
        winner = _as_dict(last_completed.get("winner")) or {}
        view["winner_model"] = winner.get("model")
        view["winner_index"] = winner.get("index")
        view["degraded"] = bool(last_completed.get("degraded"))
        view["threshold_failed"] = bool(last_completed.get("threshold_failed"))

        arbiter = _as_dict(last_completed.get("arbiter")) or {}
        if arbiter.get("model"):
            view["arbiter_model"] = arbiter.get("model")

        totals = _as_dict(last_completed.get("totals")) or {}
        if totals:
            view["total_tokens"] = totals.get("total_tokens")

        refine = _as_dict(last_completed.get("refine")) or {}
        if refine.get("passes"):
            view["refine_taken"] = bool(refine.get("taken"))

        # `all_candidates` lists every attempt (including failures and the
        # derived synth/refine session); `candidates` is the ranked list and
        # carries the scores. Key by index, because a refine/synthesis run
        # reuses the same model id as a candidate.
        attempts: dict = {}
        for row in last_completed.get("all_candidates") or []:
            row = _as_dict(row)
            if not row or row.get("index") is None:
                continue
            attempts[row["index"]] = {
                "index": row["index"],
                "model": row.get("model"),
                "status": row.get("status"),
                "aggregate": None,
                "scores": {},
            }
        for row in last_completed.get("candidates") or []:
            row = _as_dict(row)
            if not row or row.get("index") is None:
                continue
            entry = attempts.setdefault(
                row["index"],
                {
                    "index": row["index"],
                    "model": row.get("model"),
                    "status": row.get("status"),
                },
            )
            entry["model"] = row.get("model") or entry.get("model")
            entry["status"] = row.get("status") or entry.get("status")
            entry["aggregate"] = row.get("aggregate")
            entry["scores"] = _as_dict(row.get("scores")) or {}

        candidates: list[dict] = []
        for index in sorted(attempts):
            entry = attempts[index]
            # Authored candidates run as 1..N; the executor inserts any derived
            # session at index 0, so a zero index identifies it.
            if index == 0:
                role = "synthesis" if mode == "synthesize" else "refine"
            else:
                role = "candidate"
            candidates.append(
                {
                    **entry,
                    "role": role,
                    "role_label": (
                        "synthesized"
                        if role == "synthesis"
                        else "refined"
                        if role == "refine"
                        else None
                    ),
                    # Match by index: a refine pass reuses a candidate's model,
                    # so comparing model ids would mark two rows as winners.
                    "winner": (
                        view["winner_index"] is not None
                        and index == view["winner_index"]
                    ),
                }
            )
        view["candidates"] = candidates
        view["candidate_count"] = sum(1 for c in candidates if c["role"] == "candidate")

    # When nothing ran yet (a queued task), fall back to the authored models so
    # the panel still says what was meant to run rather than showing an empty
    # list. Deliberately not done after a fallback: that attempt genuinely ran
    # one model, so listing the others as "not run" would imply they were
    # skipped rather than never configured to run.
    if not view["candidates"] and view["configured_models"] and not view["fallback"]:
        view["candidates"] = [
            {
                "index": position,
                "model": model,
                "status": "not run",
                "aggregate": None,
                "scores": {},
                "role": "candidate",
                "role_label": None,
                "winner": False,
            }
            for position, model in enumerate(view["configured_models"], start=1)
        ]
        view["candidate_count"] = len(view["candidates"])

    # A fusion-kind task always keeps its panel: even when the config cannot be
    # read, "this did not run on one model" is the useful fact.
    is_fusion_kind = str(task.get("task_kind") or "") == "fusion"
    return view if (is_fusion_kind or view["configured"] or view["ran"] or view["fallback"]) else None


# =============================================================================
# Request Tracking
# =============================================================================


@router.get("/activity", response_class=HTMLResponse)
async def activity_list(
    request: Request,
    status: str | None = None,
    source: str | None = None,
    q: str | None = None,
    page: int = 1,
    per_page: int = 25,
):
    """Unified activity page — all requests from users and the daemon."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)

    # Sanitize pagination params
    page = max(1, page)
    per_page = per_page if per_page in ALLOWED_PER_PAGE else 25
    offset = (page - 1) * per_page

    search_query = (q or "").strip() or None

    # Default: hide system (schedule) tasks unless explicitly requested
    if source is None:
        source = "user,cognitive,daemon"

    # Keep the summary card and its drill-down aligned. "Active" is a UI
    # grouping rather than a persisted request status.
    status_filter = "in_progress,review,needs_rework" if status == "active" else status

    org_id = str(user.organization_id)
    requester_user_id, include_system = request_visibility_context(user)
    # Hide cancelled requests by default unless explicitly filtered
    exclude_status = None if status else "cancelled"
    requests_result = await repo.list_requests(
        org_id, status=status_filter, source=source, limit=per_page, offset=offset,
        exclude_status=exclude_status, viewer_user_id=str(user.id),
        requester_user_id=requester_user_id, include_system=include_system,
        search=search_query,
    )
    requests_data = requests_result["items"]
    total_count = requests_result["total_count"]
    total_pages = ceil(total_count / per_page) if total_count > 0 else 1
    page = min(page, total_pages)

    summary = await repo.get_active_summary(
        org_id,
        requester_user_id=requester_user_id,
        include_system=include_system,
    )

    approval_requests = [req for req in requests_data if _needs_prework_approval(req)]
    running_requests = [
        req for req in requests_data
        if not _needs_prework_approval(req) and req.get("tasks_running", 0) > 0
    ]
    other_requests = [
        req for req in requests_data
        if not _needs_prework_approval(req) and req.get("tasks_running", 0) <= 0
    ]

    pending_approval_count = await repo.count_pending_approvals(
        org_id,
        requester_user_id=requester_user_id,
        include_system=include_system,
    )

    # Name the job behind each card. `source` alone renders as "Lucent" for
    # every background request, which tells a reader nothing about which
    # workflow produced it. Best-effort: an unresolved card falls back to the
    # plain source badge.
    request_origins: dict[str, dict] = {}
    try:
        from lucent.services.request_origin import RequestOriginService

        request_origins = await RequestOriginService(pool).resolve_many(
            [str(req["id"]) for req in requests_data],
            org_id=org_id,
        )
    except Exception:
        logger.exception("Request origins unavailable for the activity list")

    template_ctx = {
        "user": user,
        "requests": other_requests,
        "approval_requests": approval_requests,
        "running_requests": running_requests,
        "request_origins": request_origins,
        "displayed_request_count": len(requests_data),
        "summary": summary,
        "filter_status": status,
        "filter_source": source,
        "search_query": search_query,
        "pending_approval_count": pending_approval_count,
        "can_review_request": lambda req: can_review_request(user, req),
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_count": total_count,
    }

    # Pagination requests need only the list. Live refreshes select their
    # marked region from the complete page so summary counts update too.
    if request.headers.get("HX-Request") and not request.headers.get("X-Live-Refresh"):
        return templates.TemplateResponse(
            request,
            "partials/activity_list.html",
            template_ctx,
        )

    return templates.TemplateResponse(
        request,
        "requests_list.html",
        template_ctx,
    )


@router.get("/activity/queue", response_class=HTMLResponse)
async def queued_tasks_list(request: Request, page: int = 1, per_page: int = 25):
    """Show the full pending/planned task backlog visible to the user."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    page = max(1, page)
    per_page = per_page if per_page in ALLOWED_PER_PAGE else 25
    repo = RequestRepository(pool)
    requester_user_id, include_system = request_visibility_context(user)
    result = await repo.list_queued_tasks(
        str(user.organization_id),
        limit=per_page,
        offset=(page - 1) * per_page,
        requester_user_id=requester_user_id,
        include_system=include_system,
    )
    ready = await repo.list_pending_tasks(
        str(user.organization_id),
        limit=1,
        requester_user_id=requester_user_id,
        include_system=include_system,
    )
    total_pages = ceil(result["total_count"] / per_page) if result["total_count"] else 1
    return templates.TemplateResponse(
        request,
        "queued_tasks.html",
        {
            "user": user,
            "tasks": result["items"],
            "total_count": result["total_count"],
            "ready_count": ready["total_count"],
            "page": min(page, total_pages),
            "per_page": per_page,
            "total_pages": total_pages,
        },
    )


@router.get("/requests", response_class=HTMLResponse)
async def requests_redirect(request: Request):
    """Redirect old /requests URL to /activity."""
    qs = str(request.url.query)
    url = "/activity" + ("?" + qs if qs else "")
    return RedirectResponse(url=url, status_code=301)


@router.get("/activity/{request_id}/live")
async def request_live_output(request: Request, request_id: str):
    """Return in-flight model output for a request's running tasks.

    Polled by the detail page while anything is running. Reads only the
    transient scratch table (see migration 148) so it stays a cheap indexed
    lookup that does not touch the task tree, events, or memory links.

    Deliberately returns text and nothing else: the page re-fetches full state
    through the normal live-refresh path, and this endpoint exists purely to
    make progress visible between those refreshes.
    """
    user = await get_user_context(request)
    pool = await get_pool()
    org_id = str(user.organization_id)
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    requester_user_id, include_system = request_visibility_context(user)
    # Reuse the detail page's visibility rule so this endpoint can never leak
    # output for a request the user cannot open.
    req = await repo.get_request(
        request_id,
        org_id,
        requester_user_id=requester_user_id,
        include_system=include_system,
    )
    if not req:
        raise HTTPException(404, "Request not found")

    outputs = await repo.get_live_task_output(request_id, org_id)
    return JSONResponse(
        {
            "request_status": req.get("status"),
            "tasks": {
                task_id: {
                    "content": payload["content"],
                    "updated_at": (
                        payload["updated_at"].isoformat()
                        if payload.get("updated_at")
                        else None
                    ),
                }
                for task_id, payload in outputs.items()
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/activity/{request_id}", response_class=HTMLResponse)
@router.get("/requests/{request_id}", response_class=HTMLResponse)
async def request_detail(request: Request, request_id: str):
    """Full request detail with task tree, events, memory links, and reviews."""
    user = await get_user_context(request)
    pool = await get_pool()
    org_id = str(user.organization_id)
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)

    requester_user_id, include_system = request_visibility_context(user)
    req = await repo.get_request_with_tasks(
        request_id,
        str(user.organization_id),
        requester_user_id=requester_user_id,
        include_system=include_system,
    )
    if not req:
        raise HTTPException(404, "Request not found")

    await repo.mark_request_viewed(request_id, str(user.organization_id), str(user.id))

    # Where this request actually came from. `source` only says "a person or
    # the machine"; the workflow/chat/background-job it belongs to lives in
    # schedule_runs, requests.origin_session_id and the tool audit log. Resolve
    # that lineage into one readable origin for the page's header.
    request_origin: dict | None = None
    try:
        from lucent.services.request_origin import RequestOriginService

        request_origin = await RequestOriginService(pool).resolve(
            request_id, org_id=org_id
        )
    except Exception:
        logger.exception("Request origin unavailable for %s", request_id)

    from lucent.db.memory import MemoryRepository
    from lucent.integrations.github_repo_access_service import GitHubRepoAccessService
    from lucent.services.memory_access_service import MemoryAccessService

    role_value = user.role if isinstance(user.role, str) else user.role.value
    memory_access = MemoryAccessService(
        MemoryRepository(pool),
        GitHubRepoAccessService(pool),
        organization_id=org_id,
        is_admin=is_admin_role(role_value),
    )
    req = await memory_access.filter_request_detail_memory_links(
        req,
        user_id=user.id,
        organization_id=org_id,
    )

    # Resolve linked goal + milestone for the request banner. Best-effort:
    # if the goal memory was deleted or isn't accessible, just skip.
    goal_info: dict | None = None
    goal_memory_id = req.get("goal_memory_id")
    if goal_memory_id:
        try:
            from uuid import UUID as _UUID

            goal_mem = await memory_access.get_accessible(
                _UUID(str(goal_memory_id)),
                user.id,
                user.organization_id,
                is_admin=is_admin_role(role_value),
            )
            if goal_mem and goal_mem.get("type") == "goal":
                meta = goal_mem.get("metadata") or {}
                milestones = meta.get("milestones") or []
                idx = req.get("goal_milestone_index")
                milestone_label: str | None = None
                milestone_status: str | None = None
                if (
                    isinstance(milestones, list)
                    and isinstance(idx, int)
                    and 1 <= idx <= len(milestones)
                ):
                    m = milestones[idx - 1] or {}
                    if isinstance(m, dict):
                        milestone_label = (
                            m.get("description") or m.get("title") or m.get("name")
                        )
                        milestone_status = m.get("status")
                goal_info = {
                    "id": str(goal_memory_id),
                    "title": (
                        meta.get("title")
                        or (goal_mem.get("content") or "").split("\n", 1)[0][:80]
                        or "Goal"
                    ),
                    "milestone_index": idx,
                    "milestone_total": (
                        len(milestones) if isinstance(milestones, list) else None
                    ),
                    "milestone_label": milestone_label,
                    "milestone_status": milestone_status,
                }
        except Exception:
            goal_info = None

    # Resolve chat/LLM sessions linked to this request. Prefer the canonical
    # origin columns, but also fall back to llm_session_requests so older or
    # partially-linked rows still show their conversation lineage.
    origin_session: dict | None = None
    origin_sessions: list[dict] = []
    try:
        from lucent.db.llm_sessions import LLMSessionRepository

        session_repo = LLMSessionRepository(pool)
        session_relations: dict[str, str] = {}
        origin_session_id = req.get("origin_session_id")
        if origin_session_id:
            session_relations[str(origin_session_id)] = "origin"

        linked_rows = await session_repo.list_request_relations(
            str(req["id"]), str(user.organization_id)
        )
        for row in linked_rows:
            sid = str(row["session_id"])
            session_relations.setdefault(sid, row["relation"] or "linked")

        for session_id, relation in session_relations.items():
            loaded_session = await session_repo.get_session_detail(
                session_id,
                str(user.organization_id),
                user_id=None if is_admin_role(role_value) else str(user.id),
                requester_role=role_value,
                include_events=True,
            )
            if not loaded_session:
                continue
            session_owner = loaded_session.get("user_id")
            can_view_session = (
                is_admin_role(role_value)
                or str(session_owner) == str(user.id)
            )
            if not can_view_session:
                continue
            messages = loaded_session.get("messages") or []
            events = loaded_session.get("events") or []
            session_summary = {
                "id": str(loaded_session["id"]),
                "title": loaded_session.get("title") or "Chat conversation",
                "kind": loaded_session.get("kind"),
                "model": loaded_session.get("model"),
                "reasoning_effort": loaded_session.get("reasoning_effort"),
                "created_at": loaded_session.get("created_at"),
                "relation": relation,
                "messages": messages,
                "events": events,
                "message_count": len(messages),
                "event_count": len(events),
                "first_user_message": next(
                    (m for m in messages if m.get("role") == "user"),
                    None,
                ),
            }
            origin_sessions.append(session_summary)
        origin_session = origin_sessions[0] if origin_sessions else None
    except Exception:
        logger.exception(
            "Linked-session block failed on request %s — showing none",
            req.get("id"),
        )
        origin_sessions = []
        origin_session = None

    # In-flight model output for running tasks (migration 148 scratch rows).
    # Rendered server-side on first paint, then kept fresh by the page's
    # poller so a long task visibly streams instead of just saying "Running".
    try:
        live_output = await repo.get_live_task_output(request_id, org_id)
    except Exception:
        logger.exception("Live task output unavailable for request %s", request_id)
        live_output = {}

    # Split the task list into the steps a person asked for and the agent's own
    # bookkeeping sessions (post-completion review et al). The latter stay
    # reachable under Details but no longer dominate the page.
    work_tasks: list[dict] = []
    internal_tasks: list[dict] = []
    for task in req.get("task_tree", []):
        bucket = (
            internal_tasks
            if str(task.get("agent_type") or "") in INTERNAL_AGENT_TYPES
            else work_tasks
        )
        bucket.append(task)

    now = datetime.now(timezone.utc)

    def _decorate(task: dict) -> None:
        """Attach the view-model fields the detail page renders."""
        task["activity_line"] = _task_activity_line(task)
        task["fusion"] = _fusion_view(task)
        task["live_output"] = (live_output.get(str(task["id"])) or {}).get("content")
        started = task.get("claimed_at")
        ended = task.get("completed_at")
        if started and ended:
            task["duration_seconds"] = int((ended - started).total_seconds())
        elif started and task.get("status") in ("claimed", "running"):
            task["duration_seconds"] = int((now - started).total_seconds())
        else:
            task["duration_seconds"] = None
        for sub in task.get("sub_tasks") or []:
            _decorate(sub)

    for task in work_tasks + internal_tasks:
        _decorate(task)

    # Progress counts cover nested sub-tasks too: a root task with three
    # children is four units of work, and reporting "1 of 1" while three
    # sub-tasks were still running would be a lie the old stats bar avoided.
    def _walk(tasks: list[dict]) -> list[dict]:
        flat: list[dict] = []
        for task in tasks:
            flat.append(task)
            flat.extend(_walk(task.get("sub_tasks") or []))
        return flat

    work_flat = _walk(work_tasks)
    work_progress = {
        "total": len(work_flat),
        "done": sum(1 for t in work_flat if t.get("status") == "completed"),
        "running": sum(
            1 for t in work_flat if t.get("status") in ("claimed", "running")
        ),
        "attention": sum(
            1 for t in work_flat if t.get("status") in ("failed", "needs_review")
        ),
        "queued": sum(
            1 for t in work_flat if t.get("status") in ("pending", "planned")
        ),
    }

    # Load review history for this request
    from lucent.db.reviews import ReviewRepository

    review_repo = ReviewRepository(pool)
    reviews = await review_repo.get_reviews_for_request(
        request_id, str(user.organization_id)
    )

    # Edit-affordance support: only fetch the choice lists when the request
    # actually contains an editable task. Avoids loading them on every view.
    available_models: list[dict] = []
    available_agents: list[dict] = []
    available_sandbox_templates: list[dict] = []
    has_editable_task = any(
        t.get("status") not in RequestRepository._NON_EDITABLE_TASK_STATUSES
        for t in req.get("tasks", [])
    )
    if has_editable_task:
        from lucent.db.definitions import DefinitionRepository
        from lucent.db.models import ModelRepository, get_authorized_models_pool

        model_pool = await get_authorized_models_pool(
            pool,
            {"id": str(user.id), "organization_id": str(user.organization_id)},
        )
        model_rows = (
            await ModelRepository(model_pool).list_models_accessible_by(
                str(user.id),
                str(user.organization_id),
            )
        )["items"]
        available_models = [
            {
                "id": model["id"],
                "name": model["name"] or model["id"],
                "tags": list(model.get("tags") or []),
                "reasoning_efforts": list(model.get("reasoning_efforts") or []),
            }
            for model in model_rows
        ]
        available_models.sort(key=lambda m: m["id"])
        from lucent.settings import auto_selection_enabled

        if auto_selection_enabled(organization_id=str(user.organization_id)):
            # The pseudo-id resolves at dispatch through the auto-selection
            # arbiter (flag-gated); decision-check cards exclude it client-side.
            available_models.insert(
                0,
                {
                    "id": "auto",
                    "name": "Auto — chosen at dispatch",
                    "tags": [],
                    "reasoning_efforts": [],
                },
            )

        # Usage picks are clearance-driven and default-deny.
        from lucent.db.definitions import get_authorized_definitions_pool

        agent_pool = await get_authorized_definitions_pool(
            pool, {"id": str(user.id), "organization_id": str(user.organization_id)}
        )
        agents_page = await DefinitionRepository(agent_pool).list_agents_accessible_by(
            str(user.id),
            str(user.organization_id),
            status="active",
            limit=200,
        )
        available_agents = sorted(
            [{"name": a["name"], "description": a.get("description", "")}
             for a in agents_page.get("items", [])],
            key=lambda a: a["name"],
        )

        try:
            from lucent.db.sandbox_template import SandboxTemplateRepository

            tpl_repo = SandboxTemplateRepository(pool)
            tpls = await tpl_repo.list_dispatchable(str(user.organization_id))
            available_sandbox_templates = sorted(
                [{"id": str(t["id"]), "name": t.get("name", str(t["id"]))}
                 for t in tpls],
                key=lambda t: t["name"],
            )
        except Exception:
            available_sandbox_templates = []

    return templates.TemplateResponse(
        request,
        "request_detail.html",
        {
            "user": user,
            "req": req,
            "work_tasks": work_tasks,
            "internal_tasks": internal_tasks,
            "work_progress": work_progress,
            "reviews": reviews,
            "available_models": available_models,
            "available_agents": available_agents,
            "available_sandbox_templates": available_sandbox_templates,
            "goal_info": goal_info,
            "request_origin": request_origin,
            "origin_session": origin_session,
            "origin_sessions": origin_sessions,
            "live_revision": _request_live_revision(req, reviews),
            "can_review_request": can_review_request(user, req),
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.post("/requests/{request_id}/approval", response_class=HTMLResponse)
async def request_approval_action(
    request: Request,
    request_id: str,
    action: str = Form(...),
    comment: str = Form(""),
):
    """Approve or reject a request that is waiting at the pre-work approval gate."""
    await _check_csrf(request)
    user = await get_user_context(request)
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
    if len(comment) > 10000:
        raise HTTPException(status_code=422, detail="Comments must be at most 10000 characters")
    if action == "reject" and not comment.strip():
        raise HTTPException(status_code=400, detail="Comments are required when rejecting")

    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    org_id = str(user.organization_id)
    requester_user_id, include_system = request_visibility_context(user)
    req = await repo.get_request(
        request_id,
        org_id,
        requester_user_id=requester_user_id,
        include_system=include_system,
    )
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if not can_review_request(user, req):
        raise HTTPException(status_code=403, detail="You cannot review this request")
    if req.get("approval_status") != "pending_approval":
        raise HTTPException(status_code=409, detail="Request is not awaiting approval")

    if action == "approve":
        result = await repo.approve_request(
            request_id,
            org_id,
            str(user.id),
            comment.strip() or None,
        )
    else:
        result = await repo.reject_request(request_id, org_id, str(user.id), comment.strip())
    if not result:
        raise HTTPException(status_code=409, detail="Request already processed")

    await _notify_request_ready(pool, request_id=request_id, org_id=org_id, action=action)
    if action == "reject":
        await _record_rejection_lesson(
            pool,
            user=user,
            req=req,
            request_id=request_id,
            comment=comment.strip(),
        )

    return RedirectResponse(f"/requests/{request_id}", status_code=303)


@router.post("/requests/{request_id}/cancel", response_class=HTMLResponse)
async def cancel_request(request: Request, request_id: str):
    """Cancel an accessible request without deleting its audit history."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    org_id = str(user.organization_id)
    requester_user_id, include_system = request_visibility_context(user)
    req = await repo.get_request(
        request_id,
        org_id,
        requester_user_id=requester_user_id,
        include_system=include_system,
    )
    if not req or not can_review_request(user, req):
        raise HTTPException(404, "Request not found")
    if req.get("status") in ("completed", "cancelled"):
        raise HTTPException(409, "Completed or cancelled requests cannot be cancelled")
    result = await repo.update_request_status(request_id, "cancelled", org_id=org_id)
    if not result:
        raise HTTPException(404, "Request not found")
    await _notify_request_ready(pool, request_id=request_id, org_id=org_id, action="cancel")
    return RedirectResponse(f"/requests/{request_id}", status_code=303)


@router.post("/requests/tasks/{task_id}/edit", response_class=HTMLResponse)
async def edit_task(request: Request, task_id: str):
    """Edit a pending task's description, model, agent, or sandbox template."""
    user = await get_user_context(request)
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get("csrf_token", "")))
    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    org_id = str(user.organization_id)

    existing, _req = await _get_mutable_task_request(repo, task_id, user)
    if existing.get("status") in repo._NON_EDITABLE_TASK_STATUSES:
        raise HTTPException(
            409,
            f"Task is in status '{existing.get('status')}' — tasks that are "
            "running or already completed cannot be edited.",
        )

    def _val(name: str) -> str | None:
        v = form.get(name)
        if v is None:
            return None
        s = str(v).strip()
        return s if s != "" else None

    title = _val("title")
    description = _val("description")
    model = _val("model")
    reasoning_effort = _val("reasoning_effort") or ""
    agent_type = _val("agent_type")
    sandbox_template_id = _val("sandbox_template_id")
    clear_sandbox = form.get("sandbox_template_id") == "__none__"

    # Validate model + agent against the allowed sets
    if model:
        from lucent.llm.model_auto import split_auto_sentinel
        from lucent.settings import auto_selection_enabled

        is_auto, auto_error = split_auto_sentinel(
            model, enabled=auto_selection_enabled(organization_id=org_id)
        )
        if auto_error:
            raise HTTPException(422, auto_error)
        if is_auto and agent_type == "decision-step":
            raise HTTPException(
                422,
                'Model "auto" applies to chat tasks — decision-step tasks '
                "name a decision model directly",
            )
        if not is_auto:
            from lucent.access_control import AccessControlService
            from lucent.model_registry import (
                validate_model,
                validate_reasoning_effort,
            )

            err = validate_model(model)
            if err:
                raise HTTPException(422, err)
            if not await AccessControlService(pool).can_access(
                str(user.id), "model", model, org_id
            ):
                raise HTTPException(403, "Model is not available to this user")
            effort_err = validate_reasoning_effort(model, reasoning_effort)
            if effort_err:
                raise HTTPException(422, effort_err)
    elif reasoning_effort:
        raise HTTPException(422, "reasoning_effort requires model")

    if agent_type:
        from lucent.db.definitions import DefinitionRepository, get_authorized_definitions_pool

        # Usage is clearance-driven and default-deny.
        agent_pool = await get_authorized_definitions_pool(
            pool, {"id": str(user.id), "organization_id": org_id}
        )
        agents_page = await DefinitionRepository(agent_pool).list_agents_accessible_by(
            str(user.id),
            org_id,
            status="active",
            limit=200,
        )
        if not any(a["name"] == agent_type for a in agents_page.get("items", [])):
            raise HTTPException(422, f"Unknown or unapproved agent_type '{agent_type}'.")

    updated = await repo.update_pending_task(
        task_id,
        org_id,
        title=title,
        description=description,
        model=model,
        reasoning_effort=reasoning_effort,
        agent_type=agent_type,
        sandbox_template_id=None if clear_sandbox else sandbox_template_id,
        clear_sandbox_template=clear_sandbox,
    )
    if not updated:
        raise HTTPException(
            409,
            "Task could not be updated — it may have been claimed by the daemon.",
        )

    request_id = str(updated["request_id"])
    return RedirectResponse(f"/requests/{request_id}#task-{task_id}", status_code=303)


@router.post("/requests/tasks/{task_id}/retry", response_class=HTMLResponse)
async def retry_task(request: Request, task_id: str):
    """Retry a failed task — resets it to pending for the daemon to pick up."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    _existing, parent_request = await _get_mutable_task_request(repo, task_id, user)

    task = await repo.retry_task(task_id, org_id=str(user.organization_id))
    if not task:
        raise HTTPException(409, "Task not in failed state")

    return RedirectResponse(f"/requests/{parent_request['id']}", status_code=303)


@router.post("/requests/tasks/{task_id}/cancel", response_class=HTMLResponse)
async def cancel_task(request: Request, task_id: str):
    """Cancel a task, retaining it in the request audit trail.

    Inactive tasks are cancelled immediately. A claimed/running task receives a
    durable cancel request instead: the daemon interrupts the owning session and
    settles the task to ``cancelled`` moments later, so the redirect lands on a
    task that is still stopping rather than one that silently ignored the click.
    """
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    _existing, parent_request = await _get_mutable_task_request(repo, task_id, user)
    task = await repo.cancel_task(
        task_id,
        org_id=str(user.organization_id),
        requested_by=str(user.id),
    )
    if not task:
        raise HTTPException(
            409, "Task is completed, already cancelled, or already stopping"
        )
    return RedirectResponse(f"/requests/{parent_request['id']}#task-{task_id}", status_code=303)


@router.post("/requests/tasks/{task_id}/stop")
async def stop_task(request: Request, task_id: str):
    """Stop a task from a session-authenticated page, returning JSON.

    The chat's delegated-work panel is a browser page, not an API client, so it
    cannot use the bearer-key REST routes. This mirrors the form-based cancel
    route but answers with JSON so the panel can update in place instead of
    navigating away from the conversation.

    CSRF comes from the ``X-CSRF-Token`` header (the convention other
    session-cookie JSON routes use), not a form field.
    """
    token = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken") or ""
    await _check_csrf(request, form_token=token)
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    _existing, parent_request = await _get_mutable_task_request(repo, task_id, user)
    task = await repo.cancel_task(
        task_id,
        org_id=str(user.organization_id),
        requested_by=str(user.id),
    )
    if not task:
        raise HTTPException(
            409, "Task is completed, already cancelled, or already stopping"
        )
    return {
        "task_id": task_id,
        "request_id": str(parent_request["id"]),
        "status": task.get("status"),
        "cancel_requested": bool(task.get("cancel_requested_at")),
    }
