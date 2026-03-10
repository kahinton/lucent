"""Memory search API endpoints."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from lucent.api.deps import AuthenticatedUser
from lucent.api.models import (
    SearchRequest,
    SearchResponse,
    SearchResultMemory,
)
from lucent.db import AccessRepository, MemoryRepository, get_pool
from lucent.logging import get_logger

logger = get_logger("api.search")


router = APIRouter()


def _memory_to_search_result(memory: dict[str, Any]) -> SearchResultMemory:
    """Convert a memory dict to a search result model."""
    related_ids = memory.get("related_memory_ids") or []
    return SearchResultMemory(
        id=memory["id"],
        username=memory["username"],
        type=memory["type"],
        content=memory["content"],
        content_truncated=memory.get("content_truncated", False),
        tags=memory.get("tags") or [],
        importance=memory["importance"],
        related_memory_ids=[uid for uid in related_ids],
        created_at=memory["created_at"],
        updated_at=memory["updated_at"],
        similarity_score=memory.get("similarity_score"),
        user_id=memory.get("user_id"),
        organization_id=memory.get("organization_id"),
        shared=memory.get("shared", False),
        last_accessed_at=memory.get("last_accessed_at"),
    )


@router.post(
    "",
    response_model=SearchResponse,
)
async def search_memories(
    data: SearchRequest,
    user: AuthenticatedUser,
) -> SearchResponse:
    """Search memories by content with fuzzy matching."""
    pool = await get_pool()
    repo = MemoryRepository(pool)
    access_repo = AccessRepository(pool)

    logger.info("Search: query=%s, type=%s, tags=%s, user=%s", data.query, data.type, data.tags, user.id)

    result = await repo.search(
        query=data.query,
        username=data.username,
        type=data.type,
        tags=data.tags,
        importance_min=data.importance_min,
        importance_max=data.importance_max,
        created_after=data.created_after,
        created_before=data.created_before,
        offset=data.offset,
        limit=data.limit,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    # Log access for returned memories
    if result["memories"]:
        memory_ids = [m["id"] for m in result["memories"]]
        await access_repo.log_batch_access(
            memory_ids=memory_ids,
            access_type="search_result",
            user_id=user.id,
            organization_id=user.organization_id,
            context={
                "query": data.query,
                "type": data.type,
                "tags": data.tags,
            },
        )

    return SearchResponse(
        memories=[_memory_to_search_result(m) for m in result["memories"]],
        total_count=result["total_count"],
        offset=result["offset"],
        limit=result["limit"],
        has_more=result["has_more"],
    )


@router.get(
    "",
    response_model=SearchResponse,
)
async def search_memories_get(
    user: AuthenticatedUser,
    query: str | None = Query(default=None, description="Search query"),
    username: str | None = Query(default=None, description="Filter by username"),
    type: str | None = Query(default=None, description="Filter by memory type"),
    tags: list[str] | None = Query(default=None, description="Filter by tags"),
    importance_min: int | None = Query(default=None, ge=1, le=10),
    importance_max: int | None = Query(default=None, ge=1, le=10),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> SearchResponse:
    """Search memories by content (GET version for convenience)."""
    request = SearchRequest(
        query=query,
        username=username,
        type=type,
        tags=tags,
        importance_min=importance_min,
        importance_max=importance_max,
        created_after=created_after,
        created_before=created_before,
        offset=offset,
        limit=limit,
    )
    return await search_memories(request, user)


@router.post(
    "/full",
    response_model=SearchResponse,
)
async def search_memories_full(
    data: SearchRequest,
    user: AuthenticatedUser,
) -> SearchResponse:
    """Search across all fields: content, tags, and metadata."""
    if not data.query:
        return SearchResponse(
            memories=[],
            total_count=0,
            offset=data.offset,
            limit=data.limit,
            has_more=False,
        )

    pool = await get_pool()
    repo = MemoryRepository(pool)
    access_repo = AccessRepository(pool)

    result = await repo.search_full(
        query=data.query,
        username=data.username,
        type=data.type,
        importance_min=data.importance_min,
        importance_max=data.importance_max,
        offset=data.offset,
        limit=data.limit,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    # Log access for returned memories
    if result["memories"]:
        memory_ids = [m["id"] for m in result["memories"]]
        await access_repo.log_batch_access(
            memory_ids=memory_ids,
            access_type="search_result",
            user_id=user.id,
            organization_id=user.organization_id,
            context={
                "query": data.query,
                "search_type": "full",
                "type": data.type,
            },
        )

    return SearchResponse(
        memories=[_memory_to_search_result(m) for m in result["memories"]],
        total_count=result["total_count"],
        offset=result["offset"],
        limit=result["limit"],
        has_more=result["has_more"],
    )
