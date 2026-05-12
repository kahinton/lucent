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
    ]:
        annotations = _tool_annotations(mcp, name)
        assert annotations is not None, name
        assert annotations.readOnlyHint is True, name


def test_core_mutating_tools_are_annotated():
    mcp = FastMCP("test")
    register_memory_tools(mcp)
    register_request_tools(mcp)

    for name in ["create_memory", "update_memory", "delete_memory", "create_request", "create_task"]:
        annotations = _tool_annotations(mcp, name)
        assert annotations is not None, name
        assert annotations.readOnlyHint is False, name
