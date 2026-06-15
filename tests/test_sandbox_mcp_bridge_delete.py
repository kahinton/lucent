"""Tests for the sandbox MCP bridge ``delete_memory`` exposure.

The bridge that runs inside sandbox containers must explicitly expose
``delete_memory`` so daemon-dispatched memory-agent tasks can perform
consolidation deletes. Prior to wiring this up, calls failed with
``Tool 'memory-server-delete_memory' does not exist.``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from lucent.sandbox.mcp_bridge import BridgeServer, _MEMORY_TOOL_NAMES


def test_delete_memory_is_in_tool_list():
    bridge = BridgeServer(api_url="http://example", api_key="k", task_id=None)
    names = {tool["name"] for tool in bridge.tool_list()}
    assert "delete_memory" in names


def test_delete_memory_is_tracked_as_memory_tool():
    assert "delete_memory" in _MEMORY_TOOL_NAMES


def test_delete_memory_spec_requires_memory_id_uuid():
    bridge = BridgeServer(api_url="http://example", api_key="k", task_id=None)
    spec = next(t for t in bridge.tool_list() if t["name"] == "delete_memory")
    schema = spec["inputSchema"]
    assert schema["required"] == ["memory_id"]
    assert schema["properties"]["memory_id"]["type"] == "string"
    assert schema["properties"]["memory_id"].get("format") == "uuid"


def test_delete_memory_proxies_to_rest_delete(monkeypatch):
    bridge = BridgeServer(api_url="http://example", api_key="k", task_id=None)
    calls: list[tuple[str, str, dict]] = []

    def fake_proxy(self, method, path, payload):
        calls.append((method, path, payload))
        return {"success": True}

    monkeypatch.setattr(BridgeServer, "_proxy", fake_proxy, raising=True)
    memory_id = str(uuid4())
    result = bridge.handle_tool_call("delete_memory", {"memory_id": memory_id})

    assert result == {"success": True}
    assert calls == [("DELETE", f"/memories/{memory_id}", {})]


def test_delete_memory_rejects_missing_memory_id():
    bridge = BridgeServer(api_url="http://example", api_key="k", task_id=None)
    with pytest.raises(ValueError, match="memory_id is required"):
        bridge.handle_tool_call("delete_memory", {})
