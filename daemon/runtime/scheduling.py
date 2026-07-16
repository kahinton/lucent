"""Heartbeat and due-schedule coordination for the daemon runtime."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone

import httpx


class SchedulingMixin:
    """Maintains instance liveness and fires due schedules."""

    async def _update_heartbeat(self):
        from daemon.runtime.module_proxy import runtime

        await runtime.RequestAPI.heartbeat_instance(
            self.instance_id,
            metadata={
                "cycle_count": self.cycle_count,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": runtime.MODEL,
                "roles": sorted(self.roles),
                "max_sessions": runtime.MAX_CONCURRENT_SESSIONS,
            },
        )

    async def _check_due_schedules(self):
        from daemon.runtime.module_proxy import runtime
        from lucent.api.system_schedules import STALE_TASK_REAPER_TITLE

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{runtime.API_BASE}/schedules/due", headers=runtime.API_HEADERS
                )
                if response.status_code != 200:
                    return
                due = response.json()
                if not due:
                    return
            runtime.log(f"Found {len(due)} due schedules")
            span = (
                self._tracer.start_as_current_span(
                    "daemon.scheduler.check",
                    attributes={"daemon.scheduler.due_count": len(due)},
                )
                if self._tracer
                else contextlib.nullcontext()
            )
            with span:
                for schedule in due:
                    schedule_id = str(schedule["id"])
                    title = schedule.get("title", "Scheduled task")
                    if schedule.get("is_system") and title == STALE_TASK_REAPER_TITLE:
                        continue
                    try:
                        async with httpx.AsyncClient(timeout=15) as client:
                            response = await client.post(
                                f"{runtime.API_BASE}/schedules/{schedule_id}/trigger",
                                headers=runtime.API_HEADERS,
                            )
                        if response.status_code in (200, 201):
                            data = response.json()
                            if data.get("already_fired"):
                                runtime.log(
                                    f"Schedule {schedule_id[:8]} '{title}' already fired, skipping"
                                )
                            else:
                                request_id = data.get("request", {}).get("id", "?")
                                runtime.log(
                                    f"Triggered schedule {schedule_id[:8]} '{title}' "
                                    f"→ request {str(request_id)[:8]}"
                                )
                        else:
                            runtime.log(
                                f"Failed to trigger schedule {schedule_id[:8]}: "
                                f"{response.status_code}",
                                "WARN",
                            )
                    except Exception as error:
                        runtime.log(
                            f"Error triggering schedule {schedule_id[:8]}: {error}",
                            "WARN",
                        )
        except Exception as error:
            runtime.log(f"Error checking due schedules: {error}", "WARN")