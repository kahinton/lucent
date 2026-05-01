"""Memory access enforcement with GitHub repo ACL filtering."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from lucent.db.memory import MemoryRepository
from lucent.integrations.github_repo_access_service import GitHubRepoAccessService


class MemoryAccessService:
    """Wraps memory reads and filters by metadata.repo access."""

    def __init__(
        self,
        repo: MemoryRepository,
        github_repo_access: GitHubRepoAccessService,
        *,
        is_admin: bool = False,
    ) -> None:
        self.repo = repo
        self.github_repo_access = github_repo_access
        self._is_admin = is_admin

    async def get_accessible(
        self,
        memory_id: UUID,
        user_id: UUID,
        organization_id: UUID,
        memory_scope: str | None = None,
        is_admin: bool | None = None,
    ) -> dict[str, Any] | None:
        memory = await self.repo.get_accessible(
            memory_id, user_id, organization_id, memory_scope
        )
        if not memory:
            return None
        # Admins/owners always have full access (check per-call flag OR constructor flag)
        if (is_admin is True) or self._is_admin:
            return memory
        # Shared memories are accessible to anyone in the org
        if memory.get("shared"):
            return memory
        filtered = await self.filter_memory(memory, user_id)
        if filtered is None:
            # Memory exists but repo access denied — attach a flag
            # so callers can distinguish 404 vs 403
            return {
                "_access_denied": True,
                "id": memory.get("id"),
                "metadata": memory.get("metadata", {}),
            }
        return filtered

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

    async def filter_memory_links(
        self,
        links: list[dict[str, Any]],
        *,
        user_id: UUID | None,
        organization_id: UUID | None,
        memory_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return only linked memories the caller can read.

        Request/task detail loaders historically joined directly against the
        memories table.  This helper re-hydrates each linked memory through the
        same access-control path as ordinary memory reads, then copies the safe
        display fields back onto the link row.
        """
        if user_id is None or organization_id is None:
            return []

        filtered: list[dict[str, Any]] = []
        for link in links:
            memory_id = link.get("memory_id")
            try:
                memory_uuid = UUID(str(memory_id))
            except (TypeError, ValueError):
                continue

            memory = await self.get_accessible(
                memory_uuid,
                user_id,
                organization_id,
                memory_scope=memory_scope,
            )
            if not memory or memory.get("_access_denied"):
                continue

            safe_link = dict(link)
            safe_link["content"] = memory.get("content")
            safe_link["memory_type"] = memory.get("type")
            safe_link["tags"] = memory.get("tags") or []
            safe_link["metadata"] = memory.get("metadata") or {}
            filtered.append(safe_link)
        return filtered

    async def filter_request_detail_memory_links(
        self,
        request_detail: dict[str, Any],
        *,
        user_id: UUID | None,
        organization_id: UUID | None,
        memory_scope: str | None = None,
    ) -> dict[str, Any]:
        """Filter request-level and task-level memory links in a detail payload."""
        if not request_detail:
            return request_detail

        request_detail["memories"] = await self.filter_memory_links(
            request_detail.get("memories") or [],
            user_id=user_id,
            organization_id=organization_id,
            memory_scope=memory_scope,
        )

        for task in request_detail.get("tasks") or []:
            task["memories"] = await self.filter_memory_links(
                task.get("memories") or [],
                user_id=user_id,
                organization_id=organization_id,
                memory_scope=memory_scope,
            )

        def _filter_tree(tasks: list[dict[str, Any]]) -> None:
            for task in tasks:
                task_id = str(task.get("id"))
                source = next(
                    (t for t in request_detail.get("tasks") or [] if str(t.get("id")) == task_id),
                    None,
                )
                if source is not None:
                    task["memories"] = source.get("memories") or []
                _filter_tree(task.get("sub_tasks") or [])

        _filter_tree(request_detail.get("task_tree") or [])
        return request_detail

    async def _resolve_accessible_repos(
        self, user_id: UUID | None, memory_scope: str | None = None
    ) -> list[str] | None:
        """Return the lowercased list of repos the user can access, or
        ``None`` to mean "no repo filter".

        Admins/owners bypass repo ACL. Org-shared maintenance scopes also
        bypass repo ACL because the scoped key already restricts visibility to
        shared organization memories; applying the daemon service account's
        GitHub repo cache would hide the shared technical corpus that
        consolidation is supposed to maintain.

        Anonymous callers (``user_id is None``) get an empty list, meaning
        only memories without a ``metadata.repo`` are visible.
        """
        if self._is_admin or memory_scope == "org_shared_only":
            return None
        if user_id is None:
            return []
        # Pull every positively cached access decision for this user. Stale
        # entries (expires_at < now) are excluded so revoked access takes
        # effect on the next page load.
        try:
            async with self.repo.pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT repo_full_name FROM github_repo_access_cache
                       WHERE user_id = $1 AND has_access = true
                         AND expires_at > NOW()""",
                    user_id,
                )
        except Exception:
            # If the cache lookup fails for any reason, fall back to the
            # safest answer (no repo-tagged memories accessible).
            return []
        return [row["repo_full_name"].lower() for row in rows]

    async def search(self, *, user_id: UUID | None, **kwargs: Any) -> dict[str, Any]:
        accessible_repos = await self._resolve_accessible_repos(
            user_id, kwargs.get("memory_scope")
        )
        result = await self.repo.search(accessible_repos=accessible_repos, **kwargs)
        # The SQL filter already enforces repo ACL, so total_count is accurate
        # and the page does not need a second post-filter pass.
        return result

    async def search_full(self, *, user_id: UUID | None, **kwargs: Any) -> dict[str, Any]:
        accessible_repos = await self._resolve_accessible_repos(
            user_id, kwargs.get("memory_scope")
        )
        result = await self.repo.search_full(accessible_repos=accessible_repos, **kwargs)
        return result

    async def get_existing_tags(
        self, *, user_id: UUID | None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Tag counts that respect both basic ACL and GitHub repo ACL —
        consistent with what ``search`` exposes."""
        accessible_repos = await self._resolve_accessible_repos(
            user_id, kwargs.get("memory_scope")
        )
        repo_kwargs = dict(kwargs)
        repo_kwargs.pop("memory_scope", None)
        return await self.repo.get_existing_tags(
            accessible_repos=accessible_repos, **repo_kwargs
        )

    async def get_tag_suggestions(
        self, *, user_id: UUID | None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        accessible_repos = await self._resolve_accessible_repos(
            user_id, kwargs.get("memory_scope")
        )
        repo_kwargs = dict(kwargs)
        repo_kwargs.pop("memory_scope", None)
        return await self.repo.get_tag_suggestions(
            accessible_repos=accessible_repos, **repo_kwargs
        )

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
