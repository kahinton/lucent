"""Workflow and legacy schedule management routes."""

import json
from math import ceil
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.auth_providers import CSRF_COOKIE_NAME
from lucent.db import get_pool

from ._shared import _check_csrf, get_user_context, templates

router = APIRouter()

ALLOWED_PER_PAGE = {10, 25, 50, 100}


def _annotate_workflow_execution(sched: dict[str, Any]) -> None:
    """Add template-friendly execution flags derived from workflow actions."""
    actions = sched.get("actions") or []
    if not isinstance(actions, list):
        actions = []
    server_actions = [
        action
        for action in actions
        if isinstance(action, dict) and action.get("action_type") == "server_function"
    ]
    interaction_actions = [
        action
        for action in actions
        if isinstance(action, dict) and action.get("action_type") == "user_interaction"
    ]
    task_actions = [
        action
        for action in actions
        if isinstance(action, dict) and action.get("action_type", "task") == "task"
    ]
    sched["server_actions"] = server_actions
    sched["interaction_actions"] = interaction_actions
    sched["is_server_workflow"] = bool(server_actions)
    if server_actions:
        action = server_actions[0]
        sched["executor_label"] = "Server function"
        sched["executor_detail"] = action.get("function") or "api_process"
    elif interaction_actions and not task_actions:
        sched["executor_label"] = "Inbox"
        sched["executor_detail"] = f"{len(interaction_actions)} user interaction action(s)"
    else:
        sched["executor_label"] = "Agent"
        sched["executor_detail"] = sched.get("agent_type") or "default"


# =============================================================================
# Workflows / Schedules
# =============================================================================


@router.get("/workflows", response_class=HTMLResponse)
@router.get("/schedules", response_class=HTMLResponse)
async def schedules_list(
    request: Request,
    status: str | None = None,
    enabled: str | None = None,
    page: int = 1,
    per_page: int = 25,
):
    """List all workflows with filtering. /schedules remains a compatibility alias."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)

    page = max(1, page)
    per_page = per_page if per_page in ALLOWED_PER_PAGE else 25
    offset = (page - 1) * per_page

    org_id = str(user.organization_id)
    enabled_filter = True if enabled == "true" else (False if enabled == "false" else None)
    result = await repo.list_schedules(
        org_id, status=status, enabled=enabled_filter, limit=per_page, offset=offset
    )
    schedules = result["items"]
    for sched in schedules:
        json_fields = (("actions", []), ("trigger_config", {}), ("request_template", {}))
        for json_col, default in json_fields:
            if isinstance(sched.get(json_col), str):
                try:
                    sched[json_col] = json.loads(sched[json_col])
                except (TypeError, ValueError):
                    sched[json_col] = default
        _annotate_workflow_execution(sched)
    total_count = result["total_count"]
    total_pages = ceil(total_count / per_page) if total_count > 0 else 1
    page = min(page, total_pages)
    summary = await repo.get_summary(org_id)
    base_path = "/workflows" if request.url.path.startswith("/workflows") else "/schedules"

    return templates.TemplateResponse(
        request,
        "schedules_list.html",
        {
            "user": user,
            "schedules": schedules,
            "summary": summary,
            "filter_status": status,
            "filter_enabled": enabled,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total_count": total_count,
            "base_path": base_path,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


async def _workflow_form_options(pool, user) -> dict[str, Any]:
    from lucent.db.definitions import DefinitionRepository
    from lucent.model_registry import list_models

    role_value = user.role if isinstance(user.role, str) else user.role.value
    def_repo = DefinitionRepository(pool)
    active_agents = (
        await def_repo.list_agents(
            str(user.organization_id),
            status="active",
            requester_user_id=str(user.id),
            requester_role=role_value,
            limit=500,
        )
    )["items"]
    workflow_composer_agent = next(
        (agent for agent in active_agents if agent.get("name") == "workflow-composer"),
        None,
    )
    available_models = [
        {
            "id": m.id,
            "name": m.name or m.id,
            "reasoning_efforts": list(m.reasoning_efforts or []),
        }
        for m in list_models(include_disabled=False)
    ]
    available_models.sort(key=lambda m: m["id"])
    return {
        "active_agents": active_agents,
        "available_models": available_models,
        "workflow_composer_agent": workflow_composer_agent,
    }


@router.get("/workflows/new", response_class=HTMLResponse)
async def workflow_new(request: Request):
    """Guided workflow creation wizard."""
    user = await get_user_context(request)
    pool = await get_pool()
    options = await _workflow_form_options(pool, user)
    return templates.TemplateResponse(
        request,
        "workflow_new.html",
        {
            "user": user,
            "base_path": "/workflows",
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
            **options,
        },
    )


def _form_list(form, key: str) -> list[str]:
    return [str(v or "") for v in form.getlist(key)]


def _form_value(values: list[str], index: int, default: str = "") -> str:
    if index >= len(values):
        return default
    return values[index].strip()


@router.post("/workflows/new", response_class=HTMLResponse)
async def workflow_create_from_wizard(request: Request):
    """Create a workflow from the non-technical wizard form."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.schedules import ScheduleRepository, webhook_secret_hash

    form = await request.form()
    title = str(form.get("title", "")).strip()
    if not title:
        raise HTTPException(422, "Workflow title is required")
    description = str(form.get("description", "")).strip()
    trigger_type = str(form.get("trigger_type", "schedule")).strip()
    if trigger_type not in {"schedule", "manual", "webhook", "integration_event"}:
        raise HTTPException(422, "Invalid trigger type")

    schedule_type = str(form.get("schedule_type", "interval")).strip()
    cron_expression = str(form.get("cron_expression", "")).strip() or None
    interval_seconds = None
    if trigger_type == "schedule" and schedule_type == "interval":
        try:
            amount = int(str(form.get("interval_amount", "1")).strip() or "1")
        except ValueError as exc:
            raise HTTPException(422, "Interval amount must be a number") from exc
        unit = str(form.get("interval_unit", "hours")).strip()
        multiplier = {"minutes": 60, "hours": 3600, "days": 86400}.get(unit, 3600)
        interval_seconds = max(60, amount * multiplier)
    elif trigger_type == "schedule" and schedule_type == "cron" and not cron_expression:
        raise HTTPException(422, "Cron expression is required for cron workflows")
    elif trigger_type != "schedule":
        schedule_type = trigger_type

    webhook_secret = str(form.get("webhook_secret", "")).strip() or None
    if trigger_type == "webhook" and not webhook_secret:
        raise HTTPException(422, "Webhook workflows require a shared secret")

    action_titles = _form_list(form, "action_title")
    action_prompts = _form_list(form, "action_prompt")
    action_agents = _form_list(form, "action_agent_type")
    action_models = _form_list(form, "action_model")
    action_efforts = _form_list(form, "action_reasoning_effort")
    actions = []
    max_actions = max(len(action_titles), len(action_prompts), 1)
    for idx in range(max_actions):
        action_title = _form_value(action_titles, idx, title)
        action_prompt = _form_value(action_prompts, idx, description)
        if not action_title and not action_prompt:
            continue
        actions.append(
            {
                "action_type": "task",
                "title": action_title or f"{title} action {idx + 1}",
                "description": action_prompt or description,
                "agent_type": _form_value(action_agents, idx, "code") or "code",
                "model": _form_value(action_models, idx) or None,
                "reasoning_effort": _form_value(action_efforts, idx) or None,
                "priority": str(form.get("priority", "medium")).strip() or "medium",
                "sequence_order": idx,
            }
        )
    if not actions:
        actions = [
            {
                "action_type": "task",
                "title": title,
                "description": description or "Run the workflow and record any outputs.",
                "agent_type": "code",
                "priority": str(form.get("priority", "medium")).strip() or "medium",
                "sequence_order": 0,
            }
        ]

    request_template = {
        "title_prefix": "[Scheduled]" if trigger_type == "schedule" else "[Workflow]",
        "title": str(form.get("request_title", "")).strip() or title,
        "description": str(form.get("request_description", "")).strip() or description,
        "dependency_policy": str(form.get("dependency_policy", "strict")).strip() or "strict",
    }
    trigger_config = {
        "allow_concurrent": str(form.get("allow_concurrent", "")).lower() == "on",
    }
    if trigger_type == "integration_event":
        provider = str(form.get("integration_provider", "")).strip()
        event_name = str(form.get("integration_event_name", "")).strip()
        if provider:
            trigger_config["provider"] = provider
        if event_name:
            trigger_config["event_name"] = event_name

    repo = ScheduleRepository(pool)
    workflow = await repo.create_schedule(
        title=title,
        org_id=str(user.organization_id),
        schedule_type=schedule_type,
        description=description,
        agent_type=actions[0].get("agent_type", "code"),
        cron_expression=cron_expression,
        interval_seconds=interval_seconds,
        priority=str(form.get("priority", "medium")).strip() or "medium",
        timezone_str=str(form.get("timezone", "UTC")).strip() or "UTC",
        created_by=str(user.id),
        trigger_type=trigger_type,
        trigger_config=trigger_config,
        request_template=request_template,
        actions=actions,
        review_instructions=str(form.get("review_instructions", "")).strip(),
        webhook_secret_hash=webhook_secret_hash(webhook_secret),
    )
    return RedirectResponse(url=f"/workflows/{workflow['id']}", status_code=303)


@router.get("/workflows/{schedule_id}", response_class=HTMLResponse)
@router.get("/schedules/{schedule_id}", response_class=HTMLResponse)
async def schedule_detail(
    request: Request,
    schedule_id: str,
    page: int = 1,
    per_page: int = 25,
):
    """Schedule detail page with run history."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)

    org_id = str(user.organization_id)
    sched = await repo.get_schedule(schedule_id, org_id)
    if not sched:
        raise HTTPException(404, "Workflow not found")

    for json_col, default in (
        ("trigger_config", {}),
        ("request_template", {}),
        ("actions", []),
        ("task_template", {}),
        ("sandbox_config", None),
    ):
        if isinstance(sched.get(json_col), str):
            try:
                sched[json_col] = json.loads(sched[json_col])
            except (TypeError, ValueError):
                sched[json_col] = default
    if not sched.get("actions"):
        sched["actions"] = [
            {
                "title": (sched.get("task_template") or {}).get("title") or sched["title"],
                "description": sched.get("prompt") or sched.get("description") or "",
                "agent_type": sched.get("agent_type") or "code",
                "priority": sched.get("priority") or "medium",
                "sequence_order": 0,
            }
        ]
    _annotate_workflow_execution(sched)

    # Paginate run history
    page = max(1, page)
    per_page = per_page if per_page in ALLOWED_PER_PAGE else 25
    run_offset = (page - 1) * per_page
    runs_result = await repo.list_runs(schedule_id, limit=per_page, offset=run_offset)
    sched["runs"] = runs_result["items"]
    run_total_count = runs_result["total_count"]
    run_total_pages = ceil(run_total_count / per_page) if run_total_count > 0 else 1
    page = min(page, run_total_pages)

    def_repo = DefinitionRepository(pool)
    role_value = user.role if isinstance(user.role, str) else user.role.value
    active_agents = (
        await def_repo.list_agents(
            org_id,
            status="active",
            requester_user_id=str(user.id),
            requester_role=role_value,
        )
    )["items"]

    from lucent.model_registry import list_models

    available_models = [
        {
            "id": m.id,
            "name": m.name or m.id,
            "reasoning_efforts": list(m.reasoning_efforts or []),
        }
        for m in list_models(include_disabled=False)
    ]
    available_models.sort(key=lambda m: m["id"])

    # Resolve sandbox template name if linked
    sandbox_template = None
    if sched.get("sandbox_template_id"):
        from lucent.db.sandbox_template import SandboxTemplateRepository

        tmpl_repo = SandboxTemplateRepository(pool)
        sandbox_template = await tmpl_repo.get_accessible(
            str(sched["sandbox_template_id"]),
            org_id,
            str(user.id),
            user_role=role_value,
        )

    return templates.TemplateResponse(
        request,
        "schedule_detail.html",
        {
            "user": user,
            "sched": sched,
            "base_path": (
                "/workflows" if request.url.path.startswith("/workflows") else "/schedules"
            ),
            "active_agents": active_agents,
            "available_models": available_models,
            "sandbox_template": sandbox_template,
            "run_page": page,
            "run_per_page": per_page,
            "run_total_pages": run_total_pages,
            "run_total_count": run_total_count,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.post("/workflows/{schedule_id}/trigger", response_class=HTMLResponse)
@router.post("/schedules/{schedule_id}/trigger", response_class=HTMLResponse)
async def workflow_trigger_now(request: Request, schedule_id: str):
    """Manually trigger a workflow from the web UI."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.api.routers.schedules import _trigger_schedule_execution

    await _trigger_schedule_execution(
        schedule_id,
        user,
        force=True,
        pool=pool,
        advance_schedule=False,
    )
    base_path = "/workflows" if request.url.path.startswith("/workflows") else "/schedules"
    return RedirectResponse(url=f"{base_path}/{schedule_id}", status_code=303)


@router.post("/workflows/{schedule_id}/toggle", response_class=HTMLResponse)
@router.post("/schedules/{schedule_id}/toggle", response_class=HTMLResponse)
async def schedule_toggle(request: Request, schedule_id: str):
    """Toggle a schedule between enabled and paused."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    org_id = str(user.organization_id)

    sched = await repo.get_schedule(schedule_id, org_id)
    if not sched:
        raise HTTPException(404, "Schedule not found")

    new_enabled = not sched["enabled"]
    await repo.toggle_schedule(schedule_id, org_id, new_enabled)

    # Redirect back to referrer or detail page
    referer = request.headers.get("referer", "")
    base_path = "/workflows" if request.url.path.startswith("/workflows") else "/schedules"
    if f"{base_path}/" in referer and schedule_id in referer:
        return RedirectResponse(url=f"{base_path}/{schedule_id}", status_code=303)
    return RedirectResponse(url=base_path, status_code=303)


@router.post("/workflows/{schedule_id}/delete", response_class=HTMLResponse)
@router.post("/schedules/{schedule_id}/delete", response_class=HTMLResponse)
async def schedule_delete(request: Request, schedule_id: str):
    """Delete a schedule."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    org_id = str(user.organization_id)

    try:
        deleted = await repo.delete_schedule(schedule_id, org_id)
    except ValueError:
        raise HTTPException(409, "System schedules cannot be deleted. Disable it instead.")
    if not deleted:
        raise HTTPException(404, "Schedule not found")

    base_path = "/workflows" if request.url.path.startswith("/workflows") else "/schedules"
    return RedirectResponse(url=base_path, status_code=303)


@router.post("/workflows/{schedule_id}/edit", response_class=HTMLResponse)
@router.post("/schedules/{schedule_id}/edit", response_class=HTMLResponse)
async def schedule_edit(request: Request, schedule_id: str):
    """Update editable schedule fields."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    org_id = str(user.organization_id)

    sched = await repo.get_schedule(schedule_id, org_id)
    if not sched:
        raise HTTPException(404, "Schedule not found")

    form = await request.form()
    updates: dict[str, Any] = {}

    # Text fields
    title = form.get("title", "").strip()
    if title and title != sched["title"]:
        updates["title"] = title

    description = form.get("description", "").strip()
    if description != (sched["description"] or ""):
        updates["description"] = description

    if "agent_type" in form:
        agent_type = form.get("agent_type", "").strip()
        if agent_type and agent_type != sched["agent_type"]:
            updates["agent_type"] = agent_type

    # Schedule-type-specific fields
    cron_expression = form.get("cron_expression", "").strip()
    if cron_expression and cron_expression != (sched.get("cron_expression") or ""):
        updates["cron_expression"] = cron_expression

    interval_str = form.get("interval_seconds", "").strip()
    if interval_str:
        try:
            interval_val = int(interval_str)
            if interval_val != (sched.get("interval_seconds") or 0):
                updates["interval_seconds"] = interval_val
        except ValueError:
            pass

    # Compatibility prompt. Workflow actions are the execution source of truth.
    if "prompt" in form:
        prompt = form.get("prompt", "").strip()
        if prompt != (sched.get("prompt") or ""):
            updates["prompt"] = prompt

    review_instructions = form.get("review_instructions", "").strip()
    if review_instructions != (sched.get("review_instructions") or ""):
        updates["review_instructions"] = review_instructions

    for field_name, default in (
        ("request_template", {}),
        ("trigger_config", {}),
        ("actions", []),
    ):
        raw = form.get(f"{field_name}_json", "")
        if raw is None or str(raw).strip() == "":
            continue
        try:
            parsed = json.loads(str(raw))
        except json.JSONDecodeError as exc:
            raise HTTPException(422, f"Invalid JSON for {field_name}") from exc
        if not isinstance(parsed, type(default)):
            raise HTTPException(422, f"{field_name} must be a JSON {type(default).__name__}")
        current = sched.get(field_name)
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except (TypeError, ValueError):
                current = default
        if parsed != (current if current is not None else default):
            updates[field_name] = parsed

    # Model override (blank = use daemon default)
    if "model" in form:
        model = form.get("model", "").strip() or None
        if model != (sched.get("model") or None):
            updates["model"] = model

    if "reasoning_effort" in form:
        reasoning_effort = form.get("reasoning_effort", "").strip() or None
        if reasoning_effort != (sched.get("reasoning_effort") or None):
            updates["reasoning_effort"] = reasoning_effort

    effective_model = updates.get("model", sched.get("model"))
    effective_effort = updates.get("reasoning_effort", sched.get("reasoning_effort"))
    if effective_model:
        from lucent.model_registry import validate_model, validate_reasoning_effort

        model_error = validate_model(effective_model, require_tools=True)
        if model_error:
            raise HTTPException(422, model_error)
        effort_error = validate_reasoning_effort(effective_model, effective_effort)
        if effort_error:
            raise HTTPException(422, effort_error)
    elif effective_effort:
        raise HTTPException(422, "reasoning_effort requires model")

    if updates:
        await repo.update_schedule(schedule_id, org_id, **updates)

    base_path = "/workflows" if request.url.path.startswith("/workflows") else "/schedules"
    return RedirectResponse(url=f"{base_path}/{schedule_id}", status_code=303)
