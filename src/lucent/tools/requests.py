"""MCP tools for request tracking and task queue operations."""

import json
import os
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from lucent.db.requests import RequestRepository
from lucent.tools.memories import _get_current_user_context


async def _get_request_repository() -> RequestRepository:
    """Get or create a RequestRepository instance."""
    from lucent.db import init_db

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is required")
    pool = await init_db(database_url)
    return RequestRepository(pool)


def register_request_tools(mcp: FastMCP) -> None:
    """Register request tracking tools with the MCP server."""

    @mcp.tool(
        description="""Create a tracked request \u2014 a top-level work item
that will be broken into tasks.

Use this when you identify new work to do (during cognitive cycles, from user messages, etc.).
The request will appear in the Requests UI and can be broken into tasks.

    Args:
    title: Short descriptive title (e.g. "Improve search performance")
    description: Detailed description of what needs to be done
    source: Where this request came from — 'cognitive', 'user', 'api', 'daemon', or 'schedule'
    priority: 'low', 'medium', 'high', or 'urgent'
    dependency_policy: 'strict' (default) blocks later tasks when a predecessor fails;
        'permissive' allows continuation past failed/cancelled predecessors.

Returns: JSON with the created request including its ID."""
    )
    async def create_request(
        title: str,
        description: str = "",
        source: str = "cognitive",
        priority: str = "medium",
        dependency_policy: str = "strict",
    ) -> str:
        user_id, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_request_repository()
        req = await repo.create_request(
            title=title,
            description=description,
            source=source,
            priority=priority,
            created_by=str(user_id) if user_id else None,
            org_id=str(org_id),
            dependency_policy=dependency_policy,
        )
        return json.dumps({"id": str(req["id"]), "title": req["title"], "status": req["status"]})

    @mcp.tool(
        description="""Create a task within a tracked request.

Use after create_request to break a request into individual tasks that agents will execute.
Each task can be assigned to a specific agent type and will be dispatched in sequence_order.

IMPORTANT: The agent_type must match an approved agent definition in /definitions.
Tasks with unrecognized agent types will be rejected. Use list_available_agents or
check /definitions to see which agents are available.

Args:
    request_id: ID of the parent request
    title: Short descriptive title for the task
    description: Full task instructions for the agent
    agent_type: Name of an approved agent definition to handle this task
    model: LLM model to use for this task. If not set, the daemon
        picks a default. See list_available_models for options.
    priority: 'low', 'medium', 'high', or 'urgent'
    sequence_order: Execution order (0-based, lower runs first)
    parent_task_id: Optional \u2014 ID of parent task for sub-tasks
    sandbox_template_id: Optional UUID of a saved sandbox template to use
    sandbox_config: Optional inline sandbox config dict (keys: image, repo_url,
        branch, working_dir, timeout_seconds, output_mode, env_vars, etc.)
    output_contract: Optional structured output contract dict:
        {json_schema: {...}, on_failure: fail|fallback|retry_then_fallback, max_retries: int}

Returns: JSON with the created task including its ID, or an error
if the agent type is not approved."""
    )
    async def create_task(
        request_id: str,
        title: str,
        description: str = "",
        agent_type: str = "code",
        model: str | None = None,
        priority: str = "medium",
        sequence_order: int = 0,
        parent_task_id: str | None = None,
        sandbox_template_id: str | None = None,
        sandbox_config: dict | None = None,
        output_contract: dict | None = None,
    ) -> str:
        user_id, org_id, user_role = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})

        if sequence_order < 0:
            return json.dumps({"error": "sequence_order must be >= 0"})

        # Validate model against registry
        if model:
            from lucent.model_registry import validate_model

            error = validate_model(model)
            if error:
                return json.dumps({"error": error})

        # Validate agent_type resolves to an approved definition
        from lucent.db import get_pool
        from lucent.db.definitions import DefinitionRepository

        pool = await get_pool()
        def_repo = DefinitionRepository(pool)
        agents = (
            await def_repo.list_agents(
                str(org_id),
                status="active",
                requester_user_id=str(user_id),
                requester_role=user_role,
            )
        )["items"]
        active_names = {a["name"] for a in agents}
        if agent_type and agent_type not in active_names:
            avail = sorted(active_names) if active_names else "none — approve definitions first"
            return json.dumps(
                {
                    "error": f"No approved agent definition for '{agent_type}'. "
                    f"Create and approve one at /definitions before assigning tasks. "
                    f"Available agents: {avail}",
                }
            )

        repo = await _get_request_repository()
        parent_request = await repo.get_request(request_id, str(org_id))
        if not parent_request:
            return json.dumps({"error": "Request not found"})
        requesting_user_id = parent_request.get("created_by")
        try:
            task = await repo.create_task(
                request_id=request_id,
                title=title,
                org_id=str(org_id),
                description=description,
                agent_type=agent_type,
                priority=priority,
                sequence_order=sequence_order,
                parent_task_id=parent_task_id,
                model=model,
                sandbox_template_id=sandbox_template_id,
                sandbox_config=sandbox_config,
                requesting_user_id=str(requesting_user_id) if requesting_user_id else None,
                output_contract=output_contract,
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(
            {
                "id": str(task["id"]),
                "title": task["title"],
                "status": task["status"],
                "agent_type": task["agent_type"],
                "model": task.get("model"),
            }
        )

    @mcp.tool(
        description="""Log a progress event on a tracked task.

Use during task execution to record progress, state changes, or noteworthy actions.
Events appear in the task's timeline in the UI.

Args:
    task_id: ID of the task
    event_type: Type of event \u2014 'progress', 'info', 'warning',
        'agent_dispatched', 'agent_completed', etc.
    detail: Human-readable description of what happened

Returns: JSON confirmation."""
    )
    async def log_task_event(
        task_id: str,
        event_type: str,
        detail: str = "",
    ) -> str:
        # Anti-spoofing V6: require authentication
        user_id, org_id, _ = await _get_current_user_context()
        if not user_id:
            return json.dumps({"error": "Authentication required"})

        repo = await _get_request_repository()
        event = await repo.add_task_event(task_id, event_type, detail)
        return json.dumps({"id": str(event["id"]), "event_type": event_type})

    @mcp.tool(
        description="""Link a memory to a tracked task \u2014 showing which
memories were created, read, or updated during task execution.

This creates the lineage between tasks and the memories they interact with,
visible in the request detail UI.

Args:
    task_id: ID of the task
    memory_id: ID of the memory that was touched
    relation: How the memory was used — 'created', 'read', or 'updated'

Returns: JSON confirmation."""
    )
    async def link_task_memory(
        task_id: str,
        memory_id: str,
        relation: str = "created",
    ) -> str:
        # Anti-spoofing V6: require authentication
        user_id, org_id, _ = await _get_current_user_context()
        if not user_id:
            return json.dumps({"error": "Authentication required"})

        repo = await _get_request_repository()
        await repo.link_memory(task_id, memory_id, relation)
        return json.dumps({"status": "linked", "task_id": task_id, "memory_id": memory_id})

    @mcp.tool(
        description="""Get the full details of a tracked request
including its task tree, events, and memory links.

Args:
    request_id: ID of the request

Returns: JSON with request details, task breakdown, events timeline, and memory links."""
    )
    async def get_request_details(request_id: str) -> str:
        _, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_request_repository()
        req = await repo.get_request_with_tasks(request_id, str(org_id))
        if not req:
            return json.dumps({"error": "Request not found"})

        # Serialize for JSON
        def serialize(obj):
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            if isinstance(obj, UUID):
                return str(obj)
            return str(obj)

        return json.dumps(req, default=serialize)

    @mcp.tool(
        description="""List pending requests \u2014 top-level work items
waiting to be planned or executed.

Returns requests with status 'pending', including how many tasks each has.
Requests with 0 tasks need to be broken into tasks before they can be dispatched.
Use this during cognitive cycles to discover new work."""
    )
    async def list_pending_requests() -> str:
        _, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_request_repository()
        requests = await repo.list_pending_requests(str(org_id))

        def serialize(obj):
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            if isinstance(obj, UUID):
                return str(obj)
            return str(obj)

        return json.dumps(requests, default=serialize)

    @mcp.tool(
        description="""List all active (non-completed) work \
— requests and their task status summaries.

Returns requests in pending/in_progress/planned status along with task counts
broken down by status (pending, running, completed, failed). Use this during
cognitive cycles to understand what's already being worked on BEFORE creating
new requests. This prevents duplicate work items."""
    )
    async def list_active_work() -> str:
        _, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_request_repository()
        work = await repo.list_active_work(str(org_id))

        def serialize(obj):
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            if isinstance(obj, UUID):
                return str(obj)
            return str(obj)

        return json.dumps(work, default=serialize)

    @mcp.tool(
        description="""List available LLM models for task assignment.

Use this to choose a model when creating tasks. Returns all available models
with their categories, capabilities, and provider info.

Args:
    category: Optional filter — one of: general, fast, reasoning, agentic, visual
    agent_type: Optional agent type to also return the recommended model for
        (code, research, memory, reflection, documentation, planning, review, fast, agentic)

Returns: JSON with list of models and, if agent_type provided, the recommended model."""
    )
    async def list_available_models(
        category: str | None = None,
        agent_type: str | None = None,
    ) -> str:
        from lucent.model_registry import get_recommended_model, list_models

        models = list_models(category=category)
        result: dict = {
            "models": [
                {
                    "id": m.id,
                    "name": m.name,
                    "provider": m.provider,
                    "category": m.category,
                    "supports_tools": m.supports_tools,
                    "supports_vision": m.supports_vision,
                    "context_window": m.context_window,
                    "notes": m.notes,
                    "tags": m.tags,
                }
                for m in models
            ]
        }
        if agent_type:
            result["recommended"] = get_recommended_model(agent_type)
        return json.dumps(result)

    @mcp.tool(
        description="""List pending tracked tasks in the queue.

Returns tasks that are waiting to be claimed and executed, ordered by priority.
Use this to see what work is queued up."""
    )
    async def list_pending_tasks() -> str:
        _, org_id, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_request_repository()
        tasks = await repo.list_pending_tasks(str(org_id))

        def serialize(obj):
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            if isinstance(obj, UUID):
                return str(obj)
            return str(obj)

        return json.dumps(tasks, default=serialize)
