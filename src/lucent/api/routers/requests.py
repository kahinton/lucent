"""API router for request tracking and task queue."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from lucent.api.deps import AuthenticatedUser, get_pool

router = APIRouter(prefix="/requests", tags=["requests"])


# ── Models ────────────────────────────────────────────────────────────────


class RequestCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str | None = None
    source: str = Field(default="user", pattern=r"^(user|cognitive|api|daemon)$")
    priority: str = Field(default="medium", pattern=r"^(low|medium|high|urgent)$")


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str | None = None
    agent_type: str | None = None
    agent_definition_id: str | None = None
    parent_task_id: str | None = None
    priority: str = Field(default="medium", pattern=r"^(low|medium|high|urgent)$")
    sequence_order: int = 0
    model: str | None = None
    sandbox_template_id: str | None = None  # Reference a saved sandbox template
    sandbox_config: dict | None = None  # Or inline sandbox config (template takes precedence)


class TaskEventCreate(BaseModel):
    event_type: str = Field(..., min_length=1, max_length=32)
    detail: str | None = None
    metadata: dict | None = None


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
    )


@router.get("")
async def list_requests(
    user: AuthenticatedUser,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.list_requests(
        str(user.organization_id), status=status, limit=limit, offset=offset
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
    status: str,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    result = await repo.update_request_status(
        str(request_id), status, org_id=str(user.organization_id)
    )
    if not result:
        raise HTTPException(404, "Request not found")
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

    # Validate that agent_type or agent_definition_id resolves to an approved definition
    def_repo = DefinitionRepository(pool)
    if body.agent_definition_id:
        agent_def = await def_repo.get_agent(body.agent_definition_id, org_id)
        if not agent_def or agent_def.get("status") != "active":
            raise HTTPException(
                422,
                f"Agent definition '{body.agent_definition_id}' not found or not approved. "
                f"Approve it at /definitions before assigning tasks.",
            )
    elif body.agent_type:
        agents = await def_repo.list_agents(org_id, status="active")
        if not any(a["name"] == body.agent_type for a in agents):
            raise HTTPException(
                422,
                f"No approved agent definition found for type '{body.agent_type}'. "
                f"Create and approve one at /definitions before assigning tasks.",
            )

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
    )


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
    instance_id: str,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    result = await repo.claim_task(str(task_id), instance_id, org_id=str(user.organization_id))
    if not result:
        raise HTTPException(409, "Task already claimed or not pending")
    return result


@router.post("/tasks/{task_id}/start")
async def start_task(task_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    result = await repo.start_task(str(task_id), org_id=str(user.organization_id))
    if not result:
        raise HTTPException(409, "Task not in claimed state")
    return result


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: UUID,
    result: str,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    task = await repo.complete_task(str(task_id), result, org_id=str(user.organization_id))
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/tasks/{task_id}/fail")
async def fail_task(
    task_id: UUID,
    error: str,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    task = await repo.fail_task(str(task_id), error, org_id=str(user.organization_id))
    if not task:
        raise HTTPException(404, "Task not found")
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
