"""API router for request tracking and task queue."""

import json
import logging
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from jsonschema import ValidationError, validate
from pydantic import BaseModel, Field

from lucent.api.deps import AuthenticatedUser, get_pool
from lucent.constants import REQUEST_SOURCE_PATTERN
from lucent.rbac import Role

router = APIRouter(prefix="/requests", tags=["requests"])

logger = logging.getLogger(__name__)
_deprecation_logger = logging.getLogger(f"{__name__}.deprecation")


def _is_daemon_user(user: AuthenticatedUser) -> bool:
    return user.role == Role.DAEMON or user.is_daemon_service


def _is_privileged_request_actor(user: AuthenticatedUser) -> bool:
    return user.role >= Role.ADMIN or _is_daemon_user(user)


async def _require_model_access(pool, model_id: str, user: AuthenticatedUser) -> None:
    from lucent.access_control import AccessControlService

    if not await AccessControlService(pool).can_access(
        str(user.id), "model", model_id, str(user.organization_id)
    ):
        raise HTTPException(403, "Model is not available to this user")


def _can_mutate_request(req: dict, user: AuthenticatedUser) -> bool:
    if _is_privileged_request_actor(user):
        return True
    created_by = req.get("created_by")
    if not created_by:
        return True
    return bool(created_by and str(created_by) == str(user.id))


def _require_request_mutation(req: dict, user: AuthenticatedUser) -> None:
    if not _can_mutate_request(req, user):
        raise HTTPException(404, "Request not found")


def _is_matching_sandbox_task_actor(task_id: str, user: AuthenticatedUser) -> bool:
    return bool(
        user.auth_method == "api_key"
        and getattr(user, "sandbox_task_id", None)
        and str(user.sandbox_task_id) == str(task_id)
    )


async def _get_task_and_request(
    repo, task_id: str, org_id: str, user: AuthenticatedUser
) -> tuple[dict, dict]:
    task = await repo.get_task(task_id, org_id=org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    req = await _get_visible_request(repo, str(task["request_id"]), user)
    if not req:
        raise HTTPException(404, "Task not found")
    return task, req


async def _require_task_mutation(repo, task_id: str, org_id: str, user: AuthenticatedUser) -> dict:
    task, req = await _get_task_and_request(repo, task_id, org_id, user)
    if _is_matching_sandbox_task_actor(task_id, user):
        return task
    _require_request_mutation(req, user)
    return task


def _effective_memory_user_id(user: AuthenticatedUser) -> UUID:
    return user.effective_memory_user_id


def _memory_admin_override(user: AuthenticatedUser) -> bool:
    return user.role >= Role.ADMIN and not user.is_memory_scoped


def _request_visibility_args(user: AuthenticatedUser) -> dict:
    if _is_daemon_user(user) and not user.is_memory_scoped:
        return {}
    return {
        "requester_user_id": str(user.effective_memory_user_id),
        "include_system": user.role >= Role.ADMIN and not user.is_memory_scoped,
    }


async def _get_visible_request(repo, request_id: str, user: AuthenticatedUser):
    return await repo.get_request(
        request_id,
        str(user.organization_id),
        **_request_visibility_args(user),
    )


def _build_memory_access(pool, user: AuthenticatedUser):
    from lucent.db import MemoryRepository
    from lucent.integrations.github_repo_access_service import GitHubRepoAccessService
    from lucent.services.memory_access_service import MemoryAccessService

    return MemoryAccessService(
        MemoryRepository(pool),
        GitHubRepoAccessService(pool),
        is_admin=_memory_admin_override(user),
    )


async def _require_accessible_memory(pool, memory_id: str, user: AuthenticatedUser) -> dict:
    try:
        memory_uuid = UUID(str(memory_id))
    except ValueError as exc:
        raise HTTPException(422, "Invalid memory_id") from exc

    memory_access = _build_memory_access(pool, user)
    memory = await memory_access.get_accessible(
        memory_uuid,
        _effective_memory_user_id(user),
        user.organization_id,
        memory_scope=user.memory_scope,
        is_admin=_memory_admin_override(user),
    )
    if not memory or memory.get("_access_denied"):
        raise HTTPException(404, "Memory not found")
    return memory


# ── Models ────────────────────────────────────────────────────────────────


class RequestCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str | None = None
    source: str = Field(default="user", pattern=REQUEST_SOURCE_PATTERN)
    priority: str = Field(default="medium", pattern=r"^(low|medium|high|urgent)$")
    dependency_policy: str = Field(
        default="strict", pattern=r"^(strict|permissive)$"
    )
    goal_id: str | None = Field(
        default=None,
        description=(
            "Memory ID of the goal this request advances. When set, the goal "
            "is validated as 'active' before the request is created."
        ),
    )
    goal_milestone_index: int | None = Field(
        default=None,
        ge=1,
        description=(
            "1-based index of the milestone within the goal's metadata. "
            "Required when the goal has a milestones array; the named "
            "milestone must currently be 'active'."
        ),
    )


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str | None = None
    agent_type: str | None = None
    agent_definition_id: str | None = None
    parent_task_id: str | None = None
    priority: str = Field(default="medium", pattern=r"^(low|medium|high|urgent)$")
    sequence_order: int = Field(default=0, ge=0)
    model: str | None = None
    reasoning_effort: str | None = Field(default=None, max_length=64)
    sandbox_template_id: str | None = None  # Reference a saved sandbox template
    sandbox_config: dict | None = None  # Or inline sandbox config (template takes precedence)
    requesting_user_id: str | None = None
    output_contract: dict | None = None  # Optional structured output contract (JSON Schema)
    output_schema: dict | None = None  # Backwards-compatible alias for output_contract.json_schema


class TaskEventCreate(BaseModel):
    event_type: str = Field(..., min_length=1, max_length=32)
    detail: str | None = None
    metadata: dict | None = None


class StatusUpdate(BaseModel):
    """JSON body for request status updates (not query params)."""
    status: str = Field(
        ...,
        pattern=r"^(pending|planned|in_progress|review|needs_rework|completed|failed|cancelled)$",
    )


class ReviewRejectBody(BaseModel):
    feedback: str = Field(..., min_length=1, max_length=10000)


class ClaimBody(BaseModel):
    """JSON body for task claim (not query params)."""
    instance_id: str = Field(..., min_length=1, max_length=128)


class TaskTransitionBody(BaseModel):
    """Optional ownership context for task lifecycle transitions."""
    instance_id: str | None = Field(default=None, min_length=1, max_length=128)


class InstanceRegistrationBody(BaseModel):
    instance_id: str = Field(..., min_length=1, max_length=128)
    hostname: str | None = Field(default=None, max_length=255)
    pid: int | None = Field(default=None, ge=1)
    roles: list[str] | None = None
    metadata: dict | None = None


class InstanceHeartbeatBody(BaseModel):
    metadata: dict | None = None


class InstanceStopBody(BaseModel):
    instance_id: str = Field(..., min_length=1, max_length=128)


class MemoryLinkCreate(BaseModel):
    memory_id: str
    relation: str = Field(default="created", pattern=r"^(created|read|updated)$")


class TaskOutputCreate(BaseModel):
    output_type: str = Field(
        default="link",
        pattern=r"^(link|github_issue|github_pr|email|document|file|memory|deployment|artifact|other)$",
    )
    provider: str | None = Field(default=None, max_length=64)
    title: str = Field(..., min_length=1, max_length=256)
    description: str | None = None
    url: str | None = None
    external_id: str | None = None
    mime_type: str | None = Field(default=None, max_length=128)
    metadata: dict | None = None
    is_primary: bool = False


# ── Request endpoints ─────────────────────────────────────────────────────


@router.post("")
async def create_request(
    body: RequestCreate, user: AuthenticatedUser, pool=Depends(get_pool)
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.create_request(
        title=body.title,
        description=body.description,
        source=body.source,
        priority=body.priority,
        created_by=str(user.id),
        org_id=str(user.organization_id),
        dependency_policy=body.dependency_policy,
        goal_id=body.goal_id,
        goal_milestone_index=body.goal_milestone_index,
    )


@router.get("")
async def list_requests(
    user: AuthenticatedUser,
    status: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.list_requests(
        str(user.organization_id),
        status=status,
        source=source,
        limit=limit,
        offset=offset,
        **_request_visibility_args(user),
    )


@router.get("/active")
async def list_active_work(user: AuthenticatedUser, pool=Depends(get_pool)):
    """Get all non-completed requests with task status summaries for the cognitive loop."""
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.list_active_work(str(user.organization_id))


@router.get("/planning-targets")
async def list_planning_targets(
    user: AuthenticatedUser,
    pool=Depends(get_pool),
    user_id: str | None = None,
    limit: int = 50,
):
    """Goal milestones the cognitive planner MUST progress this cycle.

    Returns a list pre-filtered to:
      - active goals only
      - first 'active' milestone whose start_after has passed
      - no open request already targeting (goal, milestone)

    The planner does not choose between entries — it advances every one
    by calling create_request with goal_id and goal_milestone_index from
    the response. Worst case under parallel cycles is a duplicate
    create_request which is rejected by the validator (status: skipped,
    reason: in-flight) and the planner moves on.

    When called with a user-scoped API key (memory_scope_user_id set),
    automatically restricts results to that user's goals so the per-user
    fan-out gets only its own user's targets.
    """
    from lucent.db.requests import RequestRepository

    # If the caller's key is scoped to a user, force-filter to that user.
    # An explicit ?user_id= override is only honored when it matches the
    # scoped user id (defense in depth — a scoped key could not actually
    # broaden its view but we shouldn't pretend otherwise).
    effective_user_id: str | None = user_id
    if user.memory_scope == "user" and user.memory_scope_user_id is not None:
        scoped = str(user.memory_scope_user_id)
        if effective_user_id and effective_user_id != scoped:
            effective_user_id = scoped
        else:
            effective_user_id = scoped

    repo = RequestRepository(pool)
    targets = await repo.list_planning_targets(
        str(user.organization_id),
        user_id=effective_user_id,
        limit=limit,
    )
    return {"targets": targets, "count": len(targets)}


@router.get("/recently-completed")
async def list_recently_completed(
    user: AuthenticatedUser,
    pool=Depends(get_pool),
    hours: int = 2,
):
    """Get requests completed in the last N hours for dedup in the cognitive loop."""
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    items = await repo.list_recently_completed(str(user.organization_id), hours=hours)
    return {"items": items}


@router.get("/review")
async def list_requests_in_review(
    user: AuthenticatedUser,
    limit: int = 50,
    offset: int = 0,
    pool=Depends(get_pool),
):
    """List requests in review or rework states."""
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.get_requests_in_review(
        str(user.organization_id), limit=limit, offset=offset
    )


@router.get("/summary")
async def request_summary(user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.get_active_summary(
        str(user.organization_id), **_request_visibility_args(user)
    )


@router.get("/events")
async def recent_events(user: AuthenticatedUser, limit: int = 50, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.get_recent_events(
        str(user.organization_id), limit=limit, **_request_visibility_args(user)
    )


@router.get("/{request_id}/memories")
async def request_memories(request_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    req = await _get_visible_request(repo, str(request_id), user)
    if not req:
        raise HTTPException(404, "Request not found")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT rm.memory_id, rm.relation, rm.created_at,
                      m.content, m.type AS memory_type, m.tags,
                      m.metadata->>'status' AS status
               FROM request_memories rm
               JOIN memories m ON rm.memory_id = m.id
               WHERE rm.request_id = $1
                      AND m.organization_id = $2
                      AND m.deleted_at IS NULL
               ORDER BY rm.created_at""",
            request_id,
                user.organization_id,
        )
    memory_access = _build_memory_access(pool, user)
    items = await memory_access.filter_memory_links(
        [dict(r) for r in rows],
        user_id=_effective_memory_user_id(user),
        organization_id=user.organization_id,
        memory_scope=user.memory_scope,
    )
    return {"items": items}


@router.get("/{request_id}")
async def get_request(request_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    result = await repo.get_request_with_tasks(
        str(request_id),
        str(user.organization_id),
        **_request_visibility_args(user),
    )
    if not result:
        raise HTTPException(404, "Request not found")
    memory_access = _build_memory_access(pool, user)
    return await memory_access.filter_request_detail_memory_links(
        result,
        user_id=_effective_memory_user_id(user),
        organization_id=user.organization_id,
        memory_scope=user.memory_scope,
    )


@router.patch("/{request_id}/status")
async def update_request_status(
    request_id: UUID,
    user: AuthenticatedUser,
    body: StatusUpdate = Body(...),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    req = await _get_visible_request(repo, str(request_id), user)
    if not req:
        raise HTTPException(404, "Request not found")
    _require_request_mutation(req, user)
    result = await repo.update_request_status(
        str(request_id), body.status, org_id=str(user.organization_id)
    )
    if not result:
        raise HTTPException(404, "Request not found")
    return result


@router.post("/{request_id}/review/approve")
async def approve_request_review(
    request_id: UUID,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    """Approve a request in review state.

    .. deprecated::
        Use POST /api/reviews instead for first-class review records.
        This endpoint will be removed in a future version.
    """
    _deprecation_logger.warning(
        "Deprecated endpoint POST /requests/%s/review/approve called. "
        "Use POST /api/reviews instead.",
        request_id,
    )
    from lucent.db.requests import RequestRepository

    if (
        user.role < Role.ADMIN
        and user.role != Role.DAEMON
        and not user.is_daemon_service
    ):
        raise HTTPException(403, "Admin or owner role required")

    repo = RequestRepository(pool)
    req = await _get_visible_request(repo, str(request_id), user)
    if not req:
        raise HTTPException(404, "Request not found")
    if req["status"] != "review":
        raise HTTPException(409, "Request not in review state")
    request_creator = req.get("created_by")
    if request_creator and str(request_creator) == str(user.id):
        raise HTTPException(403, "Request creators cannot review their own requests")

    # Create a review record for backward compatibility
    from lucent.db.reviews import ReviewRepository

    review_repo = ReviewRepository(pool)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await review_repo.create_review(
                request_id=str(request_id),
                organization_id=str(user.organization_id),
                status="approved",
                reviewer_user_id=str(user.id),
                reviewer_display_name=user.display_name or user.email,
                source="human",
                conn=conn,
            )

            updated = await review_repo.mark_request_completed(
                request_id=str(request_id),
                organization_id=str(user.organization_id),
                conn=conn,
            )
            if not updated:
                raise HTTPException(409, "Request is not in review state")
            return updated


@router.post("/{request_id}/review/reject")
async def reject_request_review(
    request_id: UUID,
    user: AuthenticatedUser,
    body: ReviewRejectBody = Body(...),
    pool=Depends(get_pool),
):
    """Reject a request in review state with feedback.

    .. deprecated::
        Use POST /api/reviews instead for first-class review records.
        This endpoint will be removed in a future version.
    """
    _deprecation_logger.warning(
        "Deprecated endpoint POST /requests/%s/review/reject called. "
        "Use POST /api/reviews instead.",
        request_id,
    )
    from lucent.db.requests import RequestRepository

    if (
        user.role < Role.ADMIN
        and user.role != Role.DAEMON
        and not user.is_daemon_service
    ):
        raise HTTPException(403, "Admin or owner role required")

    repo = RequestRepository(pool)
    req = await _get_visible_request(repo, str(request_id), user)
    if not req:
        raise HTTPException(404, "Request not found")
    if req["status"] != "review":
        raise HTTPException(409, "Request not in review state")
    request_creator = req.get("created_by")
    if request_creator and str(request_creator) == str(user.id):
        raise HTTPException(403, "Request creators cannot review their own requests")

    # Create a review record for backward compatibility
    from lucent.db.reviews import ReviewRepository

    review_repo = ReviewRepository(pool)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await review_repo.create_review(
                request_id=str(request_id),
                organization_id=str(user.organization_id),
                status="rejected",
                reviewer_user_id=str(user.id),
                reviewer_display_name=user.display_name or user.email,
                comments=body.feedback,
                source="human",
                conn=conn,
            )

            updated = await review_repo.mark_request_needs_rework(
                request_id=str(request_id),
                organization_id=str(user.organization_id),
                feedback=body.feedback,
                conn=conn,
            )
            if not updated:
                raise HTTPException(409, "Request is not in review state")
            return updated


# ── Task endpoints ────────────────────────────────────────────────────────


@router.post("/{request_id}/tasks")
async def create_task(
    request_id: UUID,
    body: TaskCreate,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    from lucent.db.definitions import DefinitionRepository
    from lucent.db.requests import RequestRepository

    org_id = str(user.organization_id)
    repo = RequestRepository(pool)
    req = await _get_visible_request(repo, str(request_id), user)
    if not req:
        raise HTTPException(404, "Request not found")
    _require_request_mutation(req, user)

    requesting_user_id = str(req["created_by"]) if req.get("created_by") else None
    if body.requesting_user_id:
        if not _is_privileged_request_actor(user):
            raise HTTPException(403, "Only admins/owners/daemon can set requesting_user_id")
        async with pool.acquire() as conn:
            target_user_exists = await conn.fetchval(
                """SELECT 1 FROM users
                   WHERE id = $1::uuid
                     AND organization_id = $2::uuid
                     AND is_active = true""",
                body.requesting_user_id,
                org_id,
            )
        if not target_user_exists:
            raise HTTPException(422, "requesting_user_id must be an active user in this org")
        requesting_user_id = body.requesting_user_id

    # Validate model against registry (matches MCP create_task behavior)
    if body.model:
        from lucent.model_registry import validate_model, validate_reasoning_effort

        error = validate_model(body.model, require_tools=True)
        if error:
            raise HTTPException(422, error)
        await _require_model_access(pool, body.model, user)
        effort_error = validate_reasoning_effort(body.model, body.reasoning_effort)
        if effort_error:
            raise HTTPException(422, effort_error)
    elif body.reasoning_effort:
        raise HTTPException(422, "reasoning_effort requires model")

    # Validate that agent_type or agent_definition_id resolves to an approved definition
    def_repo = DefinitionRepository(pool)
    if body.agent_definition_id:
        agent_def = await def_repo.get_agent(
            body.agent_definition_id,
            org_id,
            requester_user_id=str(user.id),
            requester_role=user.role.value,
        )
        if not agent_def or agent_def.get("status") != "active":
            raise HTTPException(
                422,
                f"Agent definition '{body.agent_definition_id}' not found or not approved. "
                f"Approve it at /definitions before assigning tasks.",
            )
    elif body.agent_type:
        agents = (
            await def_repo.list_agents(
                org_id,
                status="active",
                limit=200,
                requester_user_id=str(user.id),
                requester_role=user.role.value,
            )
        )["items"]
        if not any(a["name"] == body.agent_type for a in agents):
            raise HTTPException(
                422,
                f"No approved agent definition found for type '{body.agent_type}'. "
                f"Create and approve one at /definitions before assigning tasks.",
            )

    if body.output_contract and body.output_schema:
        raise HTTPException(422, "Provide either output_contract or output_schema, not both")
    output_contract = body.output_contract
    if body.output_schema:
        # Compatibility shim: accept output_schema and normalize to the contract shape.
        output_contract = {
            "json_schema": body.output_schema,
            "on_failure": "fallback",
            "max_retries": 1,
        }

    try:
        return await repo.create_task(
            request_id=str(request_id),
            title=body.title,
            org_id=org_id,
            description=body.description,
            agent_type=body.agent_type,
            agent_definition_id=body.agent_definition_id,
            parent_task_id=body.parent_task_id,
            priority=body.priority,
            sequence_order=body.sequence_order,
            model=body.model,
            reasoning_effort=body.reasoning_effort,
            sandbox_template_id=body.sandbox_template_id,
            sandbox_config=body.sandbox_config,
            requesting_user_id=requesting_user_id,
            output_contract=output_contract,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{request_id}/tasks")
async def list_tasks(
    request_id: UUID,
    user: AuthenticatedUser,
    status: str | None = None,
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.list_tasks(str(request_id), status=status, org_id=str(user.organization_id))


@router.post("/tasks/{task_id}/claim")
async def claim_task(
    task_id: UUID,
    user: AuthenticatedUser,
    body: ClaimBody = Body(...),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)
    result = await repo.claim_task(str(task_id), body.instance_id, org_id=str(user.organization_id))
    if not result:
        raise HTTPException(409, "Task already claimed or not pending")
    return result


class TaskModelBody(BaseModel):
    model: str
    reasoning_effort: str | None = Field(default=None, max_length=64)


@router.post("/tasks/{task_id}/model")
async def update_task_model(
    task_id: UUID,
    user: AuthenticatedUser,
    body: TaskModelBody = Body(...),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    task, req = await _get_task_and_request(
        repo, str(task_id), str(user.organization_id), user
    )
    _require_request_mutation(req, user)
    from lucent.model_registry import validate_model, validate_reasoning_effort

    error = validate_model(body.model, require_tools=True)
    if error:
        raise HTTPException(422, error)
    await _require_model_access(pool, body.model, user)
    effort_error = validate_reasoning_effort(body.model, body.reasoning_effort)
    if effort_error:
        raise HTTPException(422, effort_error)

    result = await repo.update_task_model(str(task_id), body.model)
    if result:
        result = await repo.update_task_reasoning_effort(str(task_id), body.reasoning_effort)
    if not result:
        raise HTTPException(404, "Task not found")
    return result


class TaskEditBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)
    description: str | None = None
    model: str | None = None
    reasoning_effort: str | None = Field(default=None, max_length=64)
    agent_type: str | None = None
    sandbox_template_id: str | None = None
    clear_sandbox_template: bool = False


@router.patch("/tasks/{task_id}")
async def edit_pending_task(
    task_id: UUID,
    user: AuthenticatedUser,
    body: TaskEditBody = Body(...),
    pool=Depends(get_pool),
):
    """Edit a pending/planned task in place.

    Tasks that have been claimed or completed cannot be edited — their work
    is either in flight or done. Validates model against the registry and
    agent_type against approved definitions before applying changes.
    """
    from lucent.db.definitions import DefinitionRepository
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    org_id = str(user.organization_id)

    # Fetch existing task to confirm it exists and is editable
    existing = await _require_task_mutation(repo, str(task_id), org_id, user)
    if existing.get("status") in repo._NON_EDITABLE_TASK_STATUSES:
        raise HTTPException(
            409,
            (
                f"Task is in status '{existing.get('status')}' — tasks that are "
                "running or already completed cannot be edited."
            ),
        )

    if body.model:
        from lucent.model_registry import validate_model, validate_reasoning_effort

        error = validate_model(body.model, require_tools=True)
        if error:
            raise HTTPException(422, error)
        await _require_model_access(pool, body.model, user)
        effort_error = validate_reasoning_effort(body.model, body.reasoning_effort)
        if effort_error:
            raise HTTPException(422, effort_error)
    elif body.reasoning_effort:
        raise HTTPException(422, "reasoning_effort requires model")

    if body.agent_type:
        def_repo = DefinitionRepository(pool)
        agents = (
            await def_repo.list_agents(
                org_id,
                status="active",
                limit=200,
                requester_user_id=str(user.id),
                requester_role=user.role.value,
            )
        )["items"]
        if not any(a["name"] == body.agent_type for a in agents):
            raise HTTPException(
                422,
                f"No approved agent definition found for type '{body.agent_type}'.",
            )

    if body.sandbox_template_id and not body.clear_sandbox_template:
        # Validate template exists and is approved/accessible
        try:
            from lucent.db.sandbox_template import SandboxTemplateRepository

            tpl_repo = SandboxTemplateRepository(pool)
            tpl = await tpl_repo.get_accessible(
                body.sandbox_template_id,
                organization_id=org_id,
                user_id=str(user.id),
                user_role=user.role.value,
            )
            if not tpl or tpl.get("status") != "approved":
                raise HTTPException(
                    422,
                    f"Sandbox template '{body.sandbox_template_id}' not found or not approved.",
                )
        except ImportError:
            # Sandbox templates module optional in some deployments
            pass

    next_reasoning_effort = (
        body.reasoning_effort if body.model is None else (body.reasoning_effort or "")
    )

    result = await repo.update_pending_task(
        str(task_id),
        org_id,
        title=body.title,
        description=body.description,
        model=body.model,
        reasoning_effort=next_reasoning_effort,
        agent_type=body.agent_type,
        sandbox_template_id=body.sandbox_template_id if not body.clear_sandbox_template else None,
        clear_sandbox_template=body.clear_sandbox_template,
    )
    if not result:
        raise HTTPException(
            409,
            (
                "Task could not be updated — it may have been claimed by the "
                "daemon between the read and write."
            ),
        )
    return result


@router.post("/tasks/{task_id}/start")
async def start_task(
    task_id: UUID,
    user: AuthenticatedUser,
    body: TaskTransitionBody = Body(default=TaskTransitionBody()),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)
    result = await repo.start_task(
        str(task_id),
        org_id=str(user.organization_id),
        instance_id=body.instance_id,
    )
    if not result:
        raise HTTPException(409, "Task not in claimed state")
    return result


class TaskCompleteBody(BaseModel):
    result: str = ""
    instance_id: str | None = Field(default=None, min_length=1, max_length=128)
    result_structured: dict | None = None
    result_summary: str | None = None
    validation_status: str = Field(
        default="not_applicable",
        pattern=r"^(not_applicable|valid|invalid|extraction_failed|fallback_used|repair_succeeded)$",
    )
    validation_errors: list | None = None
    outputs: list[TaskOutputCreate] | None = None


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: UUID,
    user: AuthenticatedUser,
    body: TaskCompleteBody = Body(...),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    task_row = await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)

    output_contract = task_row.get("output_contract")
    if isinstance(output_contract, str):
        output_contract = json.loads(output_contract)
    if output_contract and body.validation_status in ("valid", "repair_succeeded"):
        if body.result_structured is None:
            raise HTTPException(
                422,
                "result_structured is required when validation_status is valid/repair_succeeded",
            )
        try:
            validate(instance=body.result_structured, schema=output_contract.get("json_schema", {}))
        except ValidationError as exc:
            raise HTTPException(
                422, f"result_structured failed schema validation: {exc.message}"
            ) from exc

    try:
        task = await repo.complete_task(
            str(task_id),
            body.result,
            org_id=str(user.organization_id),
            instance_id=body.instance_id,
            result_structured=body.result_structured,
            result_summary=body.result_summary,
            validation_status=body.validation_status,
            validation_errors=body.validation_errors,
            outputs=[o.model_dump(exclude_none=True) for o in (body.outputs or [])],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not task:
        raise HTTPException(
            409,
            "Task not found or not in a transitionable state (must be claimed/running)",
        )
    return task


@router.post("/tasks/{task_id}/outputs")
async def create_task_output(
    task_id: UUID,
    user: AuthenticatedUser,
    body: TaskOutputCreate,
    pool=Depends(get_pool),
):
    """Record a user-facing deliverable produced by a task."""
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)
    try:
        return await repo.create_task_output(
            task_id=str(task_id),
            org_id=str(user.organization_id),
            output=body.model_dump(exclude_none=True),
            created_by=str(user.id),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class TaskFailBody(BaseModel):
    error: str = ""
    result: str | None = None
    instance_id: str | None = Field(default=None, min_length=1, max_length=128)


@router.post("/tasks/{task_id}/fail")
async def fail_task(
    task_id: UUID,
    user: AuthenticatedUser,
    body: TaskFailBody = Body(...),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)
    task = await repo.fail_task(
        str(task_id),
        body.error,
        org_id=str(user.organization_id),
        instance_id=body.instance_id,
        result=body.result,
    )
    if not task:
        raise HTTPException(
            409,
            "Task not found or not in a transitionable state (must be claimed/running)",
        )
    return task


@router.post("/tasks/{task_id}/release")
async def release_task(
    task_id: UUID,
    user: AuthenticatedUser,
    body: TaskTransitionBody = Body(default=TaskTransitionBody()),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)
    task = await repo.release_task(
        str(task_id),
        org_id=str(user.organization_id),
        instance_id=body.instance_id,
    )
    if not task:
        raise HTTPException(409, "Task not in claimed/running state")
    return task


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)
    task = await repo.retry_task(str(task_id), org_id=str(user.organization_id))
    if not task:
        raise HTTPException(409, "Task not in failed state")
    return task


@router.post("/tasks/{task_id}/retry-with-feedback")
async def retry_task_with_feedback(
    task_id: UUID,
    user: AuthenticatedUser,
    body: ReviewRejectBody = Body(...),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)
    task = await repo.retry_task_with_feedback(
        str(task_id), body.feedback, org_id=str(user.organization_id)
    )
    if not task:
        raise HTTPException(409, "Task not in failed state")
    return task


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.list_task_events(str(task_id), org_id=str(user.organization_id))


@router.post("/tasks/{task_id}/events")
async def add_task_event(
    task_id: UUID,
    body: TaskEventCreate,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)
    task = await repo.get_task(str(task_id), str(user.organization_id))
    if not task:
        raise HTTPException(404, "Task not found")
    return await repo.add_task_event(
        str(task_id),
        body.event_type,
        body.detail,
        body.metadata,
    )


@router.post("/tasks/{task_id}/memories")
async def link_memory(
    task_id: UUID,
    body: MemoryLinkCreate,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)
    await _require_accessible_memory(pool, body.memory_id, user)
    await repo.link_memory(
        str(task_id),
        body.memory_id,
        body.relation,
        org_id=str(user.organization_id),
    )
    return {"status": "linked"}


@router.get("/tasks/{task_id}/memories")
async def task_memories(task_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    result = await repo.list_task_memories(str(task_id), org_id=str(user.organization_id))
    memory_access = _build_memory_access(pool, user)
    result["items"] = await memory_access.filter_memory_links(
        result.get("items") or [],
        user_id=_effective_memory_user_id(user),
        organization_id=user.organization_id,
        memory_scope=user.memory_scope,
    )
    result["total_count"] = len(result["items"])
    result["has_more"] = False
    return result


# ── Queue management ──────────────────────────────────────────────────────


@router.get("/queue/pending")
async def pending_queue(user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.list_pending_tasks(str(user.organization_id))


@router.post("/queue/release-stale")
async def release_stale(
    user: AuthenticatedUser,
    stale_minutes: int = 30,
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    count = await repo.release_stale_tasks(stale_minutes, org_id=str(user.organization_id))
    return {"released": count}


@router.post("/queue/reconcile")
async def reconcile_statuses(
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    """Reconcile request statuses that got out of sync with their tasks."""
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    fixed = await repo.reconcile_request_statuses(org_id=str(user.organization_id))
    return {"reconciled": fixed}


@router.post("/instances/register")
async def register_instance(
    user: AuthenticatedUser,
    body: InstanceRegistrationBody = Body(...),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.register_instance(
        org_id=str(user.organization_id),
        instance_id=body.instance_id,
        hostname=body.hostname,
        pid=body.pid,
        roles=body.roles,
        metadata=body.metadata,
        status="active",
    )


@router.post("/instances/{instance_id}/heartbeat")
async def heartbeat_instance(
    instance_id: str,
    user: AuthenticatedUser,
    body: InstanceHeartbeatBody = Body(default=InstanceHeartbeatBody()),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    row = await repo.heartbeat_instance(
        org_id=str(user.organization_id),
        instance_id=instance_id,
        metadata=body.metadata,
    )
    if not row:
        raise HTTPException(404, "Instance not found")
    return row


@router.post("/instances/stop")
async def stop_instance(
    user: AuthenticatedUser,
    body: InstanceStopBody = Body(...),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    row = await repo.mark_instance_stopped(
        org_id=str(user.organization_id),
        instance_id=body.instance_id,
    )
    if not row:
        raise HTTPException(404, "Instance not found")
    return row
