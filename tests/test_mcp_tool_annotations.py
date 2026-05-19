"""Tests for MCP ToolAnnotations on Lucent tools."""

from mcp.server.fastmcp import FastMCP

from lucent.tools.definitions import register_definition_tools
from lucent.tools.memories import register_tools as register_memory_tools
from lucent.tools.requests import register_request_tools


def _tool_annotations(mcp: FastMCP, name: str):
    return mcp._tool_manager._tools[name].annotations  # noqa: SLF001 - test introspection


def test_core_read_only_tools_are_annotated():
    mcp = FastMCP("test")
    register_memory_tools(mcp)
    register_request_tools(mcp)
    register_definition_tools(mcp)

    for name in [
        "search_memories",
        "search_memories_full",
        "get_memory",
        "get_current_user_context",
        "list_active_work",
        "list_planning_targets",
        "list_available_models",
        "list_agent_definitions",
        "list_handoffs",
        "get_handoff",
    ]:
        annotations = _tool_annotations(mcp, name)
        assert annotations is not None, name
        assert annotations.readOnlyHint is True, name


def test_core_mutating_tools_are_annotated():
    mcp = FastMCP("test")
    register_memory_tools(mcp)
    register_request_tools(mcp)

    for name in [
        "create_memory",
        "update_memory",
        "delete_memory",
        "create_request",
        "create_task",
        "send_handoff",
        "resolve_handoff",
    ]:
        annotations = _tool_annotations(mcp, name)
        assert annotations is not None, name
        assert annotations.readOnlyHint is False, name


def test_legacy_interaction_tools_are_not_registered():
    mcp = FastMCP("test")
    register_request_tools(mcp)

    tool_names = set(mcp._tool_manager._tools)
    assert "send_user_interaction" not in tool_names
    assert "list_user_interactions" not in tool_names
    assert "get_user_interaction" not in tool_names
    assert "resolve_user_interaction" not in tool_names
