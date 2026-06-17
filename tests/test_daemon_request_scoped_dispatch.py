"""Request-scoped dispatch ACL tests for daemon resource selection."""

import io
import json
import logging
from uuid import UUID

import pytest

from daemon.daemon import (
    LucentDaemon,
    _build_mcp_tool_summary,
    _is_operational_tool_call,
    load_accessible_agent,
)
from lucent.db.definitions import DefinitionRepository
from lucent.db.user import UserRepository
from lucent.log_context import clear_log_context
from lucent.logging import JSONFormatter


def test_validate_task_result_rejects_empty_consolidation_execution():
    daemon = LucentDaemon()
    success, reason = daemon._validate_task_result(
        "Identified 3 merges planned. No writes executed.",
        task={
            "title": "Memory consolidation pass",
            "description": "Run memory consolidation and merge duplicates",
        },
        tool_counts={"update_memory": 0, "delete_memory": 0},
    )
    assert success is False
    assert reason == "Plan identified 3 operations but 0 were executed"


def test_validate_task_result_rejects_long_blocked_report():
    daemon = LucentDaemon()
    result = (
        "I cannot complete this task in the current sub-agent environment.\n\n"
        "## Status: BLOCKED — missing required tooling\n\n"
        "No GitHub MCP tool is available, and neither gh nor git is exposed.\n\n"
        + "More diagnostic detail. " * 120
    )

    success, reason = daemon._validate_task_result(
        result,
        task={"title": "Bootstrap repo", "description": "Create GitHub repo"},
        tool_counts={"bash": 4, "create_memory": 1},
    )

    assert success is False
    assert reason == "output reports task is blocked or missing required tooling"


def test_prefixed_memory_server_tool_counts_as_memory_tool():
    summary = _build_mcp_tool_summary(
        [
            {
                "tool": "memory-server-search_memories",
                "params": "query='repo bootstrap'",
            }
        ]
    )

    assert "Memory tool calls: 1 total" in summary
    assert "search=1" in summary
    assert "search_memories=1" in summary


def test_report_intent_is_not_operational_tool_usage():
    assert _is_operational_tool_call({"tool": "report_intent"}) is False
    assert _is_operational_tool_call({"tool": "bash"}) is True
    assert _is_operational_tool_call({"tool": "memory-server-search_memories"}) is True


def test_extract_suggested_breakdown_items_from_request_description():
    description = """
Some request context.

Suggested task breakdown:
1. Bootstrap private repo and initial docs.
2. Research the local market and customer hypotheses.
3. Synthesize the launch roadmap.

Definition of done:
Everything is documented.
"""

    items = LucentDaemon._extract_suggested_breakdown_items(description)

    assert items == [
        "Bootstrap private repo and initial docs.",
        "Research the local market and customer hypotheses.",
        "Synthesize the launch roadmap.",
    ]


def test_milestone_scoped_fallback_ignores_full_goal_breakdown():
    daemon = LucentDaemon()

    specs = daemon._build_fallback_decomposition_tasks(
        {
            "title": "Full goal request",
            "goal_milestone_index": 1,
            "goal_milestone_description": "Bootstrap the private planning repository.",
            "target_repo": "kahinton/example",
            "target_paths": ["README.md", "docs/"],
            "description": """
Suggested task breakdown:
1. Bootstrap repo.
2. Research market.
3. Build financial model.

Required outcome:
- Create README.md and docs/ placeholders only.
""",
        }
    )

    assert len(specs) == 1
    assert specs[0]["title"] == "Bootstrap the private planning repository"
    assert "Bootstrap the private planning repository" in specs[0]["description"]
    assert "Required outcome" in specs[0]["description"]
    assert "README.md" in specs[0]["description"]
    assert "Research market" not in specs[0]["description"]
    assert "create_task" not in specs[0]["description"]


def test_decomposition_prompt_names_single_goal_milestone_scope():
    daemon = LucentDaemon()

    prompt = daemon._build_decomposition_prompt(
        {
            "request_id": "request-1",
            "title": "Full goal request",
            "priority": "high",
            "goal_milestone_index": 1,
            "goal_milestone_description": "Bootstrap the private planning repository.",
            "description": "Full goal context with later milestones.",
        }
    )

    assert "ONLY goal milestone 1" in prompt
    assert "Bootstrap the private planning repository" in prompt
    assert "Do not decompose the whole goal" in prompt


@pytest.mark.asyncio
async def test_fallback_decomposition_creates_tasks_from_suggested_breakdown(monkeypatch):
    daemon = LucentDaemon()
    created: list[dict] = []

    async def _create_task(
        request_id,
        title,
        agent_type=None,
        description=None,
        priority="medium",
        sequence_order=0,
        **_kwargs,
    ):
        created.append({
            "request_id": request_id,
            "title": title,
            "agent_type": agent_type,
            "description": description,
            "priority": priority,
            "sequence_order": sequence_order,
        })
        return {"id": f"task-{sequence_order}", "title": title}

    monkeypatch.setattr("daemon.daemon.RequestAPI.create_task", _create_task)

    count = await daemon._create_fallback_decomposition_tasks(
        {
            "request_id": "request-1",
            "title": "Build business proof of concept",
            "priority": "high",
            "target_repo": "kahinton/example",
            "target_paths": ["docs/"],
            "description": """
Suggested task breakdown:
1. Bootstrap private GitHub repo and initial docs.
2. Research Metro Detroit customer segments.
3. Design autonomous roles and tooling.
""",
        },
        "I would create three tasks.",
        [],
    )

    assert count == 3
    assert [task["sequence_order"] for task in created] == [0, 1, 2]
    assert created[0]["agent_type"] == "code"
    assert created[1]["agent_type"] == "research"
    assert created[2]["agent_type"] == "planning"
    assert all(task["priority"] == "high" for task in created)


@pytest.mark.asyncio
async def test_load_accessible_agent_filters_by_requesting_user(
    db_pool, test_organization, test_user, clean_test_data
):
    org_id = str(test_organization["id"])
    user_repo = UserRepository(db_pool)
    other_user = await user_repo.create(
        external_id=f"{clean_test_data}other-user",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{clean_test_data}other@test.com",
        display_name="Other User",
    )

    repo = DefinitionRepository(db_pool)
    agent = await repo.create_agent(
        name=f"{clean_test_data}code",
        description="Owned agent",
        content="agent content",
        org_id=org_id,
        created_by=str(test_user["id"]),
        status="active",
        owner_user_id=str(test_user["id"]),
    )

    accessible = await load_accessible_agent(
        org_id=org_id,
        requester_user_id=str(test_user["id"]),
        agent_type=f"{clean_test_data}code",
    )
    blocked = await load_accessible_agent(
        org_id=org_id,
        requester_user_id=str(other_user["id"]),
        agent_type=f"{clean_test_data}code",
    )

    assert accessible is not None
    assert accessible["id"] == agent["id"]
    assert blocked is None


@pytest.mark.asyncio
async def test_dispatch_fails_gracefully_when_no_accessible_agent(monkeypatch):
    daemon = LucentDaemon()
    failed: list[str] = []
    events: list[tuple[str, str, str | None]] = []
    starts: list[str] = []

    async def _pending():
        return [
            {
                "id": UUID("11111111-1111-1111-1111-111111111111"),
                "request_id": UUID("22222222-2222-2222-2222-222222222222"),
                "organization_id": UUID("33333333-3333-3333-3333-333333333333"),
                "title": "Restricted task",
                "description": "Should fail cleanly",
                "agent_type": "code",
                "requesting_user_id": UUID("44444444-4444-4444-4444-444444444444"),
            }
        ]

    async def _claim(task_id, _instance_id):
        return {"id": task_id}

    async def _update_model(_task_id, _model):
        return {"ok": True}

    async def _role(_user_id, _org_id):
        return "member"

    async def _ctx(_request_id):
        return "", ""

    async def _fail(task_id, error):
        failed.append(error)
        return {"id": task_id, "error": error}

    async def _event(task_id, event_type, detail=None, metadata=None):
        events.append((task_id, event_type, detail))
        return {"id": task_id, "event_type": event_type, "metadata": metadata}

    async def _start(task_id):
        starts.append(task_id)
        return {"id": task_id}

    async def _no_agent(**_kwargs):
        return None

    monkeypatch.setattr("daemon.daemon.RequestAPI.get_pending_tasks", _pending)
    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", _claim)
    monkeypatch.setattr("daemon.daemon.RequestAPI.update_task_model", _update_model)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_user_role", _role)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request_context", _ctx)
    monkeypatch.setattr("daemon.daemon.RequestAPI.fail_task", _fail)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _event)
    monkeypatch.setattr("daemon.daemon.RequestAPI.start_task", _start)
    monkeypatch.setattr("daemon.daemon.load_accessible_agent", _no_agent)

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert starts == []
    assert failed
    assert "No accessible approved agent definition" in failed[0]
    assert any(event_type == "agent_not_found" for _, event_type, _ in events)


@pytest.mark.asyncio
async def test_dispatch_memory_server_config_carries_user_scope_headers(monkeypatch):
    import daemon.daemon as daemon_module

    daemon = LucentDaemon()
    task_id = "11111111-1111-1111-1111-111111111111"
    request_id = "22222222-2222-2222-2222-222222222222"
    org_id = "33333333-3333-3333-3333-333333333333"
    user_id = "44444444-4444-4444-4444-444444444444"
    agent_id = "55555555-5555-5555-5555-555555555555"
    captured_mcp: dict = {}

    async def _noop_ensure_reviews():
        return None

    async def _pending():
        return [
            {
                "id": UUID(task_id),
                "request_id": UUID(request_id),
                "organization_id": UUID(org_id),
                "title": "Send user handoff",
                "description": "Use send_handoff to ask the user for configuration.",
                "agent_type": "research",
                "requesting_user_id": UUID(user_id),
            }
        ]

    async def _claim(claim_task_id, _instance_id):
        return {"id": claim_task_id}

    async def _update_model_settings(_task_id, **_kwargs):
        return {"ok": True}

    async def _ctx(_request_id):
        return "", ""

    async def _request(_request_id):
        return {"id": _request_id, "title": "User owned request"}

    async def _event(task_id, event_type, detail=None, metadata=None):
        return {"id": task_id, "event_type": event_type, "metadata": metadata}

    async def _start(task_id):
        return {"id": task_id}

    async def _complete(task_id, result, **_kwargs):
        return {"id": task_id, "result": result}

    async def _agent(**_kwargs):
        return {"id": agent_id, "name": "research", "content": "agent"}

    async def _empty(*_args, **_kwargs):
        return []

    async def _prompt(*_args, **_kwargs):
        return "system prompt"

    async def _mint(**_kwargs):
        return "hs_task_scoped"

    async def _run_session(*_args, **kwargs):
        captured_mcp.update(kwargs["mcp_config_override"])
        return "completed task output"

    monkeypatch.setattr(daemon, "_ensure_request_review_tasks", _noop_ensure_reviews)
    monkeypatch.setattr(daemon, "_get_technical_context_for_request", _ctx)
    monkeypatch.setattr(daemon, "_validate_task_result", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(daemon, "run_session", _run_session)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_pending_tasks", _pending)
    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", _claim)
    monkeypatch.setattr("daemon.daemon.RequestAPI.update_task_model_settings", _update_model_settings)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request_context", _ctx)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request", _request)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _event)
    monkeypatch.setattr("daemon.daemon.RequestAPI.start_task", _start)
    monkeypatch.setattr("daemon.daemon.RequestAPI.complete_task", _complete)
    monkeypatch.setattr("daemon.daemon.load_accessible_agent", _agent)
    monkeypatch.setattr("daemon.daemon.load_accessible_skills_for_agent", _empty)
    monkeypatch.setattr("daemon.daemon.load_accessible_mcp_servers_for_agent", _empty)
    monkeypatch.setattr("daemon.daemon.load_accessible_hooks_for_agent", _empty)
    monkeypatch.setattr("daemon.daemon.load_accessible_managed_tools_for_agent", _empty)
    monkeypatch.setattr("daemon.daemon.build_subagent_prompt", _prompt)
    monkeypatch.setattr("daemon.daemon._mint_scoped_api_key", _mint)
    daemon_module.MCP_CONFIG = {
        "memory-server": {
            "type": "http",
            "url": "http://mcp",
            "headers": {"Authorization": "Bearer daemon-key"},
            "tools": ["*"],
        }
    }

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    headers = captured_mcp["memory-server"]["headers"]
    assert headers["Authorization"] == "Bearer hs_task_scoped"
    assert headers["X-Lucent-Memory-Scope"] == "user"
    assert headers["X-Lucent-Memory-Scope-User-Id"] == user_id
    assert headers["X-Lucent-Org-Id"] == org_id
    assert headers["X-Lucent-Task-Id"] == task_id
    assert headers["X-Lucent-Request-Id"] == request_id


@pytest.mark.asyncio
async def test_dispatch_log_line_includes_task_request_and_user_context(monkeypatch):
    import daemon.daemon as daemon_module

    daemon = LucentDaemon()
    stream = io.StringIO()
    capture_logger = logging.getLogger("lucent.test.dispatch_context")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())

    prev_handlers = list(capture_logger.handlers)
    prev_level = capture_logger.level
    prev_propagate = capture_logger.propagate
    prev_daemon_logger = daemon_module._logger

    capture_logger.handlers = [handler]
    capture_logger.setLevel(logging.INFO)
    capture_logger.propagate = False
    daemon_module._logger = capture_logger

    task_id = "11111111-1111-1111-1111-111111111111"
    request_id = "22222222-2222-2222-2222-222222222222"
    org_id = "33333333-3333-3333-3333-333333333333"
    user_id = "44444444-4444-4444-4444-444444444444"

    async def _noop_ensure_reviews():
        return None

    async def _pending():
        return [
            {
                "id": UUID(task_id),
                "request_id": UUID(request_id),
                "organization_id": UUID(org_id),
                "title": "Restricted task",
                "description": "Should fail cleanly",
                "agent_type": "code",
                "requesting_user_id": UUID(user_id),
            }
        ]

    async def _claim(claim_task_id, _instance_id):
        return {"id": claim_task_id}

    async def _update_model(_task_id, _model):
        return {"ok": True}

    async def _ctx(_request_id):
        return "", ""

    async def _fail(task_id, error):
        return {"id": task_id, "error": error}

    async def _event(task_id, event_type, detail=None, metadata=None):
        return {"id": task_id, "event_type": event_type, "detail": detail, "metadata": metadata}

    async def _start(task_id):
        return {"id": task_id}

    async def _no_agent(**_kwargs):
        return None

    monkeypatch.setattr(daemon, "_ensure_request_review_tasks", _noop_ensure_reviews)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_pending_tasks", _pending)
    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", _claim)
    monkeypatch.setattr("daemon.daemon.RequestAPI.update_task_model", _update_model)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request_context", _ctx)
    monkeypatch.setattr("daemon.daemon.RequestAPI.fail_task", _fail)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _event)
    monkeypatch.setattr("daemon.daemon.RequestAPI.start_task", _start)
    monkeypatch.setattr("daemon.daemon.load_accessible_agent", _no_agent)

    try:
        await daemon._dispatch_tracked_tasks(max_tasks=1)

        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        dispatch_line = next(
            (line for line in lines if "Dispatching tracked task" in str(line.get("message", ""))),
            None,
        )
        assert dispatch_line is not None
        assert dispatch_line["request_id"] == task_id
        assert dispatch_line["user_id"] == user_id
    finally:
        daemon_module._logger = prev_daemon_logger
        capture_logger.handlers = prev_handlers
        capture_logger.setLevel(prev_level)
        capture_logger.propagate = prev_propagate
        clear_log_context()


@pytest.mark.asyncio
async def test_request_review_approved_auto_completes(monkeypatch):
    daemon = LucentDaemon()
    task_id = "11111111-1111-1111-1111-111111111111"
    request_id = "22222222-2222-2222-2222-222222222222"
    events: list[tuple[str, str, str | None, dict | None]] = []
    status_updates: list[tuple[str, str]] = []

    async def _get_request(_request_id):
        return {"id": request_id, "status": "review", "tasks": []}

    async def _add_event(tid, event_type, detail=None, metadata=None):
        events.append((tid, event_type, detail, metadata))
        return {"id": tid}

    async def _update_status(rid, status):
        status_updates.append((rid, status))
        return {"id": rid, "status": status}

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError("should not be called in auto-complete review flow")

    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request", _get_request)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _add_event)
    monkeypatch.setattr("daemon.daemon.RequestAPI.create_review", _forbidden)
    monkeypatch.setattr("daemon.daemon.RequestAPI.update_request_status", _update_status)
    monkeypatch.setattr("daemon.daemon.RequestAPI.retry_task", _forbidden)
    monkeypatch.setattr("daemon.daemon.RequestAPI.create_task", _forbidden)

    await daemon._process_request_review_task(
        {"id": task_id, "request_id": request_id},
        "REQUEST_REVIEW_DECISION: APPROVED\nFEEDBACK: Looks good.",
    )

    assert len(events) == 1
    tid, event_type, detail, metadata = events[0]
    assert tid == task_id
    assert event_type == "request_review_approved"
    assert "APPROVED" in (detail or "")
    assert metadata and metadata.get("recommendation") == "APPROVED"
    # Verify request was auto-completed
    assert len(status_updates) == 1
    assert status_updates[0] == (request_id, "completed")


@pytest.mark.asyncio
async def test_request_review_needs_rework_auto_transitions(monkeypatch):
    daemon = LucentDaemon()
    task_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    request_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    target_task_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    events: list[tuple[str, str, str | None, dict | None]] = []
    status_updates: list[tuple[str, str]] = []

    async def _get_request(_request_id):
        return {"id": request_id, "status": "review", "tasks": []}

    async def _add_event(tid, event_type, detail=None, metadata=None):
        events.append((tid, event_type, detail, metadata))
        return {"id": tid}

    async def _update_status(rid, status):
        status_updates.append((rid, status))
        return {"id": rid, "status": status}

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError("should not be called in auto-rework review flow")

    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request", _get_request)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _add_event)
    monkeypatch.setattr("daemon.daemon.RequestAPI.create_review", _forbidden)
    monkeypatch.setattr("daemon.daemon.RequestAPI.update_request_status", _update_status)
    monkeypatch.setattr("daemon.daemon.RequestAPI.retry_task", _forbidden)
    monkeypatch.setattr("daemon.daemon.RequestAPI.create_task", _forbidden)

    await daemon._process_request_review_task(
        {"id": task_id, "request_id": request_id},
        (
            "REQUEST_REVIEW_DECISION: NEEDS_REWORK\n"
            f"TASK_IDS_TO_REWORK: {target_task_id}\n"
            "FEEDBACK: Add tests."
        ),
    )

    assert len(events) == 1
    tid, event_type, detail, metadata = events[0]
    assert tid == task_id
    assert event_type == "request_review_needs_rework"
    assert "NEEDS_REWORK" in (detail or "")
    assert metadata and metadata.get("recommendation") == "NEEDS_REWORK"
    assert metadata.get("task_ids_to_rework") == [target_task_id]
    # Verify request was auto-transitioned to needs_rework
    assert len(status_updates) == 1
    assert status_updates[0] == (request_id, "needs_rework")
