"""Memory CRUD API endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from lucent.api.deps import AuthenticatedUser
from lucent.api.models import (
    ErrorResponse,
    MemoryCreate,
    MemoryResponse,
    MemoryUpdate,
    SuccessResponse,
    TagCount,
    TagListResponse,
    TagSuggestion,
    TagSuggestionsResponse,
)
from lucent.db import AccessRepository, AuditRepository, MemoryRepository, get_pool
from lucent.logging import get_logger
from lucent.models.validation import validate_metadata
from lucent.rbac import Permission

logger = get_logger("api.memories")


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


@router.post(
    "",
    response_model=MemoryResponse,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
)
async def create_memory(
    data: MemoryCreate,
    user: AuthenticatedUser,
) -> MemoryResponse:
    """Create a new memory."""
    user.require_permission(Permission.MEMORY_CREATE)

    pool = await get_pool()
    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    # Validate memory type
    valid_types = ["experience", "technical", "procedural", "goal", "individual"]
    if data.type not in valid_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid memory type. Must be one of: {', '.join(valid_types)}",
        )

    # Individual memories cannot be created via API - they are auto-created when users are added
    if data.type == "individual":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Individual memories cannot be created directly."
                " They are automatically created when users are"
                " added to the system."
            ),
        )

    # Validate and normalize metadata for the memory type
    try:
        validated_metadata = validate_metadata(data.type, data.metadata)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    # Use authenticated user's info if username not provided
    username = data.username or user.display_name or user.email or str(user.id)

    result = await repo.create(
        username=username,
        type=data.type,
        content=data.content,
        tags=data.tags,
        importance=data.importance,
        related_memory_ids=data.related_memory_ids,
        metadata=validated_metadata,
        user_id=user.id,
        organization_id=user.organization_id,
        shared=data.shared,
    )

    logger.info("Memory created: id=%s, type=%s, user=%s", result["id"], data.type, user.id)
    await audit_repo.log(
        memory_id=result["id"],
        action_type="create",
        user_id=user.id,
        organization_id=user.organization_id,
        new_values={
            "username": username,
            "type": data.type,
            "content": data.content,
            "tags": data.tags,
            "importance": data.importance,
            "metadata": data.metadata,
        },
        context=user.get_audit_context(),
    )

    return _memory_to_response(result)


# Tag routes - MUST be defined before /{memory_id} routes to avoid path conflicts
@router.get(
    "/tags/list",
    response_model=TagListResponse,
)
async def list_tags(
    user: AuthenticatedUser,
    username: str | None = None,
    type: str | None = None,
    limit: int = 50,
) -> TagListResponse:
    """Get existing tags with usage counts."""
    pool = await get_pool()
    repo = MemoryRepository(pool)

    result = await repo.get_existing_tags(
        username=username,
        type=type,
        limit=min(limit, 100),
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    return TagListResponse(
        tags=[TagCount(tag=t["tag"], count=t["count"]) for t in result],
        total_count=len(result),
    )


@router.get(
    "/tags/suggest",
    response_model=TagSuggestionsResponse,
)
async def suggest_tags(
    query: str,
    user: AuthenticatedUser,
    username: str | None = None,
    limit: int = 10,
) -> TagSuggestionsResponse:
    """Get tag suggestions based on fuzzy matching."""
    pool = await get_pool()
    repo = MemoryRepository(pool)

    result = await repo.get_tag_suggestions(
        query=query,
        username=username,
        limit=min(limit, 25),
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    return TagSuggestionsResponse(
        suggestions=[
            TagSuggestion(tag=s["tag"], count=s["count"], similarity=s["similarity"])
            for s in result
        ],
        query=query,
    )


# Memory by ID routes - after tag routes
@router.get(
    "/{memory_id}",
    response_model=MemoryResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_memory(
    memory_id: UUID,
    user: AuthenticatedUser,
) -> MemoryResponse:
    """Get a memory by ID."""
    pool = await get_pool()
    repo = MemoryRepository(pool)
    access_repo = AccessRepository(pool)

    # Use access-controlled get
    result = await repo.get_accessible(
        memory_id=memory_id,
        user_id=user.id,
        organization_id=user.organization_id,
    )

    if result is None:
        # Check if admin can see it
        if user.has_permission(Permission.MEMORY_READ_ALL):
            result = await repo.get(memory_id)
            if result and result.get("organization_id") == user.organization_id:
                # Admin can see org memories
                pass
            else:
                result = None

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found or not accessible",
        )

    # Log access
    await access_repo.log_access(
        memory_id=memory_id,
        access_type="view",
        user_id=user.id,
        organization_id=user.organization_id,
    )

    return _memory_to_response(result)


@router.patch(
    "/{memory_id}",
    response_model=MemoryResponse,
    responses={404: {"model": ErrorResponse}},
)
async def update_memory(
    memory_id: UUID,
    data: MemoryUpdate,
    user: AuthenticatedUser,
) -> MemoryResponse:
    """Update a memory."""
    user.require_permission(Permission.MEMORY_UPDATE_OWN)

    pool = await get_pool()
    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    # Get the memory first to check ownership - use get_accessible to prevent leaking existence
    existing = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found",
        )

    # Check ownership (only owner can update)
    if existing.get("user_id") != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only update your own memories",
        )

    # Validate metadata if provided
    validated_metadata = data.metadata
    if data.metadata is not None:
        try:
            validated_metadata = validate_metadata(existing["type"], data.metadata)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

    result = await repo.update(
        memory_id=memory_id,
        content=data.content,
        tags=data.tags,
        importance=data.importance,
        related_memory_ids=data.related_memory_ids,
        metadata=validated_metadata,
    )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found",
        )

    # Build audit log
    changed_fields = []
    old_values = {}
    new_values = {}

    if data.content is not None and existing["content"] != data.content:
        changed_fields.append("content")
        old_values["content"] = existing["content"]
        new_values["content"] = data.content

    if data.tags is not None and existing["tags"] != data.tags:
        changed_fields.append("tags")
        old_values["tags"] = existing["tags"]
        new_values["tags"] = data.tags

    if data.importance is not None and existing["importance"] != data.importance:
        changed_fields.append("importance")
        old_values["importance"] = existing["importance"]
        new_values["importance"] = data.importance

    if data.metadata is not None and existing["metadata"] != data.metadata:
        changed_fields.append("metadata")
        old_values["metadata"] = existing["metadata"]
        new_values["metadata"] = data.metadata

    if changed_fields:
        await audit_repo.log(
            memory_id=memory_id,
            action_type="update",
            user_id=user.id,
            organization_id=user.organization_id,
            changed_fields=changed_fields,
            old_values=old_values,
            new_values=new_values,
            context=user.get_audit_context(),
        )

    return _memory_to_response(result)


@router.delete(
    "/{memory_id}",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}},
)
async def delete_memory(
    memory_id: UUID,
    user: AuthenticatedUser,
) -> SuccessResponse:
    """Delete a memory (soft delete)."""
    pool = await get_pool()
    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    # Get memory first - use get_accessible to prevent leaking existence of other org's memories
    existing = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found",
        )

    # Individual memories cannot be deleted via API - they are deleted when users are removed
    if existing.get("type") == "individual":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Individual memories cannot be deleted directly."
                " They are automatically deleted when users are"
                " removed from the system."
            ),
        )

    # Check permissions
    is_owner = existing.get("user_id") == user.id
    can_delete_any = user.has_permission(Permission.MEMORY_DELETE_ANY)
    same_org = existing.get("organization_id") == user.organization_id

    if not is_owner and not (can_delete_any and same_org):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own memories",
        )

    success = await repo.delete(memory_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found",
        )

    # Log deletion
    await audit_repo.log(
        memory_id=memory_id,
        action_type="delete",
        user_id=user.id,
        organization_id=user.organization_id,
        old_values={
            "content": existing["content"],
            "tags": existing["tags"],
            "importance": existing["importance"],
        },
        context=user.get_audit_context(),
    )

    return SuccessResponse(success=True, message=f"Memory {memory_id} deleted")


@router.post(
    "/{memory_id}/share",
    response_model=MemoryResponse,
    responses={404: {"model": ErrorResponse}},
)
async def share_memory(
    memory_id: UUID,
    user: AuthenticatedUser,
) -> MemoryResponse:
    """Share a memory with your organization."""
    user.require_permission(Permission.MEMORY_SHARE)

    pool = await get_pool()
    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    result = await repo.set_shared(
        memory_id=memory_id,
        user_id=user.id,
        shared=True,
    )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found or you are not the owner",
        )

    await audit_repo.log(
        memory_id=memory_id,
        action_type="share",
        user_id=user.id,
        organization_id=user.organization_id,
        changed_fields=["shared"],
        old_values={"shared": False},
        new_values={"shared": True},
        context=user.get_audit_context(),
    )

    return _memory_to_response(result)


@router.post(
    "/{memory_id}/unshare",
    response_model=MemoryResponse,
    responses={404: {"model": ErrorResponse}},
)
async def unshare_memory(
    memory_id: UUID,
    user: AuthenticatedUser,
) -> MemoryResponse:
    """Unshare a memory from your organization."""
    pool = await get_pool()
    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    result = await repo.set_shared(
        memory_id=memory_id,
        user_id=user.id,
        shared=False,
    )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found or you are not the owner",
        )

    await audit_repo.log(
        memory_id=memory_id,
        action_type="unshare",
        user_id=user.id,
        organization_id=user.organization_id,
        changed_fields=["shared"],
        old_values={"shared": True},
        new_values={"shared": False},
        context=user.get_audit_context(),
    )

    return _memory_to_response(result)
