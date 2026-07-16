"""REST operations for daemon task execution and lifecycle state."""

from __future__ import annotations

import httpx


class TaskAPI:
    """Task claim, execution, completion, events, and queue operations."""

    API_TIMEOUT = 15

    @staticmethod
    async def _post(path: str, body: dict, operation: str) -> dict | None:
        from daemon.runtime.module_proxy import runtime

        try:
            async with httpx.AsyncClient(timeout=TaskAPI.API_TIMEOUT) as client:
                response = await client.post(
                    f"{runtime.API_BASE}{path}", json=body, headers=runtime.API_HEADERS
                )
                if response.status_code in (200, 201):
                    return response.json()
                runtime.log(
                    f"API {operation} returned {response.status_code}: {response.text[:200]}",
                    "WARN",
                )
        except Exception as error:
            runtime.log(f"API {operation} failed: {error}", "WARN")
        return None

    @staticmethod
    async def claim_task(task_id: str, instance_id: str) -> dict | None:
        return await TaskAPI._post(
            f"/requests/tasks/{task_id}/claim", {"instance_id": instance_id}, "claim_task"
        )

    @staticmethod
    async def update_task_model(task_id: str, model: str) -> dict | None:
        return await TaskAPI.update_task_model_settings(task_id, model=model)

    @staticmethod
    async def update_task_model_settings(
        task_id: str, *, model: str, reasoning_effort: str | None = None
    ) -> dict | None:
        return await TaskAPI._post(
            f"/requests/tasks/{task_id}/model",
            {"model": model, "reasoning_effort": reasoning_effort},
            "update_task_model",
        )

    @staticmethod
    async def start_task(task_id: str, instance_id: str | None = None) -> dict | None:
        return await TaskAPI._post(
            f"/requests/tasks/{task_id}/start",
            {"instance_id": instance_id} if instance_id else {},
            "start_task",
        )

    @staticmethod
    async def complete_task(
        task_id: str,
        result: str,
        instance_id: str | None = None,
        result_structured: dict | None = None,
        result_summary: str | None = None,
        validation_status: str = "not_applicable",
        validation_errors: list | None = None,
    ) -> dict | None:
        return await TaskAPI._post(
            f"/requests/tasks/{task_id}/complete",
            {
                "result": result[:50000],
                "instance_id": instance_id,
                "result_structured": result_structured,
                "result_summary": result_summary[:2000] if result_summary else None,
                "validation_status": validation_status,
                "validation_errors": validation_errors,
            },
            "complete_task",
        )

    @staticmethod
    async def fail_task(
        task_id: str,
        error: str,
        instance_id: str | None = None,
        result: str | None = None,
    ) -> dict | None:
        body = {"error": error[:10000], "instance_id": instance_id}
        if result is not None:
            body["result"] = result[:200000]
        return await TaskAPI._post(f"/requests/tasks/{task_id}/fail", body, "fail_task")

    @staticmethod
    async def add_event(
        task_id: str,
        event_type: str,
        detail: str | None = None,
        metadata: dict | None = None,
    ) -> dict | None:
        body = {"event_type": event_type}
        if detail:
            body["detail"] = detail
        if metadata:
            body["metadata"] = metadata
        return await TaskAPI._post(
            f"/requests/tasks/{task_id}/events", body, "add_event"
        )

    @staticmethod
    async def link_memory(
        task_id: str, memory_id: str, relation: str = "created"
    ) -> None:
        await TaskAPI._post(
            f"/requests/tasks/{task_id}/memories",
            {"memory_id": memory_id, "relation": relation},
            "link_memory",
        )

    @staticmethod
    async def get_pending_tasks() -> list[dict]:
        from daemon.runtime.module_proxy import runtime

        try:
            async with httpx.AsyncClient(timeout=TaskAPI.API_TIMEOUT) as client:
                response = await client.get(
                    f"{runtime.API_BASE}/requests/queue/pending",
                    headers=runtime.API_HEADERS,
                )
                if response.status_code == 200:
                    data = response.json()
                    return data.get("items", data) if isinstance(data, dict) else data
        except Exception as error:
            runtime.log(f"API get_pending_tasks failed: {error}", "WARN")
        return []