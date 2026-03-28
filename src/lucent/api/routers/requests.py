"""API router for request tracking and task queue."""

import json
import logging
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from jsonschema import ValidationError, validate
from pydantic import BaseModel, Field

from lucent.api.deps import AuthenticatedUser, get_pool
from lucent.constants import REQUEST_SOURCE_PATTERN

router = APIRouter(prefix="/requests", tags=["requests"])

_deprecation_logger = logging.getLogger("lucent.api.deprecation")


# ── Models ────────────────────────────────────────────────────────────────


class RequestCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str | None = None
    source: str = Field(default="user", pattern=REQUEST_SOURCE_PATTERN)
    priority: str = Field(default="medium", pattern=r"^(low|medium|high|urgent)$")
    dependency_policy: str = Field(
        default="strict", pattern=r"^(strict|permissive)$"
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
    sandbox_template_id: str | None = None  # Reference a saved sandbox template
    sandbox_config: dict | None = None  # Or inline sandbox config (template takes precedence)
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
    feedback: str = Field(..., min_length=1)


class ClaimBody(BaseModel):
    """JSON body for task claim (not query params)."""
    instance_id: str = Field(..., min_length=1, max_length=128)


class MemoryLinkCreate(BaseModel):
    memory_id: str
    relation: str = Field(default="created", pattern=r"^(created|read|updated)$")


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
        str(user.organization_id), status=status, source=source, limit=limit, offset=offset
    )


@router.get("/active")
async def list_active_work(user: AuthenticatedUser, pool=Depends(get_pool)):
    """Get all non-completed requests with task status summaries for the cognitive loop."""
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.list_active_work(str(user.organization_id))


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
    return await repo.get_active_summary(str(user.organization_id))


@router.get("/events")
async def recent_events(user: AuthenticatedUser, limit: int = 50, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.get_recent_events(str(user.organization_id), limit=limit)


@router.get("/{request_id}")
async def get_request(request_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    result = await repo.get_request_with_tasks(str(request_id), str(user.organization_id))
    if not result:
        raise HTTPException(404, "Request not found")
    return result


@router.patch("/{request_id}/status")
async def update_request_status(
    request_id: UUID,
    user: AuthenticatedUser,
    body: StatusUpdate = Body(...),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
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

    repo = RequestRepository(pool)
    req = await repo.get_request(str(request_id), str(user.organization_id))
    if not req:
        raise HTTPException(404, "Request not found")
    if req["status"] != "review":
        raise HTTPException(409, "Request not in review state")

    # Create a review record for backward compatibility
    from lucent.db.reviews import ReviewRepository

    review_repo = ReviewRepository(pool)
    await review_repo.create_review(
        request_id=str(request_id),
        organization_id=str(user.organization_id),
        status="approved",
        reviewer_user_id=str(user.id),
        reviewer_display_name=user.display_name or user.email,
        source="human",
    )

    return await repo.update_request_status(
        str(request_id), "completed", org_id=str(user.organization_id)
    )


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

    repo = RequestRepository(pool)
    req = await repo.get_request(str(request_id), str(user.organization_id))
    if not req:
        raise HTTPException(404, "Request not found")
    if req["status"] != "review":
        raise HTTPException(409, "Request not in review state")

    # Create a review record for backward compatibility
    from lucent.db.reviews import ReviewRepository

    review_repo = ReviewRepository(pool)
    await review_repo.create_review(
        request_id=str(request_id),
        organization_id=str(user.organization_id),
        status="rejected",
        reviewer_user_id=str(user.id),
        reviewer_display_name=user.display_name or user.email,
        comments=body.feedback,
        source="human",
    )

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE requests
               SET status = 'needs_rework',
                   review_feedback = $2,
                   review_count = review_count + 1,
                   reviewed_at = NOW(),
                   updated_at = NOW()
               WHERE id = $1 AND organization_id = $3""",
            request_id,
            body.feedback,
            user.organization_id,
        )
    result = await repo.get_request(str(request_id), str(user.organization_id))
    return result


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
    req = await repo.get_request(str(request_id), org_id)
    if not req:
        raise HTTPException(404, "Request not found")

    # Validate model against registry (matches MCP create_task behavior)
    if body.model:
        from lucent.model_registry import validate_model

        error = validate_model(body.model)
        if error:
            raise HTTPException(422, error)

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
            sandbox_template_id=body.sandbox_template_id,
            sandbox_config=body.sandbox_config,
            requesting_user_id=str(req["created_by"]) if req.get("created_by") else None,
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
    result = await repo.claim_task(str(task_id), body.instance_id, org_id=str(user.organization_id))
    if not result:
        raise HTTPException(409, "Task already claimed or not pending")
    return result


class TaskModelBody(BaseModel):
    model: str


@router.post("/tasks/{task_id}/model")
async def update_task_model(
    task_id: UUID,
    user: AuthenticatedUser,
    body: TaskModelBody = Body(...),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    result = await repo.update_task_model(str(task_id), body.model)
    if not result:
        raise HTTPException(404, "Task not found")
    return result


@router.post("/tasks/{task_id}/start")
async def start_task(task_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    result = await repo.start_task(str(task_id), org_id=str(user.organization_id))
    if not result:
        raise HTTPException(409, "Task not in claimed state")
    return result


class TaskCompleteBody(BaseModel):
    result: str = ""
    result_structured: dict | None = None
    result_summary: str | None = None
    validation_status: str = Field(
        default="not_applicable",
        pattern=r"^(not_applicable|valid|invalid|extraction_failed|fallback_used|repair_succeeded)$",
    )
    validation_errors: list | None = None


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: UUID,
    user: AuthenticatedUser,
    body: TaskCompleteBody = Body(...),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    task_row = await repo.get_task(str(task_id), org_id=str(user.organization_id))
    if not task_row:
        raise HTTPException(404, "Task not found")

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
            result_structured=body.result_structured,
            result_summary=body.result_summary,
            validation_status=body.validation_status,
            validation_errors=body.validation_errors,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not task:
        raise HTTPException(
            409,
            "Task not found or not in a transitionable state (must be claimed/running)",
        )
    return task


class TaskFailBody(BaseModel):
    error: str = ""


@router.post("/tasks/{task_id}/fail")
async def fail_task(
    task_id: UUID,
    user: AuthenticatedUser,
    body: TaskFailBody = Body(...),
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    task = await repo.fail_task(str(task_id), body.error, org_id=str(user.organization_id))
    if not task:
        raise HTTPException(
            409,
            "Task not found or not in a transitionable state (must be claimed/running)",
        )
    return task


@router.post("/tasks/{task_id}/release")
async def release_task(task_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    task = await repo.release_task(str(task_id), org_id=str(user.organization_id))
    if not task:
        raise HTTPException(409, "Task not in claimed/running state")
    return task


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
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
    task = await repo.get_task(str(task_id), str(user.organization_id))
    if not task:
        raise HTTPException(404, "Task not found")
    await repo.link_memory(str(task_id), body.memory_id, body.relation)
    return {"status": "linked"}


@router.get("/tasks/{task_id}/memories")
async def task_memories(task_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.list_task_memories(str(task_id), org_id=str(user.organization_id))


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
