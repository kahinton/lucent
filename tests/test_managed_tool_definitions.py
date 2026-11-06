"""Tests for managed tool definitions and MCP tool-builder surfaces."""

import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from mcp.server import MCPServer as FastMCP

from lucent.auth import set_current_user
from lucent.db.definitions import DEFINITIONS_AUTH_POLICIES, DefinitionRepository
from lucent.db.pool import AuthorizedDatabasePool, AuthPrincipal
from lucent.llm.context import clear_llm_context, set_llm_context
from lucent.secrets.utils import validate_env_var_references
from lucent.services.managed_tools import (
    ManagedToolArgumentError,
    ManagedToolBlockedError,
    ManagedToolDefinitionError,
    ManagedToolError,
    ManagedToolExecutionResult,
    ManagedToolExecutor,
)
from lucent.services.tool_runtimes.base import RuntimeExecutionResult
from lucent.settings import clear_runtime_setting_cache, set_runtime_setting_cache
from lucent.tools.definitions import register_definition_tools


@pytest_asyncio.fixture
async def repo(db_pool):
    return DefinitionRepository(db_pool)


@pytest_asyncio.fixture
async def mcp(db_pool):
    server = FastMCP("test-managed-tools")
    register_definition_tools(server)
    return server


@pytest_asyncio.fixture
async def auth_user(test_user):
    set_current_user(
        {
            "id": test_user["id"],
            "organization_id": test_user["organization_id"],
            "role": "admin",
            "display_name": "Managed Tool Tester",
            "email": "managed-tool@test.com",
        }
    )
    yield test_user
    set_current_user(None)
    clear_llm_context()


@pytest_asyncio.fixture(autouse=True)
async def cleanup_managed_tools(db_pool, test_organization):
    yield
    org_id = test_organization["id"]
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_managed_tools WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE organization_id = $1)",
            org_id,
        )
        await conn.execute(
            "DELETE FROM managed_tool_runs WHERE organization_id = $1",
            org_id,
        )
        await conn.execute("DELETE FROM agent_definitions WHERE organization_id = $1", org_id)
        await conn.execute(
            "DELETE FROM managed_tool_definitions WHERE organization_id = $1",
            org_id,
        )


async def _call(mcp, tool_name: str, args: dict | None = None) -> dict | list:
    result = await mcp._tool_manager.call_tool(tool_name, args or {}, None)
    return json.loads(result)


TOOL_CODE = """
def handler(args):
    return {"echo": args.get("value")}
""".strip()


def test_managed_tool_runner_redacts_unhandled_exception_details():
    from lucent.services.tool_runtimes.python_runtime import PythonRuntime

    runner = PythonRuntime._runner_script("handler")

    assert "'error': 'Managed tool execution failed'" in runner
    assert "'error_type': type(exc).__name__" in runner
    assert runner.index("except Exception as exc:") < runner.index("'error_type': type(exc).__name__")
    assert "traceback" not in runner
    assert "str(exc)" not in runner


def test_sensitive_environment_values_require_just_in_time_references():
    validate_env_var_references({"NODE_ENV": "production", "API_TOKEN": "secret://api.token"})
    validate_env_var_references({"GITHUB_TOKEN": "credential://github/access_token"})

    with pytest.raises(ValueError, match="API_TOKEN.*secret:// or credential://"):
        validate_env_var_references({"API_TOKEN": "plaintext-token"})


@pytest.mark.asyncio
async def test_repository_rejects_plaintext_managed_tool_secret(repo, auth_user):
    with pytest.raises(ValueError, match="GITHUB_TOKEN.*secret:// or credential://"):
        await repo.create_managed_tool(
            name="plaintext-managed-tool-secret",
            description="Must reject plaintext credentials",
            source_code=TOOL_CODE,
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            env_vars={"GITHUB_TOKEN": "plaintext-token"},
        )


@pytest.mark.asyncio
async def test_repository_update_rejects_plaintext_managed_tool_secret(repo, auth_user):
    tool = await repo.create_managed_tool(
        name="reference-managed-tool-secret",
        description="Allows a secret reference",
        source_code=TOOL_CODE,
        org_id=str(auth_user["organization_id"]),
        created_by=str(auth_user["id"]),
        env_vars={"GITHUB_TOKEN": "credential://github/access_token"},
    )
    with pytest.raises(ValueError, match="GITHUB_TOKEN.*secret:// or credential://"):
        await repo.update_managed_tool(
            str(tool["id"]),
            str(auth_user["organization_id"]),
            env_vars={"GITHUB_TOKEN": "plaintext-token"},
        )


@pytest.mark.asyncio
async def test_managed_tool_execution_is_blocked_when_org_sandboxes_are_disabled():
    repo = AsyncMock()
    executor = ManagedToolExecutor(repo)
    org_id = "disabled-org"
    set_runtime_setting_cache(org_id, "features.sandboxes_enabled", False)
    try:
        with pytest.raises(ManagedToolBlockedError, match="Custom tooling is disabled"):
            await executor.execute(
                tool={"id": "tool-1", "status": "active"},
                arguments={},
                org_id=org_id,
                user_id="user-1",
                enforce_agent_grant=False,
            )
    finally:
        clear_runtime_setting_cache(org_id)

    repo.create_managed_tool_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_proxy_discovery_normalizes_native_tool_descriptors(monkeypatch):
    executor = ManagedToolExecutor(AsyncMock())
    captured = {}

    async def fake_execute(**kwargs):
        captured.update(kwargs)
        return ManagedToolExecutionResult(
            ok=True,
            result={
                "tools": [
                    {
                        "name": "search_repositories",
                        "description": "Search repositories.",
                        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
                    }
                ]
            },
        )

    monkeypatch.setattr(executor, "_execute", fake_execute)
    tools = await executor.discover_mcp_proxy_tools(
        tool={"runtime_config": {"mcp_proxy": True}},
        org_id="org-1",
        user_id="user-1",
    )

    assert tools == [
        {
            "name": "search_repositories",
            "description": "Search repositories.",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
    ]
    assert captured["arguments"] == {"_lucent_mcp_operation": "list_tools"}
    assert captured["validate_input"] is False
    assert captured["validate_output"] is False


@pytest.mark.asyncio
async def test_mcp_proxy_discovery_rejects_non_tool_output(monkeypatch):
    executor = ManagedToolExecutor(AsyncMock())

    async def fake_execute(**_kwargs):
        return ManagedToolExecutionResult(ok=True, result={"unexpected": []})

    monkeypatch.setattr(executor, "_execute", fake_execute)
    with pytest.raises(ManagedToolError, match="tools list"):
        await executor.discover_mcp_proxy_tools(
            tool={"runtime_config": {"mcp_proxy": True}},
            org_id="org-1",
            user_id="user-1",
        )


@pytest.mark.asyncio
async def test_repository_caches_and_invalidates_managed_mcp_proxy_tools(repo, auth_user):
    org_id = str(auth_user["organization_id"])
    user_id = str(auth_user["id"])
    tool = await repo.create_managed_tool(
        name="cached-mcp-proxy",
        description="Caches native MCP tool descriptors",
        source_code=TOOL_CODE,
        input_schema={"type": "object", "properties": {}},
        org_id=org_id,
        created_by=user_id,
        owner_user_id=user_id,
        runtime_config={"mcp_proxy": True},
    )
    descriptors = [{
        "name": "search_repositories",
        "description": "Search repositories.",
        "input_schema": {"type": "object", "properties": {}},
    }]

    saved = await repo.save_managed_mcp_proxy_tools(str(tool["id"]), descriptors, org_id)
    assert saved["discovered_tools"] == descriptors
    cached = await repo.get_managed_mcp_proxy_tools(str(tool["id"]), org_id)
    assert cached["discovered_tools"] == descriptors
    assert cached["tools_discovered_at"] is not None

    updated = await repo.update_managed_tool(
        str(tool["id"]),
        org_id,
        source_code=TOOL_CODE + "\n# changed",
    )
    assert updated["discovered_tools"] is None
    assert updated["tools_discovered_at"] is None


@pytest.mark.asyncio
async def test_repository_managed_tool_proposal_and_grant(repo, auth_user):
    org_id = str(auth_user["organization_id"])
    user_id = str(auth_user["id"])
    tool = await repo.create_managed_tool(
        name="echo-value",
        description="Echo a value",
        source_code=TOOL_CODE,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        org_id=org_id,
        created_by=user_id,
        owner_user_id=user_id,
        proposal_reason="Needed for agent tool-builder tests",
    )

    assert tool["status"] == "proposed"
    proposals = await repo.get_pending_proposals(org_id)
    assert any(item["name"] == "echo-value" for item in proposals["managed_tools"])

    # Two-step approval: owner sign-off lands as 'owner_approved'; the
    # admin's second approval activates the tool.
    approved = await repo.approve_managed_tool(str(tool["id"]), org_id, user_id)
    assert approved["status"] == "owner_approved"

    activated = await repo.approve_managed_tool(str(tool["id"]), org_id, user_id)
    assert activated["status"] == "active"

    agent = await repo.create_agent(
        name="tool-agent",
        description="Uses managed tools",
        content="# Tool Agent",
        org_id=org_id,
        created_by=user_id,
        status="active",
        owner_user_id=user_id,
    )
    assert await repo.grant_managed_tool(
        str(agent["id"]), str(tool["id"]),
        org_id=org_id, actor_user_id=user_id,
    )
    granted = await repo.get_agent_managed_tools(str(agent["id"]))
    assert [item["name"] for item in granted] == ["echo-value"]
    assert await repo.is_managed_tool_granted_to_agent(str(agent["id"]), str(tool["id"]))


@pytest.mark.asyncio
async def test_mcp_create_list_get_tool_definition(mcp, auth_user):
    result = await _call(
        mcp,
        "create_tool_definition",
        {
            "name": "mcp-echo",
            "description": "Echo through managed tool",
            "source_code": TOOL_CODE,
            "input_schema": {
                "type": "OBJECT",
                "properties": {"value": {"type": "STRING"}},
            },
            "network_policy": {"network_mode": "none", "allowed_hosts": []},
            "proposal_evidence": "Requested in chat to test custom tool creation.",
        },
    )
    assert result["name"] == "mcp-echo"
    assert result["status"] == "proposed"
    assert result["auth_policy"]["mode"] == "agent_grant"
    assert result["input_schema"] == {
        "type": "object",
        "properties": {"value": {"type": "string"}},
    }
    assert result["proposal_evidence"] == {
        "summary": "Requested in chat to test custom tool creation."
    }

    listed = await _call(mcp, "list_tool_definitions", {"status": "proposed"})
    assert any(item["name"] == "mcp-echo" for item in listed["items"])

    fetched = await _call(mcp, "get_tool_definition", {"tool": "mcp-echo"})
    assert fetched["name"] == "mcp-echo"
    assert "source_code" in fetched


@pytest.mark.asyncio
async def test_run_managed_tool_uses_trusted_agent_context(mcp, repo, auth_user):
    org_id = str(auth_user["organization_id"])
    user_id = str(auth_user["id"])
    tool = await repo.create_managed_tool(
        name="active-echo",
        description="Echo a value",
        source_code=TOOL_CODE,
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        org_id=org_id,
        created_by=user_id,
        status="active",
        owner_user_id=user_id,
    )
    agent = await repo.create_agent(
        name="runtime-tool-agent",
        description="Uses runtime tools",
        content="# Runtime Tool Agent",
        org_id=org_id,
        created_by=user_id,
        status="active",
        owner_user_id=user_id,
    )
    await repo.grant_managed_tool(
        str(agent["id"]), str(tool["id"]),
        org_id=org_id, actor_user_id=user_id,
    )
    set_llm_context(agent_definition_id=str(agent["id"]))

    mocked = AsyncMock(
        return_value=ManagedToolExecutionResult(
            ok=True,
            result={"echo": "hello"},
            run_id="run-1",
            duration_ms=3,
        )
    )
    with patch("lucent.services.managed_tools.ManagedToolExecutor.execute", mocked):
        result = await _call(
            mcp,
            "run_managed_tool",
            {"tool": "active-echo", "arguments": {"value": "hello"}},
        )

    assert result["ok"] is True
    assert result["result"] == {"echo": "hello"}
    kwargs = mocked.await_args.kwargs
    assert kwargs["agent_id"] == str(agent["id"])
    assert kwargs["enforce_agent_grant"] is True


# ── jsonb definition rows on the clearance (usage) path ───────────────────
#
# Regression: the clearance-gated usage lookups normalized managed-tool rows
# with the generic definition normalizer, so jsonb columns (`auth_policy`,
# `input_schema`, `runtime_config`, …) stayed asyncpg text. The executor then
# did `auth_policy.get("roles")` on a str and every managed tool — db_query,
# docker_ops, db_snapshot — failed identically with HTTP 500 /
# "'str' object has no attribute 'get'".


def _raw_managed_tool_row(name: str = "db_query", **overrides) -> dict:
    """A row shaped the way asyncpg returns it: jsonb columns as text."""
    row = {
        "id": str(uuid4()),
        "name": name,
        "description": "read-only SQL",
        "runtime_type": "http",
        "source_code": "",
        "entrypoint": "handler",
        "requirements": "[]",
        "runtime_config": '{"url": "https://example.com/query", "method": "POST"}',
        "input_schema": '{"type": "object", "properties": {"action": {"type": "string"}}}',
        "output_schema": None,
        "auth_policy": '{"mode": "agent_grant", "require_user_access": true, "roles": ["admin"]}',
        "network_policy": '{"network_mode": "none", "allowed_hosts": []}',
        "resource_limits": '{"memory_limit": "512m", "cpu_limit": 1.0, "disk_limit": "1g"}',
        "env_vars": "{}",
        "timeout_seconds": 300,
        "status": "active",
        "scope": "instance",
        "organization_id": str(uuid4()),
        "proposal_evidence": "{}",
    }
    row.update(overrides)
    return row


class _StaticConnection:
    def __init__(self, row: dict):
        self.row = row

    async def execute(self, *_args, **_kwargs):
        return None

    async def fetchrow(self, _query, *_params):
        return self.row

    async def fetch(self, _query, *_params):
        return [self.row]


class _StaticPool:
    def __init__(self, connection: _StaticConnection):
        self.connection = connection

    def acquire(self):
        connection = self.connection

        class _Context:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_args):
                return False

        return _Context()


@pytest.mark.asyncio
async def test_usable_managed_tool_by_name_decodes_jsonb_fields():
    principal = AuthPrincipal(uuid4(), "managed-tool-tester", uuid4())
    pool = AuthorizedDatabasePool(
        _StaticPool(_StaticConnection(_raw_managed_tool_row())),
        principal,
        DEFINITIONS_AUTH_POLICIES,
    )

    tool = await DefinitionRepository(pool).get_usable_managed_tool_by_name(
        "db_query", str(principal.organization_id)
    )

    assert tool is not None
    assert tool["auth_policy"] == {
        "mode": "agent_grant",
        "require_user_access": True,
        "roles": ["admin"],
    }
    assert tool["input_schema"]["properties"]["action"] == {"type": "string"}
    assert tool["runtime_config"]["url"] == "https://example.com/query"
    assert tool["network_policy"] == {"network_mode": "none", "allowed_hosts": []}
    assert tool["resource_limits"]["memory_limit"] == "512m"
    assert tool["requirements"] == []


@pytest.mark.asyncio
async def test_executor_enforces_roles_from_json_string_auth_policy():
    executor = ManagedToolExecutor(AsyncMock())

    with pytest.raises(ManagedToolBlockedError, match="role is not allowed"):
        await executor.execute(
            tool=_raw_managed_tool_row(),
            arguments={},
            org_id="org-1",
            user_id="user-1",
            user_role="member",
            enforce_agent_grant=False,
        )


@pytest.mark.asyncio
async def test_executor_reports_unusable_definition_field():
    executor = ManagedToolExecutor(AsyncMock())

    with pytest.raises(ManagedToolDefinitionError, match="auth_policy"):
        await executor.execute(
            tool=_raw_managed_tool_row(auth_policy="{not-json"),
            arguments={},
            org_id="org-1",
            user_id="user-1",
            user_role="admin",
            enforce_agent_grant=False,
        )


@pytest.mark.asyncio
async def test_executor_accepts_json_string_arguments(monkeypatch):
    captured = {}

    class _RecordingRuntime:
        async def execute(self, context):
            captured["arguments"] = context.arguments
            return RuntimeExecutionResult(ok=True, result={"ok": True})

    monkeypatch.setattr(
        "lucent.services.managed_tools.get_runtime", lambda _kind: _RecordingRuntime()
    )
    repo = AsyncMock()
    repo.create_managed_tool_run.return_value = {"id": "run-1"}
    executor = ManagedToolExecutor(repo)

    result = await executor.execute(
        tool=_raw_managed_tool_row(),
        arguments='{"action": "query"}',
        org_id="org-1",
        user_id="user-1",
        user_role="admin",
        enforce_agent_grant=False,
    )

    assert result.ok is True
    assert captured["arguments"] == {"action": "query"}


@pytest.mark.asyncio
async def test_executor_rejects_non_object_arguments():
    executor = ManagedToolExecutor(AsyncMock())

    with pytest.raises(ManagedToolArgumentError, match="JSON object"):
        await executor.execute(
            tool=_raw_managed_tool_row(),
            arguments="[1, 2]",
            org_id="org-1",
            user_id="user-1",
            user_role="admin",
            enforce_agent_grant=False,
        )
