"""Daemon activity routes — messages, review queue, feedback, legacy redirects."""

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.db import AuditRepository, MemoryRepository, get_pool
from lucent.logging import get_logger
from lucent.metrics import metrics

from ._shared import _check_csrf, get_user_context, templates

logger = get_logger("web.routes.daemon")

router = APIRouter()


# Valid priorities for tasks
_TASK_PRIORITIES = {"low", "medium", "high"}
# Legacy agent type names used for classifying memory-based tasks in the UI
_TASK_AGENT_TYPES = {"research", "code", "memory", "reflection", "documentation", "planning"}


def _memory_to_task_view(memory: dict) -> dict:
    """Convert a daemon-task memory to a view-friendly dict."""
    tags = memory.get("tags") or []
    metadata = memory.get("metadata") or {}

    # Derive status
    if "completed" in tags:
        status = "completed"
    elif any(t.startswith("claimed-by-") for t in tags):
        status = "claimed"
    elif "pending" in tags:
        status = "pending"
    else:
        status = "unknown"

    # Extract agent type, priority, claimed_by
    agent_type = next((t for t in tags if t in _TASK_AGENT_TYPES), "unknown")
    priority = next((t for t in tags if t in _TASK_PRIORITIES), "medium")
    claimed_by = next((t[len("claimed-by-") :] for t in tags if t.startswith("claimed-by-")), None)

    internal_tags = (
        {"daemon-task", "pending", "completed", "daemon"} | _TASK_AGENT_TYPES | _TASK_PRIORITIES
    )
    display_tags = [t for t in tags if t not in internal_tags and not t.startswith("claimed-by-")]

    return {
        "id": memory["id"],
        "description": memory["content"],
        "agent_type": agent_type,
        "priority": priority,
        "status": status,
        "tags": display_tags,
        "created_at": memory["created_at"],
        "updated_at": memory["updated_at"],
        "result": metadata.get("result"),
        "claimed_by": claimed_by,
    }


# =============================================================================
# Daemon Activity
# =============================================================================


@router.get("/daemon", response_class=HTMLResponse)
async def daemon_activity(request: Request):
    """Redirect old /daemon URL to /activity filtered to Lucent's work."""
    return RedirectResponse(url="/activity?source=cognitive", status_code=301)


@router.post("/daemon/messages", response_class=HTMLResponse)
async def send_daemon_message(request: Request):
    """Send a message from the human to the daemon."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)

    form = await request.form()
    content = form.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message content is required")

    username = user.display_name or user.email or str(user.id)
    await repo.create(
        username=username,
        type="experience",
        content=content,
        tags=["daemon-message", "daemon", "from-human", "pending"],
        importance=5,
        metadata={"source": "web-ui"},
        user_id=user.id,
        organization_id=user.organization_id,
    )

    # Re-fetch messages and return the partial for HTMX swap
    messages_result = await repo.search(
        tags=["daemon-message"],
        limit=50,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    daemon_messages = []
    for mem in messages_result.get("memories", []):
        tags = mem.get("tags") or []
        metadata = mem.get("metadata") or {}
        daemon_messages.append(
            {
                "id": mem["id"],
                "content": mem["content"],
                "sender": "daemon" if "from-daemon" in tags else "human",
                "acknowledged": "acknowledged" in tags,
                "created_at": mem["created_at"],
                "in_reply_to": metadata.get("in_reply_to"),
            }
        )
    daemon_messages.reverse()

    return templates.TemplateResponse(
        request,
        "partials/message_thread.html",
        {"daemon_messages": daemon_messages},
    )


@router.get("/daemon/review", response_class=HTMLResponse)
async def daemon_review_queue(request: Request):
    """Show memories tagged 'needs-review' that need human approval."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)

    result = await repo.search(
        tags=["daemon", "needs-review"],
        limit=50,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    return templates.TemplateResponse(
        request,
        "daemon_review.html",
        {
            "user": user,
            "review_memories": result["memories"],
        },
    )


@router.post("/daemon/feedback/{memory_id}", response_class=HTMLResponse)
async def daemon_feedback(
    request: Request,
    memory_id: UUID,
    action: str = Form(...),
    comment: str = Form(""),
):
    """Handle feedback on daemon work (approve/reject/comment/reset)."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    existing_metadata = memory.get("metadata") or {}
    existing_feedback = existing_metadata.get("feedback", {})
    existing_tags = list(memory.get("tags") or [])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if action == "approve":
        feedback = {
            "status": "approved",
            "reviewed_at": now,
            "reviewed_by": user.display_name or user.email,
        }
        if comment:
            feedback["comment"] = comment
    elif action == "reject":
        feedback = {
            "status": "rejected",
            "reviewed_at": now,
            "reviewed_by": user.display_name or user.email,
        }
        if comment:
            feedback["comment"] = comment
    elif action == "comment":
        feedback = {
            **existing_feedback,
            "comment": comment,
            "reviewed_at": now,
            "reviewed_by": user.display_name or user.email,
        }
        if "status" not in feedback:
            feedback["status"] = "pending"
    elif action == "reset":
        feedback = {"status": "pending"}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    # Update tags to make feedback discoverable by the daemon's tag-based search.
    # Remove any existing feedback tags first.
    feedback_tag_prefixes = ("feedback-approved", "feedback-rejected")
    updated_tags = [t for t in existing_tags if t not in feedback_tag_prefixes]
    if action == "approve":
        updated_tags.append("feedback-approved")
        if "needs-review" in updated_tags:
            updated_tags.remove("needs-review")
    elif action == "reject":
        updated_tags.append("feedback-rejected")
        if "needs-review" in updated_tags:
            updated_tags.remove("needs-review")
    elif action == "reset":
        # Restore needs-review if this is daemon work
        if "daemon" in updated_tags and "needs-review" not in updated_tags:
            updated_tags.append("needs-review")
        # Remove feedback-processed if re-opening
        if "feedback-processed" in updated_tags:
            updated_tags.remove("feedback-processed")

    updated_metadata = {**existing_metadata, "feedback": feedback}
    await repo.update(memory_id=memory_id, metadata=updated_metadata, tags=updated_tags)

    # Wake the daemon's cognitive loop so it processes the feedback immediately.
    # Retry once on failure — approve/reject are wake-critical events.
    if action in ("approve", "reject"):
        notify_payload = (
            f'{{"type": "feedback", "action": "{action}", '
            f'"memory_id": "{memory_id}"}}'
        )
        notify_attrs = {"action": action}
        sent = False
        for attempt in range(2):
            try:
                metrics.wake_notify_total.add(1, notify_attrs)
                async with pool.acquire() as conn:
                    await conn.execute(
                        "SELECT pg_notify('request_ready', $1)",
                        notify_payload,
                    )
                sent = True
                break
            except Exception as notify_err:
                metrics.wake_notify_failures.add(1, notify_attrs)
                logger.warning(
                    "pg_notify failed for feedback %s on %s (attempt %d): %s",
                    action, memory_id, attempt + 1, notify_err,
                )
                if attempt == 0:
                    await asyncio.sleep(0.1)
        if not sent:
            logger.error(
                "pg_notify exhausted retries for feedback %s on %s; "
                "daemon will pick up on next poll cycle",
                action, memory_id,
            )

    await audit_repo.log(
        memory_id=memory_id,
        action_type="update",
        user_id=user.id,
        organization_id=user.organization_id,
        changed_fields=["metadata.feedback", "tags"],
        old_values={"feedback": existing_feedback, "tags": existing_tags},
        new_values={"feedback": feedback, "tags": updated_tags},
        notes=f"feedback:{action}",
    )

    # Return the partial HTML for HTMX swap
    # Re-fetch memory to get updated state
    updated_memory = await repo.get(memory_id)
    return templates.TemplateResponse(
        request,
        "partials/feedback_actions.html",
        {"memory": updated_memory},
    )


# =============================================================================
# Daemon Tasks (Legacy redirects)
# =============================================================================


# Legacy Task Queue routes — redirect to Activity
@router.get("/daemon/tasks", response_class=HTMLResponse)
async def daemon_tasks_redirect(request: Request):
    """Redirect legacy task queue to activity page."""
    return RedirectResponse(url="/activity", status_code=301)


@router.get("/daemon/tasks/new", response_class=HTMLResponse)
async def daemon_tasks_new_redirect(request: Request):
    """Redirect legacy new task form to activity page."""
    return RedirectResponse(url="/activity", status_code=301)


@router.get("/daemon/tasks/{task_id}", response_class=HTMLResponse)
async def daemon_task_detail_redirect(request: Request, task_id: UUID):
    """Redirect legacy task detail to activity page."""
    return RedirectResponse(url="/activity", status_code=301)
