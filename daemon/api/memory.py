"""Direct daemon client for memory REST operations."""

from __future__ import annotations

import httpx


class MemoryAPI:
    """Direct REST API client for memory operations that do not need an LLM."""

    API_TIMEOUT = 15

    @staticmethod
    async def search(
        query: str,
        tags: list[str] | None = None,
        type: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search memories through the server API."""
        from daemon.runtime.module_proxy import runtime

        params = {"query": query, "limit": limit}
        if tags:
            params["tags"] = tags
        if type:
            params["type"] = type
        try:
            async with httpx.AsyncClient(timeout=MemoryAPI.API_TIMEOUT) as client:
                response = await client.post(
                    f"{runtime.API_BASE}/search",
                    json=params,
                    headers=runtime.API_HEADERS,
                )
                if response.status_code == 200:
                    data = response.json()
                    return data.get("memories", data.get("results", []))
        except Exception as error:
            runtime.log(f"API search failed: {error}", "WARN")
        return []

    @staticmethod
    async def create(
        type: str,
        content: str,
        tags: list[str],
        importance: int = 5,
        metadata: dict | None = None,
    ) -> dict | None:
        """Create an organization-visible memory through the server API."""
        from daemon.runtime.module_proxy import runtime

        body = {
            "type": type,
            "content": content,
            "tags": tags,
            "importance": importance,
            "shared": True,
        }
        if metadata:
            body["metadata"] = metadata
        try:
            async with httpx.AsyncClient(timeout=MemoryAPI.API_TIMEOUT) as client:
                response = await client.post(
                    f"{runtime.API_BASE}/memories",
                    json=body,
                    headers=runtime.API_HEADERS,
                )
                if response.status_code in (200, 201):
                    return response.json()
        except Exception as error:
            runtime.log(f"API create failed: {error}", "WARN")
        return None

    @staticmethod
    async def update(
        memory_id: str,
        tags: list[str] | None = None,
        content: str | None = None,
        importance: int | None = None,
        metadata: dict | None = None,
    ) -> dict | None:
        """Update a memory through the server API."""
        from daemon.runtime.module_proxy import runtime

        body = {}
        if tags is not None:
            body["tags"] = tags
        if content is not None:
            body["content"] = content
        if importance is not None:
            body["importance"] = importance
        if metadata is not None:
            body["metadata"] = metadata
        try:
            async with httpx.AsyncClient(timeout=MemoryAPI.API_TIMEOUT) as client:
                response = await client.patch(
                    f"{runtime.API_BASE}/memories/{memory_id}",
                    json=body,
                    headers=runtime.API_HEADERS,
                )
                if response.status_code == 200:
                    return response.json()
        except Exception as error:
            runtime.log(f"API update failed: {error}", "WARN")
        return None

    @staticmethod
    async def get(memory_id: str) -> dict | None:
        """Get a single memory by ID through the server API."""
        from daemon.runtime.module_proxy import runtime

        try:
            async with httpx.AsyncClient(timeout=MemoryAPI.API_TIMEOUT) as client:
                response = await client.get(
                    f"{runtime.API_BASE}/memories/{memory_id}",
                    headers=runtime.API_HEADERS,
                )
                if response.status_code == 200:
                    return response.json()
        except Exception as error:
            runtime.log(f"API get failed: {error}", "WARN")
        return None