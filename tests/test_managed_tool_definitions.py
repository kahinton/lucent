"""Tests for managed tool definitions and MCP tool-builder surfaces."""

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from mcp.server.fastmcp import FastMCP

from lucent.auth import set_current_user
from lucent.db.definitions import DefinitionRepository
from lucent.llm.context import clear_llm_context, set_llm_context
from lucent.services.managed_tools import ManagedToolExecutionResult
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
    result = await mcp._tool_manager.call_tool(tool_name, args or {})
    return json.loads(result)


TOOL_CODE = """
def handler(args):
    return {"echo": args.get("value")}
""".strip()


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

    approved = await repo.approve_managed_tool(str(tool["id"]), org_id, user_id)
    assert approved["status"] == "active"

    agent = await repo.create_agent(
        name="tool-agent",
        description="Uses managed tools",
        content="# Tool Agent",
        org_id=org_id,
        created_by=user_id,
        status="active",
        owner_user_id=user_id,
    )
    assert await repo.grant_managed_tool(str(agent["id"]), str(tool["id"]), org_id=org_id)
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
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
            "network_policy": {"network_mode": "none", "allowed_hosts": []},
        },
    )
    assert result["name"] == "mcp-echo"
    assert result["status"] == "proposed"
    assert result["auth_policy"]["mode"] == "agent_grant"

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
    await repo.grant_managed_tool(str(agent["id"]), str(tool["id"]), org_id=org_id)
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
