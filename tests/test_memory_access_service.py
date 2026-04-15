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

    assert result is None
    github_access.check_access.assert_awaited_once_with(user_id, "org/repo")


@pytest.mark.asyncio
async def test_search_updates_counts_after_filtering() -> None:
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
    github_access.check_access = AsyncMock(side_effect=[True, False])
    service = MemoryAccessService(repo, github_access)

    result = await service.search(user_id=user_id, query="x")

    assert [m["id"] for m in result["memories"]] == [1]
    assert result["total_count"] == 1
    assert result["has_more"] is False


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
    user_id = uuid4()
    repo = AsyncMock()
    repo.search_full = AsyncMock(
        return_value={
            "memories": [
                {"id": 1, "metadata": {"repo": "org/allowed"}},
                {"id": 2, "metadata": {"repo": "org/blocked"}},
                {"id": 3, "metadata": {}},
            ],
            "total_count": 3,
            "offset": 0,
            "limit": 10,
            "has_more": False,
        }
    )
    github_access = AsyncMock()
    github_access.check_access = AsyncMock(side_effect=[True, False])
    service = MemoryAccessService(repo, github_access)

    result = await service.search_full(user_id=user_id, query="repo")

    assert [m["id"] for m in result["memories"]] == [1, 3]
    assert result["total_count"] == 2
