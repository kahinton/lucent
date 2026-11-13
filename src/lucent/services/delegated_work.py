"""Delegated-work context for chat turns.

When a conversation hands work to a request, the daemon owns execution but the
conversation stays the place the person talks. Two things are needed to make
that feel like a sub-agent rather than a fire-and-forget queue:

1. The next turn's system prompt must already know what the delegated work is
   doing, so the model never answers "no idea, let me check" about work it
   started itself.
2. Background results must arrive as a readable report, not as a number the
   browser happens to poll.

This module owns the shared, deterministic text for both. Nothing here calls a
model — the phrasing must be predictable so a report never drifts from the
database state it describes.
"""

from __future__ import annotations

from typing import Any

from lucent.db.requests import RequestRepository

# Bounded so a session that delegated a lot of work can never crowd out the
# conversation itself. Five requests covers realistic delegation; the panel in
# the UI is the unbounded view.
MAX_DIGEST_REQUESTS = 5
MAX_DIGEST_TASKS_PER_REQUEST = 3
MAX_RESULT_CHARS = 1200

_ACTIVE_TASK_STATUSES = ("claimed", "running")
_OPEN_REQUEST_STATUSES = (
    "pending",
    "planned",
    "in_progress",
    "review",
    "needs_rework",
    "rejection_processing",
)

_REQUEST_STATUS_LABELS = {
    "pending": "waiting to be planned",
    "planned": "planned",
    "in_progress": "running",
    "review": "finishing up",
    "needs_rework": "sent back for rework",
    "rejection_processing": "closing out after rejection",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}

_TASK_STATUS_LABELS = {
    "pending": "queued",
    "planned": "queued",
    "claimed": "starting",
    "running": "running",
    "completed": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "needs_review": "needs review",
}


def request_status_label(status: str | None) -> str:
    return _REQUEST_STATUS_LABELS.get(str(status or ""), str(status or "unknown"))


def is_blocked_request(request: dict[str, Any]) -> bool:
    """Whether a request has spent its rework budget and cannot advance alone.

    Kept here rather than in the report service because both the transcript card
    and the standing digest must agree on it: a digest that called blocked work
    "sent back for rework" while the card said "blocked" is exactly the kind of
    contradiction that makes the model tell the person to wait for nothing.
    """
    status = str(request.get("status") or "")
    if status not in {"review", "needs_rework"}:
        return False
    try:
        review_count = int(request.get("review_count") or 0)
        max_reviews = int(request.get("max_reviews") or 0)
    except (TypeError, ValueError):
        return False
    return max_reviews > 0 and review_count >= max_reviews


def task_status_label(status: str | None) -> str:
    return _TASK_STATUS_LABELS.get(str(status or ""), str(status or "unknown"))


def _clip(text: str | None, limit: int = MAX_RESULT_CHARS) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _clip_result(text: str | None, request_id: str | None) -> str:
    """Clip a task result, saying how to get the rest when it is cut.

    A bare ellipsis is indistinguishable from "the output was short". The model
    then reports the work as having produced nothing useful, when in fact the
    text was simply longer than the digest. Naming the escape hatch — and that it
    is truncated — is what turns a cut into an actionable instruction.
    """
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return ""
    if len(collapsed) <= MAX_RESULT_CHARS:
        return collapsed
    clipped = collapsed[: MAX_RESULT_CHARS - 1].rstrip()
    hint = (
        "… [truncated — full output is on the request page or via "
        f"get_request_details('{request_id}')]"
        if request_id
        else "… [truncated — full output via get_request_details]"
    )
    return f"{clipped}{hint}"


def _task_line(task: dict[str, Any], request_id: str | None = None) -> str:
    status = task_status_label(task.get("status"))
    title = _clip(task.get("title"), 90)
    if task.get("cancel_requested_at") and task.get("status") in _ACTIVE_TASK_STATUSES:
        status = "stopping"
    # Prefer the actual output; a remote executor's result_summary is a terse
    # status line that would mask the host output the person asked for.
    #
    # ``result_excerpt`` is what the read model actually returns — the raw
    # ``result`` column is not selected. Checking only ``result`` meant this
    # always fell through to the terse summary, so the digest told the model
    # "Completed on remote X in 5.7s" and it reported the work as having no
    # output while the card body held the full text.
    detail = _clip_result(
        task.get("result_excerpt")
        or task.get("result")
        or task.get("result_summary")
        or task.get("error"),
        request_id,
    )
    line = f"    - {title} [{status}]"
    if not detail and task.get("status") == "failed" and task.get("error"):
        # A failed task with no result excerpt still has a *cause*. Omitting it
        # is what left the model able to say only "it failed" with no way to act.
        detail = _clip_result(task.get("error"), request_id)
    return f"{line}\n      {detail}" if detail else line


async def load_delegated_work(
    pool,
    *,
    session_id: str | None,
    org_id: str,
) -> dict[str, Any]:
    """Read the delegated-work tree for a chat session (never raises)."""
    if not session_id:
        return {
            "requests": [],
            "active_task_count": 0,
            "revision": "empty",
            "report_revision": "none",
        }
    try:
        return await RequestRepository(pool).list_delegated_work_for_session(
            session_id, org_id
        )
    except Exception:  # noqa: BLE001 — context enrichment must never break a turn
        return {
            "requests": [],
            "active_task_count": 0,
            "revision": "error",
            "report_revision": "none",
        }


def build_delegated_work_digest(data: dict[str, Any]) -> str:
    """Render the compact "work I delegated" block for a system prompt.

    Returns an empty string when the session delegated nothing, so prompts
    without delegation are byte-identical to what they were before.
    """
    requests = data.get("requests") or []
    if not requests:
        return ""

    open_requests = [r for r in requests if str(r.get("status")) in _OPEN_REQUEST_STATUSES]
    finished = [r for r in requests if str(r.get("status")) not in _OPEN_REQUEST_STATUSES]
    # Open work first — it is what the person is most likely asking about.
    ordered = (open_requests + finished)[:MAX_DIGEST_REQUESTS]

    lines = [
        "--- DELEGATED WORK ---",
        "Work this conversation handed to the daemon. The daemon runs it; you "
        "supervise it and answer for it. Treat this as your own current knowledge "
        "— do not tell the person you have no way to know, and do not call tools "
        "just to restate it. Use list_active_work or get_request_details only "
        "when you need more detail than this summary.",
        "Below each request is one line per task, including its RESULT. When a "
        "result ends with a 'truncated' marker, the rest is real output you have "
        "not seen — call get_request_details(<request id>) to read all of it "
        "before describing the work as having no output.",
        "",
    ]
    for req in ordered:
        counts = req.get("task_counts") or {}
        total = int(req.get("task_total") or 0)
        running = int(counts.get("claimed", 0)) + int(counts.get("running", 0))
        done = int(counts.get("completed", 0))
        bits = [request_status_label(req.get("status"))]
        if total:
            bits.append(f"{done}/{total} tasks done")
        if running:
            bits.append(f"{running} running")
        if is_blocked_request(req):
            bits.append(
                f"blocked — {int(req.get('review_count') or 0)}/"
                f"{int(req.get('max_reviews') or 0)} rework budget used"
            )
        lines.append(
            f"- \"{_clip(req.get('title'), 120)}\" — {', '.join(bits)} "
            f"(request {req.get('id')})"
        )
        for task in (req.get("tasks") or [])[:MAX_DIGEST_TASKS_PER_REQUEST]:
            lines.append(_task_line(task, req.get("id")))
        remaining = total - min(total, MAX_DIGEST_TASKS_PER_REQUEST)
        if remaining > 0:
            lines.append(f"    … and {remaining} more task(s)")

    if len(requests) > len(ordered):
        lines.append(
            f"- …and {len(requests) - len(ordered)} older delegated request(s) "
            "not shown here."
        )
    blocked = [r for r in requests if is_blocked_request(r)]
    if blocked:
        # The one state where "wait for the report" is wrong advice. Saying so
        # here matters because this digest is what the model reads on the *next*
        # turn — the turn after a failure report, when the person is most likely
        # to ask "so what now?".
        lines.append("")
        lines.append(
            "A request above is BLOCKED: it used its entire rework budget and "
            "nothing will run on it again by itself. Do not wait for another "
            "report and do not describe it as still working. Read its actual "
            "failure with get_request_details, then either fix the cause "
            "yourself (retry_task / create_task / update_task / "
            "update_request_status) or tell the person exactly what is "
            "stopping it and what only they can decide."
        )
    failed = [
        r
        for r in requests
        if str(r.get("status")) == "failed" and not is_blocked_request(r)
    ]
    if failed:
        lines.append("")
        lines.append(
            "A request above failed on a task but is still retryable. Read the "
            "task error (get_request_details), fix the cause if you can, then "
            "retry_task with that as guidance — do not simply relay the failure "
            "to the person as though it were their problem."
        )
    # "Finished" must exclude a failed request: its tasks are terminal, but the
    # work is not done, and telling the model everything is finished is how a
    # retryable failure turned into "all delegated work has finished".
    if len(open_requests) == 0 and not failed:
        lines.append("")
        lines.append(
            "All delegated work has finished. Report results when asked; do not "
            "claim anything is still running."
        )
    elif open_requests and all(
        str(r.get("status")) == "review" and not is_blocked_request(r)
        for r in open_requests
    ):
        # `review` is the automatic post-completion quality check, not a human
        # gate. Without this note a model reads "in review"/"finishing up" as
        # work still running and tells the person it has not finished yet.
        lines.append("")
        lines.append(
            "Every remaining request above is in the automatic post-completion "
            "check, which resolves on its own within about a minute. Their work "
            "itself is done — do not describe them as still running, and do not "
            "present the check as a decision the person needs to make."
        )
    return "\n".join(lines)


async def build_delegated_work_digest_for_session(
    pool,
    *,
    session_id: str | None,
    org_id: str,
) -> str:
    """Convenience wrapper: load then render, returning '' when there is none."""
    data = await load_delegated_work(pool, session_id=session_id, org_id=org_id)
    return build_delegated_work_digest(data)


def summarize_report_event(
    *,
    kind: str,
    request: dict[str, Any] | None,
    task: dict[str, Any] | None,
) -> str:
    """One-line headline for a report event.

    The request title is rendered separately on the card, so it is deliberately
    not repeated here — a headline that restates it reads as noise.
    """
    if kind.startswith("request."):
        status = kind.split(".", 1)[1]
        if status == "review":
            # Lucent's own post-completion quality gate runs automatically and
            # resolves in seconds. Calling it "ready for your review" told both
            # the person and the model that a human decision was pending, so
            # replies described finished work as unfinished.
            return "Checking the finished work automatically."
        if status == "needs_rework":
            return "Review sent the work back for another pass."
        if status == "completed":
            return "This request is complete."
        if status == "failed":
            return "This request stopped on a failed task."
        if status == "cancelled":
            return "This request was cancelled."
        return f"Request is now {request_status_label(status)}."
    if kind.startswith("task."):
        status = kind.split(".", 1)[1]
        task_title = _clip((task or {}).get("title"), 120)
        if status == "completed":
            return f"Finished: {task_title}"
        if status == "failed":
            return f"Failed: {task_title}"
        if status == "cancelled":
            return f"Stopped: {task_title}"
        if status == "needs_review":
            return f"Needs your review: {task_title}"
        return f"{task_title} — {task_status_label(status)}"
    return "Delegated work reported an update."
