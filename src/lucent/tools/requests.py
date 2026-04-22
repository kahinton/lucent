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


async def _get_pool():
    """Get the database connection pool."""
    from lucent.db import init_db

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is required")
    return await init_db(database_url)


def register_request_tools(mcp: FastMCP) -> None:
    """Register request tracking tools with the MCP server."""

    @mcp.tool(
        description="""Create a tracked request \u2014 a top-level work item
that will be broken into tasks.

Use this when you identify new work to do (during cognitive cycles, from user messages, etc.).
The request will appear in the Requests UI and can be broken into tasks.

IMPORTANT: When creating a request for a goal memory, ALWAYS pass the goal's memory ID
as goal_id. This enables automatic deduplication — if a request for that goal already
has an active request (not completed/failed/cancelled), the existing request is returned
instead of creating a duplicate.

    Args:
    title: Short descriptive title (e.g. "Improve search performance")
    description: Detailed description of what needs to be done
    source: Where this request came from — 'cognitive', 'user', 'api', 'daemon', or 'schedule'
    priority: 'low', 'medium', 'high', or 'urgent'
    dependency_policy: 'strict' (default) blocks later tasks when a predecessor fails;
        'permissive' allows continuation past failed/cancelled predecessors.
    goal_id: Memory ID of the goal this request serves. Enables deduplication —
        only one active request per memory at a time.
    target_repo: Optional repository this request targets (owner/repo format, e.g. 'octocat/hello-world').
        When set, working agents automatically receive relevant technical memories as context.
    target_paths: Optional list of specific directories or files this request targets
        (e.g. ['src/lucent/db/', 'src/lucent/api/routers/']). Narrows which technical memories are injected.

Returns: JSON with the created request including its ID."""
    )
    async def create_request(
        title: str,
        description: str = "",
        source: str = "cognitive",
        priority: str = "medium",
        dependency_policy: str = "strict",
        goal_id: str = "",
        target_repo: str = "",
        target_paths: list[str] | None = None,
    ) -> str:
        user_id, org_id, _, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        memory_ids = None
        if goal_id:
            memory_ids = [{"id": goal_id, "relation": "goal"}]

        repo = await _get_request_repository()
        req = await repo.create_request(
            title=title,
            description=description,
            source=source,
            priority=priority,
            created_by=str(user_id) if user_id else None,
            org_id=str(org_id),
            dependency_policy=dependency_policy,
            memory_ids=memory_ids,
            target_repo=target_repo or None,
            target_paths=target_paths,
            force_pending_approval=(memory_scope == "user"),
        )

        # Goal already completed — no request created
        if req.get("status") == "skipped":
            reason = req.get("reason", "")
            if reason == "stale_goal_progress":
                return json.dumps({
                    "status": "skipped",
                    "reason": reason,
                    "detail": req.get("detail", ""),
                    "note": (
                        "STOP. The system refused this request because a recent "
                        "completed request exists for this goal but the goal's "
                        "milestone state hasn't been updated. Call update_memory "
                        "on the goal first to mark the completed milestone, then "
                        "decide whether new work is actually needed for the next "
                        "active milestone."
                    ),
                })
            return json.dumps({
                "status": "skipped",
                "reason": reason,
                "note": "This goal memory is already completed. Do NOT create work for it.",
            })

        result = {
            "id": str(req["id"]),
            "title": req["title"],
            "status": req["status"],
        }
        # If the returned title doesn't match what we asked for, this was a dedup hit
        if req["title"] != title:
            result["deduplicated"] = True
            result["note"] = (
                "An active request already exists for this goal memory. "
                "Do NOT create new tasks — work is already in progress."
            )
        return json.dumps(result)

    @mcp.tool(
        description="""Create a task within a tracked request.

Use after create_request to break a request into individual tasks that agents will execute.
Each task can be assigned to a specific agent type and will be dispatched in sequence_order.

IMPORTANT: The agent_type must match an approved agent definition in /definitions.
Tasks with unrecognized agent types will be rejected. Use list_available_agents or
check /definitions to see which agents are available.

SANDBOX POLICY: Tasks that need a sandbox MUST reference an approved sandbox
template via ``sandbox_template_id``. Inline ``sandbox_config`` is no longer
accepted from planners — first call ``list_sandbox_templates`` to see what's
approved, and if nothing fits, call ``propose_sandbox_template`` to submit a
new design for review.

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
    sandbox_template_id: UUID of an approved sandbox template (required for
        tasks that need a sandbox; use list_sandbox_templates to discover IDs).
    sandbox_overrides: Optional dict of fields to override on top of the
        template (currently supports: ``repo_url``, ``branch``,
        ``timeout_seconds``, ``output_mode``, ``commit_approved``). All other
        sandbox parameters come from the template and cannot be overridden.
    output_contract: Optional structured output contract dict:
        {json_schema: {...}, on_failure: fail|fallback|retry_then_fallback, max_retries: int}

Returns: JSON with the created task including its ID, or an error
if the agent type is not approved or the sandbox template is invalid."""
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
        sandbox_overrides: dict | None = None,
        output_contract: dict | None = None,
    ) -> str:
        user_id, org_id, user_role, _, _ = await _get_current_user_context()
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
                limit=200,
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

        # Sandbox policy: template-only. Resolve and validate the template
        # here, materializing the inline sandbox_config the daemon expects so
        # we keep a single dispatch path. Overrides are limited to a small,
        # safe whitelist.
        sandbox_config: dict | None = None
        if sandbox_template_id:
            from lucent.db.sandbox_template import SandboxTemplateRepository

            tpl_repo = SandboxTemplateRepository(pool)
            try:
                tpl = await tpl_repo.get(sandbox_template_id, str(org_id))
            except Exception as exc:
                return json.dumps(
                    {"error": f"Invalid sandbox_template_id: {exc}"}
                )
            if not tpl:
                approved = await tpl_repo.list_dispatchable(str(org_id))
                return json.dumps(
                    {
                        "error": (
                            f"Sandbox template {sandbox_template_id} not found "
                            f"in this organization."
                        ),
                        "available_templates": [
                            {"id": str(t["id"]), "name": t["name"]} for t in approved
                        ],
                        "hint": (
                            "Call list_sandbox_templates to discover IDs, or "
                            "propose_sandbox_template to submit a new design."
                        ),
                    }
                )
            if tpl.get("status") != "approved":
                return json.dumps(
                    {
                        "error": (
                            f"Sandbox template '{tpl.get('name')}' has status "
                            f"{tpl.get('status')!r} — only 'approved' templates "
                            "may be referenced by tasks. A human admin must "
                            "approve it first."
                        )
                    }
                )
            sandbox_config = tpl_repo.to_sandbox_config(tpl)

            allowed_overrides = {
                "repo_url",
                "branch",
                "timeout_seconds",
                "output_mode",
                "commit_approved",
            }
            if sandbox_overrides:
                bad = set(sandbox_overrides) - allowed_overrides
                if bad:
                    return json.dumps(
                        {
                            "error": (
                                f"sandbox_overrides may only set "
                                f"{sorted(allowed_overrides)}; rejected keys: "
                                f"{sorted(bad)}. Anything else must be baked "
                                "into a new sandbox template via "
                                "propose_sandbox_template."
                            )
                        }
                    )
                for k, v in sandbox_overrides.items():
                    sandbox_config[k] = v
        elif sandbox_overrides:
            return json.dumps(
                {
                    "error": (
                        "sandbox_overrides requires sandbox_template_id. "
                        "Pick an approved template first."
                    )
                }
            )

        repo = await _get_request_repository()
        parent_request = await repo.get_request(request_id, str(org_id))
        if not parent_request:
            return json.dumps({"error": "Request not found"})

        # Ownership gate: a caller may only attach tasks to a request they
        # own (request.created_by == caller). This aligns with the per-user
        # cognitive cycle, where each scoped session can only see and act
        # on its own user's requests. Org admins/owners are exempt so they
        # can fix or augment work across the org from the UI/MCP.
        request_owner_id = parent_request.get("created_by")
        if user_role not in ("admin", "owner"):
            if not request_owner_id or str(request_owner_id) != str(user_id):
                return json.dumps(
                    {
                        "error": (
                            "You may only create tasks on a request you own. "
                            "This request belongs to another user. If you "
                            "need to add work to it, ask the owner or an "
                            "organization admin."
                        )
                    }
                )

        requesting_user_id = request_owner_id
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
                "sandbox_template_id": (
                    str(task["sandbox_template_id"])
                    if task.get("sandbox_template_id")
                    else None
                ),
            }
        )

    @mcp.tool(
        description="""List approved sandbox templates the planner can reference.

Use this BEFORE calling create_task whenever a task needs to run in a sandbox.
Returns the templates currently approved for dispatch in this organization,
including built-in templates and any organization-approved custom templates.

If none of the returned templates fit the work you're planning, call
propose_sandbox_template to submit a new design for human review — do NOT
fall back to inline sandbox_config (it's no longer accepted)."""
    )
    async def list_sandbox_templates() -> str:
        _, org_id, _, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        from lucent.db import get_pool
        from lucent.db.sandbox_template import SandboxTemplateRepository

        pool = await get_pool()
        repo = SandboxTemplateRepository(pool)
        approved = await repo.list_dispatchable(str(org_id))
        proposed = await repo.list_proposed(str(org_id))

        def _summary(tpl: dict) -> dict:
            return {
                "id": str(tpl["id"]),
                "name": tpl["name"],
                "scope": tpl.get("scope"),
                "description": tpl.get("description", ""),
                "image": tpl.get("image"),
                "network_mode": tpl.get("network_mode"),
                "allowed_hosts": tpl.get("allowed_hosts") or [],
                "memory_limit": tpl.get("memory_limit"),
                "cpu_limit": float(tpl.get("cpu_limit", 0)) if tpl.get("cpu_limit") else None,
                "timeout_seconds": tpl.get("timeout_seconds"),
            }

        return json.dumps(
            {
                "approved": [_summary(t) for t in approved],
                "proposed_pending_review": [_summary(t) for t in proposed],
                "hint": (
                    "Reference one of the approved templates by id via "
                    "create_task(sandbox_template_id=...). If none fit the "
                    "work, call propose_sandbox_template instead of falling "
                    "back to inline configs."
                ),
            }
        )

    @mcp.tool(
        description="""Propose a new sandbox template for human approval.

Use when none of the templates returned by list_sandbox_templates fit the
work you need to do. The proposed template is saved with status='proposed'
and shows up in the admin's review queue. It cannot be referenced by a task
until a human approves it (status='approved').

Be specific in ``reason`` — explain what the planner needs that no existing
template provides, so the reviewer can decide whether to approve it as a
one-off or promote it to a built-in.

Args:
    name: Unique template name (snake-case-or-dash, e.g. ``rust-cargo-builder``)
    description: What this sandbox is for and when to use it
    image: Docker image (must be a public/known image; reviewers verify this)
    reason: Why no existing template suffices and what's special about this one
    network_mode: 'none' (default), 'bridge', or 'allowlist'
    allowed_hosts: List of hosts (required when network_mode='allowlist')
    setup_commands: Commands to run after clone, before the agent task starts
    env_vars: Static environment variables (NEVER put secrets here)
    memory_limit: e.g. '2g'  (default '2g')
    cpu_limit: e.g. 2.0      (default 2.0)
    disk_limit: e.g. '10g'   (default '10g')
    timeout_seconds: Max wall-clock time per task (default 1800)
    working_dir: Default '/workspace'

Returns: JSON with the proposed template id and status."""
    )
    async def propose_sandbox_template(
        name: str,
        description: str,
        image: str,
        reason: str,
        network_mode: str = "none",
        allowed_hosts: list[str] | None = None,
        setup_commands: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
        memory_limit: str = "2g",
        cpu_limit: float = 2.0,
        disk_limit: str = "10g",
        timeout_seconds: int = 1800,
        working_dir: str = "/workspace",
    ) -> str:
        user_id, org_id, _, _, _ = await _get_current_user_context()
        if not user_id or not org_id:
            return json.dumps({"error": "Authentication required"})

        if network_mode not in {"none", "bridge", "allowlist"}:
            return json.dumps({"error": "network_mode must be 'none', 'bridge', or 'allowlist'"})
        if network_mode == "allowlist" and not allowed_hosts:
            return json.dumps(
                {
                    "error": (
                        "allowed_hosts is required when network_mode='allowlist'."
                    )
                }
            )

        from lucent.db import get_pool
        from lucent.db.sandbox_template import SandboxTemplateRepository

        pool = await get_pool()
        repo = SandboxTemplateRepository(pool)

        existing = await repo.get_by_name(name, str(org_id))
        if existing:
            return json.dumps(
                {
                    "error": (
                        f"A template named '{name}' already exists "
                        f"(status={existing.get('status')}). Pick a different "
                        "name or wait for the existing proposal to be reviewed."
                    ),
                    "existing_id": str(existing["id"]),
                }
            )

        try:
            tpl = await repo.create(
                name=name,
                organization_id=str(org_id),
                description=description,
                image=image,
                network_mode=network_mode,
                allowed_hosts=allowed_hosts or [],
                setup_commands=setup_commands or [],
                env_vars=env_vars or {},
                memory_limit=memory_limit,
                cpu_limit=cpu_limit,
                disk_limit=disk_limit,
                timeout_seconds=timeout_seconds,
                working_dir=working_dir,
                created_by=str(user_id),
                owner_user_id=str(user_id),
                scope="instance",
                status="proposed",
                proposed_by=str(user_id),
                proposal_reason=reason,
            )
        except Exception as exc:
            return json.dumps({"error": f"Failed to create proposal: {exc}"})

        return json.dumps(
            {
                "id": str(tpl["id"]),
                "name": tpl["name"],
                "status": tpl["status"],
                "note": (
                    "Submitted for human review. The template cannot be "
                    "referenced by a task until an admin approves it. Either "
                    "wait for approval, or — if the work is urgent — pick the "
                    "closest existing approved template and adapt the task "
                    "description to match its constraints."
                ),
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
        user_id, org_id, _, _, _ = await _get_current_user_context()
        if not user_id:
            return json.dumps({"error": "Authentication required"})
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_request_repository()
        try:
            event = await repo.add_task_event(task_id, event_type, detail, org_id=str(org_id))
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
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
        user_id, org_id, _, _, _ = await _get_current_user_context()
        if not user_id:
            return json.dumps({"error": "Authentication required"})
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_request_repository()
        try:
            await repo.link_memory(task_id, memory_id, relation, org_id=str(org_id))
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({"status": "linked", "task_id": task_id, "memory_id": memory_id})

    @mcp.tool(
        description="""Link a memory to a tracked request \u2014 establishing a relationship
between the request and a memory (typically a goal).

Use this to connect a request to the goal memory that motivated it, or to any other
relevant memory. Linked memories are shown in the request detail UI and are included
in the post-completion review task for update.

Args:
    request_id: ID of the request
    memory_id: ID of the memory to link
    relation: How the memory relates — 'goal' (default), 'context', or 'reference'

Returns: JSON confirmation."""
    )
    async def link_request_memory(
        request_id: str,
        memory_id: str,
        relation: str = "goal",
    ) -> str:
        user_id, org_id, _, _, _ = await _get_current_user_context()
        if not user_id:
            return json.dumps({"error": "Authentication required"})
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_request_repository()
        result = await repo.link_request_memory(
            request_id, memory_id, relation, org_id=str(org_id),
        )
        if result is None:
            return json.dumps({"error": "Request not found or access denied"})
        return json.dumps({
            "status": "linked",
            "request_id": request_id,
            "memory_id": memory_id,
            "relation": relation,
        })

    @mcp.tool(
        description="""Get the full details of a tracked request
including its task tree, events, memory links, and review history.

Args:
    request_id: ID of the request

Returns: JSON with request details, task breakdown, events timeline, memory links, and reviews."""
    )
    async def get_request_details(request_id: str) -> str:
        _, org_id, _, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_request_repository()
        req = await repo.get_request_with_tasks(request_id, str(org_id))
        if not req:
            return json.dumps({"error": "Request not found"})

        # Include review history
        from lucent.db.reviews import ReviewRepository

        pool = await _get_pool()
        review_repo = ReviewRepository(pool)
        reviews = await review_repo.get_reviews_for_request(request_id, str(org_id))
        req["reviews"] = reviews

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
        _, org_id, _, _, _ = await _get_current_user_context()
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
        _, org_id, _, _, _ = await _get_current_user_context()
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
        _, org_id, _, _, _ = await _get_current_user_context()
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

    @mcp.tool(
        description="""Close out a rejected request after the feedback loop has been processed.

This is the ONLY MCP-exposed status transition for requests. It is intentionally
narrow: it can only move a request from `rejection_processing` to `cancelled`,
and only when the caller is the request's owner. It cannot approve, reject, or
otherwise change request state.

Use this AFTER you have:
1. Read the rejection reason on the request,
2. Updated the linked goal memory (or memories) with the feedback, and
3. Tagged the related feedback memory as `feedback-processed`.

Calling this closes the rejection feedback loop so the request stops appearing
in the daemon's `rejection_processing` queue. Without this call, rejected
requests remain unprocessed forever.

Args:
    request_id: UUID of the request currently in `rejection_processing` state.
    note: Optional short note describing what was done with the feedback.

Returns: JSON with the updated request, or an error if the request is not in
    `rejection_processing` or is not owned by the caller."""
    )
    async def mark_rejection_processed(
        request_id: str,
        note: str = "",
    ) -> str:
        user_id, org_id, _, _, _ = await _get_current_user_context()
        if not user_id:
            return json.dumps({"error": "Authentication required"})
        if not org_id:
            return json.dumps({"error": "No organization context"})

        repo = await _get_request_repository()
        existing = await repo.get_request(request_id, str(org_id))
        if not existing:
            return json.dumps({"error": f"Request {request_id} not found"})
        if str(existing.get("created_by")) != str(user_id):
            return json.dumps(
                {
                    "error": (
                        "Only the request's owner may close out a rejected "
                        "request. The owner is the user the request was created "
                        "on behalf of."
                    )
                }
            )
        if existing.get("status") != "rejection_processing":
            return json.dumps(
                {
                    "error": (
                        f"Request is in status {existing.get('status')!r}, not "
                        "'rejection_processing'. This tool only closes out "
                        "requests that have already been rejected by the user."
                    )
                }
            )

        try:
            updated = await repo.update_request_status(
                request_id, "cancelled", org_id=str(org_id)
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        if not updated:
            return json.dumps({"error": "Failed to update request"})

        return json.dumps(
            {
                "id": str(updated["id"]),
                "status": updated["status"],
                "note": note or "feedback processed",
            }
        )

    @mcp.tool(
        description="""Create a review for a request or task.

This MCP tool is informational-only and cannot approve/reject requests.
Final approval decisions must go through the authenticated REST API, which
applies transactional status transitions and follow-up side effects.

Args:
    request_id: UUID of the request being reviewed
    status: Reserved for compatibility. MCP rejects approval/rejection decisions.
    task_id: Optional UUID of the specific task being reviewed
    comments: Optional review comments/feedback (max 10,000 chars)
    source: Origin of the review — 'human', 'daemon', or 'agent' (default: 'agent')

Returns: JSON error for approval/rejection attempts from MCP."""
    )
    async def create_review(
        request_id: str,
        status: str,
        task_id: str | None = None,
        comments: str | None = None,
        source: str = "agent",
    ) -> str:
        user_id, org_id, _, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        if source not in ("human", "daemon", "agent"):
            return json.dumps(
                {"error": "source must be one of: 'human', 'daemon', 'agent'"}
            )

        try:
            req_repo = await _get_request_repository()
            request = await req_repo.get_request(request_id, str(org_id))
            if not request:
                return json.dumps({"error": "Request not found"})

            # Prevent request creators from reviewing their own request.
            request_creator = request.get("created_by")
            if request_creator and user_id and str(request_creator) == str(user_id):
                return json.dumps(
                    {"error": "Request creators cannot review their own requests"}
                )

            if task_id:
                task = await req_repo.get_task(task_id, str(org_id))
                if not task:
                    return json.dumps({"error": "Task not found"})
                if str(task.get("request_id")) != request_id:
                    return json.dumps(
                        {"error": "Task does not belong to the specified request"}
                    )

            if comments and len(comments) > 10000:
                return json.dumps({"error": "comments must be at most 10000 characters"})

            # MCP tools are agent-originated and do not perform request status
            # transitions/follow-up side effects. Reject approval/rejection decisions
            # so agents cannot unilaterally move requests through lifecycle states.
            if status in ("approved", "rejected"):
                return json.dumps(
                    {
                        "error": (
                            "MCP create_review cannot submit 'approved' or 'rejected' "
                            "decisions; use the REST API /api/reviews"
                        )
                    }
                )
            return json.dumps(
                {
                    "error": (
                        "status must be 'approved' or 'rejected' "
                        "(REST API only; MCP is informational-only)"
                    )
                }
            )
        except Exception:
            return json.dumps({"error": "Failed to create review"})

    @mcp.tool(
        description="""List reviews with optional filters.

Query reviews by request, task, status, or source. Returns paginated results.

Args:
    request_id: Optional — filter to reviews for a specific request
    task_id: Optional — filter to reviews for a specific task
    status: Optional — 'approved' or 'rejected'
    source: Optional — 'human', 'daemon', or 'agent'
    limit: Max results (default 25, max 100)
    offset: Pagination offset (default 0)

Returns: JSON with paginated review list."""
    )
    async def list_reviews(
        request_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        _, org_id, _, _, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})

        from lucent.db.reviews import ReviewRepository

        pool = await _get_pool()
        repo = ReviewRepository(pool)
        result = await repo.list_reviews(
            str(org_id),
            request_id=request_id,
            task_id=task_id,
            status=status,
            source=source,
            limit=min(limit, 100),
            offset=offset,
        )

        def serialize(obj):
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            if isinstance(obj, UUID):
                return str(obj)
            return str(obj)

        return json.dumps(result, default=serialize)
