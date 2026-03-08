"""Daemon task API endpoints for external agent integration.

Provides a structured API for submitting, polling, and retrieving daemon tasks.
Under the hood, daemon tasks are stored as memories tagged with 'daemon-task'.

Requires API key with 'daemon-tasks' scope (or full 'read'+'write' scopes).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from lucent.api.deps import DaemonTaskUser
from lucent.api.models import (
    DaemonTaskCreate,
    DaemonTaskResponse,
    DaemonTaskListResponse,
    ErrorResponse,
    SuccessResponse,
)
from lucent.db import MemoryRepository, get_pool
from lucent.logging import get_logger

logger = get_logger("api.daemon_tasks")


router = APIRouter()

# Valid agent types for daemon tasks
VALID_AGENT_TYPES = {"research", "code", "memory", "reflection", "documentation", "planning"}
VALID_PRIORITIES = {"low", "medium", "high"}


def _memory_to_task(memory: dict[str, Any]) -> DaemonTaskResponse:
    """Convert a daemon-task memory to a DaemonTaskResponse."""
    tags = memory.get("tags") or []

    # Derive status from tags
    if "completed" in tags:
        task_status = "completed"
    elif any(t.startswith("claimed-by-") for t in tags):
        task_status = "claimed"
    elif "pending" in tags:
        task_status = "pending"
    else:
        task_status = "unknown"

    # Extract agent type from tags
    agent_type = "unknown"
    for t in tags:
        if t in VALID_AGENT_TYPES:
            agent_type = t
            break

    # Extract priority from tags
    priority = "medium"
    for p in VALID_PRIORITIES:
        if p in tags:
            priority = p
            break

    # Extract claimed-by instance
    claimed_by = None
    for t in tags:
        if t.startswith("claimed-by-"):
            claimed_by = t[len("claimed-by-"):]
            break

    # Result is stored in metadata
    metadata = memory.get("metadata") or {}
    result = metadata.get("result")

    # Filter display tags (remove internal tags)
    internal_tags = (
        {"daemon-task", "pending", "completed", "daemon"} | VALID_AGENT_TYPES | VALID_PRIORITIES
    )
    display_tags = [t for t in tags if t not in internal_tags and not t.startswith("claimed-by-")]

    return DaemonTaskResponse(
        id=memory["id"],
        description=memory["content"],
        agent_type=agent_type,
        priority=priority,
        status=task_status,
        tags=display_tags,
        created_at=memory["created_at"],
        updated_at=memory["updated_at"],
        result=result,
        claimed_by=claimed_by,
    )


@router.post(
    "",
    response_model=DaemonTaskResponse,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
)
async def create_task(
    data: DaemonTaskCreate,
    user: DaemonTaskUser,
) -> DaemonTaskResponse:
    """Submit a new daemon task for processing.

    The task will be picked up by the next daemon cognitive cycle.
    """
    if data.agent_type not in VALID_AGENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid agent_type. Must be one of: {', '.join(sorted(VALID_AGENT_TYPES))}",
        )

    if data.priority not in VALID_PRIORITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid priority. Must be one of: {', '.join(sorted(VALID_PRIORITIES))}",
        )

    pool = await get_pool()
    repo = MemoryRepository(pool)

    # Build tags: daemon-task + pending + agent_type + priority + user extras
    tags = ["daemon-task", "daemon", "pending", data.agent_type, data.priority]
    if data.tags:
        tags.extend(data.tags)

    # Store context in metadata
    metadata: dict[str, Any] = {"submitted_by": str(user.id), "source": "api"}
    if data.context:
        metadata["context"] = data.context

    username = user.display_name or user.email or str(user.id)

    result = await repo.create(
        username=username,
        type="technical",
        content=data.description,
        tags=tags,
        importance={"low": 3, "medium": 5, "high": 8}.get(data.priority, 5),
        metadata=metadata,
        user_id=user.id,
        organization_id=user.organization_id,
    )

    logger.info("Daemon task created: id=%s, agent=%s, priority=%s", result["id"], data.agent_type, data.priority)

    return _memory_to_task(result)


@router.get(
    "",
    response_model=DaemonTaskListResponse,
)
async def list_tasks(
    user: DaemonTaskUser,
    task_status: str | None = Query(
        None, alias="status", description="Filter: pending, claimed, completed",
    ),
    since: datetime | None = Query(None, description="Only tasks updated after this ISO timestamp"),
    limit: int = Query(20, ge=1, le=100),
) -> DaemonTaskListResponse:
    """List daemon tasks, with optional status and time filters.

    Use `?status=pending&since=<last_poll_time>` for efficient polling.
    """
    pool = await get_pool()
    repo = MemoryRepository(pool)

    # Search for daemon-task memories owned by this user
    tags_filter = ["daemon-task"]
    if task_status == "pending":
        tags_filter.append("pending")
    elif task_status == "completed":
        tags_filter.append("completed")
    # "claimed" status requires post-filtering since it's a prefix tag

    search_result = await repo.search(
        tags=tags_filter,
        limit=limit,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    tasks = []
    for mem in search_result["memories"]:
        task = _memory_to_task(mem)

        # Post-filter by status if needed
        if task_status == "claimed" and task.status != "claimed":
            continue

        # Filter by since timestamp
        if since and task.updated_at <= since:
            continue

        tasks.append(task)

    return DaemonTaskListResponse(tasks=tasks, total_count=len(tasks))


@router.get(
    "/{task_id}",
    response_model=DaemonTaskResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_task(
    task_id: UUID,
    user: DaemonTaskUser,
) -> DaemonTaskResponse:
    """Get a daemon task by ID."""
    pool = await get_pool()
    repo = MemoryRepository(pool)

    memory = await repo.get_accessible(
        memory_id=task_id,
        user_id=user.id,
        organization_id=user.organization_id,
    )

    if memory is None or "daemon-task" not in (memory.get("tags") or []):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    return _memory_to_task(memory)


@router.get(
    "/{task_id}/result",
    response_model=DaemonTaskResponse,
    responses={404: {"model": ErrorResponse}, 202: {"model": DaemonTaskResponse}},
)
async def get_task_result(
    task_id: UUID,
    user: DaemonTaskUser,
):
    """Get a daemon task's result.

    Returns 200 with result if completed, or 202 if still in progress.
    """
    pool = await get_pool()
    repo = MemoryRepository(pool)

    memory = await repo.get_accessible(
        memory_id=task_id,
        user_id=user.id,
        organization_id=user.organization_id,
    )

    if memory is None or "daemon-task" not in (memory.get("tags") or []):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    task = _memory_to_task(memory)

    if task.status != "completed":
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=task.model_dump(mode="json"),
        )

    return task


@router.delete(
    "/{task_id}",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}},
)
async def cancel_task(
    task_id: UUID,
    user: DaemonTaskUser,
) -> SuccessResponse:
    """Cancel a pending daemon task (soft delete)."""
    pool = await get_pool()
    repo = MemoryRepository(pool)

    memory = await repo.get_accessible(
        memory_id=task_id,
        user_id=user.id,
        organization_id=user.organization_id,
    )

    if memory is None or "daemon-task" not in (memory.get("tags") or []):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    # Only pending tasks can be cancelled
    tags = memory.get("tags") or []
    if "pending" not in tags:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending tasks can be cancelled",
        )

    # Check ownership
    if memory.get("user_id") != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only cancel your own tasks",
        )

    await repo.delete(task_id)

    return SuccessResponse(success=True, message=f"Task {task_id} cancelled")
