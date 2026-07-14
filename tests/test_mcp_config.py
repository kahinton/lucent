from lucent.mcp_config import (
    build_internal_mcp_server,
    build_scoped_internal_mcp_server,
)
from lucent.tool_policy import (
    chat_allowed_tools_for_agent,
    memory_server_tools_for_task,
)


def test_internal_mcp_server_has_single_auth_header_and_unique_tools():
    config = build_internal_mcp_server(
        url="http://localhost:8766/mcp",
        bearer_token="session-token",
        tools=["search_memories", "search_memories", "get_memory"],
        extra_headers={"X-Lucent-LLM-Session-Id": "session-1", "Ignore": ""},
    )

    assert config == {
        "type": "http",
        "url": "http://localhost:8766/mcp",
        "headers": {
            "Authorization": "Bearer session-token",
            "X-Lucent-LLM-Session-Id": "session-1",
        },
        "tools": ["search_memories", "get_memory"],
        "internal": True,
    }


def test_scoped_mcp_server_preserves_scope_and_trace_headers():
    config = build_scoped_internal_mcp_server(
        url="http://lucent:8766/mcp",
        bearer_token="scoped-key",
        memory_scope="user",
        organization_id="org-1",
        memory_scope_user_id="user-1",
        tools=["get_memory"],
        extra_headers={"X-Lucent-Task-Id": "task-1"},
    )

    assert config["headers"] == {
        "Authorization": "Bearer scoped-key",
        "X-Lucent-Memory-Scope": "user",
        "X-Lucent-Memory-Scope-User-Id": "user-1",
        "X-Lucent-Org-Id": "org-1",
        "X-Lucent-Task-Id": "task-1",
    }
    assert config["internal"] is True


def test_chat_and_task_policies_keep_chat_restricted_and_tasks_unrestricted():
    chat_tools = chat_allowed_tools_for_agent("definition-engineer", ["definition-engineering"])
    task_tools = memory_server_tools_for_task("research", "Consolidate duplicates")

    assert "create_agent_definition" in chat_tools
    assert task_tools == ["*"]
    assert "exec_sandbox_command" not in chat_tools
