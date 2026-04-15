"""Memory access enforcement with GitHub repo ACL filtering."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from lucent.db.memory import MemoryRepository
from lucent.integrations.github_repo_access_service import GitHubRepoAccessService


class MemoryAccessService:
    """Wraps memory reads and filters by metadata.repo access."""

    def __init__(self, repo: MemoryRepository, github_repo_access: GitHubRepoAccessService) -> None:
        self.repo = repo
        self.github_repo_access = github_repo_access

    async def get_accessible(
        self,
        memory_id: UUID,
        user_id: UUID,
        organization_id: UUID,
        memory_scope: str | None = None,
    ) -> dict[str, Any] | None:
        memory = await self.repo.get_accessible(memory_id, user_id, organization_id, memory_scope)
        if not memory:
            return None
        return await self.filter_memory(memory, user_id)

    async def filter_memory(
        self,
        memory: dict[str, Any],
        user_id: UUID | None,
    ) -> dict[str, Any] | None:
        repo_name = self._extract_repo(memory)
        if not repo_name:
            return memory
        if user_id is None:
            return None
        has_access = await self.github_repo_access.check_access(user_id, repo_name)
        return memory if has_access else None

    async def filter_memories(
        self, memories: list[dict[str, Any]], user_id: UUID | None
    ) -> list[dict[str, Any]]:
        if user_id is None:
            return [memory for memory in memories if self._extract_repo(memory) is None]
        repos = sorted({repo for memory in memories if (repo := self._extract_repo(memory))})
        if not repos:
            return memories

        checks = await asyncio.gather(
            *(self.github_repo_access.check_access(user_id, repo) for repo in repos)
        )
        access_by_repo = dict(zip(repos, checks, strict=False))

        filtered: list[dict[str, Any]] = []
        for memory in memories:
            repo_name = self._extract_repo(memory)
            if not repo_name or access_by_repo.get(repo_name, False):
                filtered.append(memory)
        return filtered

    async def search(self, *, user_id: UUID | None, **kwargs: Any) -> dict[str, Any]:
        result = await self.repo.search(**kwargs)
        filtered = await self.filter_memories(result["memories"], user_id)
        return self._filtered_result(result, filtered)

    async def search_full(self, *, user_id: UUID | None, **kwargs: Any) -> dict[str, Any]:
        result = await self.repo.search_full(**kwargs)
        filtered = await self.filter_memories(result["memories"], user_id)
        return self._filtered_result(result, filtered)

    @staticmethod
    def _extract_repo(memory: dict[str, Any]) -> str | None:
        metadata = memory.get("metadata")
        if not isinstance(metadata, dict):
            return None
        repo = metadata.get("repo")
        if not isinstance(repo, str):
            return None
        normalized = repo.strip().lower()
        return normalized or None

    @staticmethod
    def _filtered_result(result: dict[str, Any], filtered: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "memories": filtered,
            "total_count": len(filtered),
            "offset": result["offset"],
            "limit": result["limit"],
            "has_more": False,
        }
