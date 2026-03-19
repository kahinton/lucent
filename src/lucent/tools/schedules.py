"""MCP tools for schedule management."""

import json
import os

from mcp.server.fastmcp import FastMCP

from lucent.db.schedules import ScheduleRepository
from lucent.tools.memories import _get_current_user_context


async def _get_schedule_repository() -> ScheduleRepository:
    from lucent.db import init_db

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is required")
    pool = await init_db(database_url)
    return ScheduleRepository(pool)


def register_schedule_tools(mcp: FastMCP) -> None:
    """Register schedule management tools with the MCP server."""

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
        cron_expression: str | None = None,
        interval_seconds: int | None = None,
        priority: str = "medium",
        max_runs: int | None = None,
        sandbox_template_id: str | None = None,
    ) -> str:
        user_id, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

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
            from lucent.model_registry import validate_model

            error = validate_model(model)
            if error:
                return json.dumps({"error": error})

        repo = await _get_schedule_repository()
        sched = await repo.create_schedule(
            title=title,
            org_id=str(org_id),
            schedule_type=schedule_type,
            description=description,
            agent_type=agent_type,
            model=model,
            cron_expression=cron_expression,
            interval_seconds=interval_seconds,
            priority=priority,
            max_runs=max_runs,
            sandbox_template_id=sandbox_template_id,
            created_by=str(user_id) if user_id else None,
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
        user_id, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_schedule_repository()
        result = await repo.list_schedules(
            str(org_id),
            status=status,
            enabled=True if enabled_only else None,
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
        user_id, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_schedule_repository()
        result = await repo.toggle_schedule(schedule_id, str(org_id), enabled)
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
        user_id, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_schedule_repository()
        result = await repo.get_schedule_with_runs(schedule_id, str(org_id))
        if not result:
            return json.dumps({"error": "Schedule not found"})
        return json.dumps(
            {k: str(v) if hasattr(v, "hex") else str(v) for k, v in result.items()},
            default=str,
        )
