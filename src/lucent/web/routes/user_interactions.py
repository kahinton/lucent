"""Web routes for the Lucent Inbox user-interaction surface."""

from math import ceil

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.auth_providers import CSRF_COOKIE_NAME
from lucent.db import get_pool
from lucent.db.user_interactions import UserInteractionRepository

from ._shared import _check_csrf, get_user_context, templates

router = APIRouter()

ALLOWED_PER_PAGE = {10, 25, 50}


def _status_filter(status: str | None) -> tuple[str | None, bool]:
    if status in {"open", "waiting_on_user", "responded", "resolved", "dismissed"}:
        return status, status in {"resolved", "dismissed"}
    if status == "all":
        return None, True
    return None, False


@router.get("/inbox", response_class=HTMLResponse)
async def inbox_list(
    request: Request,
    status: str | None = None,
    page: int = 1,
    per_page: int = 25,
):
    """Lucent Inbox — proactive messages and clarification requests."""
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


@router.get("/inbox/{interaction_id}", response_class=HTMLResponse)
async def inbox_detail(request: Request, interaction_id: str):
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
        raise HTTPException(404, "Inbox item not found")
    await repo.mark_viewed(
        interaction_id=interaction_id,
        org_id=str(user.organization_id),
        user_id=str(user.id),
    )

    return templates.TemplateResponse(
        request,
        "user_interaction_detail.html",
        {
            "user": user,
            "interaction": interaction,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.post("/inbox/{interaction_id}/reply", response_class=HTMLResponse)
async def inbox_reply(
    request: Request,
    interaction_id: str,
    body: str = Form(...),
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
        raise HTTPException(404, "Inbox item not found")
    try:
        await repo.add_message(
            interaction_id=interaction_id,
            org_id=str(user.organization_id),
            sender_type="user",
            sender_user_id=str(user.id),
            body=content,
            metadata={"source": "web-ui"},
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/inbox/{interaction_id}", status_code=303)


@router.post("/inbox/{interaction_id}/resolve", response_class=HTMLResponse)
async def inbox_resolve(
    request: Request,
    interaction_id: str,
    note: str = Form(""),
):
    """Mark an Inbox item resolved."""
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
        raise HTTPException(404, "Inbox item not found")
    return RedirectResponse(f"/inbox/{interaction_id}", status_code=303)


@router.post("/inbox/{interaction_id}/dismiss", response_class=HTMLResponse)
async def inbox_dismiss(
    request: Request,
    interaction_id: str,
    note: str = Form(""),
):
    """Dismiss an Inbox item without asking the daemon to continue."""
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
        raise HTTPException(404, "Inbox item not found")
    return RedirectResponse("/inbox", status_code=303)
