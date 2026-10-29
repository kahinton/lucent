"""The server-side tool gate and the ``ask_user`` tool.

These two are what make approval mode real for *every* engine: each engine reaches
Lucent's tools through the same MCP server, so one middleware covers them all.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lucent.llm import context as llm_context
from lucent.llm.tool_approval import InteractionBroker, interaction_broker
from lucent.llm.tool_approval_gate import ToolApprovalMiddleware


class _Ctx:
    """Minimal stand-in for ServerRequestContext (only the fields read are used)."""

    def __init__(self, method: str, params: dict | None):
        self.method = method
        self.params = params


@pytest.fixture(autouse=True)
def _isolated_broker(monkeypatch):
    """Keep each test's channel/pending state out of the process-wide broker."""
    broker = InteractionBroker()
    monkeypatch.setattr("lucent.llm.tool_approval.interaction_broker", broker)
    monkeypatch.setattr("lucent.llm.tool_approval_gate.interaction_broker", broker)
    monkeypatch.setattr("lucent.tools.interaction.interaction_broker", broker)
    yield broker
    llm_context.clear_llm_context()


@pytest.mark.asyncio
async def test_non_tool_requests_pass_straight_through():
    middleware = ToolApprovalMiddleware()
    called = {"n": 0}

    async def call_next(_ctx):
        called["n"] += 1
        return "handled"

    result = await middleware(_Ctx("tools/list", None), call_next)
    assert result == "handled"
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_auto_mode_never_prompts(_isolated_broker):
    llm_context.set_llm_context(session_id="s", tool_approval_mode="auto")
    middleware = ToolApprovalMiddleware()
    ran = {"n": 0}

    async def call_next(_ctx):
        ran["n"] += 1
        return "ran"

    result = await middleware(
        _Ctx("tools/call", {"name": "create_memory", "arguments": {}}), call_next
    )
    assert result == "ran"
    assert ran["n"] == 1


@pytest.mark.asyncio
async def test_missing_mode_is_treated_as_auto(_isolated_broker):
    """Daemon and background callers carry no mode and must never block."""
    llm_context.clear_llm_context()
    middleware = ToolApprovalMiddleware()
    ran = {"n": 0}

    async def call_next(_ctx):
        ran["n"] += 1
        return "ran"

    result = await middleware(
        _Ctx("tools/call", {"name": "delete_memory", "arguments": {}}), call_next
    )
    assert result == "ran"
    assert ran["n"] == 1


@pytest.mark.asyncio
async def test_read_only_tool_runs_without_prompt_in_ask_mode(_isolated_broker):
    llm_context.set_llm_context(session_id="s", tool_approval_mode="ask")
    middleware = ToolApprovalMiddleware()
    ran = {"n": 0}

    async def call_next(_ctx):
        ran["n"] += 1
        return "ran"

    result = await middleware(
        _Ctx("tools/call", {"name": "search_memories", "arguments": {"query": "x"}}),
        call_next,
    )
    assert result == "ran"
    assert ran["n"] == 1


@pytest.mark.asyncio
async def test_ask_mode_denies_when_no_one_can_answer(_isolated_broker):
    """No live channel + ask mode = refuse, not run unsupervised."""
    llm_context.set_llm_context(session_id="s", tool_approval_mode="ask")
    middleware = ToolApprovalMiddleware()
    ran = {"n": 0}

    async def call_next(_ctx):
        ran["n"] += 1
        return "ran"

    result = await middleware(
        _Ctx("tools/call", {"name": "create_memory", "arguments": {}}), call_next
    )
    assert ran["n"] == 0
    # Flagged as a tool error so the MCP client skips structured-output
    # validation — an unflagged result would surface as a transport error.
    assert getattr(result, "is_error", False) is True
    text = result.content[0].text
    assert "denied" in text.lower()


@pytest.mark.asyncio
async def test_ask_mode_runs_the_tool_after_approval(_isolated_broker, monkeypatch):
    monkeypatch.setattr(
        "lucent.llm.tool_approval_gate._approval_timeout_seconds", lambda: 5
    )
    llm_context.set_llm_context(session_id="s", tool_approval_mode="ask")
    events: list[dict] = []
    _isolated_broker.open_channel("s", events.append)

    async def approve():
        while not events:
            await asyncio.sleep(0)
        _isolated_broker.resolve(
            events[0]["request_id"],
            answer={"decision": "approve"},
            session_id="s",
            user_id=None,
        )

    approver = asyncio.create_task(approve())
    middleware = ToolApprovalMiddleware()
    ran = {"n": 0}

    async def call_next(_ctx):
        ran["n"] += 1
        return "ran"

    result = await middleware(
        _Ctx("tools/call", {"name": "create_memory", "arguments": {"type": "experience"}}),
        call_next,
    )
    await approver

    assert result == "ran"
    assert ran["n"] == 1
    prompt = events[0]
    assert prompt["kind"] == "approval"
    assert prompt["tool"] == "create_memory"


@pytest.mark.asyncio
async def test_ask_user_is_answered_through_the_broker(_isolated_broker):
    from lucent.server import mcp

    llm_context.set_llm_context(session_id="s", tool_approval_mode="ask")
    events: list[dict] = []
    _isolated_broker.open_channel("s", events.append)

    async def answer():
        while not events:
            await asyncio.sleep(0)
        _isolated_broker.resolve(
            events[0]["request_id"],
            answer={"answer": "Tuesday", "choice": None},
            session_id="s",
            user_id=None,
        )

    responder = asyncio.create_task(answer())
    tool = mcp._tool_manager.get_tool("ask_user")
    from mcp.server.mcpserver.context import Context

    result = await tool.run(
        {"question": "Which day?", "choices": ["Monday", "Tuesday"]},
        Context(mcp_server=mcp),
    )
    await responder

    payload = json.loads(result.content[0].text if hasattr(result, "content") else result)
    assert payload["answered"] is True
    assert payload["answers"][0]["answer"] == "Tuesday"
    assert events[0]["kind"] == "question"


@pytest.mark.asyncio
async def test_ask_user_without_a_channel_reports_unanswered():
    from lucent.server import mcp

    llm_context.clear_llm_context()
    tool = mcp._tool_manager.get_tool("ask_user")
    from mcp.server.mcpserver.context import Context

    result = await tool.run({"question": "Which day?"}, Context(mcp_server=mcp))
    payload = json.loads(result.content[0].text if hasattr(result, "content") else result)
    assert payload["answered"] is False
    assert "send_handoff" in payload["reason"]


@pytest.mark.asyncio
async def test_ask_user_requires_a_question():
    from lucent.server import mcp

    tool = mcp._tool_manager.get_tool("ask_user")
    from mcp.server.mcpserver.context import Context

    result = await tool.run({"question": "   "}, Context(mcp_server=mcp))
    payload = json.loads(result.content[0].text if hasattr(result, "content") else result)
    assert "error" in payload


def test_ask_user_is_registered_read_only():
    """Asking a question has no side effects, so it never needs approval."""
    from lucent.server import mcp

    tool = mcp._tool_manager.get_tool("ask_user")
    assert tool is not None
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
