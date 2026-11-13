"""Heartbeat and due-schedule coordination for the daemon runtime."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone

import httpx


class SchedulingMixin:
    """Maintains instance liveness and fires due schedules."""

    async def _update_heartbeat(self, organization_id: str | None = None):
        from daemon.runtime.module_proxy import runtime

        return await runtime.RequestAPI.heartbeat_instance(
            self.instance_id,
            metadata={
                "cycle_count": self.cycle_count,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": runtime.MODEL,
                "roles": sorted(self.roles),
                "max_sessions": runtime.MAX_CONCURRENT_SESSIONS,
            },
            # Only the tasks this instance still believes it is working on.
            # A task that finished but whose result failed to persist has been
            # removed from this map, so its claim stops being renewed and the
            # stale-task reaper can recover it. Without this the row looked
            # "busy" forever and the work was silently orphaned.
            active_task_ids=sorted(self._active_task_sessions),
            organization_id=organization_id,
        )

    async def _heartbeat_once(self) -> bool:
        """Heartbeat for every org this process serves, on that org's own key.

        Registration and heartbeat rows live per org, so a controller must do
        this once per served org. It also keeps each org's credential honest:
        an expired or revoked key is discovered by its own org's heartbeat and
        rotated for that org alone.
        """
        from daemon.runtime.module_proxy import runtime

        org_ids = await self._scheduled_org_ids()
        if not org_ids:
            return False
        healthy = True
        for org_id in org_ids:
            if await self._heartbeat_for_org(org_id):
                continue
            healthy = False
            runtime.log(
                f"Heartbeat failed for org {org_id[:8]} after key refresh",
                "WARN",
            )
        return healthy

    async def _heartbeat_for_org(self, organization_id: str) -> bool:
        from daemon.runtime.module_proxy import runtime

        if not await runtime._verify_and_provision_key(
            self.instance_id, organization_id=organization_id
        ):
            return False
        if await self._update_heartbeat(organization_id) is not None:
            return True

        runtime.log(
            f"Heartbeat failed for org {organization_id[:8]}; "
            "refreshing that org's API key and retrying",
            "WARN",
        )
        if not await runtime._verify_and_provision_key(
            self.instance_id, organization_id=organization_id
        ):
            return False
        return await self._update_heartbeat(organization_id) is not None

    async def _scheduled_org_ids(self) -> list[str]:
        """Organizations this process may fire schedules for.

        Each org is polled with its OWN credential, so scheduling never widens
        one org's key into another's schedule list. A worker schedules only for
        its pinned org; the controller schedules for every org it serves.
        """
        from daemon.runtime.module_proxy import runtime

        if runtime.DAEMON_WORKER and runtime.DAEMON_ORG:
            resolved = await runtime._resolve_daemon_org_conn()
            return [resolved[0]] if resolved else []
        return sorted(runtime.API_HEADERS_BY_ORG)

    async def _check_due_schedules(self):
        from daemon.runtime.module_proxy import runtime

        try:
            for org_id in await self._scheduled_org_ids():
                await self._check_due_schedules_for_org(org_id)
        except Exception as error:
            runtime.log(f"Error checking due schedules: {error}", "WARN")

    async def _check_due_schedules_for_org(self, org_id: str):
        from daemon.runtime.module_proxy import runtime
        from lucent.api.system_schedules import STALE_TASK_REAPER_TITLE

        try:
            # The org's own credential: /schedules/due is scoped to the caller's
            # organization, so this is what keeps orgs separate.
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{runtime.API_BASE}/schedules/due",
                    headers=runtime.api_headers_for(org_id),
                )
                if response.status_code != 200:
                    return
                due = response.json()
                if not due:
                    return
            runtime.log(
                f"Found {len(due)} due schedule(s) for org {org_id[:8]}"
            )
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
                                headers=runtime.api_headers_for(org_id),
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
