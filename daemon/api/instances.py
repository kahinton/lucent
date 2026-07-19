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
                    headers=runtime.API_HEADERS,
                )
                if response.status_code in (200, 201):
                    return response.json()
        except Exception as error:
            runtime.log(f"API register_instance failed: {error}", "WARN")
        return None

    @staticmethod
    async def heartbeat_instance(
        instance_id: str, metadata: dict | None = None
    ) -> dict | None:
        from daemon.runtime.module_proxy import runtime

        try:
            async with httpx.AsyncClient(timeout=InstanceAPI.API_TIMEOUT) as client:
                response = await client.post(
                    f"{runtime.API_BASE}/requests/instances/{instance_id}/heartbeat",
                    json={"metadata": metadata or {}},
                    headers=runtime.API_HEADERS,
                )
                if response.status_code in (200, 201):
                    return response.json()
        except Exception as error:
            runtime.log(f"API heartbeat_instance failed: {error}", "WARN")
        return None

    @staticmethod
    async def mark_instance_stopped(instance_id: str) -> dict | None:
        from daemon.runtime.module_proxy import runtime

        try:
            async with httpx.AsyncClient(timeout=InstanceAPI.API_TIMEOUT) as client:
                response = await client.post(
                    f"{runtime.API_BASE}/requests/instances/stop",
                    json={"instance_id": instance_id},
                    headers=runtime.API_HEADERS,
                )
                if response.status_code in (200, 201):
                    return response.json()
        except Exception as error:
            runtime.log(f"API mark_instance_stopped failed: {error}", "WARN")
        return None