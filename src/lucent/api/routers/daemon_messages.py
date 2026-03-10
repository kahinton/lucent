"""Daemon message API endpoints for human-daemon communication.

Messages are stored as memories tagged with 'daemon-message'.
Human messages also carry 'from-human'; daemon messages carry 'from-daemon'.
The 'pending' tag indicates unacknowledged human messages.

Requires API key with 'daemon-tasks' scope (reuses daemon scope).
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from lucent.api.deps import DaemonTaskUser
from lucent.api.models import (
    DaemonMessageCreate,
    DaemonMessageListResponse,
    DaemonMessageResponse,
    ErrorResponse,
    SuccessResponse,
)
from lucent.db import MemoryRepository, get_pool
from lucent.logging import get_logger

logger = get_logger("api.daemon_messages")


router = APIRouter()


def _memory_to_message(memory: dict[str, Any]) -> DaemonMessageResponse:
    """Convert a daemon-message memory to a DaemonMessageResponse."""
    tags = memory.get("tags") or []
    metadata = memory.get("metadata") or {}

    sender = "daemon" if "from-daemon" in tags else "human"
    acknowledged = "acknowledged" in tags

    return DaemonMessageResponse(
        id=memory["id"],
        content=memory["content"],
        sender=sender,
        acknowledged=acknowledged,
        created_at=memory["created_at"],
        updated_at=memory["updated_at"],
        in_reply_to=metadata.get("in_reply_to"),
        acknowledged_at=metadata.get("acknowledged_at"),
    )


@router.get(
    "",
    response_model=DaemonMessageListResponse,
)
async def list_messages(
    user: DaemonTaskUser,
    pending_only: bool = Query(False, description="Only return unacknowledged human messages"),
    limit: int = Query(50, ge=1, le=100),
) -> DaemonMessageListResponse:
    """List daemon messages, optionally filtering to pending human messages only."""
    pool = await get_pool()
    repo = MemoryRepository(pool)

    tags_filter = ["daemon-message"]
    if pending_only:
        tags_filter.extend(["from-human", "pending"])

    result = await repo.search(
        tags=tags_filter,
        limit=limit,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    messages = [_memory_to_message(mem) for mem in result["memories"]]
    return DaemonMessageListResponse(messages=messages, total_count=len(messages))


@router.post(
    "",
    response_model=DaemonMessageResponse,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
)
async def send_daemon_message(
    data: DaemonMessageCreate,
    user: DaemonTaskUser,
) -> DaemonMessageResponse:
    """Send a message from the daemon."""
    pool = await get_pool()
    repo = MemoryRepository(pool)

    tags = ["daemon-message", "daemon", "from-daemon"]
    metadata: dict[str, Any] = {"source": "daemon-api"}
    if data.in_reply_to:
        metadata["in_reply_to"] = str(data.in_reply_to)

    username = user.display_name or user.email or str(user.id)

    result = await repo.create(
        username=username,
        type="experience",
        content=data.content,
        tags=tags,
        importance=5,
        metadata=metadata,
        user_id=user.id,
        organization_id=user.organization_id,
    )

    logger.info("Daemon message sent: id=%s, reply_to=%s", result["id"], data.in_reply_to)

    return _memory_to_message(result)


@router.post(
    "/{message_id}/acknowledge",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}},
)
async def acknowledge_message(
    message_id: UUID,
    user: DaemonTaskUser,
) -> SuccessResponse:
    """Mark a human message as acknowledged by the daemon."""
    pool = await get_pool()
    repo = MemoryRepository(pool)

    memory = await repo.get_accessible(
        memory_id=message_id,
        user_id=user.id,
        organization_id=user.organization_id,
    )

    if memory is None or "daemon-message" not in (memory.get("tags") or []):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )

    tags = list(memory.get("tags") or [])
    if "pending" in tags:
        tags.remove("pending")
    if "acknowledged" not in tags:
        tags.append("acknowledged")

    metadata = dict(memory.get("metadata") or {})
    metadata["acknowledged_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    await repo.update(memory_id=message_id, tags=tags, metadata=metadata)

    return SuccessResponse(success=True, message=f"Message {message_id} acknowledged")
