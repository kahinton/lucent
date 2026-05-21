"""Tests for declarative agent hooks and file-memory context injection."""

import json
import sys

import pytest
import pytest_asyncio
from mcp.server.fastmcp import FastMCP

from lucent.auth import set_current_user
from lucent.db.definitions import DefinitionRepository
from lucent.llm.hooks import HookManager, append_hook_context, extract_file_references
from lucent.tools.definitions import register_definition_tools


@pytest_asyncio.fixture
async def repo(db_pool):
    return DefinitionRepository(db_pool)


@pytest_asyncio.fixture
async def auth_user(test_user):
    set_current_user(
        {
            "id": test_user["id"],
            "organization_id": test_user["organization_id"],
            "role": "admin",
            "display_name": "Test User",
            "email": "test@test.com",
        }
    )
    yield test_user
    set_current_user(None)


@pytest_asyncio.fixture
async def mcp():
    server = FastMCP("test-hooks")
    register_definition_tools(server)
    return server


@pytest_asyncio.fixture(autouse=True)
async def cleanup_hooks(db_pool, test_organization):
    yield
    org_id = test_organization["id"]
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_hooks WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE organization_id = $1)",
            org_id,
        )
        await conn.execute("DELETE FROM hook_definitions WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM agent_definitions WHERE organization_id = $1", org_id)


async def _call(mcp, tool_name: str, args: dict | None = None) -> dict | list:
    result = await mcp._tool_manager.call_tool(tool_name, args or {})
    return json.loads(result)


class FakeMemoryBridge:
    def __init__(self):
        self.calls = []

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return json.dumps(
            {
                "memories": [
                    {
                        "id": "11111111-2222-3333-4444-555555555555",
                        "content": "src/lucent/widgets.py uses the widget cache guard.",
                        "tags": ["lucent", "widgets"],
                    }
                ],
                "total_count": 1,
            }
        )


@pytest.mark.asyncio
async def test_hook_repository_grants_active_hooks_to_agents(repo, auth_user):
    agent = await repo.create_agent(
        name="hooked-agent",
        description="",
        content="# Agent",
        org_id=str(auth_user["organization_id"]),
        created_by=str(auth_user["id"]),
        status="active",
        owner_user_id=str(auth_user["id"]),
    )
    hook = await repo.create_hook(
        name="remember-widgets",
        description="Inject widget context",
        trigger_event="tool_call",
        action_type="static_context",
        content="Remember the widget cache guard.",
        config={"tool_names": ["read_file"], "require_file_reference": True},
        org_id=str(auth_user["organization_id"]),
        created_by=str(auth_user["id"]),
        status="proposed",
        owner_user_id=str(auth_user["id"]),
    )
    await repo.approve_hook(
        str(hook["id"]), str(auth_user["organization_id"]), str(auth_user["id"])
    )
    assert await repo.grant_hook(
        str(agent["id"]),
        str(hook["id"]),
        org_id=str(auth_user["organization_id"]),
        user_id=str(auth_user["id"]),
    )

    hooks = await repo.get_agent_hooks(str(agent["id"]))
    assert [h["name"] for h in hooks] == ["remember-widgets"]
    assert hooks[0]["config"]["tool_names"] == ["read_file"]

    detail = await repo.get_agent(str(agent["id"]), str(auth_user["organization_id"]))
    assert "remember-widgets" in detail["hook_names"]


@pytest.mark.asyncio
async def test_sync_built_in_hooks_grants_default_hook_to_existing_agents(repo, auth_user):
    agent = await repo.create_agent(
        name="existing-agent-default-hook",
        description="",
        content="# Agent",
        org_id=str(auth_user["organization_id"]),
        created_by=str(auth_user["id"]),
        status="active",
        owner_user_id=str(auth_user["id"]),
    )

    assert await repo.get_agent_hooks(str(agent["id"])) == []

    synced = await repo.sync_built_in_hooks(str(auth_user["organization_id"]))
    assert synced == 1

    hooks = await repo.get_agent_hooks(str(agent["id"]))
    assert [h["name"] for h in hooks] == ["file-memory-lookup"]


@pytest.mark.asyncio
async def test_create_agent_gets_default_hook_when_builtin_exists(repo, auth_user):
    await repo.sync_built_in_hooks(str(auth_user["organization_id"]))

    agent = await repo.create_agent(
        name="future-agent-default-hook",
        description="",
        content="# Agent",
        org_id=str(auth_user["organization_id"]),
        created_by=str(auth_user["id"]),
        status="active",
        owner_user_id=str(auth_user["id"]),
    )

    hooks = await repo.get_agent_hooks(str(agent["id"]))
    assert [h["name"] for h in hooks] == ["file-memory-lookup"]


@pytest.mark.asyncio
async def test_active_agent_with_grants_includes_default_hooks(repo, auth_user):
    await repo.sync_built_in_hooks(str(auth_user["organization_id"]))
    await repo.create_agent(
        name="active-agent-with-hooks",
        description="",
        content="# Agent",
        org_id=str(auth_user["organization_id"]),
        created_by=str(auth_user["id"]),
        status="active",
        owner_user_id=str(auth_user["id"]),
    )

    agent = await repo.get_active_agent_with_grants(
        "active-agent-with-hooks", str(auth_user["organization_id"])
    )

    assert agent is not None
    assert [h["name"] for h in agent["hooks"]] == ["file-memory-lookup"]


@pytest.mark.asyncio
async def test_hook_mcp_tools_create_and_grant_after_human_approval(mcp, auth_user, repo):
    agent = await repo.create_agent(
        name="mcp-hook-agent",
        description="",
        content="# Agent",
        org_id=str(auth_user["organization_id"]),
        created_by=str(auth_user["id"]),
        status="active",
        owner_user_id=str(auth_user["id"]),
    )

    hook = await _call(
        mcp,
        "create_hook_definition",
        {
            "name": "mcp-static-hook",
            "description": "MCP-created static hook",
            "action_type": "static_context",
            "content": "Use MCP hook context.",
            "config": {"tool_names": ["read_file"]},
        },
    )
    assert hook["status"] == "proposed"

    approved = await repo.approve_hook(
        str(hook["id"]),
        str(auth_user["organization_id"]),
        str(auth_user["id"]),
    )
    assert approved["status"] == "active"

    grant = await _call(
        mcp,
        "grant_hook_to_agent",
        {"agent_id": str(agent["id"]), "hook_id": hook["id"]},
    )
    assert grant["status"] == "granted"

    listed = await _call(mcp, "list_hook_definitions", {"status": "active"})
    assert "mcp-static-hook" in [h["name"] for h in listed["items"]]


@pytest.mark.asyncio
async def test_command_hook_can_be_created_through_mcp(mcp, auth_user):
    hook = await _call(
        mcp,
        "create_hook_definition",
        {
            "name": "mcp-command-hook",
            "description": "Runs a bounded command",
            "action_type": "command",
            "config": {
                "tool_names": ["read_file"],
                "command": [sys.executable, "-c", "print('command hook ok')"],
                "timeout_seconds": 5,
            },
        },
    )

    assert hook["status"] == "proposed"
    assert hook["action_type"] == "command"


@pytest.mark.asyncio
async def test_file_memory_hook_extracts_file_refs_and_injects_context():
    refs = extract_file_references(
        "read_file",
        {"filePath": "/workspace/src/lucent/widgets.py", "startLine": 1},
    )
    assert refs == ["/workspace/src/lucent/widgets.py"]

    bridge = FakeMemoryBridge()
    manager = HookManager([])
    executions = await manager.before_tool_call(
        tool_name="read_file",
        arguments={"filePath": "/workspace/src/lucent/widgets.py"},
        memory_bridge=bridge,
    )

    assert bridge.calls
    assert bridge.calls[0][0] == "search_memories_full"
    assert executions
    assert "widgets.py" in executions[0].text
    assert "widget cache guard" in executions[0].text

    enriched = append_hook_context("file contents", executions)
    assert "file contents" in enriched
    assert "Lucent hook context" in enriched
    assert "widget cache guard" in enriched


@pytest.mark.asyncio
async def test_static_context_hook_can_be_user_defined():
    manager = HookManager(
        [
            {
                "name": "style-reminder",
                "status": "active",
                "trigger_event": "tool_call",
                "action_type": "static_context",
                "content": "Preserve existing style.",
                "config": {"tool_names": ["edit_file"], "require_file_reference": True},
            }
        ]
    )

    executions = await manager.before_tool_call(
        tool_name="edit_file",
        arguments={"path": "src/lucent/widgets.py"},
        memory_bridge=None,
    )

    assert len(executions) == 1
    assert executions[0].hook_name == "style-reminder"
    assert executions[0].text == "Preserve existing style."


@pytest.mark.asyncio
async def test_command_hook_receives_event_json_and_injects_output():
    command = [
        sys.executable,
        "-c",
        "import json, sys; payload=json.load(sys.stdin); "
        "print('tool=' + payload['tool_name']); "
        "print('path=' + payload['arguments']['filePath'])",
    ]
    manager = HookManager(
        [
            {
                "name": "script-hook",
                "status": "active",
                "trigger_event": "tool_call",
                "action_type": "command",
                "content": "",
                "config": {
                    "tool_names": ["read_file"],
                    "command": command,
                    "timeout_seconds": 5,
                    "max_output_chars": 2000,
                },
            }
        ]
    )

    executions = await manager.before_tool_call(
        tool_name="read_file",
        arguments={"filePath": "src/lucent/widgets.py"},
        memory_bridge=None,
    )

    assert len(executions) == 1
    assert executions[0].hook_name == "script-hook"
    assert "tool=read_file" in executions[0].text
    assert "path=src/lucent/widgets.py" in executions[0].text
    assert executions[0].metadata["return_code"] == 0
    assert executions[0].metadata["file_refs"] == ["src/lucent/widgets.py"]


@pytest.mark.asyncio
async def test_command_hook_can_block_tool_calls():
    command = [
        sys.executable,
        "-c",
        "import json; print(json.dumps({'action': 'block', 'message': 'blocked by policy'}))",
    ]
    manager = HookManager(
        [
            {
                "name": "policy-hook",
                "status": "active",
                "trigger_event": "before_tool_call",
                "action_type": "command",
                "config": {"tool_names": ["write_file"], "command": command},
            }
        ]
    )

    outcome = await manager.before_tool_call(
        tool_name="write_file",
        arguments={"filePath": "src/lucent/widgets.py"},
        memory_bridge=None,
    )

    assert outcome.blocked
    assert outcome.block_message == "blocked by policy"
    assert outcome[0].decision == "block"


@pytest.mark.asyncio
async def test_command_hook_can_replace_tool_args():
    command = [
        sys.executable,
        "-c",
        "import json; print(json.dumps({'action': 'replace_args', "
        "'arguments': {'filePath': 'src/lucent/safe.py'}}))",
    ]
    manager = HookManager(
        [
            {
                "name": "rewrite-hook",
                "status": "active",
                "trigger_event": "before_tool_call",
                "action_type": "command",
                "config": {"tool_names": ["read_file"], "command": command},
            }
        ]
    )

    outcome = await manager.before_tool_call(
        tool_name="read_file",
        arguments={"filePath": "src/lucent/widgets.py"},
        memory_bridge=None,
    )

    assert outcome.modified_arguments == {"filePath": "src/lucent/safe.py"}
    assert outcome[0].decision == "replace_args"


@pytest.mark.asyncio
async def test_after_tool_command_hook_can_replace_result():
    command = [
        sys.executable,
        "-c",
        "import json, sys; payload=json.load(sys.stdin); "
        "print(json.dumps({'action': 'replace_result', "
        "'result': 'sanitized: ' + payload['tool_result']}))",
    ]
    manager = HookManager(
        [
            {
                "name": "sanitize-hook",
                "status": "active",
                "trigger_event": "after_tool_call",
                "action_type": "command",
                "config": {"tool_names": ["read_file"], "command": command},
            }
        ]
    )

    outcome = await manager.after_tool_call(
        tool_name="read_file",
        arguments={"filePath": "src/lucent/widgets.py"},
        tool_result="raw contents",
        memory_bridge=None,
    )

    assert outcome.modified_result == "sanitized: raw contents"
    assert outcome[0].decision == "replace_result"


@pytest.mark.asyncio
async def test_model_lifecycle_static_context_hooks():
    manager = HookManager(
        [
            {
                "name": "model-context-hook",
                "status": "active",
                "trigger_event": "before_model_call",
                "action_type": "static_context",
                "content": "Remember the release checklist.",
                "config": {},
            }
        ]
    )

    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "ship it"}],
    )

    assert len(outcome) == 1
    assert outcome[0].hook_name == "model-context-hook"
    assert outcome[0].text == "Remember the release checklist."
