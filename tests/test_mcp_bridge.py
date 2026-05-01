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

    def __init__(self, *_args, **_kwargs):
        self.initialized = False
        self.calls = []
        _FakeClientSession.last = self

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
    yield object(), object(), lambda: "session-id"


@pytest.mark.asyncio
async def test_bridge_uses_streamable_http_session(monkeypatch):
    import mcp
    import mcp.client.streamable_http

    monkeypatch.setattr(mcp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(
        mcp.client.streamable_http,
        "streamablehttp_client",
        _fake_streamable_client,
    )

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
