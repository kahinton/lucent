"""API router for scheduled tasks."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from lucent.api.deps import get_current_user, get_pool

router = APIRouter(prefix="/schedules", tags=["schedules"])


# ── Models ────────────────────────────────────────────────────────────────


class ScheduleCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str = ""
    agent_type: str = "code"
    task_template: dict | None = None
    sandbox_template_id: str | None = None  # Reference a saved sandbox template
    sandbox_config: dict | None = None  # Or inline sandbox config
    schedule_type: str = Field(default="once", pattern=r"^(once|interval|cron)$")
    cron_expression: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)  # min 1 minute
    next_run_at: datetime | None = None
    priority: str = Field(default="medium", pattern=r"^(low|medium|high|urgent)$")
    timezone: str = "UTC"
    max_runs: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None


class ScheduleUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    agent_type: str | None = None
    task_template: dict | None = None
    sandbox_template_id: str | None = None
    sandbox_config: dict | None = None
    cron_expression: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)
    next_run_at: datetime | None = None
    priority: str | None = Field(default=None, pattern=r"^(low|medium|high|urgent)$")
    max_runs: int | None = None
    expires_at: datetime | None = None


class ScheduleToggle(BaseModel):
    enabled: bool


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("")
async def create_schedule(
    body: ScheduleCreate, user=Depends(get_current_user), pool=Depends(get_pool)
):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    return await repo.create_schedule(
        title=body.title,
        org_id=str(user.organization_id),
        schedule_type=body.schedule_type,
        description=body.description,
        agent_type=body.agent_type,
        task_template=body.task_template,
        sandbox_config=body.sandbox_config,
        sandbox_template_id=body.sandbox_template_id,
        cron_expression=body.cron_expression,
        interval_seconds=body.interval_seconds,
        next_run_at=body.next_run_at,
        priority=body.priority,
        timezone_str=body.timezone,
        max_runs=body.max_runs,
        expires_at=body.expires_at,
        created_by=str(user.id),
    )


@router.get("")
async def list_schedules(
    status: str | None = None,
    enabled: bool | None = None,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    return await repo.list_schedules(str(user.organization_id), status=status, enabled=enabled)


@router.get("/summary")
async def schedule_summary(user=Depends(get_current_user), pool=Depends(get_pool)):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    return await repo.get_summary(str(user.organization_id))


@router.get("/due")
async def get_due_schedules(user=Depends(get_current_user), pool=Depends(get_pool)):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    return await repo.get_due_schedules(str(user.organization_id))


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str, user=Depends(get_current_user), pool=Depends(get_pool)):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    result = await repo.get_schedule_with_runs(schedule_id, str(user.organization_id))
    if not result:
        raise HTTPException(404, "Schedule not found")
    return result


@router.put("/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    body: ScheduleUpdate,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if "timezone" in fields:
        fields["timezone_str"] = fields.pop("timezone")
    result = await repo.update_schedule(schedule_id, str(user.organization_id), **fields)
    if not result:
        raise HTTPException(404, "Schedule not found")
    return result


@router.post("/{schedule_id}/toggle")
async def toggle_schedule(
    schedule_id: str,
    body: ScheduleToggle,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    result = await repo.toggle_schedule(schedule_id, str(user.organization_id), body.enabled)
    if not result:
        raise HTTPException(404, "Schedule not found")
    return result


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: str, user=Depends(get_current_user), pool=Depends(get_pool)):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    ok = await repo.delete_schedule(schedule_id, str(user.organization_id))
    if not ok:
        raise HTTPException(404, "Schedule not found")
    return {"deleted": True}


@router.get("/{schedule_id}/runs")
async def list_runs(schedule_id: str, user=Depends(get_current_user), pool=Depends(get_pool)):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    # Verify ownership
    sched = await repo.get_schedule(schedule_id, str(user.organization_id))
    if not sched:
        raise HTTPException(404, "Schedule not found")
    return await repo.list_runs(schedule_id)


@router.post("/{schedule_id}/trigger")
async def trigger_now(schedule_id: str, user=Depends(get_current_user), pool=Depends(get_pool)):
    """Manually trigger a schedule immediately, regardless of next_run_at."""
    from lucent.db.requests import RequestRepository
    from lucent.db.schedules import ScheduleRepository

    sched_repo = ScheduleRepository(pool)
    req_repo = RequestRepository(pool)

    sched = await sched_repo.get_schedule(schedule_id, str(user.organization_id))
    if not sched:
        raise HTTPException(404, "Schedule not found")

    # Create a request from the schedule
    template = sched.get("task_template") or {}
    prompt = sched.get("prompt") or ""
    req = await req_repo.create_request(
        title=f"[Scheduled] {sched['title']}",
        org_id=str(user.organization_id),
        description=sched.get("description", ""),
        source="schedule",
        priority=sched.get("priority", "medium"),
        created_by=str(user.id),
    )

    # Create the task — use prompt as description if set, else fall back to template/description
    task_description = prompt or template.get("description", sched.get("description", ""))
    await req_repo.create_task(
        request_id=str(req["id"]),
        title=template.get("title", sched["title"]),
        description=task_description,
        agent_type=sched.get("agent_type", "code"),
        priority=sched.get("priority", "medium"),
        model=sched.get("model"),
        sandbox_template_id=str(sched["sandbox_template_id"])
        if sched.get("sandbox_template_id")
        else None,
        sandbox_config=sched.get("sandbox_config"),
        org_id=str(user.organization_id),
    )

    # Record the run
    run = await sched_repo.mark_schedule_run(schedule_id, str(req["id"]))

    return {"schedule": sched, "request": req, "run": run}
