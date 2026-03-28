"""Request tracking and activity routes."""

from math import ceil

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.db import MemoryRepository, get_pool

from ._shared import _check_csrf, get_user_context, templates

router = APIRouter()

ALLOWED_PER_PAGE = {10, 25, 50, 100}


# =============================================================================
# Request Tracking
# =============================================================================


@router.get("/activity", response_class=HTMLResponse)
async def activity_list(
    request: Request,
    status: str | None = None,
    source: str | None = None,
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

    org_id = str(user.organization_id)
    requests_result = await repo.list_requests(
        org_id, status=status, source=source, limit=per_page, offset=offset
    )
    requests_data = requests_result["items"]
    total_count = requests_result["total_count"]
    total_pages = ceil(total_count / per_page) if total_count > 0 else 1
    page = min(page, total_pages)

    summary = await repo.get_active_summary(org_id)

    # Load task counts for each request
    for req in requests_data:
        tasks = (await repo.list_tasks(str(req["id"])))["items"]
        statuses = [t["status"] for t in tasks]
        req["task_count"] = len(tasks)
        req["tasks_completed"] = sum(1 for s in statuses if s == "completed")
        req["tasks_running"] = sum(1 for s in statuses if s in ("claimed", "running"))
        req["tasks_failed"] = sum(1 for s in statuses if s == "failed")
        req["models_used"] = sorted({t["model"] for t in tasks if t.get("model")})

    # Count needs-review items for the badge
    memory_repo = MemoryRepository(pool)
    review_result = await memory_repo.search(
        tags=["daemon", "needs-review"],
        limit=1,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    needs_review_count = review_result.get("total_count", 0)

    template_ctx = {
        "user": user,
        "requests": requests_data,
        "summary": summary,
        "filter_status": status,
        "filter_source": source,
        "needs_review_count": needs_review_count,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_count": total_count,
    }

    # For HTMX partial updates (pagination clicks)
    if request.headers.get("HX-Request"):
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


@router.get("/requests", response_class=HTMLResponse)
async def requests_redirect(request: Request):
    """Redirect old /requests URL to /activity."""
    qs = str(request.url.query)
    url = "/activity" + ("?" + qs if qs else "")
    return RedirectResponse(url=url, status_code=301)


@router.get("/activity/{request_id}", response_class=HTMLResponse)
@router.get("/requests/{request_id}", response_class=HTMLResponse)
async def request_detail(request: Request, request_id: str):
    """Full request detail with task tree, events, memory links, and reviews."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)

    req = await repo.get_request_with_tasks(request_id, str(user.organization_id))
    if not req:
        raise HTTPException(404, "Request not found")

    # Get recent events for the activity feed
    recent_events = []
    for task in req.get("tasks", []):
        for event in task.get("events", []):
            event["task_title"] = task["title"]
            event["agent_type"] = task.get("agent_type")
            recent_events.append(event)
    recent_events.sort(key=lambda e: e["created_at"], reverse=True)

    # Load review history for this request
    from lucent.db.reviews import ReviewRepository

    review_repo = ReviewRepository(pool)
    reviews = await review_repo.get_reviews_for_request(
        request_id, str(user.organization_id)
    )

    return templates.TemplateResponse(
        request,
        "request_detail.html",
        {
            "user": user,
            "req": req,
            "recent_events": recent_events[:50],
            "reviews": reviews,
        },
    )


@router.post("/requests/tasks/{task_id}/retry", response_class=HTMLResponse)
async def retry_task(request: Request, task_id: str):
    """Retry a failed task — resets it to pending for the daemon to pick up."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)

    task = await repo.retry_task(task_id, org_id=str(user.organization_id))
    if not task:
        raise HTTPException(409, "Task not in failed state")

    request_id = str(task["request_id"])
    return RedirectResponse(f"/requests/{request_id}", status_code=303)
