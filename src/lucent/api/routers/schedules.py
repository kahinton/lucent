"""API router for scheduled tasks."""

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from lucent.api.deps import AuthenticatedUser, get_pool
from lucent.constants import REQUEST_SOURCE_SCHEDULE
from lucent.rbac import Role

router = APIRouter(prefix="/schedules", tags=["schedules"])
workflow_router = APIRouter(prefix="/workflows", tags=["workflows"])
logger = logging.getLogger(__name__)


def _is_daemon_user(user: AuthenticatedUser) -> bool:
    return user.role == Role.DAEMON or user.is_daemon_service


def _include_daemon_workflows(user: AuthenticatedUser) -> bool:
    return user.role >= Role.ADMIN


async def _require_model_access(pool, model_id: str, user: AuthenticatedUser) -> None:
    from lucent.access_control import AccessControlService

    if not await AccessControlService(pool).can_access(
        str(user.id), "model", model_id, str(user.organization_id)
    ):
        raise HTTPException(403, "Model is not available to this user")


async def _schedule_owner_context(
    pool,
    sched: dict,
    fallback_user: AuthenticatedUser | None,
) -> tuple[str, str]:
    """Return (owner_user_id, owner_role) for work created by a schedule."""
    if sched.get("is_system"):
        from lucent.daemon_identity import ensure_daemon_service_user

        async with pool.acquire() as conn:
            daemon_user = await ensure_daemon_service_user(
                conn, str(sched["organization_id"])
            )
        return str(daemon_user["id"]), "daemon"

    owner_id = str(sched.get("created_by") or (fallback_user.id if fallback_user else ""))
    if not owner_id:
        raise RuntimeError("Workflow has no owner user to create request work")
    try:
        from lucent.db import UserRepository

        owner = await UserRepository(pool).get_by_id(owner_id)
        if owner and owner.get("role"):
            owner_ext = owner.get("external_id") or ""
            owner_is_daemon = (
                owner.get("role") == "daemon"
                or owner_ext == "daemon-service"
                or owner_ext.startswith("daemon-service:")
            )
            if not sched.get("is_system") and owner_is_daemon:
                if fallback_user is not None and not _is_daemon_user(fallback_user):
                    return str(fallback_user.id), str(fallback_user.role)
                async with pool.acquire() as conn:
                    human_owner = await conn.fetchrow(
                        """SELECT id::text, role
                           FROM users
                           WHERE organization_id = $1::uuid
                             AND role <> 'daemon'
                             AND COALESCE(external_id, '') NOT LIKE 'daemon-service%'
                           ORDER BY CASE role
                               WHEN 'owner' THEN 0
                               WHEN 'admin' THEN 1
                               ELSE 2
                           END, created_at ASC
                           LIMIT 1""",
                        str(sched.get("organization_id")),
                    )
                if human_owner:
                    return str(human_owner["id"]), str(human_owner["role"] or "member")
            return owner_id, str(owner["role"])
    except Exception:
        logger.debug("Failed to resolve schedule owner %s", owner_id, exc_info=True)
    return owner_id, "member"


# ── Models ────────────────────────────────────────────────────────────────


class ScheduleCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str = ""
    agent_type: str = "code"
    model: str | None = Field(
        default=None,
        max_length=64,
        description="LLM model override for tasks created by this schedule",
    )
    reasoning_effort: str | None = Field(
        default=None,
        max_length=64,
        description="Optional reasoning effort for models that expose selectable levels",
    )
    task_template: dict | None = None
    sandbox_template_id: str | None = None  # Reference a saved sandbox template
    sandbox_config: dict | None = None  # Or inline sandbox config
    schedule_type: str = Field(
        default="once",
        pattern=r"^(once|interval|cron|manual|webhook|integration_event)$",
    )
    cron_expression: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)  # min 1 minute
    next_run_at: datetime | None = None
    priority: str = Field(default="medium", pattern=r"^(low|medium|high|urgent)$")
    timezone: str = "UTC"
    max_runs: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None
    trigger_type: str | None = Field(
        default=None,
        pattern=r"^(schedule|manual|webhook|integration_event)$",
        description="Workflow trigger kind. Defaults to 'schedule' for legacy schedules.",
    )
    trigger_config: dict | None = None
    request_template: dict | None = None
    actions: list[dict] | None = None
    review_instructions: str = ""
    webhook_secret: str | None = Field(default=None, max_length=512)


class ScheduleUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    agent_type: str | None = None
    model: str | None = Field(
        default=None,
        max_length=64,
        description="LLM model override for tasks created by this schedule",
    )
    reasoning_effort: str | None = Field(
        default=None,
        max_length=64,
        description="Optional reasoning effort for models that expose selectable levels",
    )
    task_template: dict | None = None
    sandbox_template_id: str | None = None
    sandbox_config: dict | None = None
    cron_expression: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)
    next_run_at: datetime | None = None
    priority: str | None = Field(default=None, pattern=r"^(low|medium|high|urgent)$")
    max_runs: int | None = None
    expires_at: datetime | None = None
    trigger_type: str | None = Field(
        default=None,
        pattern=r"^(schedule|manual|webhook|integration_event)$",
    )
    trigger_config: dict | None = None
    request_template: dict | None = None
    actions: list[dict] | None = None
    review_instructions: str | None = None
    webhook_secret: str | None = Field(default=None, max_length=512)


class ScheduleToggle(BaseModel):
    enabled: bool


class WorkflowAction(BaseModel):
    action_type: str = Field(default="task", pattern=r"^(task|user_interaction)$")
    title: str = Field(..., min_length=1, max_length=256)
    description: str | None = None
    prompt: str | None = None
    agent_type: str | None = None
    agent_definition_id: str | None = None
    priority: str | None = Field(default=None, pattern=r"^(low|medium|high|urgent)$")
    sequence_order: int | None = Field(default=None, ge=0)
    model: str | None = Field(default=None, max_length=64)
    reasoning_effort: str | None = Field(default=None, max_length=64)
    sandbox_template_id: str | None = None
    sandbox_config: dict | None = None
    output_contract: dict | None = None
    output_schema: dict | None = None
    interaction_type: str | None = Field(
        default=None,
        pattern=r"^(message|clarification|review|decision|workflow_output|handoff)$",
    )
    requires_response: bool | None = None
    response_prompt: str | None = None
    metadata: dict | None = None
    references: list[dict] | None = None
    dedupe_key: str | None = Field(default=None, max_length=512)


class WorkflowCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str = ""
    trigger_type: str = Field(
        default="schedule",
        pattern=r"^(schedule|manual|webhook|integration_event)$",
    )
    schedule_type: str | None = Field(
        default=None,
        pattern=r"^(once|interval|cron|manual|webhook|integration_event)$",
    )
    cron_expression: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)
    next_run_at: datetime | None = None
    timezone: str = "UTC"
    priority: str = Field(default="medium", pattern=r"^(low|medium|high|urgent)$")
    max_runs: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None
    request_template: dict | None = None
    actions: list[WorkflowAction] = Field(default_factory=list)
    review_instructions: str = ""
    trigger_config: dict | None = None
    webhook_secret: str | None = Field(default=None, max_length=512)


def _json_object(value, default: dict | None = None) -> dict:
    if default is None:
        default = {}
    if value is None:
        return default
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return default
    return value if isinstance(value, dict) else default


def _json_array(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    return value if isinstance(value, list) else []


def _render_template_text(template: str, values: dict[str, str]) -> str:
    text = str(template or "")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
        text = text.replace("{" + key + "}", value)
    return text


def _trigger_summary(trigger_context: dict | None) -> str:
    if not trigger_context:
        return "manual run"
    summary = trigger_context.get("summary") or trigger_context.get("event_type")
    if summary:
        return str(summary)[:120]
    payload = trigger_context.get("payload")
    if isinstance(payload, dict):
        for key in ("action", "event", "type", "title", "name"):
            if payload.get(key):
                return str(payload[key])[:120]
    return str(trigger_context.get("trigger_type") or "event")[:120]


def _format_trigger_context(trigger_context: dict | None) -> str:
    if not trigger_context:
        return ""
    payload = trigger_context.get("payload")
    try:
        payload_text = json.dumps(payload, indent=2, default=str)
    except (TypeError, ValueError):
        payload_text = str(payload)
    if len(payload_text) > 8000:
        payload_text = payload_text[:8000] + "\n…(trigger payload truncated)…"
    headers = trigger_context.get("headers") or {}
    safe_headers = {
        k: v for k, v in headers.items()
        if k.lower() not in {"authorization", "cookie", "x-lucent-workflow-token"}
    }
    return (
        "\n\nWorkflow trigger context:\n"
        f"- trigger_type: {trigger_context.get('trigger_type', 'unknown')}\n"
        f"- summary: {_trigger_summary(trigger_context)}\n"
        f"- headers: {json.dumps(safe_headers, default=str)[:2000]}\n"
        f"- payload:\n{payload_text}"
    )


def _build_request_fields(sched: dict, trigger_context: dict | None) -> tuple[str, str, str]:
    template = _json_object(sched.get("request_template"))
    trigger_type = sched.get("trigger_type") or "schedule"
    title_prefix = str(
        template.get("title_prefix")
        or ("[Scheduled]" if trigger_type == "schedule" else "[Workflow]")
    ).strip()
    title_template = str(template.get("title") or sched.get("title") or "Workflow")
    values = {
        "workflow_title": str(sched.get("title") or "Workflow"),
        "trigger_type": str(trigger_type),
        "event_summary": _trigger_summary(trigger_context),
    }
    rendered_title = _render_template_text(title_template, values).strip()
    if title_prefix and not rendered_title.startswith(title_prefix):
        rendered_title = f"{title_prefix} {rendered_title}".strip()

    description_template = str(template.get("description") or sched.get("description") or "")
    description = _render_template_text(description_template, values).strip()
    trigger_section = _format_trigger_context(trigger_context)
    review_instructions = str(sched.get("review_instructions") or "").strip()
    if review_instructions:
        description += (
            "\n\nWorkflow reviewer instructions:\n"
            f"{review_instructions}\n\n"
            "The post-completion reviewer must apply this checklist before approving."
        )
    if trigger_section:
        description += trigger_section
    dependency_policy = str(template.get("dependency_policy") or "strict")
    if dependency_policy not in {"strict", "permissive"}:
        dependency_policy = "strict"
    return rendered_title, description, dependency_policy


def _workflow_allows_concurrent(sched: dict) -> bool:
    trigger_type = sched.get("trigger_type") or "schedule"
    config = _json_object(sched.get("trigger_config"))
    if "allow_concurrent" in config:
        return bool(config.get("allow_concurrent"))
    return trigger_type in {"webhook", "manual", "integration_event"}


def _workflow_interaction_references(
    *,
    action: dict,
    sched: dict,
    run: dict | None = None,
    req: dict | None = None,
) -> list[dict]:
    """Build references that let a workflow handoff recover its context."""
    refs = []
    raw_refs = action.get("references") or []
    if isinstance(raw_refs, list):
        refs.extend(ref for ref in raw_refs if isinstance(ref, dict))
    workflow_id = str(sched.get("id") or "")
    if workflow_id:
        refs.append(
            {
                "reference_type": "workflow",
                "reference_id": workflow_id,
                "label": sched.get("title") or "Workflow",
                "url": f"/workflows/{workflow_id}",
            }
        )
    if run and run.get("id"):
        refs.append(
            {
                "reference_type": "schedule_run",
                "reference_id": str(run["id"]),
                "label": f"Run record for {sched.get('title') or 'workflow'}",
                "metadata": {
                    "workflow_id": workflow_id,
                    "request_id": str(req["id"]) if req and req.get("id") else None,
                    "description": "Internal workflow run record used by Lucent for grounding.",
                },
            }
        )
    if req and req.get("id"):
        refs.append(
            {
                "reference_type": "request",
                "reference_id": str(req["id"]),
                "label": req.get("title") or "Workflow request",
                "url": f"/activity/{req['id']}",
            }
        )
    return refs


async def _create_workflow_interaction(
    *,
    pool,
    sched: dict,
    run: dict | None,
    action: dict,
    org_id: str,
    owner_user_id: str,
    req: dict | None = None,
    trigger_context: dict | None = None,
) -> dict:
    from lucent.db.user_interactions import UserInteractionRepository

    repo = UserInteractionRepository(pool)
    requires_response = bool(action.get("requires_response", False))
    interaction_type = (
        action.get("interaction_type")
        or ("clarification" if requires_response else "workflow_output")
    )
    values = {
        "workflow_title": str(sched.get("title") or "Workflow"),
        "trigger_type": str(sched.get("trigger_type") or "schedule"),
        "event_summary": _trigger_summary(trigger_context),
    }
    title = _render_template_text(
        str(action.get("title") or sched.get("title") or "Workflow update"),
        values,
    )
    body = _render_template_text(
        str(
            action.get("body")
            or action.get("description")
            or action.get("prompt")
            or sched.get("description")
            or "Workflow produced an update for review."
        ),
        values,
    )
    trigger_section = _format_trigger_context(trigger_context)
    if trigger_section and action.get("include_trigger_context", True):
        body = f"{body}{trigger_section}"
    metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
    metadata = {
        **metadata,
        "workflow_id": str(sched.get("id")),
        "workflow_title": sched.get("title"),
        "workflow_run_id": str(run.get("id")) if run and run.get("id") else None,
        "request_id": str(req.get("id")) if req and req.get("id") else None,
        "trigger_summary": _trigger_summary(trigger_context),
    }
    return await repo.create_interaction(
        org_id=org_id,
        user_id=owner_user_id,
        created_by=owner_user_id,
        title=title,
        body=body,
        source="workflow",
        interaction_type=interaction_type,
        priority=action.get("priority") or sched.get("priority") or "medium",
        requires_response=requires_response,
        response_prompt=action.get("response_prompt"),
        metadata=metadata,
        references=_workflow_interaction_references(
            action=action,
            sched=sched,
            run=run,
            req=req,
        ),
        dedupe_key=action.get("dedupe_key") or None,
    )


async def _trigger_schedule_execution(
    schedule_id: str,
    user: AuthenticatedUser | None,
    *,
    force: bool = False,
    pool,
    trigger_context: dict | None = None,
    advance_schedule: bool | None = None,
) -> dict:
    """Trigger a schedule/workflow and materialize its actions as request tasks."""
    from lucent.db.requests import RequestRepository
    from lucent.db.schedules import ScheduleRepository, workflow_actions_for_schedule

    sched_repo = ScheduleRepository(pool)
    req_repo = RequestRepository(pool)

    if user is not None:
        sched = await sched_repo.get_schedule(
            schedule_id,
            str(user.organization_id),
            created_by=str(user.id),
            include_daemon_created=_include_daemon_workflows(user),
        )
        if not sched:
            raise HTTPException(404, "Workflow not found")
        org_id = str(user.organization_id)
    else:
        sched = await sched_repo.get_schedule_by_id(schedule_id)
        if not sched:
            raise HTTPException(404, "Workflow not found")
        org_id = str(sched["organization_id"])

    if sched.get("status") != "active":
        raise HTTPException(409, f"Workflow is {sched.get('status')}, cannot trigger")

    trigger_type = sched.get("trigger_type") or "schedule"
    if advance_schedule is None:
        advance_schedule = trigger_type == "schedule"

    actions = workflow_actions_for_schedule(sched)
    server_actions = [a for a in actions if a.get("action_type") == "server_function"]
    if server_actions:
        function_name = str(server_actions[0].get("function") or "")
        if function_name == "release_stale_tasks":
            from lucent.api.system_schedules import execute_stale_task_reaper_schedule

            result = await execute_stale_task_reaper_schedule(
                sched,
                force=force,
                advance_schedule=advance_schedule,
            )
            return result or {"schedule": sched, "workflow": sched, "already_fired": True}
        raise HTTPException(422, f"Unsupported server workflow function: {function_name}")
    task_actions = [a for a in actions if a.get("action_type", "task") == "task"]
    interaction_actions = [
        a for a in actions if a.get("action_type") == "user_interaction"
    ]

    run = await sched_repo.mark_schedule_run(
        schedule_id,
        force=force,
        advance_schedule=advance_schedule,
    )
    if run is None:
        return {"schedule": sched, "workflow": sched, "already_fired": True}

    owner_user_id, owner_role = await _schedule_owner_context(pool, sched, user)
    request_title, request_description, dependency_policy = _build_request_fields(
        sched, trigger_context,
    )

    try:
        if task_actions and not _workflow_allows_concurrent(sched):
            async with pool.acquire() as conn:
                active_request = await conn.fetchval(
                    """SELECT id FROM requests
                       WHERE title = $1
                         AND organization_id = $2::uuid
                         AND status NOT IN ('completed', 'failed', 'cancelled')
                       LIMIT 1""",
                    request_title,
                    org_id,
                )
            if active_request:
                logger.info(
                    "Workflow %s skipped — active request %s exists",
                    schedule_id, str(active_request)[:8],
                )
                await sched_repo.complete_run(
                    str(run["id"]),
                    result=f"Skipped: active request {str(active_request)} already exists",
                )
                return {
                    "schedule": sched,
                    "workflow": sched,
                    "skipped": True,
                    "active_request": str(active_request),
                }

        if trigger_type == "schedule":
            has_work = await sched_repo.built_in_schedule_has_work(
                str(sched["title"]),
                org_id,
                schedule_id=schedule_id,
            )
            if has_work is False:
                skip_event = {
                    "event_type": "schedule.skipped",
                    "schedule_id": schedule_id,
                    "schedule_name": sched["title"],
                    "reason": "no_eligible_work",
                    "candidate_count": 0,
                }
                logger.info(json.dumps(skip_event, sort_keys=True))
                await sched_repo.complete_run(str(run["id"]), result=json.dumps(skip_event))
                return {
                    "schedule": sched,
                    "workflow": sched,
                    "run": run,
                    "skipped": True,
                    "event": skip_event,
                }

        req = None
        created_tasks = []
        if task_actions:
            req = await req_repo.create_request(
                title=request_title,
                org_id=org_id,
                description=request_description,
                source=REQUEST_SOURCE_SCHEDULE,
                priority=sched.get("priority", "medium"),
                created_by=owner_user_id,
                dependency_policy=dependency_policy,
            )

            from lucent.db.definitions import DefinitionRepository

            def_repo = DefinitionRepository(pool)
            agents = (
                await def_repo.list_agents(
                    org_id,
                    status="active",
                    limit=500,
                    requester_user_id=owner_user_id,
                    requester_role=owner_role,
                )
            )["items"]
            active_names = {a["name"] for a in agents}
            trigger_section = _format_trigger_context(trigger_context)

            for idx, action in enumerate(task_actions):
                agent_type = action.get("agent_type") or sched.get("agent_type") or "code"
                if agent_type and agent_type not in active_names:
                    raise RuntimeError(
                        f"Workflow action references agent_type '{action.get('agent_type')}', "
                        "but that agent is not approved/accessible for the workflow owner."
                    )

                task_model = action.get("model") if "model" in action else sched.get("model")
                task_reasoning_effort = (
                    action.get("reasoning_effort")
                    if "reasoning_effort" in action
                    else sched.get("reasoning_effort")
                )
                if task_model:
                    from lucent.model_registry import validate_model, validate_reasoning_effort

                    model_error = validate_model(task_model, require_tools=True)
                    if model_error:
                        logger.warning(
                            "Workflow %s action has invalid model '%s': %s — clearing override",
                            schedule_id, task_model, model_error,
                        )
                        task_model = None
                        task_reasoning_effort = None
                    else:
                        from lucent.access_control import AccessControlService

                        can_access_model = await AccessControlService(pool).can_access(
                            owner_user_id,
                            "model",
                            task_model,
                            org_id,
                        )
                        if not can_access_model:
                            logger.warning(
                                "Workflow %s action model '%s' is unavailable to its owner; "
                                "clearing override",
                                schedule_id,
                                task_model,
                            )
                            task_model = None
                            task_reasoning_effort = None
                        if task_model:
                            effort_error = validate_reasoning_effort(
                                task_model, task_reasoning_effort
                            )
                            if effort_error:
                                logger.warning(
                                    "Workflow %s action has invalid reasoning_effort '%s': %s — "
                                    "clearing override",
                                    schedule_id, task_reasoning_effort, effort_error,
                                )
                                task_reasoning_effort = None
                else:
                    task_reasoning_effort = None

                output_contract = action.get("output_contract")
                if action.get("output_schema") and not output_contract:
                    output_contract = {
                        "json_schema": action["output_schema"],
                        "on_failure": "fallback",
                        "max_retries": 1,
                    }
                task_description = (
                    action.get("description")
                    or action.get("prompt")
                    or sched.get("prompt")
                    or sched.get("description")
                    or "Run this workflow action and record any user-visible outputs."
                )
                if trigger_section:
                    task_description = f"{task_description}{trigger_section}"
                sandbox_template_id = (
                    str(action.get("sandbox_template_id") or sched.get("sandbox_template_id") or "")
                    or None
                )
                task = await req_repo.create_task(
                    request_id=str(req["id"]),
                    title=action.get("title") or sched["title"],
                    description=task_description,
                    agent_type=agent_type,
                    agent_definition_id=action.get("agent_definition_id"),
                    priority=action.get("priority") or sched.get("priority", "medium"),
                    sequence_order=int(action.get("sequence_order", idx) or idx),
                    model=task_model,
                    reasoning_effort=task_reasoning_effort,
                    sandbox_template_id=sandbox_template_id,
                    sandbox_config=action.get("sandbox_config") or sched.get("sandbox_config"),
                    org_id=org_id,
                    requesting_user_id=owner_user_id,
                    output_contract=output_contract,
                )
                created_tasks.append(task)

            await sched_repo.link_run_to_request(str(run["id"]), str(req["id"]))

        created_interactions = []
        for action in interaction_actions:
            created_interactions.append(
                await _create_workflow_interaction(
                    pool=pool,
                    sched=sched,
                    run=run,
                    action=action,
                    org_id=org_id,
                    owner_user_id=owner_user_id,
                    req=req,
                    trigger_context=trigger_context,
                )
            )

        if not created_tasks and not created_interactions:
            raise RuntimeError("Workflow did not create any task or user interaction actions")

        if created_interactions and not created_tasks:
            await sched_repo.complete_run(
                str(run["id"]),
                result=f"Sent {len(created_interactions)} Handoff interaction(s)",
            )
    except Exception as e:
        logger.error("Workflow %s triggered but task creation failed: %s", schedule_id, e)
        await sched_repo.fail_run(str(run["id"]), str(e))
        raise HTTPException(500, f"Workflow advanced but task creation failed: {e}")

    return {
        "schedule": sched,
        "workflow": sched,
        "request": req,
        "run": run,
        "tasks": created_tasks,
        "interactions": created_interactions,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("")
async def create_schedule(
    body: ScheduleCreate, user: AuthenticatedUser, pool=Depends(get_pool)
):
    from lucent.db.schedules import ScheduleRepository, webhook_secret_hash
    from lucent.model_registry import validate_model, validate_reasoning_effort

    if body.model:
        model_error = validate_model(body.model, require_tools=True)
        if model_error:
            raise HTTPException(422, model_error)
        await _require_model_access(pool, body.model, user)
        effort_error = validate_reasoning_effort(body.model, body.reasoning_effort)
        if effort_error:
            raise HTTPException(422, effort_error)
    elif body.reasoning_effort:
        raise HTTPException(422, "reasoning_effort requires model")

    repo = ScheduleRepository(pool)
    return await repo.create_schedule(
        title=body.title,
        org_id=str(user.organization_id),
        schedule_type=body.schedule_type,
        description=body.description,
        agent_type=body.agent_type,
        model=body.model,
        reasoning_effort=body.reasoning_effort,
        task_template=body.task_template,
        sandbox_config=body.sandbox_config,
        sandbox_template_id=body.sandbox_template_id,
        cron_expression=body.cron_expression,
        interval_seconds=body.interval_seconds,
        next_run_at=body.next_run_at,
        priority=body.priority,
        timezone_str=body.timezone,
        max_runs=body.max_runs,
        expires_at=body.expires_at,
        created_by=str(user.id),
        trigger_type=body.trigger_type,
        trigger_config=body.trigger_config,
        request_template=body.request_template,
        actions=body.actions,
        review_instructions=body.review_instructions,
        webhook_secret_hash=webhook_secret_hash(body.webhook_secret),
    )


@router.get("")
async def list_schedules(
    user: AuthenticatedUser,
    status: str | None = None,
    enabled: bool | None = None,
    pool=Depends(get_pool),
):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    return await repo.list_schedules(
        str(user.organization_id),
        status=status,
        enabled=enabled,
        created_by=str(user.id),
        include_daemon_created=_include_daemon_workflows(user),
    )


@router.get("/summary")
async def schedule_summary(user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    return await repo.get_summary(
        str(user.organization_id),
        created_by=str(user.id),
        include_daemon_created=_include_daemon_workflows(user),
    )


@router.get("/due")
async def get_due_schedules(user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    return await repo.get_due_schedules(str(user.organization_id))


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    result = await repo.get_schedule_with_runs(
        schedule_id,
        str(user.organization_id),
        created_by=str(user.id),
        include_daemon_created=_include_daemon_workflows(user),
    )
    if not result:
        raise HTTPException(404, "Schedule not found")
    return result


@router.put("/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    body: ScheduleUpdate,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    webhook_secret = fields.pop("webhook_secret", None)
    if webhook_secret is not None:
        from lucent.db.schedules import webhook_secret_hash

        fields["webhook_secret_hash"] = webhook_secret_hash(webhook_secret)
    if "model" in fields and "reasoning_effort" not in fields:
        fields["reasoning_effort"] = None
    if "timezone" in fields:
        fields["timezone_str"] = fields.pop("timezone")
    sched = await repo.get_schedule(
        schedule_id,
        str(user.organization_id),
        created_by=str(user.id),
        include_daemon_created=_include_daemon_workflows(user),
    )
    if not sched:
        raise HTTPException(404, "Schedule not found")
    effective_model = fields.get("model", sched.get("model"))
    effective_effort = fields.get("reasoning_effort", sched.get("reasoning_effort"))
    if effective_model:
        from lucent.model_registry import validate_model, validate_reasoning_effort

        model_error = validate_model(effective_model, require_tools=True)
        if model_error:
            raise HTTPException(422, model_error)
        await _require_model_access(pool, effective_model, user)
        effort_error = validate_reasoning_effort(effective_model, effective_effort)
        if effort_error:
            raise HTTPException(422, effort_error)
    elif effective_effort:
        raise HTTPException(422, "reasoning_effort requires model")
    try:
        result = await repo.update_schedule(
            schedule_id, str(user.organization_id),
            requester_role=user.role.value, **fields,
        )
    except ValueError as e:
        raise HTTPException(403, str(e))
    if not result:
        raise HTTPException(404, "Schedule not found")
    return result


@router.post("/{schedule_id}/toggle")
async def toggle_schedule(
    schedule_id: str,
    body: ScheduleToggle,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    sched = await repo.get_schedule(
        schedule_id,
        str(user.organization_id),
        created_by=str(user.id),
        include_daemon_created=_include_daemon_workflows(user),
    )
    if not sched:
        raise HTTPException(404, "Schedule not found")
    try:
        result = await repo.toggle_schedule(
            schedule_id, str(user.organization_id), body.enabled,
            requester_role=user.role.value,
        )
    except ValueError as e:
        raise HTTPException(403, str(e))
    if not result:
        raise HTTPException(404, "Schedule not found")
    return result


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: str, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    sched = await repo.get_schedule(
        schedule_id,
        str(user.organization_id),
        created_by=str(user.id),
        include_daemon_created=_include_daemon_workflows(user),
    )
    if not sched:
        raise HTTPException(404, "Schedule not found")
    try:
        ok = await repo.delete_schedule(schedule_id, str(user.organization_id))
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not ok:
        raise HTTPException(404, "Schedule not found")
    return {"deleted": True}


@router.get("/{schedule_id}/runs")
async def list_runs(schedule_id: str, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    # Verify ownership
    sched = await repo.get_schedule(
        schedule_id,
        str(user.organization_id),
        created_by=str(user.id),
        include_daemon_created=_include_daemon_workflows(user),
    )
    if not sched:
        raise HTTPException(404, "Schedule not found")
    return await repo.list_runs(schedule_id)


@router.post("/{schedule_id}/trigger")
async def trigger_now(
    schedule_id: str,
    user: AuthenticatedUser,
    force: bool = False,
    pool=Depends(get_pool),
):
    """Trigger a schedule. Pass force=true to bypass the time guard (manual run)."""
    return await _trigger_schedule_execution(
        schedule_id,
        user,
        force=force,
        pool=pool,
        advance_schedule=True,
    )


# ── Workflow API aliases ─────────────────────────────────────────────────


@workflow_router.post("")
async def create_workflow(
    body: WorkflowCreate, user: AuthenticatedUser, pool=Depends(get_pool)
):
    """Create a workflow with a typed trigger and ordered task actions."""
    from lucent.db.schedules import ScheduleRepository, webhook_secret_hash
    from lucent.model_registry import validate_model, validate_reasoning_effort

    if body.trigger_type == "webhook" and not body.webhook_secret:
        raise HTTPException(422, "webhook_secret is required for webhook workflows")

    schedule_type = body.schedule_type
    if not schedule_type:
        schedule_type = "interval" if body.trigger_type == "schedule" else body.trigger_type

    if body.trigger_type == "schedule":
        if schedule_type == "cron" and not body.cron_expression:
            raise HTTPException(422, "cron_expression is required for cron workflows")
        if schedule_type == "interval" and not body.interval_seconds:
            raise HTTPException(422, "interval_seconds is required for interval workflows")

    actions = [action.model_dump(exclude_none=True) for action in body.actions]
    for action in actions:
        if action.get("action_type", "task") != "task":
            continue
        if action.get("model"):
            model_error = validate_model(action["model"], require_tools=True)
            if model_error:
                raise HTTPException(422, model_error)
            await _require_model_access(pool, action["model"], user)
            effort_error = validate_reasoning_effort(
                action["model"], action.get("reasoning_effort"),
            )
            if effort_error:
                raise HTTPException(422, effort_error)
        elif action.get("reasoning_effort"):
            raise HTTPException(422, "reasoning_effort requires model")
        if action.get("output_contract") and action.get("output_schema"):
            raise HTTPException(422, "Provide either output_contract or output_schema, not both")

    repo = ScheduleRepository(pool)
    return await repo.create_schedule(
        title=body.title,
        org_id=str(user.organization_id),
        schedule_type=schedule_type,
        description=body.description,
        agent_type=actions[0].get("agent_type", "code") if actions else "code",
        cron_expression=body.cron_expression,
        interval_seconds=body.interval_seconds,
        next_run_at=body.next_run_at,
        priority=body.priority,
        timezone_str=body.timezone,
        max_runs=body.max_runs,
        expires_at=body.expires_at,
        created_by=str(user.id),
        trigger_type=body.trigger_type,
        trigger_config=body.trigger_config,
        request_template=body.request_template,
        actions=actions,
        review_instructions=body.review_instructions,
        webhook_secret_hash=webhook_secret_hash(body.webhook_secret),
    )


@workflow_router.get("")
async def list_workflows(
    user: AuthenticatedUser,
    status: str | None = None,
    enabled: bool | None = None,
    trigger_type: str | None = None,
    pool=Depends(get_pool),
):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    result = await repo.list_schedules(
        str(user.organization_id),
        status=status,
        enabled=enabled,
        created_by=str(user.id),
        include_daemon_created=_include_daemon_workflows(user),
    )
    if trigger_type:
        result["items"] = [
            item for item in result["items"]
            if (item.get("trigger_type") or "schedule") == trigger_type
        ]
        result["total_count"] = len(result["items"])
        result["has_more"] = False
    return result


@workflow_router.get("/summary")
async def workflow_summary(user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.schedules import ScheduleRepository

    return await ScheduleRepository(pool).get_summary(
        str(user.organization_id),
        created_by=str(user.id),
        include_daemon_created=_include_daemon_workflows(user),
    )


@workflow_router.get("/{workflow_id}")
async def get_workflow(workflow_id: str, user: AuthenticatedUser, pool=Depends(get_pool)):
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    result = await repo.get_schedule_with_runs(
        workflow_id,
        str(user.organization_id),
        created_by=str(user.id),
        include_daemon_created=_include_daemon_workflows(user),
    )
    if not result:
        raise HTTPException(404, "Workflow not found")
    return result


@workflow_router.post("/{workflow_id}/trigger")
async def trigger_workflow_now(
    workflow_id: str,
    user: AuthenticatedUser,
    force: bool = True,
    pool=Depends(get_pool),
):
    return await _trigger_schedule_execution(
        workflow_id,
        user,
        force=force,
        pool=pool,
        advance_schedule=False,
    )


def _webhook_secret_from_request(request: Request, token: str | None = None) -> str | None:
    if token:
        return token
    header_token = request.headers.get("X-Lucent-Workflow-Token")
    if header_token:
        return header_token
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None


@workflow_router.post("/{workflow_id}/webhook")
async def trigger_workflow_webhook(
    workflow_id: str,
    request: Request,
    token: str | None = None,
    pool=Depends(get_pool),
):
    """Receive a generic external webhook and trigger a workflow run."""
    from lucent.db.schedules import ScheduleRepository, verify_webhook_secret

    repo = ScheduleRepository(pool)
    sched = await repo.get_schedule_by_id(workflow_id)
    if not sched or (sched.get("trigger_type") or "schedule") != "webhook":
        raise HTTPException(404, "Workflow not found")
    if not sched.get("enabled") or sched.get("status") != "active":
        raise HTTPException(409, "Workflow is not active")
    secret = _webhook_secret_from_request(request, token)
    if not verify_webhook_secret(secret, sched.get("webhook_secret_hash")):
        raise HTTPException(401, "Invalid workflow webhook token")

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
    else:
        body = await request.body()
        payload = {"raw_body": body.decode("utf-8", errors="replace")}

    await repo.record_webhook_received(workflow_id)
    trigger_context = {
        "trigger_type": "webhook",
        "summary": request.headers.get("X-GitHub-Event")
        or request.headers.get("X-Event-Type")
        or request.headers.get("X-Lucent-Event")
        or "incoming webhook",
        "payload": payload,
        "headers": dict(request.headers),
    }
    return await _trigger_schedule_execution(
        workflow_id,
        None,
        force=True,
        pool=pool,
        trigger_context=trigger_context,
        advance_schedule=False,
    )
