"""Repository for scheduled tasks.

Supports one-time, interval, and cron schedules.
Each run creates a tracked request for full lineage.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from asyncpg import Pool


def _parse_cron(expression: str, after: datetime) -> datetime:
    """Calculate next run time from a cron expression (minute hour dom month dow).

    Supports: *, specific values, ranges (1-5), steps (*/15), and lists (1,3,5).
    """
    parts = expression.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: {expression}")

    def _expand(field: str, min_val: int, max_val: int) -> set[int]:
        values = set()
        for item in field.split(","):
            if item == "*":
                return set(range(min_val, max_val + 1))
            if "/" in item:
                base, step = item.split("/", 1)
                step = int(step)
                start = min_val if base == "*" else int(base)
                values.update(range(start, max_val + 1, step))
            elif "-" in item:
                lo, hi = item.split("-", 1)
                values.update(range(int(lo), int(hi) + 1))
            else:
                values.add(int(item))
        return values

    minutes = _expand(parts[0], 0, 59)
    hours = _expand(parts[1], 0, 23)
    doms = _expand(parts[2], 1, 31)
    months = _expand(parts[3], 1, 12)
    dows = _expand(parts[4], 0, 6)  # 0=Monday in isoweekday-1

    # Walk forward minute by minute from after, capped at 366 days
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + timedelta(days=366)

    while candidate < limit:
        if (
            candidate.month in months
            and candidate.day in doms
            and (candidate.weekday()) in dows  # Python weekday: 0=Monday
            and candidate.hour in hours
            and candidate.minute in minutes
        ):
            return candidate
        candidate += timedelta(minutes=1)

    raise ValueError(f"No next run found within 366 days for: {expression}")


class ScheduleRepository:
    """Manages scheduled tasks and their run history."""

    def __init__(self, pool: Pool):
        self.pool = pool

    # ── Schedules ─────────────────────────────────────────────────────────

    async def create_schedule(
        self,
        title: str,
        org_id: str,
        schedule_type: str = "once",
        description: str = "",
        agent_type: str = "code",
        model: str | None = None,
        task_template: dict | None = None,
        sandbox_config: dict | None = None,
        sandbox_template_id: str | None = None,
        cron_expression: str | None = None,
        interval_seconds: int | None = None,
        next_run_at: datetime | None = None,
        priority: str = "medium",
        timezone_str: str = "UTC",
        max_runs: int | None = None,
        expires_at: datetime | None = None,
        created_by: str | None = None,
        prompt: str = "",
    ) -> dict:
        # Calculate next_run_at if not provided
        if next_run_at is None:
            now = datetime.now(timezone.utc)
            if schedule_type == "once":
                next_run_at = now
            elif schedule_type == "interval" and interval_seconds:
                next_run_at = now + timedelta(seconds=interval_seconds)
            elif schedule_type == "cron" and cron_expression:
                next_run_at = _parse_cron(cron_expression, now)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO schedules
                   (title, organization_id, description, agent_type, model, task_template,
                    sandbox_config, sandbox_template_id, schedule_type, cron_expression,
                    interval_seconds, next_run_at, priority, timezone, max_runs,
                    expires_at, created_by, prompt)
                   VALUES ($1, $2::uuid, $3, $4, $5, $6::jsonb, $7::jsonb,
                           $8::uuid, $9, $10, $11, $12, $13, $14, $15,
                           $16, $17::uuid, $18)
                   RETURNING *""",
                title,
                org_id,
                description,
                agent_type,
                model,
                json.dumps(task_template or {}),
                json.dumps(sandbox_config) if sandbox_config else None,
                sandbox_template_id,
                schedule_type,
                cron_expression,
                interval_seconds,
                next_run_at,
                priority,
                timezone_str,
                max_runs,
                expires_at,
                created_by,
                prompt,
            )
            return dict(row)

    async def get_schedule(self, schedule_id: str, org_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM schedules WHERE id = $1::uuid AND organization_id = $2::uuid",
                schedule_id,
                org_id,
            )
            return dict(row) if row else None

    async def list_schedules(
        self,
        org_id: str,
        status: str | None = None,
        enabled: bool | None = None,
        limit: int = 100,
    ) -> list[dict]:
        async with self.pool.acquire() as conn:
            conditions = ["organization_id = $1::uuid"]
            params: list[Any] = [org_id]
            idx = 2

            if status:
                conditions.append(f"status = ${idx}")
                params.append(status)
                idx += 1
            if enabled is not None:
                conditions.append(f"enabled = ${idx}")
                params.append(enabled)
                idx += 1

            params.append(limit)
            rows = await conn.fetch(
                f"""SELECT * FROM schedules
                    WHERE {" AND ".join(conditions)}
                    ORDER BY
                        CASE WHEN enabled AND status = 'active' THEN 0 ELSE 1 END,
                        next_run_at ASC NULLS LAST
                    LIMIT ${idx}""",
                *params,
            )
            return [dict(r) for r in rows]

    async def update_schedule(self, schedule_id: str, org_id: str, **fields) -> dict | None:
        if not fields:
            return await self.get_schedule(schedule_id, org_id)

        sets = []
        params: list[Any] = []
        idx = 1
        for key, val in fields.items():
            if key == "task_template":
                sets.append(f"task_template = ${idx}::jsonb")
                params.append(json.dumps(val))
            elif key == "sandbox_template_id":
                sets.append(f"sandbox_template_id = ${idx}::uuid")
                params.append(val)
            else:
                sets.append(f"{key} = ${idx}")
                params.append(val)
            idx += 1

        sets.append(f"updated_at = ${idx}")
        params.append(datetime.now(timezone.utc))
        idx += 1

        params.extend([schedule_id, org_id])
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""UPDATE schedules SET {", ".join(sets)}
                    WHERE id = ${idx}::uuid AND organization_id = ${idx + 1}::uuid
                    RETURNING *""",
                *params,
            )
            return dict(row) if row else None

    async def toggle_schedule(self, schedule_id: str, org_id: str, enabled: bool) -> dict | None:
        return await self.update_schedule(schedule_id, org_id, enabled=enabled)

    async def delete_schedule(self, schedule_id: str, org_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM schedules WHERE id = $1::uuid AND organization_id = $2::uuid",
                schedule_id,
                org_id,
            )
            return result == "DELETE 1"

    # ── Due schedules (called by daemon) ──────────────────────────────────

    async def get_due_schedules(self, org_id: str | None = None) -> list[dict]:
        """Return schedules whose next_run_at is in the past and are active."""
        async with self.pool.acquire() as conn:
            conditions = [
                "enabled = true",
                "status = 'active'",
                "next_run_at <= now()",
            ]
            params: list[Any] = []
            idx = 1

            if org_id:
                conditions.append(f"organization_id = ${idx}::uuid")
                params.append(org_id)
                idx += 1

            rows = await conn.fetch(
                f"""SELECT * FROM schedules
                    WHERE {" AND ".join(conditions)}
                    ORDER BY
                        CASE priority
                            WHEN 'urgent' THEN 0
                            WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2
                            WHEN 'low' THEN 3
                            ELSE 4
                        END,
                        next_run_at ASC
                    LIMIT 10""",
                *params,
            )
            return [dict(r) for r in rows]

    async def mark_schedule_run(self, schedule_id: str, request_id: str | None = None) -> dict:
        """Record a run and advance the schedule's next_run_at."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                sched = await conn.fetchrow(
                    "SELECT * FROM schedules WHERE id = $1::uuid FOR UPDATE",
                    schedule_id,
                )
                if not sched:
                    raise ValueError(f"Schedule {schedule_id} not found")

                now = datetime.now(timezone.utc)
                new_run_count = (sched["run_count"] or 0) + 1

                # Create the run record
                run = await conn.fetchrow(
                    """INSERT INTO schedule_runs (schedule_id, request_id, status, started_at)
                       VALUES ($1::uuid, $2::uuid, 'running', $3)
                       RETURNING *""",
                    schedule_id,
                    request_id,
                    now,
                )

                # Calculate next run
                next_run = None
                new_status = sched["status"]

                if sched["schedule_type"] == "once":
                    new_status = "completed"
                elif sched["schedule_type"] == "interval":
                    next_run = now + timedelta(seconds=sched["interval_seconds"])
                elif sched["schedule_type"] == "cron" and sched["cron_expression"]:
                    try:
                        next_run = _parse_cron(sched["cron_expression"], now)
                    except ValueError:
                        new_status = "expired"

                # Check if we've hit max_runs
                if sched["max_runs"] and new_run_count >= sched["max_runs"]:
                    new_status = "completed"
                    next_run = None

                # Check expiration
                if sched["expires_at"] and next_run and next_run > sched["expires_at"]:
                    new_status = "expired"
                    next_run = None

                await conn.execute(
                    """UPDATE schedules SET
                       last_run_at = $2, run_count = $3,
                       next_run_at = $4, status = $5, updated_at = $2
                       WHERE id = $1::uuid""",
                    schedule_id,
                    now,
                    new_run_count,
                    next_run,
                    new_status,
                )

                return dict(run)

    async def complete_run(self, run_id: str, result: str | None = None) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE schedule_runs SET
                   status = 'completed', completed_at = now(), result = $2
                   WHERE id = $1::uuid RETURNING *""",
                run_id,
                result,
            )
            return dict(row) if row else None

    async def fail_run(self, run_id: str, error: str | None = None) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE schedule_runs SET
                   status = 'failed', completed_at = now(), error = $2
                   WHERE id = $1::uuid RETURNING *""",
                run_id,
                error,
            )
            return dict(row) if row else None

    # ── Run history ───────────────────────────────────────────────────────

    async def list_runs(self, schedule_id: str, limit: int = 20) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM schedule_runs
                   WHERE schedule_id = $1::uuid
                   ORDER BY created_at DESC LIMIT $2""",
                schedule_id,
                limit,
            )
            return [dict(r) for r in rows]

    async def get_schedule_with_runs(self, schedule_id: str, org_id: str) -> dict | None:
        """Load a schedule with its recent run history."""
        sched = await self.get_schedule(schedule_id, org_id)
        if not sched:
            return None
        sched["runs"] = await self.list_runs(schedule_id)
        return sched

    # ── Summary ───────────────────────────────────────────────────────────

    async def get_summary(self, org_id: str) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT
                   count(*) as total,
                   count(*) FILTER (WHERE enabled AND status = 'active') as active,
                   count(*) FILTER (WHERE NOT enabled OR status = 'paused') as paused,
                   count(*) FILTER (WHERE status = 'completed') as completed,
                   count(*) FILTER (
                       WHERE next_run_at <= now() AND enabled
                       AND status = 'active'
                   ) as due_now,
                   count(*) FILTER (WHERE schedule_type = 'once') as one_time,
                   count(*) FILTER (WHERE schedule_type = 'interval') as interval,
                   count(*) FILTER (WHERE schedule_type = 'cron') as cron
                   FROM schedules WHERE organization_id = $1::uuid""",
                org_id,
            )
            return dict(row)
