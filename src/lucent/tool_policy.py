"""Surface-specific MCP tool policies.

Policies are centralized, but not shared indiscriminately: browser chat and
daemon tasks intentionally have different authority. Callers select a policy
by surface rather than maintaining their own literal allow-lists.
"""

from __future__ import annotations


CHAT_ALLOWED_TOOLS = (
    "get_current_user_context", "search_memories", "search_memories_full",
    "get_memory", "get_memories", "create_memory", "update_memory",
    "delete_memory", "get_existing_tags", "get_tag_suggestions",
    "create_request", "list_active_work", "list_pending_requests",
    "list_pending_tasks", "get_request_details", "list_available_models",
)
DEFINITION_COMPOSER_TOOLS = (
    "list_agent_definitions", "get_agent_definition", "list_skill_definitions",
    "get_skill_definition", "list_mcp_server_definitions", "list_hook_definitions",
    "list_tool_definitions", "get_tool_definition", "list_proposals",
    "create_agent_definition", "create_skill_definition", "create_tool_definition",
)
WORKFLOW_COMPOSER_TOOLS = (
    "list_workflows", "get_workflow_details", "create_workflow",
    "list_agent_definitions", "list_skill_definitions", "list_available_models",
)

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
    """Return the least-privilege chat tool policy for an approved agent."""
    tools = list(CHAT_ALLOWED_TOOLS)
    normalized_agent = (agent_name or "").strip().lower()
    normalized_skills = {name.strip().lower() for name in (skill_names or [])}
    if skill_names:
        tools.extend(("get_skill_definition", "list_skill_definitions"))
    if normalized_agent == "definition-engineer" or "definition-engineering" in normalized_skills:
        tools.extend(DEFINITION_COMPOSER_TOOLS)
    if normalized_agent == "workflow-composer" or "workflow-design" in normalized_skills:
        tools.extend(WORKFLOW_COMPOSER_TOOLS)
    return list(dict.fromkeys(tools))


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
    user's permitted data and organization. Browser chat deliberately keeps its
    separate least-privilege allow-list above.
    """
    return ["*"]
