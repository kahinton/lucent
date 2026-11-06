"""Tests for the LangChain MCP tool bridge."""

from contextlib import asynccontextmanager

import pytest

from lucent.llm.mcp_bridge import MCPToolBridge


class _FakeTool:
    name = "search_memories"
    description = "Search memories"
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
    }


class _FakeDisallowedTool:
    name = "create_task"
    description = "Create task"
    input_schema = {"type": "object", "properties": {}}


class _FakeTextContent:
    text = '{"ok": true}'


class _FakeCallResult:
    content = [_FakeTextContent()]


class _FakeToolList:
    tools = [_FakeTool(), _FakeDisallowedTool()]


class _FakeClientSession:
    last = None
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.initialized = False
        self.calls = []
        _FakeClientSession.last = self
        _FakeClientSession.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def initialize(self):
        self.initialized = True

    async def list_tools(self):
        return _FakeToolList()

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return _FakeCallResult()


@asynccontextmanager
async def _fake_streamable_client(*_args, **_kwargs):
    yield object(), object()


class _FakeHttpClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None


def _fake_http_client(*_args, **_kwargs):
    return _FakeHttpClient()


class _FakeWriter:
    def close(self):
        return None

    async def wait_closed(self):
        return None


async def _fake_open_connection(*_args, **_kwargs):
    return object(), _FakeWriter()


@pytest.mark.asyncio
async def test_bridge_uses_streamable_http_session(monkeypatch):
    import mcp
    import mcp.client.streamable_http

    monkeypatch.setattr(mcp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(
        mcp.client.streamable_http,
        "streamable_http_client",
        _fake_streamable_client,
    )
    monkeypatch.setattr(
        mcp.client.streamable_http,
        "create_mcp_http_client",
        _fake_http_client,
    )
    monkeypatch.setattr("lucent.llm.mcp_bridge.asyncio.open_connection", _fake_open_connection)

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        headers={"Authorization": "Bearer test"},
        allowed_tools=["search_memories"],
        skip_url_validation=True,
    )
    try:
        tools = await bridge.discover_tools()
        result = await bridge.call_tool("search_memories", {"query": "project notes"})
        disallowed = await bridge.call_tool("create_task", {"title": "Nope"})
    finally:
        await bridge.close()

    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "search_memories",
                "description": "Search memories",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            },
        }
    ]
    assert result == '{"ok": true}'
    assert "tool is not allowed" in disallowed
    assert _FakeClientSession.last is not None
    assert _FakeClientSession.last.initialized is True
    assert _FakeClientSession.last.calls == [("search_memories", {"query": "project notes"})]


@pytest.mark.asyncio
async def test_bridge_excludes_internal_tools_from_discovery(monkeypatch):
    import mcp
    import mcp.client.streamable_http

    monkeypatch.setattr(mcp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(
        mcp.client.streamable_http,
        "streamable_http_client",
        _fake_streamable_client,
    )
    monkeypatch.setattr(
        mcp.client.streamable_http,
        "create_mcp_http_client",
        _fake_http_client,
    )
    monkeypatch.setattr("lucent.llm.mcp_bridge.asyncio.open_connection", _fake_open_connection)

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        allowed_tools=["*"],
        excluded_tools=["search_memories"],
        skip_url_validation=True,
    )
    try:
        tools = await bridge.discover_tools()
        result = await bridge.call_tool("search_memories", {"query": "project notes"})
    finally:
        await bridge.close()

    assert [tool["function"]["name"] for tool in tools] == ["create_task"]
    assert "tool is not allowed" in result


@pytest.mark.asyncio
async def test_bridge_reconnects_once_after_session_termination(monkeypatch):
    import mcp
    import mcp.client.streamable_http

    class _TerminatingFirstSession(_FakeClientSession):
        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            if len(_FakeClientSession.instances) == 1:
                raise RuntimeError("Session terminated")
            return _FakeCallResult()

    _FakeClientSession.instances = []
    monkeypatch.setattr(mcp, "ClientSession", _TerminatingFirstSession)
    monkeypatch.setattr(
        mcp.client.streamable_http,
        "streamable_http_client",
        _fake_streamable_client,
    )
    monkeypatch.setattr(
        mcp.client.streamable_http,
        "create_mcp_http_client",
        _fake_http_client,
    )
    monkeypatch.setattr("lucent.llm.mcp_bridge.asyncio.open_connection", _fake_open_connection)

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        allowed_tools=["search_memories"],
        skip_url_validation=True,
    )
    try:
        result = await bridge.call_tool("search_memories", {"query": "project notes"})
    finally:
        await bridge.close()

    assert result == '{"ok": true}'
    assert len(_FakeClientSession.instances) == 2
    assert _FakeClientSession.instances[0].calls == [
        ("search_memories", {"query": "project notes"})
    ]
    assert _FakeClientSession.instances[1].calls == [
        ("search_memories", {"query": "project notes"})
    ]


# --- lost-session recovery -------------------------------------------------
#
# The classifier and the bounded reconnect are the two halves of the
# "Session not found" fix in mcp_bridge.call_tool. These tests pin both
# directions: a stale-session error must reconnect exactly once, and an
# ordinary tool error must not reconnect at all.

_LOST_SESSION_MESSAGES = [
    "Session not found",
    "Session terminated",
    "SSE stream ended without a response",
    "SESSION NOT FOUND",
]


@pytest.mark.parametrize("message", _LOST_SESSION_MESSAGES)
def test_is_terminated_session_error_matches_lost_session_messages(message):
    assert MCPToolBridge._is_terminated_session_error(RuntimeError(message)) is True


def test_is_terminated_session_error_matches_real_mcperror_shape():
    """The production shape is MCPError, whose __str__ is the bare message."""
    from mcp.shared.exceptions import MCPError

    error = MCPError(code=-32600, message="Session not found")

    assert str(error) == "Session not found"
    assert MCPToolBridge._is_terminated_session_error(error) is True


@pytest.mark.parametrize(
    "message",
    [
        "Memory not found or not accessible",
        "Invalid query: 'limit' must be a positive integer",
        "permission denied for memory 3f2a",
    ],
)
def test_is_terminated_session_error_ignores_unrelated_errors(message):
    assert MCPToolBridge._is_terminated_session_error(RuntimeError(message)) is False


def test_is_terminated_session_error_matches_chat_session_near_miss():
    """A "Chat session not found" message matches — accepted, deliberately.

    The predicate is one substring vocabulary ("session ... not found"), not a
    taxonomy of which subsystem owns a session. Anchoring it to the exact
    string would re-open the regression it exists to fix: the JSON-RPC body
    reaching us may carry transport or tool context around the message, so a
    prefix/suffix match is what keeps the real case working.

    A false positive is cheap and bounded: one forced reconnect and one retry,
    then the honest session-lost error. It only costs anything if a chat
    subsystem's message reaches an MCP tool call, which this path cannot
    produce — the bridge session carries MCP tool traffic only.

    Pinned so any future narrowing has to be a conscious change.
    """
    assert (
        MCPToolBridge._is_terminated_session_error(RuntimeError("Chat session not found"))
        is True
    )


class _ScriptedSession:
    """Session stub whose call_tool pops scripted outcomes (exception or result)."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _stub_bridge(monkeypatch, *, sessions):
    """Build a bridge whose whole session lifecycle is scripted by ``sessions``.

    Each entry is either an exception (raised from that ``_ensure_session``
    call) or a session object (returned). ``close`` is stubbed so the real
    exit stack never tears down, and the best-effort audit sink is stubbed so
    the test never touches the database.
    """
    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        allowed_tools=["search_memories_full"],
        skip_url_validation=True,
    )
    ensure_calls = []
    close_calls = []

    async def fake_ensure_session():
        outcome = sessions[len(ensure_calls)]
        ensure_calls.append(1)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def fake_close():
        close_calls.append(1)
        bridge._session = None

    async def noop_audit(**_kwargs):
        return None

    monkeypatch.setattr(bridge, "_ensure_session", fake_ensure_session)
    monkeypatch.setattr(bridge, "close", fake_close)
    monkeypatch.setattr(bridge, "_audit_tool_call", noop_audit)
    return bridge, ensure_calls, close_calls


@pytest.mark.asyncio
async def test_bridge_recovers_once_from_session_not_found(monkeypatch):
    """A "Session not found" call failure reconnects exactly once and retries."""
    from mcp.shared.exceptions import MCPError

    stale = _ScriptedSession([MCPError(code=-32600, message="Session not found")])
    healthy = _ScriptedSession([_FakeCallResult()])
    bridge, ensure_calls, close_calls = _stub_bridge(
        monkeypatch, sessions=[stale, healthy]
    )

    result = await bridge.call_tool("search_memories_full", {"query": "project notes"})

    assert result == '{"ok": true}'
    assert len(ensure_calls) == 2, "exactly one reconnect"
    assert len(close_calls) == 1
    assert stale.calls == [("search_memories_full", {"query": "project notes"})]
    assert healthy.calls == [("search_memories_full", {"query": "project notes"})]


@pytest.mark.asyncio
async def test_bridge_recovers_when_ensure_session_reports_lost_session(monkeypatch):
    """The other reconnect site: a dead cached session fails in _ensure_session."""
    from mcp.shared.exceptions import MCPError

    healthy = _ScriptedSession([_FakeCallResult()])
    bridge, ensure_calls, close_calls = _stub_bridge(
        monkeypatch,
        sessions=[MCPError(code=-32600, message="Session not found"), healthy],
    )

    result = await bridge.call_tool("search_memories_full", {"query": "project notes"})

    assert result == '{"ok": true}'
    assert len(ensure_calls) == 2, "exactly one reconnect"
    assert len(close_calls) == 1
    assert healthy.calls == [("search_memories_full", {"query": "project notes"})]


@pytest.mark.asyncio
async def test_bridge_returns_honest_error_when_reconnect_also_fails(monkeypatch):
    """A failed recovery is returned as a string that names the session loss."""
    from mcp.shared.exceptions import MCPError

    first = _ScriptedSession([MCPError(code=-32600, message="Session not found")])
    second = _ScriptedSession([MCPError(code=-32600, message="Session not found")])
    bridge, ensure_calls, close_calls = _stub_bridge(
        monkeypatch, sessions=[first, second]
    )

    result = await bridge.call_tool("search_memories_full", {"query": "project notes"})

    # call_tool's contract is a str return, not a raise.
    assert isinstance(result, str)
    assert len(ensure_calls) == 2, "the single allowed reconnect happened"
    assert len(close_calls) == 1
    lowered = result.lower()
    assert "session" in lowered
    assert "lost" in lowered
    assert "did not succeed" in lowered
    # The underlying cause is preserved, not swallowed.
    assert "session not found" in lowered
    assert result.startswith("Error calling tool search_memories_full:")
    # No success claim: the healthy payload never appears.
    assert '{"ok": true}' not in result


@pytest.mark.asyncio
async def test_bridge_does_not_reconnect_on_unrelated_tool_error(monkeypatch):
    """The non-match half at call_tool level: ordinary errors must not reconnect."""
    failing = _ScriptedSession([RuntimeError("Memory not found or not accessible")])
    bridge, ensure_calls, close_calls = _stub_bridge(monkeypatch, sessions=[failing])

    result = await bridge.call_tool("search_memories_full", {"query": "project notes"})

    assert result == (
        "Error calling tool search_memories_full: Memory not found or not accessible"
    )
    assert len(ensure_calls) == 1, "no reconnect for an unrelated error"
    assert len(close_calls) == 0


@pytest.mark.asyncio
async def test_unreachable_server_fails_loudly_without_opening_mcp_session(
    monkeypatch, capsys
):
    """An unavailable server must never silently compose an empty tool surface.

    The preflight TCP probe raises; discovery retries exhaust, then
    ``discover_tools`` raises LOUDLY (structured LUCENT_EVENT on stderr) and
    no MCP session is ever opened. Callers (e.g.
    ``langchain_engine`` composition) catch the raise and skip the server.
    """
    async def refuse_connection(*_args, **_kwargs):
        raise ConnectionRefusedError("connection refused")

    bridge = MCPToolBridge("http://localhost:3100", skip_url_validation=True)
    monkeypatch.setattr("lucent.llm.mcp_bridge.asyncio.open_connection", refuse_connection)
    # Keep the multi-attempt retry loop exercised but the test fast.
    monkeypatch.setattr("lucent.llm.mcp_bridge.MCP_DISCOVERY_ATTEMPTS", 2)
    monkeypatch.setattr("lucent.llm.mcp_bridge.MCP_DISCOVERY_RETRY_BACKOFF_SECONDS", 0.0)

    with pytest.raises(RuntimeError, match="unreachable at localhost:3100"):
        await bridge.discover_tools()

    # No MCP session was ever opened — the preflight refused before that.
    assert bridge._session is None

    # The failure was loud: a structured discovery-loss event on stderr.
    stderr = capsys.readouterr().err
    assert "MCP_DISCOVERY_FAILED" in stderr
    assert "localhost:3100" in stderr
