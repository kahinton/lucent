"""REST operations for daemon instance registration and liveness."""

from __future__ import annotations

import httpx


class InstanceAPI:
    """Daemon instance registration, heartbeat, and shutdown operations."""

    API_TIMEOUT = 15

    @staticmethod
    async def register_instance(
        instance_id: str,
        *,
        hostname: str | None = None,
        pid: int | None = None,
        roles: list[str] | None = None,
        metadata: dict | None = None,
        organization_id: str | None = None,
    ) -> dict | None:
        from daemon.runtime.module_proxy import runtime

        try:
            async with httpx.AsyncClient(timeout=InstanceAPI.API_TIMEOUT) as client:
                response = await client.post(
                    f"{runtime.API_BASE}/requests/instances/register",
                    json={
                        "instance_id": instance_id,
                        "hostname": hostname,
                        "pid": pid,
                        "roles": roles or [],
                        "metadata": metadata or {},
                    },
                    headers=runtime.api_headers_for(organization_id),
                )
                if response.status_code in (200, 201):
                    return response.json()
        except Exception as error:
            runtime.log(f"API register_instance failed: {error}", "WARN")
        return None

    @staticmethod
    async def heartbeat_instance(
        instance_id: str,
        metadata: dict | None = None,
        *,
        active_task_ids: list[str] | None = None,
        organization_id: str | None = None,
    ) -> dict | None:
        from daemon.runtime.module_proxy import runtime

        try:
            async with httpx.AsyncClient(timeout=InstanceAPI.API_TIMEOUT) as client:
                response = await client.post(
                    f"{runtime.API_BASE}/requests/instances/{instance_id}/heartbeat",
                    # Scoping the lease renewal to live work is what lets a lost
                    # write-back become recoverable: the orphaned claim stops
                    # being renewed and the reaper can requeue it.
                    json={
                        "metadata": metadata or {},
                        **(
                            {"active_task_ids": sorted(active_task_ids)}
                            if active_task_ids is not None
                            else {}
                        ),
                    },
                    headers=runtime.api_headers_for(organization_id),
                )
                if response.status_code in (200, 201):
                    return response.json()
        except Exception as error:
            runtime.log(f"API heartbeat_instance failed: {error}", "WARN")
        return None

    @staticmethod
    async def mark_instance_stopped(
        instance_id: str, *, organization_id: str | None = None
    ) -> dict | None:
        from daemon.runtime.module_proxy import runtime

        try:
            async with httpx.AsyncClient(timeout=InstanceAPI.API_TIMEOUT) as client:
                response = await client.post(
                    f"{runtime.API_BASE}/requests/instances/stop",
                    json={"instance_id": instance_id},
                    headers=runtime.api_headers_for(organization_id),
                )
                if response.status_code in (200, 201):
                    return response.json()
        except Exception as error:
            runtime.log(f"API mark_instance_stopped failed: {error}", "WARN")
        return None
