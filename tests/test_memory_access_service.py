"""Tests for MemoryAccessService repo-tag ACL filtering."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from lucent.services.memory_access_service import MemoryAccessService


@pytest.mark.asyncio
async def test_filter_memories_batches_repo_checks_once_per_unique_repo() -> None:
    user_id = uuid4()
    repo = AsyncMock()
    github_access = AsyncMock()
    github_access.check_access = AsyncMock(side_effect=[True, False])
    service = MemoryAccessService(repo, github_access)

    memories = [
        {"id": 1, "metadata": {"repo": "org/allowed"}},
        {"id": 2, "metadata": {"repo": "org/allowed"}},
        {"id": 3, "metadata": {"repo": "org/blocked"}},
        {"id": 4, "metadata": {}},
    ]

    filtered = await service.filter_memories(memories, user_id)

    assert [m["id"] for m in filtered] == [1, 2, 4]
    assert github_access.check_access.await_count == 2


@pytest.mark.asyncio
async def test_get_accessible_filters_single_memory_by_repo_access() -> None:
    memory_id = uuid4()
    user_id = uuid4()
    org_id = uuid4()

    repo = AsyncMock()
    repo.get_accessible = AsyncMock(
        return_value={"id": memory_id, "metadata": {"repo": "org/repo"}}
    )
    github_access = AsyncMock()
    github_access.check_access = AsyncMock(return_value=False)
    service = MemoryAccessService(repo, github_access)

    result = await service.get_accessible(memory_id, user_id, org_id)

    assert result == {
        "_access_denied": True,
        "id": memory_id,
        "metadata": {"repo": "org/repo"},
    }
    github_access.check_access.assert_awaited_once_with(user_id, "org/repo")


@pytest.mark.asyncio
async def test_search_updates_counts_after_filtering() -> None:
    """``search`` resolves the user's accessible repos and pushes them down
    into the repo as a SQL filter, then trusts repo's count instead of doing
    a post-filter pass that loses the true total."""

    user_id = uuid4()

    # Mock the github_repo_access_cache lookup that
    # ``_resolve_accessible_repos`` performs against the pool.
    cache_rows = [{"repo_full_name": "org/allowed"}]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=cache_rows)

    class _AcquireCM:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    pool = AsyncMock()
    pool.acquire = lambda: _AcquireCM()

    repo = AsyncMock()
    repo.pool = pool
    # repo.search must receive the resolved accessible_repos and (in real
    # life) filter at the SQL level. Here we just confirm it's invoked with
    # the right kwarg and pass through what it returns.
    repo.search = AsyncMock(
        return_value={
            "memories": [{"id": 1, "metadata": {"repo": "org/allowed"}}],
            "total_count": 1,
            "offset": 0,
            "limit": 5,
            "has_more": False,
        }
    )

    github_access = AsyncMock()
    service = MemoryAccessService(repo, github_access)

    result = await service.search(user_id=user_id, query="x")

    repo.search.assert_awaited_once()
    kwargs = repo.search.await_args.kwargs
    assert kwargs.get("accessible_repos") == ["org/allowed"]
    assert kwargs["query"] == "x"
    # No second post-filter pass — service trusts repo's count.
    assert result["total_count"] == 1
    assert [m["id"] for m in result["memories"]] == [1]


@pytest.mark.asyncio
async def test_search_admin_skips_repo_filter() -> None:
    """Admins/owners bypass the repo ACL entirely so they see every memory
    in their org and the total reflects that."""

    user_id = uuid4()
    repo = AsyncMock()
    repo.search = AsyncMock(
        return_value={
            "memories": [
                {"id": 1, "metadata": {"repo": "org/allowed"}},
                {"id": 2, "metadata": {"repo": "org/blocked"}},
            ],
            "total_count": 2,
            "offset": 0,
            "limit": 5,
            "has_more": False,
        }
    )
    github_access = AsyncMock()
    service = MemoryAccessService(repo, github_access, is_admin=True)

    result = await service.search(user_id=user_id, query="x")

    kwargs = repo.search.await_args.kwargs
    assert kwargs.get("accessible_repos") is None
    assert result["total_count"] == 2
    assert [m["id"] for m in result["memories"]] == [1, 2]


@pytest.mark.asyncio
async def test_search_org_shared_scope_skips_daemon_repo_acl_filter() -> None:
    """Org-shared maintenance keys already restrict the corpus to shared org
    memories, so they must not additionally filter by the daemon service
    account's GitHub repo cache. Technical consolidation needs all shared
    repo-tagged memories to build useful repo/module summaries."""

    user_id = uuid4()
    repo = AsyncMock()
    repo.search = AsyncMock(
        return_value={
            "memories": [
                {"id": 1, "metadata": {"repo": "org/allowed"}},
                {"id": 2, "metadata": {"repo": "org/blocked-for-daemon"}},
            ],
            "total_count": 2,
            "offset": 0,
            "limit": 5,
            "has_more": False,
        }
    )
    github_access = AsyncMock()
    service = MemoryAccessService(repo, github_access)

    result = await service.search(
        user_id=user_id,
        query="architecture",
        memory_scope="org_shared_only",
    )

    kwargs = repo.search.await_args.kwargs
    assert kwargs.get("accessible_repos") is None
    assert kwargs["memory_scope"] == "org_shared_only"
    assert result["total_count"] == 2


@pytest.mark.asyncio
async def test_filter_memories_passes_through_when_no_repo_tags() -> None:
    user_id = uuid4()
    repo = AsyncMock()
    github_access = AsyncMock()
    github_access.check_access = AsyncMock(return_value=True)
    service = MemoryAccessService(repo, github_access)

    memories = [{"id": 1, "metadata": {}}, {"id": 2}, {"id": 3, "metadata": {"language": "python"}}]
    filtered = await service.filter_memories(memories, user_id)

    assert [m["id"] for m in filtered] == [1, 2, 3]
    github_access.check_access.assert_not_awaited()


@pytest.mark.asyncio
async def test_filter_memories_no_user_blocks_repo_tagged_only() -> None:
    repo = AsyncMock()
    github_access = AsyncMock()
    github_access.check_access = AsyncMock(return_value=True)
    service = MemoryAccessService(repo, github_access)

    memories = [
        {"id": 1, "metadata": {"repo": "org/private"}},
        {"id": 2, "metadata": {}},
        {"id": 3},
    ]
    filtered = await service.filter_memories(memories, None)

    assert [m["id"] for m in filtered] == [2, 3]
    github_access.check_access.assert_not_awaited()


@pytest.mark.asyncio
async def test_filter_memory_no_user_blocks_repo_tagged_memory() -> None:
    repo = AsyncMock()
    github_access = AsyncMock()
    service = MemoryAccessService(repo, github_access)

    result = await service.filter_memory({"id": 1, "metadata": {"repo": "org/private"}}, None)
    assert result is None


@pytest.mark.asyncio
async def test_search_full_filters_repo_tagged_results() -> None:
    """``search_full`` mirrors ``search``: it pushes the user's accessible
    repos into the repo as a SQL filter and trusts the returned counts."""

    user_id = uuid4()

    cache_rows = [{"repo_full_name": "org/allowed"}]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=cache_rows)

    class _AcquireCM:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    pool = AsyncMock()
    pool.acquire = lambda: _AcquireCM()

    repo = AsyncMock()
    repo.pool = pool
    repo.search_full = AsyncMock(
        return_value={
            "memories": [
                {"id": 1, "metadata": {"repo": "org/allowed"}},
                {"id": 3, "metadata": {}},
            ],
            "total_count": 2,
            "offset": 0,
            "limit": 10,
            "has_more": False,
        }
    )
    github_access = AsyncMock()
    service = MemoryAccessService(repo, github_access)

    result = await service.search_full(user_id=user_id, query="repo")

    kwargs = repo.search_full.await_args.kwargs
    assert kwargs.get("accessible_repos") == ["org/allowed"]
    assert [m["id"] for m in result["memories"]] == [1, 3]
    assert result["total_count"] == 2
