"""MCP tool discovery policies.

Lucent tools remain discoverable across chat models and daemon tasks. The
requester's bearer credential, memory scope, and repository ACLs enforce what
each caller may actually do.
"""

from __future__ import annotations

CHAT_ALLOWED_TOOLS = ("*",)

BASE_TASK_MEMORY_SERVER_TOOLS = frozenset({
    "create_memory", "get_current_user_context", "get_existing_tags", "get_memory",
    "get_memories", "get_skill_definition", "get_tag_suggestions", "search_memories",
    "search_memories_full", "update_memory", "delete_memory", "create_review",
    "get_memory_versions", "get_request_details", "link_request_memory", "link_task_memory",
    "list_handoffs", "get_handoff", "resolve_handoff", "list_available_models",
    "log_task_event", "exec_sandbox_command", "send_handoff",
    "analyze_tool_failure_patterns", "propose_definition_improvement",
})
DEFINITION_ACTIVATION_TOOLS = frozenset({
    "list_agent_definitions", "get_agent_definition", "list_skill_definitions",
    "get_skill_definition", "list_proposals", "create_agent_definition",
    "create_skill_definition", "create_tool_definition", "list_tool_definitions",
    "get_tool_definition", "create_hook_definition", "list_hook_definitions",
    "get_hook_definition", "list_mcp_server_definitions", "create_mcp_server_definition",
})
WORK_ACTIVATION_TOOLS = frozenset({
    "create_request", "create_task", "list_sandbox_templates", "propose_sandbox_template",
})
CAPABILITY_ACTIVATION_AGENT_TYPES = frozenset(
    {"assessment", "definition-engineer", "lucent", "planning", "reflection"}
)


def chat_allowed_tools_for_agent(
    agent_name: str | None = None,
    skill_names: list[str] | None = None,
) -> list[str]:
    """Expose all Lucent MCP tools; the user's credential enforces authorization."""
    return list(CHAT_ALLOWED_TOOLS)


def memory_server_tools_for_task(
    agent_type: str | None,
    title: str | None = None,
    request_title: str | None = None,
    description: str | None = None,
) -> list[str]:
    """Expose every internal MCP tool to a dispatched task.

    Task descriptions and agent types cannot reliably predict the operations
    needed to complete autonomous work. Restricting discovery here repeatedly
    produced false ``tool does not exist`` failures as new tools were added or
    a task crossed an artificial capability boundary. The task's scoped API key
    remains the authorization boundary: it can only access the requesting
    user's permitted data and organization. Browser chat follows the same
    discovery rule with its authenticated user's credential and ACLs.
    """
    return ["*"]
