"""API endpoints for proactive Lucent↔user interactions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from lucent.api.deps import AuthenticatedUser, get_pool
from lucent.rbac import Role

router = APIRouter(prefix="/user-interactions", tags=["user-interactions"])


class InteractionReferenceBody(BaseModel):
    reference_type: str = Field(
        default="other",
        pattern=r"^(request|task|task_output|memory|workflow|schedule_run|llm_session|url|other)$",
    )
    reference_id: UUID | None = None
    label: str | None = Field(default=None, max_length=256)
    url: str | None = Field(default=None, max_length=2048)
    metadata: dict[str, Any] | None = None


class InteractionCreateBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    body: str = Field(..., min_length=1)
    source: str = Field(
        default="daemon",
        pattern=r"^(daemon|workflow|task|request|integration|system|human)$",
    )
    interaction_type: str = Field(
        default="message",
        pattern=r"^(message|clarification|review|decision|workflow_output|handoff)$",
    )
    priority: str = Field(default="medium", pattern=r"^(low|medium|high|urgent)$")
    requires_response: bool = False
    response_prompt: str | None = None
    metadata: dict[str, Any] | None = None
    references: list[InteractionReferenceBody] = Field(default_factory=list)
    dedupe_key: str | None = Field(default=None, max_length=512)
    user_id: UUID | None = Field(
        default=None,
        description="Target user. Omit to target the authenticated/effective user.",
    )


class InteractionReplyBody(BaseModel):
    body: str = Field(..., min_length=1, max_length=20000)
    metadata: dict[str, Any] | None = None


class InteractionCloseBody(BaseModel):
    note: str | None = Field(default=None, max_length=20000)


def _target_user_id(user: AuthenticatedUser, requested: UUID | None) -> UUID | None:
    if user.memory_scope == "user" and user.memory_scope_user_id is not None:
        if requested and str(requested) != str(user.memory_scope_user_id):
            raise HTTPException(403, "API key is scoped to a different user")
        return user.memory_scope_user_id
    if requested:
        if user.role < Role.ADMIN and str(requested) != str(user.id):
            raise HTTPException(403, "Only admins/owners can target another user")
        return requested
    return user.id


def _require_daemon_interaction_scope(user: AuthenticatedUser) -> None:
    """Creation is for daemon/workflow producers, not arbitrary read-only keys."""
    if user.role >= Role.ADMIN or user.external_id == "daemon-service":
        return
    user.require_scope("daemon-tasks")


@router.post("")
async def create_user_interaction(
    body: InteractionCreateBody,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    """Create a proactive message/clarification for a user.

    Daemon-scoped API keys can call this to pause work and ask the human for
    missing context. ``dedupe_key`` prevents recurring cycles from creating
    duplicate open messages.
    """
    if not user.organization_id:
        raise HTTPException(400, "Organization context required")
    _require_daemon_interaction_scope(user)
    from lucent.db.user_interactions import UserInteractionRepository

    repo = UserInteractionRepository(pool)
    try:
        return await repo.create_interaction(
            org_id=str(user.organization_id),
            user_id=_target_user_id(user, body.user_id),
            created_by=str(user.id),
            title=body.title,
            body=body.body,
            source=body.source,
            interaction_type=body.interaction_type,
            priority=body.priority,
            requires_response=body.requires_response,
            response_prompt=body.response_prompt,
            metadata=body.metadata or {},
            references=[r.model_dump(exclude_none=True) for r in body.references],
            dedupe_key=body.dedupe_key,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("")
async def list_user_interactions(
    user: AuthenticatedUser,
    status: str | None = None,
    include_resolved: bool = False,
    limit: int = 25,
    offset: int = 0,
    pool=Depends(get_pool),
):
    if not user.organization_id:
        raise HTTPException(400, "Organization context required")
    from lucent.db.user_interactions import UserInteractionRepository

    repo = UserInteractionRepository(pool)
    return await repo.list_interactions(
        org_id=str(user.organization_id),
        user_id=str(user.effective_memory_user_id),
        status=status,
        include_resolved=include_resolved,
        limit=min(max(limit, 1), 100),
        offset=max(offset, 0),
    )


@router.get("/attention-count")
async def user_interaction_attention_count(
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    if not user.organization_id:
        raise HTTPException(400, "Organization context required")
    from lucent.db.user_interactions import UserInteractionRepository

    repo = UserInteractionRepository(pool)
    count = await repo.count_attention_needed(
        org_id=str(user.organization_id),
        user_id=str(user.effective_memory_user_id),
    )
    return {"count": count}


@router.get("/{interaction_id}")
async def get_user_interaction(
    interaction_id: UUID,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    if not user.organization_id:
        raise HTTPException(400, "Organization context required")
    from lucent.db.user_interactions import UserInteractionRepository

    repo = UserInteractionRepository(pool)
    detail = await repo.get_interaction(
        str(interaction_id),
        str(user.organization_id),
        user_id=str(user.effective_memory_user_id),
    )
    if not detail:
        raise HTTPException(404, "Interaction not found")
    return detail


@router.post("/{interaction_id}/reply")
async def reply_to_user_interaction(
    interaction_id: UUID,
    body: InteractionReplyBody,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    if not user.organization_id:
        raise HTTPException(400, "Organization context required")
    from lucent.db.user_interactions import UserInteractionRepository

    repo = UserInteractionRepository(pool)
    existing = await repo.get_interaction(
        str(interaction_id),
        str(user.organization_id),
        user_id=str(user.effective_memory_user_id),
    )
    if not existing:
        raise HTTPException(404, "Interaction not found")
    try:
        return await repo.add_message(
            interaction_id=str(interaction_id),
            org_id=str(user.organization_id),
            sender_type="user",
            sender_user_id=str(user.id),
            body=body.body,
            metadata=body.metadata or {},
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{interaction_id}/resolve")
async def resolve_user_interaction(
    interaction_id: UUID,
    user: AuthenticatedUser,
    body: InteractionCloseBody = Body(default=InteractionCloseBody()),
    pool=Depends(get_pool),
):
    if not user.organization_id:
        raise HTTPException(400, "Organization context required")
    from lucent.db.user_interactions import UserInteractionRepository

    repo = UserInteractionRepository(pool)
    result = await repo.resolve_interaction(
        interaction_id=str(interaction_id),
        org_id=str(user.organization_id),
        user_id=str(user.effective_memory_user_id),
        note=body.note,
    )
    if not result:
        raise HTTPException(404, "Interaction not found")
    return result


@router.post("/{interaction_id}/dismiss")
async def dismiss_user_interaction(
    interaction_id: UUID,
    user: AuthenticatedUser,
    body: InteractionCloseBody = Body(default=InteractionCloseBody()),
    pool=Depends(get_pool),
):
    if not user.organization_id:
        raise HTTPException(400, "Organization context required")
    from lucent.db.user_interactions import UserInteractionRepository

    repo = UserInteractionRepository(pool)
    result = await repo.dismiss_interaction(
        interaction_id=str(interaction_id),
        org_id=str(user.organization_id),
        user_id=str(user.effective_memory_user_id),
        note=body.note,
    )
    if not result:
        raise HTTPException(404, "Interaction not found")
    return result
