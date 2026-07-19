"""MCP tools for schedule management."""

import json
import os

from mcp.server.fastmcp import FastMCP

from lucent.db.schedules import ScheduleRepository, webhook_secret_hash
from lucent.tools.memories import _get_current_user_context

FALLBACK_WORKFLOW_AGENT_TYPES = {
    "assessment",
    "code",
    "documentation",
    "lucent",
    "memory",
    "planning",
    "reflection",
    "request-review",
    "research",
    "sandbox",
    "sandbox-orchestrator",
}


async def _get_schedule_repository() -> ScheduleRepository:
    from lucent.db import init_db

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is required")
    pool = await init_db(database_url)
    return ScheduleRepository(pool)


async def _get_pool():
    from lucent.db import init_db

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is required")
    return await init_db(database_url)


def register_schedule_tools(mcp: FastMCP) -> None:
    """Register schedule management tools with the MCP server."""

    def _jsonable_schedule_value(value):
        if hasattr(value, "hex"):
            return str(value)
        return value

    def _include_daemon_workflows(user_role: str | None) -> bool:
        return user_role in ("admin", "owner")

    @mcp.tool(
        description="""Create a scheduled task — either one-time or repeating.

Schedule types:
- 'once': Runs once at next_run_at (or immediately if not set)
- 'interval': Repeats every interval_seconds (minimum 60)
- 'cron': Repeats on a cron schedule (e.g. '0 9 * * 1' for 9am every Monday)

When a schedule fires, it creates a tracked Request with tasks that flow
through the normal task queue and appear in the Requests UI.

Args:
    title: What this schedule does (e.g. "Weekly memory cleanup")
    schedule_type: 'once', 'interval', or 'cron'
    description: Detailed description of the work
    agent_type: Which agent type should handle this ('code', 'research', 'memory', etc.)
    model: LLM model to use for scheduled tasks. If not set, the daemon picks a default.
    reasoning_effort: Optional reasoning/thinking level for models that expose selectable levels.
    cron_expression: Cron expression (required if schedule_type is 'cron')
    interval_seconds: Seconds between runs (required if schedule_type is 'interval', min 60)
    priority: 'low', 'medium', 'high', or 'urgent'
    max_runs: Optional limit on total number of runs (null = unlimited)
    sandbox_template_id: Optional UUID of a sandbox template to use when executing tasks

Returns: JSON with the created schedule including its ID and next_run_at."""
    )
    async def create_schedule(
        title: str,
        schedule_type: str = "once",
        description: str = "",
        agent_type: str = "code",
        model: str | None = None,
        reasoning_effort: str | None = None,
        cron_expression: str | None = None,
        interval_seconds: int | None = None,
        priority: str = "medium",
        max_runs: int | None = None,
        sandbox_template_id: str | None = None,
    ) -> str:
        user_id, org_id, user_role, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        # Validate schedule_type and required fields
        if schedule_type not in ("once", "interval", "cron"):
            return json.dumps({"error": "schedule_type must be 'once', 'interval', or 'cron'"})
        if schedule_type == "cron" and not cron_expression:
            return json.dumps({"error": "cron_expression is required for 'cron' schedule_type"})
        if schedule_type == "interval" and (not interval_seconds or interval_seconds < 60):
            return json.dumps(
                {"error": "interval_seconds must be >= 60 for 'interval' schedule_type"}
            )
        if priority not in ("low", "medium", "high", "urgent"):
            return json.dumps({"error": "priority must be 'low', 'medium', 'high', or 'urgent'"})

        # Validate cron expression syntax (basic format check)
        if cron_expression:
            import re

            # Accept exactly 5 space-separated fields (standard cron)
            cron_pattern = re.compile(r"^(\S+\s+){4}\S+$")
            if not cron_pattern.match(cron_expression.strip()):
                return json.dumps({"error": f"Invalid cron expression format: {cron_expression}"})

        # Validate model against registry
        if model:
            from lucent.model_registry import validate_model, validate_reasoning_effort

            error = validate_model(model)
            if error:
                return json.dumps({"error": error})
            from lucent.access_control import AccessControlService

            if not user_id or not await AccessControlService(await _get_pool()).can_access(
                str(user_id), "model", model, str(org_id)
            ):
                return json.dumps({"error": "Model is not available to this user"})
            effort_error = validate_reasoning_effort(model, reasoning_effort)
            if effort_error:
                return json.dumps({"error": effort_error})
        elif reasoning_effort:
            return json.dumps({"error": "reasoning_effort requires model"})

        repo = await _get_schedule_repository()
        sched = await repo.create_schedule(
            title=title,
            org_id=str(org_id),
            schedule_type=schedule_type,
            description=description,
            agent_type=agent_type,
            model=model,
            reasoning_effort=reasoning_effort,
            cron_expression=cron_expression,
            interval_seconds=interval_seconds,
            priority=priority,
            max_runs=max_runs,
            sandbox_template_id=sandbox_template_id,
            created_by=str(user_id),
        )
        return json.dumps(
            {k: str(v) if hasattr(v, "hex") else str(v) for k, v in sched.items()}, default=str
        )

    @mcp.tool(
        description="""List scheduled tasks, optionally filtered by status or enabled state.

Args:
    status: Filter by status ('active', 'paused', 'completed', 'expired') or null for all
    enabled_only: If true, only show enabled schedules

Returns: JSON array of schedules."""
    )
    async def list_schedules(
        status: str | None = None,
        enabled_only: bool = False,
    ) -> str:
        user_id, org_id, user_role, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_schedule_repository()
        result = await repo.list_schedules(
            str(org_id),
            status=status,
            enabled=True if enabled_only else None,
            created_by=str(user_id) if user_id else None,
            include_daemon_created=_include_daemon_workflows(user_role),
        )
        serialized_items = [
            {k: str(v) if hasattr(v, "hex") else str(v) for k, v in s.items()}
            for s in result["items"]
        ]
        return json.dumps(
            {"items": serialized_items, "total_count": result["total_count"],
             "has_more": result["has_more"]},
            default=str,
        )

    @mcp.tool(
        description="""Toggle a schedule on or off.

Args:
    schedule_id: The schedule UUID to toggle
    enabled: true to enable, false to disable

Returns: JSON with the updated schedule."""
    )
    async def toggle_schedule(schedule_id: str, enabled: bool) -> str:
        user_id, org_id, user_role, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_schedule_repository()
        schedule = await repo.get_schedule(
            schedule_id,
            str(org_id),
            created_by=str(user_id) if user_id else None,
            include_daemon_created=_include_daemon_workflows(user_role),
        )
        if not schedule:
            return json.dumps({"error": "Schedule not found"})
        try:
            result = await repo.toggle_schedule(
                schedule_id, str(org_id), enabled, requester_role=user_role,
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc), "code": 403})
        if not result:
            return json.dumps({"error": "Schedule not found"})
        return json.dumps(
            {k: str(v) if hasattr(v, "hex") else str(v) for k, v in result.items()}, default=str
        )

    @mcp.tool(
        description="""Get the run history for a specific schedule.

Args:
    schedule_id: The schedule UUID

Returns: JSON with the schedule details and its run history."""
    )
    async def get_schedule_details(schedule_id: str) -> str:
        user_id, org_id, user_role, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        repo = await _get_schedule_repository()
        result = await repo.get_schedule_with_runs(
            schedule_id,
            str(org_id),
            created_by=str(user_id),
            include_daemon_created=_include_daemon_workflows(user_role),
        )
        if not result:
            return json.dumps({"error": "Schedule not found"})
        return json.dumps(
            {k: str(v) if hasattr(v, "hex") else str(v) for k, v in result.items()},
            default=str,
        )

    @mcp.tool(
        description="""Create a workflow with a typed trigger and ordered actions.

Workflows are the broader replacement for schedules. A workflow has:
- trigger_type: 'schedule', 'manual', 'webhook', or 'integration_event'
- request_template: request title/description/dependency fields used per run
- actions: ordered action objects. action_type='task' creates daemon task work;
    action_type='user_interaction' sends a Handoff message to the user.
- review_instructions: checklist included for post-completion request review

For webhook workflows, provide webhook_secret. Lucent stores only a hash;
external callers send the secret as X-Lucent-Workflow-Token, Bearer token, or
?token= when POSTing /api/workflows/{id}/webhook."""
    )
    async def create_workflow(
        title: str,
        trigger_type: str = "schedule",
        description: str = "",
        schedule_type: str | None = None,
        cron_expression: str | None = None,
        interval_seconds: int | None = None,
        priority: str = "medium",
        actions_json: str | list | None = None,
        request_template_json: str | dict | None = None,
        review_instructions: str = "",
        webhook_secret: str | None = None,
    ) -> str:
        user_id, org_id, user_role, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if trigger_type not in ("schedule", "manual", "webhook", "integration_event"):
            return json.dumps(
                {"error": "trigger_type must be schedule, manual, webhook, or integration_event"}
            )
        if trigger_type == "webhook" and not webhook_secret:
            return json.dumps({"error": "webhook_secret is required for webhook workflows"})
        if priority not in ("low", "medium", "high", "urgent"):
            return json.dumps({"error": "priority must be 'low', 'medium', 'high', or 'urgent'"})

        if not schedule_type:
            schedule_type = "interval" if trigger_type == "schedule" else trigger_type
        if trigger_type == "schedule":
            if schedule_type not in ("once", "interval", "cron"):
                return json.dumps(
                    {"error": "schedule workflows require schedule_type once, interval, or cron"}
                )
            if schedule_type == "cron" and not cron_expression:
                return json.dumps({"error": "cron_expression is required for cron workflows"})
            if schedule_type == "interval" and (not interval_seconds or interval_seconds < 60):
                return json.dumps(
                    {"error": "interval_seconds must be >= 60 for interval workflows"}
                )

        try:
            actions = (
                json.loads(actions_json)
                if isinstance(actions_json, str) and actions_json.strip()
                else actions_json
            )
            request_template = (
                json.loads(request_template_json)
                if isinstance(request_template_json, str) and request_template_json.strip()
                else request_template_json
            )
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"Invalid JSON: {exc.msg}"})
        if actions is not None and not isinstance(actions, list):
            return json.dumps({"error": "actions_json must be a JSON array"})
        if request_template is not None and not isinstance(request_template, dict):
            return json.dumps({"error": "request_template_json must be a JSON object"})

        repo = await _get_schedule_repository()
        if actions:
            normalized_actions = []
            for idx, action in enumerate(actions):
                if not isinstance(action, dict):
                    return json.dumps({"error": f"actions_json[{idx}] must be an object"})
                action = dict(action)
                if "action_type" not in action and "type" in action:
                    action["action_type"] = action.pop("type")
                if "description" not in action:
                    if "instructions" in action:
                        action["description"] = action.pop("instructions")
                    elif "prompt" in action:
                        action["description"] = action.pop("prompt")
                action.setdefault("action_type", "task")
                action.setdefault("sequence_order", idx)
                if action.get("action_type", "task") == "server_function":
                    return json.dumps(
                        {"error": "server_function actions are reserved for built-in workflows"}
                    )
                normalized_actions.append(action)
            actions = normalized_actions
            try:
                from lucent.db.definitions import DefinitionRepository

                def_repo = DefinitionRepository(repo.pool)
                active = await def_repo.list_agents(
                    str(org_id),
                    status="active",
                    limit=1000,
                    requester_user_id=str(user_id) if user_id else None,
                    requester_role=user_role,
                )
                active_names = {str(agent.get("name")) for agent in active.get("items", [])}
            except Exception as exc:
                return json.dumps(
                    {
                        "error": "Could not validate workflow action agent types",
                        "detail": str(exc)[:300],
                    }
                )
            if not active_names:
                active_names = set(FALLBACK_WORKFLOW_AGENT_TYPES)
            if active_names:
                invalid = sorted(
                    {
                        str(action.get("agent_type") or "code")
                        for action in actions
                        if action.get("action_type", "task") == "task"
                        and str(action.get("agent_type") or "code") not in active_names
                    }
                )
                if invalid:
                    return json.dumps(
                        {
                            "error": "Unknown workflow action agent_type",
                            "invalid_agent_types": invalid,
                            "active_agent_types": sorted(active_names),
                        }
                    )
        workflow = await repo.create_schedule(
            title=title,
            org_id=str(org_id),
            schedule_type=schedule_type,
            description=description,
            agent_type=(actions[0].get("agent_type", "code") if actions else "code"),
            cron_expression=cron_expression,
            interval_seconds=interval_seconds,
            priority=priority,
            created_by=str(user_id),
            trigger_type=trigger_type,
            actions=actions,
            request_template=request_template,
            review_instructions=review_instructions,
            webhook_secret_hash=webhook_secret_hash(webhook_secret),
        )
        return json.dumps(
            {k: _jsonable_schedule_value(v) for k, v in workflow.items()},
            default=str,
        )

    @mcp.tool(
        description=(
            "List workflows, optionally filtered by status, enabled state, or trigger type."
        )
    )
    async def list_workflows(
        status: str | None = None,
        enabled_only: bool = False,
        trigger_type: str | None = None,
    ) -> str:
        user_id, org_id, user_role, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        repo = await _get_schedule_repository()
        result = await repo.list_schedules(
            str(org_id),
            status=status,
            enabled=True if enabled_only else None,
            created_by=str(user_id) if user_id else None,
            include_daemon_created=_include_daemon_workflows(user_role),
        )
        items = result["items"]
        if trigger_type:
            items = [i for i in items if (i.get("trigger_type") or "schedule") == trigger_type]
        serialized_items = [
            {k: str(v) if hasattr(v, "hex") else str(v) for k, v in s.items()}
            for s in items
        ]
        return json.dumps(
            {"items": serialized_items, "total_count": len(serialized_items), "has_more": False},
            default=str,
        )

    @mcp.tool(
        description="""Get workflow details and run history by workflow UUID."""
    )
    async def get_workflow_details(workflow_id: str) -> str:
        return await get_schedule_details(workflow_id)
