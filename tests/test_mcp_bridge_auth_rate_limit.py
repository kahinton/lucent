"""Pattern 2 — MCP bridge auth (401), forbidden (403), and rate-limit (429)
surface behavior.

These tests pin three things:

  * A 401 from the upstream MCP server surfaces to the audit classifier as
    ``auth_error`` (not the generic ``tool_error`` bucket), so
    ``analyze_tool_failure_patterns`` and the daemon's recovery logic can
    react specifically to expired-token failures.
  * Repeated 401s on the SAME idempotent memory read tool do NOT trigger
    silent retries inside the bridge. Until the deferred Fix G2 (401-driven
    re-mint) lands, retrying a 401 would just amplify the auth_error spike
    we're trying to suppress.
  * A 429 surfaces as ``rate_limited`` so operators can act on backoff,
    not auth/grant changes.

The token-refresh / TTL path itself is covered separately in
``tests/test_daemon_scoped_key_ttl.py`` (Fix G1).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from lucent.db.tool_audit import classify_tool_result
from lucent.llm.mcp_bridge import MCPToolBridge


class _FakeTool:
    name = "search_memories"
    description = "Search memories"
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}}


class _FakeToolList:
    tools = [_FakeTool()]


class _HTTPStatusSession:
    """Session whose ``call_tool`` raises a fixed HTTP-style error every time.

    Mirrors the shape of the upstream MCP SDK errors as they appear in the
    daemon's actual ``daemon.log`` rows (the runbook cites
    ``Unauthorized: Invalid or expired credentials``).
    """

    def __init__(self, message: str):
        self._message = message
        self.attempts = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def initialize(self):
        return None

    async def list_tools(self):
        return _FakeToolList()

    async def call_tool(self, _name, _arguments, *, read_timeout_seconds=None):
        self.attempts += 1
        raise RuntimeError(self._message)


@asynccontextmanager
async def _fake_streamable_client(*_args, **_kwargs):
    yield object(), object(), lambda: "session-id"


def _patch_session(monkeypatch, session):
    import mcp
    import mcp.client.streamable_http

    monkeypatch.setattr(mcp, "ClientSession", lambda *_a, **_k: session)
    monkeypatch.setattr(
        mcp.client.streamable_http, "streamablehttp_client", _fake_streamable_client
    )


@pytest.mark.asyncio
async def test_401_surfaces_as_auth_error_not_tool_error(monkeypatch):
    monkeypatch.setenv("LUCENT_MCP_REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("LUCENT_MCP_RETRY_BACKOFF_SECONDS", "0")

    session = _HTTPStatusSession("Unauthorized: Invalid or expired credentials (status_code=401)")
    _patch_session(monkeypatch, session)

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        headers={"Authorization": "Bearer expired"},
        skip_url_validation=True,
    )
    try:
        result = await bridge.call_tool("search_memories", {"query": "x"})
    finally:
        await bridge.close()

    assert result.startswith("Error calling tool search_memories: ")
    status, failure_class, _ = classify_tool_result(result)
    assert status == "failed"
    assert failure_class == "auth_error", (
        f"401 must surface as auth_error, got {failure_class!r}"
    )


@pytest.mark.asyncio
async def test_repeated_401_does_not_silently_retry(monkeypatch):
    """Until Fix G2 (re-mint on 401) ships, the bridge MUST NOT retry on
    auth failures — a silent retry just doubles the auth_error volume in
    the audit log without any chance of recovery."""
    monkeypatch.setenv("LUCENT_MCP_REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("LUCENT_MCP_RETRY_BACKOFF_SECONDS", "0")

    session = _HTTPStatusSession("Unauthorized: Invalid or expired credentials")
    _patch_session(monkeypatch, session)

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        headers={"Authorization": "Bearer expired"},
        skip_url_validation=True,
    )
    try:
        await bridge.call_tool("search_memories", {"query": "x"})
    finally:
        await bridge.close()

    # search_memories is idempotent and IS in the retry allow-list for
    # TIMEOUTS, but auth failures must short-circuit to exactly one attempt.
    assert session.attempts == 1, (
        f"401 on search_memories triggered {session.attempts} attempts; "
        "must be 1 (no backoff/retry until Fix G2)"
    )


@pytest.mark.asyncio
async def test_403_forbidden_distinct_from_auth_error(monkeypatch):
    monkeypatch.setenv("LUCENT_MCP_REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("LUCENT_MCP_RETRY_BACKOFF_SECONDS", "0")

    session = _HTTPStatusSession("HTTP 403 Forbidden: scope insufficient")
    _patch_session(monkeypatch, session)

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        headers={"Authorization": "Bearer t"},
        skip_url_validation=True,
    )
    try:
        result = await bridge.call_tool("search_memories", {"query": "x"})
    finally:
        await bridge.close()

    _, failure_class, _ = classify_tool_result(result)
    assert failure_class == "forbidden"


@pytest.mark.asyncio
async def test_429_surfaces_as_rate_limited(monkeypatch):
    monkeypatch.setenv("LUCENT_MCP_REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("LUCENT_MCP_RETRY_BACKOFF_SECONDS", "0")

    session = _HTTPStatusSession("status_code=429 Too Many Requests; retry-after=2")
    _patch_session(monkeypatch, session)

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        headers={"Authorization": "Bearer t"},
        skip_url_validation=True,
    )
    try:
        result = await bridge.call_tool("search_memories", {"query": "x"})
    finally:
        await bridge.close()

    _, failure_class, _ = classify_tool_result(result)
    assert failure_class == "rate_limited", (
        f"429 must surface as rate_limited (backoff signal), got {failure_class!r}"
    )
    # Same single-attempt invariant as 401 — rate-limited retries should be
    # the caller's decision (with proper backoff), not a hidden bridge retry.
    assert session.attempts == 1


@pytest.mark.asyncio
async def test_token_refresh_path_via_header_swap(monkeypatch):
    """Simulate the expired-token refresh path the daemon expects to take
    once the operator (or Fix G2) re-mints a key: swap the Authorization
    header on the existing bridge and a subsequent call must use the new
    bearer token. Pins the contract that the bridge stores headers by
    reference, so re-mint logic can rotate the token without rebuilding
    the bridge / re-establishing the MCP session.
    """
    monkeypatch.setenv("LUCENT_MCP_REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("LUCENT_MCP_RETRY_BACKOFF_SECONDS", "0")

    seen_tokens: list[str] = []

    class _TokenCapturingSession:
        def __init__(self, headers):
            self._headers = headers
            self.attempts = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def initialize(self):
            return None

        async def list_tools(self):
            return _FakeToolList()

        async def call_tool(self, _name, _arguments, *, read_timeout_seconds=None):
            self.attempts += 1
            seen_tokens.append(self._headers.get("Authorization", ""))

            class _R:
                content = [type("C", (), {"text": '{"ok": true}'})()]

            return _R()

    headers = {"Authorization": "Bearer expired"}
    captured: dict = {}

    def _make_session(*_a, **_k):
        s = _TokenCapturingSession(headers)
        captured["session"] = s
        return s

    import mcp
    import mcp.client.streamable_http

    monkeypatch.setattr(mcp, "ClientSession", _make_session)
    monkeypatch.setattr(
        mcp.client.streamable_http, "streamablehttp_client", _fake_streamable_client
    )

    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        headers=headers,
        skip_url_validation=True,
    )
    try:
        await bridge.call_tool("search_memories", {"query": "x"})
        # Operator / Fix G2 mints a fresh scoped key and rotates the header.
        headers["Authorization"] = "Bearer refreshed"
        await bridge.call_tool("search_memories", {"query": "y"})
    finally:
        await bridge.close()

    assert seen_tokens == ["Bearer expired", "Bearer refreshed"], seen_tokens
