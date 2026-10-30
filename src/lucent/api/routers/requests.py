"""API router for request tracking and task queue."""

import json
import logging
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from jsonschema import ValidationError, validate
from pydantic import BaseModel, Field

from lucent.api.deps import AuthenticatedUser, get_pool
from lucent.constants import REQUEST_SOURCE_PATTERN
from lucent.db.pool import scoped_acquire
from lucent.rbac import Role
from lucent.settings import (
    custom_tooling_enabled,
    fusion_best_of_n_enabled,
    fusion_max_candidates,
    fusion_synthesis_enabled,
    sandboxes_enabled,
)

router = APIRouter(prefix="/requests", tags=["requests"])

logger = logging.getLogger(__name__)


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
    # Remote-agent keys run as role ``daemon`` (service identity), which would
    # otherwise make them privileged request actors for the whole organization.
    # They are NOT: a remote may only transition the tasks it personally
    # claimed, which is enforced task-by-task in ``_require_task_mutation``.
    if _is_remote_agent_key(user):
        return False
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


def _is_remote_agent_key(user: AuthenticatedUser) -> bool:
    """True for any API key carrying the ``remote-agent`` scope.

    Deliberately scope-based rather than role-based: the remote identity user's
    role (``daemon``) is shared with real daemons, so role alone cannot
    distinguish a trusted in-process daemon from an external remote peer.
    """
    return bool(
        user.auth_method == "api_key"
        and "remote-agent" in (user.api_key_scopes or [])
    )


def _is_remote_task_actor(task: dict, user: AuthenticatedUser) -> bool:
    """True when a remote-agent key is reporting on a task it holds a claim on.

    A remote may only transition work it claimed for itself.
    """
    if not _is_remote_agent_key(user):
        return False
    remote_agent_id = getattr(user, "remote_agent_id", None)
    if not remote_agent_id:
        return False
    return str(task.get("claimed_by") or "") == f"remote:{remote_agent_id}"


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
    if _is_remote_task_actor(task, user):
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
    backend_id: str | None = Field(
        default=None,
        description=(
            "Coding-engine backend pin for the work this request produces. "
            "The daemon inherits it for tasks that do not override it."
        ),
    )
    requested_model: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Model the daemon should run for this request's tasks unless a "
            "task names one."
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
    decision_config: dict | None = None  # Decision-step payload (System One questions)
    fusion_config: dict | None = None  # Fusion payload (chat candidates + decision arbiter)
    task_kind: str | None = None  # Registered task kind (list_task_kinds); inferred when omitted
    execution_config: dict | None = None  # Generic per-kind config (forward-compatible form)
    backend_id: str | None = None  # Pin a coding-engine backend (e.g. Codex via Ollama)
    # Pin execution to a registered remote instance (Agent Control Protocol).
    # Mutually exclusive with sandbox execution: a remote runs the task in its
    # own environment, so a sandbox template/config on a remote-targeted task
    # would be silently ignored — reject it instead of pretending.
    remote_target_id: str | None = None


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
    # Task IDs this instance currently considers its own active work. When
    # present, the claim heartbeat is scoped to them: a task this instance has
    # already finished and detached must NOT keep being renewed, or a lost
    # write-back becomes invisible and the reaper never recovers it. None means
    # "legacy callers, refresh every claim I own".
    active_task_ids: list[str] | None = None


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

    if body.backend_id:
        from lucent.llm.backend_selection import validate_request_execution_pin

        backend_error = await validate_request_execution_pin(
            pool,
            body.backend_id,
            body.requested_model,
            org_id=str(user.organization_id),
        )
        if backend_error:
            raise HTTPException(422, backend_error)

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
        backend_id=body.backend_id,
        requested_model=body.requested_model,
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
    return await repo.list_active_work(
        str(user.organization_id), **_request_visibility_args(user)
    )


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
    fan-out gets only its own user's targets. Every non-daemon caller —
    including humans with unscoped personal API keys and owners — is
    scoped to their own goals, mirroring _request_visibility_args.
    Only an unscoped daemon-service caller keeps the org-wide view.
    """
    from lucent.db.requests import RequestRepository

    # Scope by caller identity, mirroring _request_visibility_args:
    #   - an unscoped daemon-service caller (RequestAPI's API_HEADERS
    #     fallback) legitimately serves the whole org — keep org-wide;
    #   - a user-scoped API key (per-user cognitive fan-out) is forced
    #     to the scoped user; an explicit ?user_id= override is honored
    #     only when it matches the scoped user id (defense in depth —
    #     a scoped key could not actually broaden its view but we
    #     shouldn't pretend otherwise);
    #   - every other caller sees only their own goals, regardless of
    #     role or any caller-supplied ?user_id=.
    if _is_daemon_user(user) and not user.is_memory_scoped:
        effective_user_id = None
    elif user.memory_scope == "user" and user.memory_scope_user_id is not None:
        effective_user_id = str(user.memory_scope_user_id)
    else:
        effective_user_id = str(user.effective_memory_user_id)

    repo = RequestRepository(pool)
    targets = await repo.list_planning_targets(
        str(user.organization_id),
        user_id=effective_user_id,
        role="daemon" if _is_daemon_user(user) and not user.is_memory_scoped else "member",
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
    items = await repo.list_recently_completed(
        str(user.organization_id), hours=hours, **_request_visibility_args(user)
    )
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
        str(user.organization_id), limit=limit, offset=offset,
        **_request_visibility_args(user),
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


@router.get("/queue")
async def queued_tasks(
    user: AuthenticatedUser,
    limit: int = 50,
    offset: int = 0,
    pool=Depends(get_pool),
):
    """List the full pending/planned backlog, including work not ready to run."""
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    return await repo.list_queued_tasks(
        str(user.organization_id),
        limit=min(max(limit, 1), 100),
        offset=max(offset, 0),
        **_request_visibility_args(user),
    )


@router.get("/{request_id}/memories")
async def request_memories(request_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    req = await _get_visible_request(repo, str(request_id), user)
    if not req:
        raise HTTPException(404, "Request not found")
    rows = await repo.get_request_memory_links(
        str(request_id),
        str(user.organization_id),
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
    org_sandboxes_enabled = sandboxes_enabled(organization_id=org_id)
    if (body.sandbox_template_id or body.sandbox_config) and not org_sandboxes_enabled:
        raise HTTPException(
            409,
            "Sandboxes are disabled for this organization by the "
            "features.sandboxes_enabled setting",
        )
    repo = RequestRepository(pool)
    req = await _get_visible_request(repo, str(request_id), user)
    if not req:
        raise HTTPException(404, "Request not found")
    _require_request_mutation(req, user)

    requesting_user_id = str(req["created_by"]) if req.get("created_by") else None
    if body.requesting_user_id:
        if not _is_privileged_request_actor(user):
            raise HTTPException(403, "Only admins/owners/daemon can set requesting_user_id")
        from lucent.db.user import UserRepository

        target_user_exists = await UserRepository(pool).active_user_exists(
            organization_id=org_id,
            user_id=body.requesting_user_id,
        )
        if not target_user_exists:
            raise HTTPException(422, "requesting_user_id must be an active user in this org")
        requesting_user_id = body.requesting_user_id

    from lucent.task_kinds import (
        UnknownTaskKindError,
        resolve_task_kind,
        validate_task_kind_payload,
    )

    try:
        resolved_kind = resolve_task_kind(
            kind=body.task_kind,
            agent_type=body.agent_type,
            decision_config=body.decision_config,
            fusion_config=body.fusion_config,
        )
    except UnknownTaskKindError as exc:
        raise HTTPException(422, str(exc)) from exc
    if body.execution_config is not None and (
        body.decision_config is not None or body.fusion_config is not None
    ):
        raise HTTPException(
            422,
            "Provide execution_config or the legacy decision_config/fusion_config, "
            "not both",
        )
    if resolved_kind == "agent" and (
        body.decision_config is not None or body.fusion_config is not None
    ):
        raise HTTPException(
            422,
            "decision_config/fusion_config require their matching task kind",
        )
    effective_config = body.execution_config
    if effective_config is None:
        if resolved_kind == "decision":
            effective_config = body.decision_config
        elif resolved_kind == "fusion":
            effective_config = body.fusion_config
    try:
        validate_task_kind_payload(resolved_kind, effective_config)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    decision_config = body.decision_config or (
        body.execution_config if resolved_kind == "decision" else None
    )
    fusion_config = body.fusion_config or (
        body.execution_config if resolved_kind == "fusion" else None
    )

    # ── Remote targeting (Agent Control Protocol) ─────────────────────
    # A remote executes the task in its own environment. Validate the pin
    # here so a bad remote id fails at authoring time rather than silently
    # leaving the task unpinned (which would let a local daemon claim it).
    remote_target_id = body.remote_target_id
    if remote_target_id:
        from lucent.db.remotes import RemoteRepository

        if body.sandbox_template_id or body.sandbox_config:
            raise HTTPException(
                422,
                "remote_target_id cannot be combined with sandbox_template_id "
                "or sandbox_config — a remote runs the task in its own "
                "environment",
            )
        remote_target = await RemoteRepository(pool).require_active(
            str(remote_target_id), org_id
        )
        if not remote_target:
            raise HTTPException(
                422,
                "remote_target_id must reference an active remote instance "
                "in this organization",
            )
        if user.role < Role.ADMIN and not _is_daemon_user(user):
            raise HTTPException(
                403,
                "Only admins, owners, or the daemon can target a remote",
            )

    # Validate model against registry (matches MCP create_task behavior)
    if resolved_kind == "decision":
        from lucent.db.models import decision_models_available_for_user
        from lucent.db.requests import validate_decision_config
        from lucent.model_registry import validate_decision_model

        try:
            validate_decision_config(decision_config)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if body.output_contract or body.output_schema:
            raise HTTPException(
                422,
                "A decision-step task carries its own question schema — "
                "output_contract cannot be combined with decision_config",
            )
        if body.agent_type != "decision-step":
            raise HTTPException(
                422,
                "decision_config requires agent_type 'decision-step'",
            )
        if body.backend_id:
            raise HTTPException(
                422,
                "decision-step tasks run on the decision API and do not use backend_id",
            )
        if decision_config.get("model") and body.model:
            raise HTTPException(
                422,
                "Set the decision model either via decision_config.model or "
                "the task's model field, not both",
            )
        decision_model = (
            body.model or decision_config.get("model") or ""
        )
        if decision_model:
            error = validate_decision_model(decision_model)
            if error:
                raise HTTPException(422, error)
            await _require_model_access(pool, decision_model, user)
        else:
            available = await decision_models_available_for_user(
                pool, user_id=str(user.id), org_id=org_id, requester_role=user.role
            )
            if not available:
                raise HTTPException(
                    422,
                    "No enabled decision-class model is available to this "
                    "user. Add and grant a decision model (Settings → Models) "
                    "before creating decision steps.",
                )
    elif body.agent_type == "decision-step":
        raise HTTPException(
            422,
            "agent_type 'decision-step' requires decision_config carrying "
            "the questions to answer",
        )
    if resolved_kind == "fusion":
        from lucent.db.requests import validate_fusion_config
        from lucent.model_registry import (
            validate_decision_model,
            validate_model,
        )

        try:
            validate_fusion_config(
                fusion_config,
                max_candidates=fusion_max_candidates(organization_id=org_id),
                best_of_n_enabled=fusion_best_of_n_enabled(organization_id=org_id),
                synthesis_enabled=fusion_synthesis_enabled(organization_id=org_id),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if body.agent_type == "decision-step":
            raise HTTPException(
                422,
                "fusion_config requires a chat agent — decision-step tasks "
                "are executed by the decision model directly",
            )
        if decision_config:
            raise HTTPException(
                422,
                "Provide either fusion_config or decision_config, not both",
            )


        async def _require_chat_model(model_id: str) -> None:
            error = validate_model(model_id, require_tools=True)
            if error:
                raise HTTPException(422, error)
            await _require_model_access(pool, model_id, user)

        candidates = fusion_config.get("candidates") or {}
        for candidate_model in candidates.get("models") or []:
            await _require_chat_model(candidate_model)
        synthesis = fusion_config.get("synthesis") or {}
        if synthesis.get("model"):
            await _require_chat_model(synthesis["model"])
        refine = fusion_config.get("refine")
        if isinstance(refine, dict) and refine.get("model"):
            await _require_chat_model(refine["model"])
        arbiter = fusion_config.get("arbiter")
        arbiter_model = (
            arbiter.get("model") if isinstance(arbiter, dict) else None
        ) or None
        if arbiter_model:
            arbiter_error = validate_decision_model(arbiter_model)
            if arbiter_error:
                raise HTTPException(422, arbiter_error)
            await _require_model_access(pool, arbiter_model, user)
    if body.model:
        from lucent.llm.model_auto import split_auto_sentinel
        from lucent.settings import auto_selection_enabled

        is_auto, auto_error = split_auto_sentinel(
            body.model,
            enabled=auto_selection_enabled(organization_id=org_id),
        )
        if auto_error:
            raise HTTPException(422, auto_error)
        if is_auto and (resolved_kind == "decision" or body.agent_type == "decision-step"):
            raise HTTPException(
                422,
                'Model "auto" applies to chat tasks — decision-step tasks '
                "name a decision model directly",
            )
        if not is_auto:
            from lucent.model_registry import (
                validate_model,
                validate_reasoning_effort,
            )

            if resolved_kind != "decision":
                error = validate_model(body.model, require_tools=True)
                if error:
                    raise HTTPException(422, error)
            await _require_model_access(pool, body.model, user)
            if body.reasoning_effort and resolved_kind != "decision":
                effort_error = validate_reasoning_effort(
                    body.model, body.reasoning_effort
                )
                if effort_error:
                    raise HTTPException(422, effort_error)
    elif body.reasoning_effort:
        raise HTTPException(422, "reasoning_effort requires model")

    # Validate that agent_type or agent_definition_id resolves to an approved definition.
    # Usage is clearance-driven and default-deny: only agents this principal
    # is granted may be dispatched.
    from lucent.db.definitions import get_authorized_definitions_pool

    agent_pool = await get_authorized_definitions_pool(
        pool, {"id": str(user.id), "organization_id": org_id}
    )
    def_repo = DefinitionRepository(agent_pool)
    if body.agent_definition_id:
        agent_def = await def_repo.get_usable_agent(body.agent_definition_id)
        if not agent_def or agent_def.get("status") != "active":
            raise HTTPException(
                422,
                f"Agent definition '{body.agent_definition_id}' not found or not approved. "
                f"Approve it at /definitions before assigning tasks.",
            )
    elif body.agent_type:
        agents = (
            await def_repo.list_agents_accessible_by(
                str(user.id),
                org_id,
                status="active",
                limit=200,
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

    if body.backend_id:
        from lucent.llm.backend_selection import validate_backend_selection

        backend_error = await validate_backend_selection(
            pool, body.backend_id, org_id=org_id, model_id=body.model
        )
        if backend_error:
            raise HTTPException(422, backend_error)

    try:
        task = await repo.create_task(
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
            # When execution_config was used, let the repository mirror it
            # into the legacy columns; passing both is ambiguous.
            decision_config=(
                None if body.execution_config is not None else decision_config
            ),
            fusion_config=(
                None if body.execution_config is not None else fusion_config
            ),
            backend_id=body.backend_id,
            task_kind=resolved_kind,
            execution_config=body.execution_config,
            remote_target_id=str(remote_target_id) if remote_target_id else None,
        )
        task["sandboxes_enabled"] = org_sandboxes_enabled
        task["custom_tooling_enabled"] = custom_tooling_enabled(organization_id=org_id)
        return task
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
    org_id = str(user.organization_id)
    tasks = await repo.list_tasks(str(request_id), status=status, org_id=org_id)
    capabilities = {
        "sandboxes_enabled": sandboxes_enabled(organization_id=org_id),
        "custom_tooling_enabled": custom_tooling_enabled(organization_id=org_id),
    }
    for task in tasks:
        task.update(capabilities)
    return tasks


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
    from lucent.llm.model_auto import split_auto_sentinel
    from lucent.settings import auto_selection_enabled

    is_auto, auto_error = split_auto_sentinel(
        body.model,
        enabled=auto_selection_enabled(organization_id=str(user.organization_id)),
    )
    if auto_error:
        raise HTTPException(422, auto_error)
    if is_auto and (task or {}).get("agent_type") == "decision-step":
        raise HTTPException(
            422,
            'Model "auto" applies to chat tasks — decision-step tasks '
            "name a decision model directly",
        )
    if not is_auto:
        from lucent.model_registry import validate_model, validate_reasoning_effort

        error = validate_model(body.model, require_tools=True)
        if error:
            raise HTTPException(422, error)
        await _require_model_access(pool, body.model, user)
        effort_error = validate_reasoning_effort(body.model, body.reasoning_effort)
        if effort_error:
            raise HTTPException(422, effort_error)

    result = await repo.update_task_model(
        str(task_id), body.model, org_id=str(user.organization_id)
    )
    if result:
        result = await repo.update_task_reasoning_effort(
            str(task_id), body.reasoning_effort, org_id=str(user.organization_id)
        )
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
    backend_id: str | None = None
    clear_backend: bool = False


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
        from lucent.llm.model_auto import split_auto_sentinel
        from lucent.settings import auto_selection_enabled

        is_auto, auto_error = split_auto_sentinel(
            body.model,
            enabled=auto_selection_enabled(organization_id=org_id),
        )
        if auto_error:
            raise HTTPException(422, auto_error)
        if is_auto and (body.agent_type or (existing or {}).get("agent_type")) == (
            "decision-step"
        ):
            raise HTTPException(
                422,
                'Model "auto" applies to chat tasks — decision-step tasks '
                "name a decision model directly",
            )
        if not is_auto:
            from lucent.model_registry import (
                validate_model,
                validate_reasoning_effort,
            )

            error = validate_model(body.model, require_tools=True)
            if error:
                raise HTTPException(422, error)
            await _require_model_access(pool, body.model, user)
            effort_error = validate_reasoning_effort(
                body.model, body.reasoning_effort
            )
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

    if body.backend_id and not body.clear_backend:
        from lucent.llm.backend_selection import validate_backend_selection

        backend_error = await validate_backend_selection(
            pool, body.backend_id, org_id=org_id, model_id=body.model
        )
        if backend_error:
            raise HTTPException(422, backend_error)

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
        backend_id=body.backend_id if not body.clear_backend else None,
        clear_backend=body.clear_backend,
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


@router.post("/tasks/{task_id}/needs-review")
async def mark_task_needs_review(
    task_id: UUID,
    user: AuthenticatedUser,
    body: TaskFailBody = Body(...),
    pool=Depends(get_pool),
):
    """Stop automatic execution and preserve a task for human review."""
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)
    task = await repo.mark_task_needs_review(
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
        raise HTTPException(409, "Task not in failed or needs_review state")
    return task


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    """Cancel a task, interrupting it when it is already running."""
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)
    task = await repo.cancel_task(
        str(task_id),
        org_id=str(user.organization_id),
        requested_by=str(user.effective_memory_user_id or user.id),
    )
    if not task:
        raise HTTPException(
            409, "Task is completed, already cancelled, or already stopping"
        )
    return {**task, "cancel_requested": bool(task.get("cancel_requested_at"))}


@router.post("/tasks/{task_id}/cancel-finalize")
async def finalize_requested_cancel(
    task_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)
):
    """Settle a task to 'cancelled' after its running session was interrupted.

    Called by the daemon that owned the claim once it has stopped the provider
    session. Only acts when a cancel was actually requested, so a normal
    completion race can never be mislabeled as cancelled.
    """
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    task = await repo.finalize_requested_cancel(
        str(task_id), org_id=str(user.organization_id)
    )
    if not task:
        raise HTTPException(409, "Task has no outstanding cancellation request")
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


@router.post("/tasks/{task_id}/rework")
async def requeue_completed_task_for_rework(
    task_id: UUID,
    user: AuthenticatedUser,
    body: ReviewRejectBody = Body(...),
    pool=Depends(get_pool),
):
    """Reopen a completed task after a request-level review rejects it."""
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    await _require_task_mutation(repo, str(task_id), str(user.organization_id), user)
    task = await repo.requeue_completed_task_for_rework(
        str(task_id), body.feedback, org_id=str(user.organization_id)
    )
    if not task:
        raise HTTPException(409, "Task not in completed state")
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
        org_id=str(user.organization_id),
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
    return await repo.list_pending_tasks(
        str(user.organization_id), **_request_visibility_args(user)
    )


class RequestedCancelsBody(BaseModel):
    """Filter for the daemon's cancel sweep."""

    organization_id: str | None = None
    task_ids: list[UUID] | None = None


@router.post("/queue/requested-cancels")
async def requested_cancels(
    body: RequestedCancelsBody,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    """Claimed/running tasks carrying an outstanding cancel request.

    This is the daemon's durability backstop for a dropped ``task_cancel``
    notification: the columns are the source of truth, so re-reading them can
    never lose a cancel.
    """
    from lucent.db.requests import RequestRepository

    org_id = str(body.organization_id or user.organization_id)
    if org_id != str(user.organization_id) and user.role < Role.ADMIN:
        raise HTTPException(403, "Cannot read another organization's task queue")
    repo = RequestRepository(pool)
    items = await repo.list_requested_cancels(
        org_id,
        task_ids=[str(task_id) for task_id in body.task_ids] if body.task_ids else None,
    )
    return {"items": items, "total_count": len(items)}


@router.post("/queue/release-stale")
async def release_stale(
    user: AuthenticatedUser,
    stale_minutes: int = 30,
    pool=Depends(get_pool),
):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)
    count = await repo.release_stale_tasks(stale_minutes, org_id=str(user.organization_id))
    # Same pass also corrects instance liveness bookkeeping. A killed worker never
    # calls mark_instance_stopped, so `status='active'` accumulated rows whose
    # last_seen_at was days old — misleading when diagnosing dispatch.
    reconciled = await repo.reconcile_stale_instances(
        org_id=str(user.organization_id)
    )
    return {"released": count, "instances_reconciled": reconciled}


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
        active_task_ids=body.active_task_ids,
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
