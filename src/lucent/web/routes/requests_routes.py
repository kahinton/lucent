"""Request tracking and activity routes."""

from math import ceil

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.auth_providers import CSRF_COOKIE_NAME
from lucent.db import get_pool
from lucent.rbac import Role

from ._shared import _check_csrf, get_user_context, templates

router = APIRouter()

ALLOWED_PER_PAGE = {10, 25, 50, 100}


def _can_review_request(user) -> bool:
    """Return whether the current web user can approve/reject request gates."""
    role = user.role if isinstance(user.role, Role) else Role.from_string(str(user.role))
    return (
        role >= Role.ADMIN
        or role == Role.DAEMON
        or user.external_id == "daemon-service"
    )


async def _notify_request_ready(pool, *, request_id: str, action: str) -> None:
    """Best-effort wake notification for daemon request/review state changes."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "SELECT pg_notify('request_ready', $1)",
                f'{{"type": "approval", "action": "{action}", "request_id": "{request_id}"}}',
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

    # Default: hide system (schedule) tasks unless explicitly requested
    if source is None:
        source = "user,cognitive,daemon"

    org_id = str(user.organization_id)
    # Hide cancelled requests by default unless explicitly filtered
    exclude_status = None if status else "cancelled"
    requests_result = await repo.list_requests(
        org_id, status=status, source=source, limit=per_page, offset=offset,
        exclude_status=exclude_status,
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

    # Count pending approvals for the badge (exclude cancelled)
    async with pool.acquire() as conn:
        pending_approval_count = await conn.fetchval(
            "SELECT COUNT(*) FROM requests WHERE organization_id = $1 AND approval_status = 'pending_approval' AND status NOT IN ('cancelled', 'rejection_processing')",
            user.organization_id,
        ) or 0

    template_ctx = {
        "user": user,
        "requests": requests_data,
        "summary": summary,
        "filter_status": status,
        "filter_source": source,
        "pending_approval_count": pending_approval_count,
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

    from lucent.db.memory import MemoryRepository
    from lucent.integrations.github_repo_access_service import GitHubRepoAccessService
    from lucent.services.memory_access_service import MemoryAccessService

    role_value = user.role if isinstance(user.role, str) else user.role.value
    memory_access = MemoryAccessService(
        MemoryRepository(pool),
        GitHubRepoAccessService(pool),
        is_admin=role_value in ("admin", "owner"),
    )
    req = await memory_access.filter_request_detail_memory_links(
        req,
        user_id=user.id,
        organization_id=user.organization_id,
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
                is_admin=role_value in ("admin", "owner"),
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
        from lucent.model_registry import list_models

        available_models = [
            {"id": m.id, "name": m.name or m.id, "tags": list(m.tags or [])}
            for m in list_models(include_disabled=False)
        ]
        available_models.sort(key=lambda m: m["id"])

        def_repo = DefinitionRepository(pool)
        agents_page = await def_repo.list_agents(
            str(user.organization_id),
            status="active",
            limit=200,
            requester_user_id=str(user.id),
            requester_role=role_value,
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
            "recent_events": recent_events[:50],
            "reviews": reviews,
            "available_models": available_models,
            "available_agents": available_agents,
            "available_sandbox_templates": available_sandbox_templates,
            "goal_info": goal_info,
            "can_review_request": _can_review_request(user),
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
    if not _can_review_request(user):
        raise HTTPException(status_code=403, detail="Admin or owner role required")
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
    req = await repo.get_request(request_id, org_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
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

    await _notify_request_ready(pool, request_id=request_id, action=action)
    if action == "reject":
        await _record_rejection_lesson(
            pool,
            user=user,
            req=req,
            request_id=request_id,
            comment=comment.strip(),
        )

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

    existing = await repo.get_task(task_id, org_id=org_id)
    if not existing:
        raise HTTPException(404, "Task not found")
    if existing.get("status") in repo._NON_EDITABLE_TASK_STATUSES:
        raise HTTPException(
            409,
            f"Task is in status '{existing.get('status')}' — tasks that are running or already completed cannot be edited.",
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
    agent_type = _val("agent_type")
    sandbox_template_id = _val("sandbox_template_id")
    clear_sandbox = form.get("sandbox_template_id") == "__none__"

    # Validate model + agent against the allowed sets
    if model:
        from lucent.model_registry import validate_model

        err = validate_model(model)
        if err:
            raise HTTPException(422, err)

    if agent_type:
        from lucent.db.definitions import DefinitionRepository

        def_repo = DefinitionRepository(pool)
        agents_page = await def_repo.list_agents(
            org_id,
            status="active",
            limit=200,
            requester_user_id=str(user.id),
            requester_role=user.role.value,
        )
        if not any(a["name"] == agent_type for a in agents_page.get("items", [])):
            raise HTTPException(422, f"Unknown or unapproved agent_type '{agent_type}'.")

    updated = await repo.update_pending_task(
        task_id,
        org_id,
        title=title,
        description=description,
        model=model,
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

    task = await repo.retry_task(task_id, org_id=str(user.organization_id))
    if not task:
        raise HTTPException(409, "Task not in failed state")

    request_id = str(task["request_id"])
    return RedirectResponse(f"/requests/{request_id}", status_code=303)
