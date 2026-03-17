"""Memory export API endpoint."""

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from lucent.api.deps import AuthenticatedUser
from lucent.api.models import (
    ExportMetadata,
    ExportResponse,
    ImportRequest,
    ImportResponse,
    MemoryResponse,
)
from lucent.db import MemoryRepository, get_pool
from lucent.logging import get_logger

logger = get_logger("api.export")


router = APIRouter()


def _memory_to_response(memory: dict[str, Any]) -> MemoryResponse:
    """Convert a memory dict to a response model."""
    related_ids = memory.get("related_memory_ids") or []
    return MemoryResponse(
        id=memory["id"],
        username=memory["username"],
        type=memory["type"],
        content=memory["content"],
        tags=memory.get("tags") or [],
        importance=memory["importance"],
        related_memory_ids=[uid for uid in related_ids],
        metadata=memory.get("metadata") or {},
        created_at=memory["created_at"],
        updated_at=memory["updated_at"],
        deleted_at=memory.get("deleted_at"),
        user_id=memory.get("user_id"),
        organization_id=memory.get("organization_id"),
        shared=memory.get("shared", False),
        last_accessed_at=memory.get("last_accessed_at"),
    )


def _build_filters_dict(
    type: str | None,
    tags: list[str] | None,
    importance_min: int | None,
    importance_max: int | None,
    created_after: datetime | None,
    created_before: datetime | None,
) -> dict[str, Any]:
    """Build a filters dict for export metadata."""
    filters: dict[str, Any] = {}
    if type is not None:
        filters["type"] = type
    if tags:
        filters["tags"] = tags
    if importance_min is not None:
        filters["importance_min"] = importance_min
    if importance_max is not None:
        filters["importance_max"] = importance_max
    if created_after is not None:
        filters["created_after"] = created_after.isoformat()
    if created_before is not None:
        filters["created_before"] = created_before.isoformat()
    return filters


@router.get(
    "",
    response_model=ExportResponse,
    responses={200: {"description": "Export memories as JSON"}},
)
async def export_memories(
    user: AuthenticatedUser,
    type: str | None = Query(default=None, description="Filter by memory type"),
    tags: list[str] | None = Query(default=None, description="Filter by tags (any match)"),
    importance_min: int | None = Query(default=None, ge=1, le=10),
    importance_max: int | None = Query(default=None, ge=1, le=10),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
    format: str = Query(default="json", description="Export format: json or jsonl"),
) -> ExportResponse | StreamingResponse:
    """Export memories with full content and metadata.

    Supports two formats:
    - `json`: Returns a JSON object with metadata and memories array
    - `jsonl`: Streams one JSON object per line (first line is metadata)
    """
    pool = await get_pool()
    repo = MemoryRepository(pool)

    memories = await repo.export(
        type=type,
        tags=tags,
        importance_min=importance_min,
        importance_max=importance_max,
        created_after=created_after,
        created_before=created_before,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    filters = _build_filters_dict(
        type,
        tags,
        importance_min,
        importance_max,
        created_after,
        created_before,
    )
    responses = [_memory_to_response(m) for m in memories]

    logger.info("Export: count=%d, format=%s, user=%s", len(responses), format, user.id)

    if format == "jsonl":
        return _stream_jsonl(responses, filters)

    return ExportResponse(
        metadata=ExportMetadata(
            exported_at=datetime.now(),
            total_count=len(responses),
            filters=filters,
            format="json",
        ),
        memories=responses,
    )


def _stream_jsonl(
    memories: list[MemoryResponse],
    filters: dict[str, Any],
) -> StreamingResponse:
    """Stream memories as JSONL (one JSON object per line)."""

    def generate():
        # First line: metadata
        meta = {
            "exported_at": datetime.now().isoformat(),
            "total_count": len(memories),
            "filters": filters,
            "format": "jsonl",
        }
        yield json.dumps(meta) + "\n"

        for memory in memories:
            yield memory.model_dump_json() + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": "attachment; filename=lucent-export.jsonl",
        },
    )


@router.post(
    "/import",
    response_model=ImportResponse,
    responses={200: {"description": "Import memories from JSON"}},
)
async def import_memories(
    user: AuthenticatedUser,
    request: ImportRequest,
) -> ImportResponse:
    """Import memories from a previously exported payload.

    Deduplicates by content hash — memories with identical content, type,
    and username are skipped. All imported memories are owned by the
    authenticated user regardless of original user_id/organization_id in
    the export data.
    """
    pool = await get_pool()
    repo = MemoryRepository(pool)

    # Convert Pydantic models to dicts for the repository
    memory_dicts = [m.model_dump() for m in request.memories]

    result = await repo.import_memories(
        memories=memory_dicts,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
        requesting_username=user.display_name or user.email or str(user.id),
    )

    logger.info(
        "Import: submitted=%d, imported=%d, user=%s",
        len(memory_dicts),
        result.get("imported", 0),
        user.id,
    )

    return ImportResponse(**result)
