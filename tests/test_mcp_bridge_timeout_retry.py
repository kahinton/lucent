"""Pattern 1 hardening — MCP bridge timeout & retry behavior."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

import lucent.llm.mcp_bridge as bridge_mod
from lucent.llm.mcp_bridge import MCPToolBridge, MCPTimeoutError


class _FakeTool:
    name = "search_memories"
    description = "Search memories"
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}}


class _FakeToolList:
    tools = [_FakeTool()]


class _FakeTextContent:
    text = '{"ok": true}'


class _FakeCallResult:
    content = [_FakeTextContent()]


class _TimeoutThenSuccessSession:
    """Session that times out N times before succeeding.

    Captures the ``read_timeout_seconds`` kwarg on every attempt so we can
    assert the bridge plumbs an explicit per-call bound.
    """

    last: "_TimeoutThenSuccessSession | None" = None

    def __init__(self, *, fail_count: int):
        self.fail_count = fail_count
        self.attempts = 0
        self.read_timeouts: list = []
        _TimeoutThenSuccessSession.last = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def initialize(self):
        return None

    async def list_tools(self):
        return _FakeToolList()

    async def call_tool(self, name, arguments, *, read_timeout_seconds=None):
        self.attempts += 1
        self.read_timeouts.append(read_timeout_seconds)
        if self.attempts <= self.fail_count:
            raise asyncio.TimeoutError("simulated MCP read timeout")
        return _FakeCallResult()


@asynccontextmanager
async def _fake_streamable_client(*_args, **_kwargs):
    yield object(), object(), lambda: "session-id"


def _patch_session(monkeypatch, session_factory):
    import mcp
    import mcp.client.streamable_http

    monkeypatch.setattr(mcp, "ClientSession", lambda *_a, **_k: session_factory())
    monkeypatch.setattr(
        mcp.client.streamable_http, "streamablehttp_client", _fake_streamable_client
    )


@pytest.mark.asyncio
async def test_search_memories_timeout_then_retry_succeeds(monkeypatch):
    """First attempt times out, second succeeds — the bridge MUST retry
    idempotent memory reads exactly once and return the success payload."""

    monkeypatch.setenv("LUCENT_MCP_REQUEST_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("LUCENT_MCP_RETRY_BACKOFF_SECONDS", "0")

    sessions: list[_TimeoutThenSuccessSession] = []

    def factory():
        s = _TimeoutThenSuccessSession(fail_count=1)
        sessions.append(s)
        return s

    _patch_session(monkeypatch, factory)

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        headers={"Authorization": "Bearer t"},
        skip_url_validation=True,
    )
    try:
        result = await bridge.call_tool("search_memories", {"query": "x"})
    finally:
        await bridge.close()

    assert result == '{"ok": true}'
    session = sessions[0]
    assert session.attempts == 2, "expected exactly one retry after the first timeout"
    # Each attempt should have received an explicit read_timeout bound.
    assert all(rt == timedelta(seconds=10) for rt in session.read_timeouts), session.read_timeouts


@pytest.mark.asyncio
async def test_search_memories_timeout_exhausted_returns_mcp_timeout(monkeypatch):
    """When both attempts time out the bridge must surface the failure as a
    string that the audit classifier maps to ``mcp_timeout`` — not the
    generic ``tool_error`` bucket."""

    monkeypatch.setenv("LUCENT_MCP_REQUEST_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("LUCENT_MCP_RETRY_BACKOFF_SECONDS", "0")

    def factory():
        return _TimeoutThenSuccessSession(fail_count=99)

    _patch_session(monkeypatch, factory)

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        headers={"Authorization": "Bearer t"},
        skip_url_validation=True,
    )
    try:
        result = await bridge.call_tool("search_memories", {"query": "x"})
    finally:
        await bridge.close()

    assert result.startswith("Error calling tool search_memories: ")
    assert "MCPTimeoutError" in result or "timed out after" in result

    from lucent.db.tool_audit import classify_tool_result

    status, failure_class, _ = classify_tool_result(result)
    assert status == "failed"
    assert failure_class == "mcp_timeout"


@pytest.mark.asyncio
async def test_create_memory_does_not_retry_on_timeout(monkeypatch):
    """Non-idempotent tools (create/update/delete) must NOT be retried — a
    silent retry could double-write. The bridge should fail fast after the
    first timeout."""

    monkeypatch.setenv("LUCENT_MCP_REQUEST_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("LUCENT_MCP_RETRY_BACKOFF_SECONDS", "0")

    sessions: list[_TimeoutThenSuccessSession] = []

    def factory():
        s = _TimeoutThenSuccessSession(fail_count=99)
        sessions.append(s)
        return s

    _patch_session(monkeypatch, factory)

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        headers={"Authorization": "Bearer t"},
        skip_url_validation=True,
    )
    try:
        result = await bridge.call_tool(
            "create_memory", {"type": "experience", "content": "x"}
        )
    finally:
        await bridge.close()

    assert "Error calling tool create_memory" in result
    assert sessions[0].attempts == 1, "create_memory must not retry on timeout"


@pytest.mark.asyncio
async def test_read_timeout_kwarg_is_forwarded(monkeypatch):
    """Every successful call still carries the configured read timeout."""

    monkeypatch.setenv("LUCENT_MCP_REQUEST_TIMEOUT_SECONDS", "42")
    monkeypatch.setenv("LUCENT_MCP_RETRY_BACKOFF_SECONDS", "0")

    sessions: list[_TimeoutThenSuccessSession] = []

    def factory():
        s = _TimeoutThenSuccessSession(fail_count=0)
        sessions.append(s)
        return s

    _patch_session(monkeypatch, factory)

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        headers={"Authorization": "Bearer t"},
        skip_url_validation=True,
    )
    try:
        await bridge.call_tool("search_memories", {"query": "x"})
    finally:
        await bridge.close()

    assert sessions[0].read_timeouts == [timedelta(seconds=42)]


def test_mcp_timeout_error_is_a_timeout_error():
    """``MCPTimeoutError`` is a TimeoutError subclass so existing
    ``except TimeoutError`` handlers continue to catch it."""
    assert issubclass(MCPTimeoutError, TimeoutError)


@pytest.mark.asyncio
async def test_legacy_session_without_read_timeout_kwarg(monkeypatch):
    """If the SDK's ClientSession signature does not accept
    ``read_timeout_seconds`` (e.g. older versions or fakes), the bridge must
    still enforce the bound externally via ``asyncio.wait_for``."""

    class _LegacySession(_TimeoutThenSuccessSession):
        async def call_tool(self, name, arguments):  # type: ignore[override]
            # Note: no read_timeout_seconds kwarg accepted.
            self.attempts += 1
            self.read_timeouts.append(None)
            return _FakeCallResult()

    monkeypatch.setenv("LUCENT_MCP_REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("LUCENT_MCP_RETRY_BACKOFF_SECONDS", "0")

    sessions: list[_LegacySession] = []

    def factory():
        s = _LegacySession(fail_count=0)
        sessions.append(s)
        return s

    _patch_session(monkeypatch, factory)

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        headers={"Authorization": "Bearer t"},
        skip_url_validation=True,
    )
    try:
        result = await bridge.call_tool("search_memories", {"query": "x"})
    finally:
        await bridge.close()

    assert result == '{"ok": true}'
    assert sessions[0].attempts == 1
