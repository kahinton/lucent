"""Server-side built-in system schedule runner.

Runs critical maintenance schedules directly in the API process so they still
execute when the daemon is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from typing import Any

from lucent.db import get_pool
from lucent.db.memory import MemoryRepository
from lucent.db.requests import RequestRepository
from lucent.db.schedules import ScheduleRepository
from lucent.logging import get_logger
from lucent.settings import daemon_vitality_scoring_minutes

logger = get_logger("api.system_schedules")

STALE_TASK_REAPER_TITLE = "Stale Task Reaper"
VITALITY_SCORING_TITLE = "Memory Vitality Scoring"
STALE_TASK_REAPER_INTERVAL_SECONDS = int(
    os.environ.get("LUCENT_STALE_REAPER_INTERVAL_SECONDS", "120")
)
STALE_TASK_REAPER_STALE_MINUTES = int(
    os.environ.get("LUCENT_STALE_HEARTBEAT_MINUTES", "30")
)
SYSTEM_SCHEDULE_CHECK_SECONDS = int(
    os.environ.get("LUCENT_SYSTEM_SCHEDULE_CHECK_SECONDS", "30")
)
SYSTEM_SCHEDULE_STARTUP_DELAY_SECONDS = int(
    os.environ.get("LUCENT_SYSTEM_SCHEDULE_STARTUP_DELAY_SECONDS", "15")
)
VITALITY_SCORING_INTERVAL_SECONDS = daemon_vitality_scoring_minutes() * 60
VITALITY_SCORING_BATCH_SIZE = int(
    os.environ.get("LUCENT_VITALITY_SCORING_BATCH_SIZE", "500")
)

STALE_TASK_REAPER_ACTION = {
    "action_type": "server_function",
    "title": "Release stale task claims",
    "description": (
        "Runs directly inside the Lucent API process. It checks for expired "
        "task claims or dead daemon owners, releases eligible claims, and records "
        "the result in schedule_runs. It does not dispatch an agent task."
    ),
    "function": "release_stale_tasks",
    "module": "lucent.api.system_schedules",
    "execution_boundary": "api_process",
    "preflight": "stale_task_reaper_has_work",
    "sequence_order": 0,
}

_system_schedule_runner_task: asyncio.Task | None = None
_last_vitality_scoring_at_by_org: dict[str, datetime] = {}


async def retire_vitality_scoring_workflow_schedules() -> int:
    """Remove retired built-in vitality workflows from the visible workflow table."""
    pool = await get_pool()
    if pool is None:
        return 0

    async with pool.acquire() as conn:
        schedule_ids = [
            str(row["id"])
            for row in await conn.fetch(
                """SELECT id FROM schedules
                   WHERE title = $1
                     AND is_system = true""",
                VITALITY_SCORING_TITLE,
            )
        ]
        if not schedule_ids:
            return 0
        await conn.execute(
            "DELETE FROM schedule_runs WHERE schedule_id = ANY($1::uuid[])",
            schedule_ids,
        )
        deleted = await conn.fetchval(
            """WITH deleted AS (
                   DELETE FROM schedules
                   WHERE id = ANY($1::uuid[])
                   RETURNING id
               )
               SELECT COUNT(*) FROM deleted""",
            schedule_ids,
        )

    count = int(deleted or 0)
    if count:
        logger.info("Retired %d Memory Vitality Scoring workflow schedule(s)", count)
    return count


async def ensure_server_system_schedules() -> int:
    """Ensure built-in server-side schedules exist for each organization."""
    pool = await get_pool()
    if pool is None:
        return 0

    await retire_vitality_scoring_workflow_schedules()

    async with pool.acquire() as conn:
        repo = ScheduleRepository(pool)
        created = 0
        rows = await conn.fetch(
            """
            SELECT o.id::text AS organization_id,
                   u.id::text AS daemon_user_id
            FROM organizations o
            LEFT JOIN users u
              ON u.organization_id = o.id
             AND (u.external_id = 'daemon-service'
                  OR u.external_id = 'daemon-service:' || o.id::text)
             AND u.is_active = true
            """
        )

    schedule_specs = [
        {
            "title": STALE_TASK_REAPER_TITLE,
            "description": (
                "Server-side stale-claim reaper. Releases expired task claims "
                "without depending on daemon availability. Short-circuits with "
                "schedule.skipped when there are no stale claims to release."
            ),
            "agent_type": "lucent",
            "interval_seconds": STALE_TASK_REAPER_INTERVAL_SECONDS,
            "preflight": "stale_task_reaper_has_work",
            "actions": [STALE_TASK_REAPER_ACTION],
            "review_instructions": (
                "No model review applies. Verify by checking schedule_runs.result "
                "and task claim state if this workflow reports released tasks."
            ),
        },
    ]

    for row in rows:
        for spec in schedule_specs:
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    """SELECT 1 FROM schedules
                       WHERE title = $1
                         AND organization_id = $2::uuid
                         AND is_system = true""",
                    spec["title"],
                    row["organization_id"],
                )
            sched = await repo.ensure_system_schedule(
                title=spec["title"],
                org_id=row["organization_id"],
                description=spec["description"],
                agent_type=spec["agent_type"],
                schedule_type="interval",
                interval_seconds=spec["interval_seconds"],
                priority="low",
                prompt="",
                trigger_config={
                    "schedule_type": "interval",
                    "interval_seconds": spec["interval_seconds"],
                    "timezone": "UTC",
                    "execution_mode": "server_side",
                    "runner": "api_process",
                    "preflight": spec["preflight"],
                },
                request_template={
                    "title_prefix": "[Server Workflow]",
                    "title": spec["title"],
                    "description": (
                        "Server-side maintenance workflow. No request is created; "
                        "results are recorded on schedule_runs."
                    ),
                },
                actions=spec["actions"],
                review_instructions=spec["review_instructions"],
                created_by=row["daemon_user_id"],
            )
            if not existing and sched:
                created += 1

    if created:
        logger.info("Seeded %d server system schedule(s)", created)
    return created


async def execute_stale_task_reaper_schedule(
    sched: dict[str, Any],
    *,
    force: bool = False,
    advance_schedule: bool = True,
) -> dict[str, Any] | None:
    """Execute one Stale Task Reaper workflow row.

    This is the server-function implementation behind the workflow action. It
    intentionally does not create a request or task; its durable artifact is the
    schedule run record.
    """
    pool = await get_pool()
    if pool is None:
        return None

    sched_repo = ScheduleRepository(pool)
    req_repo = RequestRepository(pool)
    schedule_id = str(sched["id"])
    org_id = str(sched["organization_id"])

    run = await sched_repo.mark_schedule_run(
        schedule_id,
        force=force,
        advance_schedule=advance_schedule,
    )
    if not run:
        return {"schedule": sched, "already_fired": True}

    try:
        if not await req_repo.stale_task_reaper_has_work(
            stale_minutes=STALE_TASK_REAPER_STALE_MINUTES,
            org_id=org_id,
        ):
            skip_event = {
                "event_type": "schedule.skipped",
                "schedule_id": schedule_id,
                "schedule_name": STALE_TASK_REAPER_TITLE,
                "reason": "no_stale_tasks",
                "candidate_count": 0,
            }
            await sched_repo.complete_run(
                str(run["id"]),
                result=json.dumps(skip_event),
            )
            logger.info(json.dumps(skip_event, sort_keys=True))
            return {"schedule": sched, "run": run, "skipped": True, "event": skip_event}

        released = await req_repo.release_stale_tasks(
            stale_minutes=STALE_TASK_REAPER_STALE_MINUTES,
            org_id=org_id,
        )
        await sched_repo.complete_run(
            str(run["id"]),
            result=f"released={released}",
        )
        if released:
            logger.info(
                "Server stale-task reaper released %d task(s) for org=%s",
                released,
                org_id[:8],
            )
        return {"schedule": sched, "run": run, "released": released}
    except Exception as exc:
        await sched_repo.fail_run(str(run["id"]), error=str(exc)[:1000])
        logger.exception("Server stale-task reaper failed for schedule %s", schedule_id)
        raise


async def run_vitality_scoring_background_once(*, force: bool = False) -> int:
    """Run deterministic vitality scoring for due organizations without a workflow row."""
    pool = await get_pool()
    if pool is None:
        return 0

    sched_repo = ScheduleRepository(pool)
    memory_repo = MemoryRepository(pool)
    now = datetime.now(UTC)
    executed = 0

    async with pool.acquire() as conn:
        org_ids = [str(row["id"]) for row in await conn.fetch("SELECT id FROM organizations")]

    for org_id in org_ids:
        last_run_at = _last_vitality_scoring_at_by_org.get(org_id)
        if not force and last_run_at is not None:
            elapsed = (now - last_run_at).total_seconds()
            if elapsed < VITALITY_SCORING_INTERVAL_SECONDS:
                continue

        try:
            if not await sched_repo.memory_vitality_scoring_has_work(org_id):
                _last_vitality_scoring_at_by_org[org_id] = now
                continue

            score_result = await memory_repo.compute_vitality_scores(
                batch_size=VITALITY_SCORING_BATCH_SIZE,
                organization_id=org_id,
            )
            _last_vitality_scoring_at_by_org[org_id] = now
            executed += 1
            logger.info(
                json.dumps(
                    {
                        "event_type": "background.vitality_scoring.completed",
                        "organization_id": org_id,
                        "processed": score_result.get("processed", 0),
                        "updated": score_result.get("updated", 0),
                        "stage_transitions": score_result.get("stage_transitions", 0),
                        "computed_at": score_result.get("computed_at").isoformat()
                        if score_result.get("computed_at")
                        else None,
                    },
                    sort_keys=True,
                )
            )
        except Exception:
            logger.exception("Background vitality scoring failed for org=%s", org_id)

    return executed


async def run_server_system_schedules_once() -> int:
    """Execute due server-side built-in schedules once."""
    pool = await get_pool()
    if pool is None:
        return 0

    sched_repo = ScheduleRepository(pool)

    due = await sched_repo.get_due_schedules()
    executed = 0

    for sched in due:
        if not sched.get("is_system"):
            continue
        if sched.get("title") != STALE_TASK_REAPER_TITLE:
            continue

        try:
            result = await execute_stale_task_reaper_schedule(sched)
        except Exception:
            executed += 1
            continue
        if result and result.get("already_fired"):
            continue
        executed += 1

    return executed


async def _server_system_schedule_loop() -> None:
    """Poll and execute due server-side built-in schedules."""
    await asyncio.sleep(SYSTEM_SCHEDULE_STARTUP_DELAY_SECONDS)
    logger.info(
        "Server system schedule runner started (check=%ss, reaper interval=%ss)",
        SYSTEM_SCHEDULE_CHECK_SECONDS,
        STALE_TASK_REAPER_INTERVAL_SECONDS,
    )

    while True:
        try:
            await run_server_system_schedules_once()
            await run_vitality_scoring_background_once()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Server system schedule loop error")
        await asyncio.sleep(SYSTEM_SCHEDULE_CHECK_SECONDS)


async def start_server_system_schedule_runner() -> None:
    """Seed schedules and start the server-side schedule runner."""
    global _system_schedule_runner_task

    await ensure_server_system_schedules()

    if _system_schedule_runner_task is None or _system_schedule_runner_task.done():
        _system_schedule_runner_task = asyncio.create_task(
            _server_system_schedule_loop(),
            name="server-system-schedule-runner",
        )


async def stop_server_system_schedule_runner() -> None:
    """Stop the server-side schedule runner."""
    global _system_schedule_runner_task

    if _system_schedule_runner_task and not _system_schedule_runner_task.done():
        _system_schedule_runner_task.cancel()
        try:
            await _system_schedule_runner_task
        except asyncio.CancelledError:
            pass
    _system_schedule_runner_task = None
