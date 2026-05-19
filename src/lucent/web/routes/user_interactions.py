"""Web routes for Lucent handoffs and user-interaction messages."""

from math import ceil
from urllib.parse import urlparse

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.auth_providers import CSRF_COOKIE_NAME
from lucent.db import get_pool
from lucent.db.user_interactions import UserInteractionRepository

from ._shared import _check_csrf, get_user_context, templates

router = APIRouter()

ALLOWED_PER_PAGE = {10, 25, 50}


def _reference_url(ref: dict) -> str:
    if ref.get("url"):
        return str(ref["url"])
    ref_type = ref.get("reference_type")
    ref_id = ref.get("reference_id")
    if ref_type == "request" and ref_id:
        return f"/activity/{ref_id}"
    if ref_type == "memory" and ref_id:
        return f"/memories/{ref_id}"
    if ref_type == "workflow" and ref_id:
        return f"/workflows/{ref_id}"
    if ref_type == "llm_session" and ref_id:
        return f"/chat/{ref_id}"
    return ""


def _is_handoff_reference(ref: dict) -> bool:
    url = str(ref.get("url") or "").strip()
    if not url:
        return False
    parsed = urlparse(url)
    path = parsed.path or url
    normalized_path = path.rstrip("/") or "/"
    return normalized_path == "/handoffs" or normalized_path.startswith("/handoffs/")


def _display_references(interaction: dict) -> list[dict]:
    """Return references useful to humans on the handoff detail page.

    Raw references still stay attached to the interaction for daemon/chat
    grounding. This filters out operational run IDs and self-links that are
    useful to Lucent but distracting or misleading to people.
    """
    visible: list[dict] = []
    seen_destinations: set[str] = set()
    for ref in interaction.get("references") or []:
        ref_type = ref.get("reference_type")
        if ref_type == "schedule_run":
            continue
        if _is_handoff_reference(ref):
            continue
        destination = _reference_url(ref).strip()
        dedupe_key = destination or f"{ref_type}:{ref.get('reference_id') or ref.get('label')}"
        if dedupe_key in seen_destinations:
            continue
        seen_destinations.add(dedupe_key)
        visible.append(ref)

    order = {
        "workflow": 0,
        "request": 1,
        "task": 2,
        "task_output": 3,
        "memory": 4,
        "llm_session": 5,
        "url": 6,
        "other": 7,
    }
    return sorted(visible, key=lambda r: order.get(r.get("reference_type"), 99))


async def _get_or_create_interaction_chat_session(pool, user, interaction: dict) -> dict:
    """Return the persistent embedded chat session for this handoff.

    Handoffs are not just static messages; opening one starts a focused Lucent
    session from the handoff thread and attached references.
    """
    from lucent.db.llm_sessions import LLMSessionRepository

    interaction_id = str(interaction["id"])
    org_id = str(user.organization_id)
    user_id = str(user.id)
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            """SELECT * FROM llm_sessions
               WHERE organization_id = $1::uuid
                 AND user_id = $2::uuid
                 AND kind = 'embedded_chat'
                 AND status <> 'deleted'
                 AND metadata->>'interaction_id' = $3
               ORDER BY COALESCE(last_message_at, updated_at, created_at) DESC
               LIMIT 1""",
            org_id,
            user_id,
            interaction_id,
        )
    if existing:
        return dict(existing)

    repo = LLMSessionRepository(pool)
    session = await repo.create_session(
        org_id=org_id,
        user_id=user_id,
        kind="embedded_chat",
        title=f"Handoff: {interaction.get('title') or 'Lucent message'}"[:256],
        metadata={
            "interaction_id": interaction_id,
            "surface": "inbox_interaction",
            "interaction_type": interaction.get("interaction_type"),
            "source": interaction.get("source"),
        },
    )
    for message in interaction.get("messages") or []:
        role = "user" if message.get("sender_type") == "user" else "assistant"
        await repo.add_message(
            session["id"],
            role=role,
            content=message.get("body") or "",
            org_id=org_id,
            metadata={
                "source": "user_interaction_seed",
                "interaction_id": interaction_id,
                "interaction_message_id": str(message.get("id")),
                "sender_type": message.get("sender_type"),
            },
        )
    return session


def _status_filter(status: str | None) -> tuple[str | None, bool]:
    if status in {"open", "waiting_on_user", "responded", "resolved", "dismissed"}:
        return status, status in {"resolved", "dismissed"}
    if status == "all":
        return None, True
    return None, False


@router.get("/handoffs", response_class=HTMLResponse)
async def handoffs_list(
    request: Request,
    status: str | None = None,
    page: int = 1,
    per_page: int = 25,
):
    """Lucent handoffs — questions, decisions, and updates from Lucent."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = UserInteractionRepository(pool)

    page = max(1, page)
    per_page = per_page if per_page in ALLOWED_PER_PAGE else 25
    offset = (page - 1) * per_page
    status_value, include_resolved = _status_filter(status)
    result = await repo.list_interactions(
        org_id=str(user.organization_id),
        user_id=str(user.id),
        status=status_value,
        include_resolved=include_resolved,
        limit=per_page,
        offset=offset,
    )
    items = result["items"]
    total_count = result["total_count"]
    total_pages = ceil(total_count / per_page) if total_count else 1

    needs_response = [item for item in items if item.get("needs_response")]
    unread = [
        item for item in items
        if not item.get("needs_response") and item.get("is_unread")
    ]
    other = [
        item for item in items
        if not item.get("needs_response") and not item.get("is_unread")
    ]
    attention_count = await repo.count_attention_needed(
        org_id=str(user.organization_id),
        user_id=str(user.id),
    )

    return templates.TemplateResponse(
        request,
        "user_interactions_list.html",
        {
            "user": user,
            "items": items,
            "needs_response": needs_response,
            "unread": unread,
            "other": other,
            "attention_count": attention_count,
            "filter_status": status or "active",
            "page": min(page, total_pages),
            "per_page": per_page,
            "total_pages": total_pages,
            "total_count": total_count,
        },
    )


@router.get("/handoffs/{interaction_id}", response_class=HTMLResponse)
async def handoff_detail(request: Request, interaction_id: str):
    """Show an interaction thread with attached context and reply controls."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = UserInteractionRepository(pool)
    interaction = await repo.get_interaction(
        interaction_id,
        str(user.organization_id),
        user_id=str(user.id),
    )
    if not interaction:
        raise HTTPException(404, "Handoff not found")
    await repo.mark_viewed(
        interaction_id=interaction_id,
        org_id=str(user.organization_id),
        user_id=str(user.id),
    )
    request.state.user_interaction_count = await repo.count_attention_needed(
        org_id=str(user.organization_id),
        user_id=str(user.id),
    )
    chat_session = await _get_or_create_interaction_chat_session(pool, user, interaction)

    return templates.TemplateResponse(
        request,
        "user_interaction_detail.html",
        {
            "user": user,
            "interaction": interaction,
            "display_references": _display_references(interaction),
            "interaction_chat_session": chat_session,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.post("/handoffs/{interaction_id}/reply", response_class=HTMLResponse)
async def handoff_reply(
    request: Request,
    interaction_id: str,
    body: str = Form(...),
    chat_session_id: str = Form(""),
    inline_chat: str = Form(""),
):
    """Reply to a Lucent interaction; the daemon can read the response later."""
    await _check_csrf(request)
    user = await get_user_context(request)
    content = body.strip()
    if not content:
        raise HTTPException(400, "Reply cannot be empty")
    if len(content) > 20000:
        raise HTTPException(422, "Reply must be at most 20,000 characters")

    pool = await get_pool()
    repo = UserInteractionRepository(pool)
    existing = await repo.get_interaction(
        interaction_id,
        str(user.organization_id),
        user_id=str(user.id),
    )
    if not existing:
        raise HTTPException(404, "Handoff not found")
    try:
        await repo.add_message(
            interaction_id=interaction_id,
            org_id=str(user.organization_id),
            sender_type="user",
            sender_user_id=str(user.id),
            body=content,
            metadata={
                "source": "inline-chat" if inline_chat else "web-ui",
                "llm_session_id": chat_session_id or None,
            },
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/handoffs/{interaction_id}", status_code=303)


@router.post("/handoffs/{interaction_id}/resolve", response_class=HTMLResponse)
async def handoff_resolve(
    request: Request,
    interaction_id: str,
    note: str = Form(""),
):
    """Mark a handoff resolved."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    repo = UserInteractionRepository(pool)
    result = await repo.resolve_interaction(
        interaction_id=interaction_id,
        org_id=str(user.organization_id),
        user_id=str(user.id),
        note=note.strip() or None,
    )
    if not result:
        raise HTTPException(404, "Handoff not found")
    return RedirectResponse(f"/handoffs/{interaction_id}", status_code=303)


@router.post("/handoffs/{interaction_id}/dismiss", response_class=HTMLResponse)
async def handoff_dismiss(
    request: Request,
    interaction_id: str,
    note: str = Form(""),
):
    """Dismiss a handoff without asking the daemon to continue."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    repo = UserInteractionRepository(pool)
    result = await repo.dismiss_interaction(
        interaction_id=interaction_id,
        org_id=str(user.organization_id),
        user_id=str(user.id),
        note=note.strip() or None,
    )
    if not result:
        raise HTTPException(404, "Handoff not found")
    return RedirectResponse("/handoffs", status_code=303)
