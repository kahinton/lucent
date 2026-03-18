"""Schedule management routes."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.db import get_pool

from ._shared import _check_csrf, get_user_context, templates

router = APIRouter()


# =============================================================================
# Schedules
# =============================================================================


@router.get("/schedules", response_class=HTMLResponse)
async def schedules_list(request: Request, status: str | None = None, enabled: str | None = None):
    """List all scheduled tasks with filtering."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)

    org_id = str(user.organization_id)
    enabled_filter = True if enabled == "true" else (False if enabled == "false" else None)
    schedules = await repo.list_schedules(org_id, status=status, enabled=enabled_filter)
    summary = await repo.get_summary(org_id)

    return templates.TemplateResponse(
        request,
        "schedules_list.html",
        {
            "user": user,
            "schedules": schedules,
            "summary": summary,
            "filter_status": status,
            "filter_enabled": enabled,
        },
    )


@router.get("/schedules/{schedule_id}", response_class=HTMLResponse)
async def schedule_detail(request: Request, schedule_id: str):
    """Schedule detail page with run history."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)

    sched = await repo.get_schedule_with_runs(schedule_id, str(user.organization_id))
    if not sched:
        raise HTTPException(404, "Schedule not found")

    def_repo = DefinitionRepository(pool)
    active_agents = await def_repo.list_agents(str(user.organization_id), status="active")

    # Resolve sandbox template name if linked
    sandbox_template = None
    if sched.get("sandbox_template_id"):
        from lucent.db.sandbox_template import SandboxTemplateRepository

        tmpl_repo = SandboxTemplateRepository(pool)
        sandbox_template = await tmpl_repo.get(
            str(sched["sandbox_template_id"]), str(user.organization_id)
        )

    return templates.TemplateResponse(
        request,
        "schedule_detail.html",
        {
            "user": user,
            "sched": sched,
            "active_agents": active_agents,
            "sandbox_template": sandbox_template,
        },
    )


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
    if "/schedules/" in referer and schedule_id in referer:
        return RedirectResponse(url=f"/schedules/{schedule_id}", status_code=303)
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/delete", response_class=HTMLResponse)
async def schedule_delete(request: Request, schedule_id: str):
    """Delete a schedule."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    org_id = str(user.organization_id)

    deleted = await repo.delete_schedule(schedule_id, org_id)
    if not deleted:
        raise HTTPException(404, "Schedule not found")

    return RedirectResponse(url="/schedules", status_code=303)


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

    # Prompt (free-form text sent to the agent)
    prompt = form.get("prompt", "").strip()
    if prompt != (sched.get("prompt") or ""):
        updates["prompt"] = prompt

    if updates:
        await repo.update_schedule(schedule_id, org_id, **updates)

    return RedirectResponse(url=f"/schedules/{schedule_id}", status_code=303)
