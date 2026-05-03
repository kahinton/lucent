"""Server-side built-in system schedule runner.

Runs critical maintenance schedules directly in the API process so they still
execute when the daemon is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import os

from lucent.db import get_pool
from lucent.db.requests import RequestRepository
from lucent.db.schedules import ScheduleRepository
from lucent.logging import get_logger

logger = get_logger("api.system_schedules")

STALE_TASK_REAPER_TITLE = "Stale Task Reaper"
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

_system_schedule_runner_task: asyncio.Task | None = None


async def ensure_server_system_schedules() -> int:
    """Ensure built-in server-side schedules exist for each organization."""
    pool = await get_pool()
    if pool is None:
        return 0

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
             AND u.external_id = 'daemon-service'
             AND u.is_active = true
            """
        )

    for row in rows:
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                """SELECT 1 FROM schedules
                   WHERE title = $1
                     AND organization_id = $2::uuid
                     AND is_system = true""",
                STALE_TASK_REAPER_TITLE,
                row["organization_id"],
            )
        sched = await repo.ensure_system_schedule(
            title=STALE_TASK_REAPER_TITLE,
            org_id=row["organization_id"],
            description=(
                "Server-side stale-claim reaper. Releases expired task claims "
                "without depending on daemon availability. Short-circuits with "
                "schedule.skipped when there are no stale claims to release."
            ),
            agent_type="system",
            schedule_type="interval",
            interval_seconds=STALE_TASK_REAPER_INTERVAL_SECONDS,
            priority="low",
            prompt="Built-in server schedule: invoke release_stale_tasks directly.",
            created_by=row["daemon_user_id"],
        )
        if not existing and sched:
            created += 1

    if created:
        logger.info("Seeded %d server system schedule(s)", created)
    return created


async def run_server_system_schedules_once() -> int:
    """Execute due server-side built-in schedules once."""
    pool = await get_pool()
    if pool is None:
        return 0

    sched_repo = ScheduleRepository(pool)
    req_repo = RequestRepository(pool)

    due = await sched_repo.get_due_schedules()
    executed = 0

    for sched in due:
        if not sched.get("is_system"):
            continue
        if sched.get("title") != STALE_TASK_REAPER_TITLE:
            continue

        schedule_id = str(sched["id"])
        org_id = str(sched["organization_id"])

        run = await sched_repo.mark_schedule_run(schedule_id)
        if not run:
            continue

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
                executed += 1
                continue

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
        except Exception as exc:
            await sched_repo.fail_run(str(run["id"]), error=str(exc)[:1000])
            logger.exception("Server stale-task reaper failed for schedule %s", schedule_id)
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
