"""Tests for operational tool-call audit logging."""

import json
from uuid import uuid4

import pytest
import pytest_asyncio

from lucent.db.llm_sessions import LLMSessionRepository
from lucent.db.tool_audit import ToolAuditRepository, classify_tool_result
from lucent.llm.mcp_bridge import MCPToolBridge


@pytest_asyncio.fixture(autouse=True)
async def cleanup_tool_audit_rows(db_pool, test_user):
    yield
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM tool_call_audit_log WHERE organization_id = $1",
            test_user["organization_id"],
        )
        await conn.execute(
            "DELETE FROM llm_sessions WHERE organization_id = $1",
            test_user["organization_id"],
        )


@pytest.mark.asyncio
async def test_tool_audit_repository_enriches_from_session_and_redacts(db_pool, test_user):
    session_repo = LLMSessionRepository(db_pool)
    session = await session_repo.create_session(
        org_id=test_user["organization_id"],
        user_id=test_user["id"],
        kind="chat",
        title="Audit tool failure",
        engine="langchain",
        model="gpt-5.1",
        reasoning_effort="high",
    )

    repo = ToolAuditRepository(db_pool)
    row = await repo.log_tool_call(
        tool_name="create_memory",
        status="failed",
        source="test",
        input_payload={"content": "hello", "api_key": "super-secret"},
        output_payload="Error calling tool create_memory: boom token=abc123",
        failure_class="RuntimeError",
        error_message="boom password=hunter2",
        context={"session_id": str(session["id"]), "turn_id": str(uuid4())},
    )

    assert row["status"] == "failed"
    assert row["tool_name"] == "create_memory"
    assert row["organization_id"] == test_user["organization_id"]
    assert row["user_id"] == test_user["id"]
    assert row["model"] == "gpt-5.1"
    assert row["engine"] == "langchain"
    assert row["reasoning_effort"] == "high"
    preview = row["input_preview"]
    if isinstance(preview, str):
        preview = json.loads(preview)
    assert preview["api_key"] == "[REDACTED]"
    assert "hunter2" not in row["error_message"]
    assert "abc123" not in row["output_preview"]


@pytest.mark.asyncio
async def test_mcp_bridge_audits_failed_tool_call(db_pool, test_user, monkeypatch):
    session_repo = LLMSessionRepository(db_pool)
    session = await session_repo.create_session(
        org_id=test_user["organization_id"],
        user_id=test_user["id"],
        kind="chat",
        title="Bridge failure",
        engine="langchain",
        model="claude-sonnet-4.5",
    )

    class FakeSession:
        async def call_tool(self, _tool_name, _arguments):
            raise RuntimeError("tool exploded token=bad")

    async def fake_ensure_session(self):
        return FakeSession()

    monkeypatch.setattr(MCPToolBridge, "_ensure_session", fake_ensure_session)
    bridge = MCPToolBridge(
        "http://localhost:8766/mcp",
        headers={"X-Lucent-LLM-Session-Id": str(session["id"])},
        allowed_tools=["explode"],
        skip_url_validation=True,
        audit_context={"source": "test.bridge", "mcp_server": "memory-server"},
    )

    result = await bridge.call_tool("explode", {"token": "bad", "query": "x"})

    assert result.startswith("Error calling tool explode")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM tool_call_audit_log
               WHERE session_id = $1 AND tool_name = 'explode'""",
            session["id"],
        )
    assert row is not None
    assert row["status"] == "failed"
    assert row["failure_class"] == "RuntimeError"
    assert row["source"] == "test.bridge"
    assert row["mcp_server"] == "memory-server"
    preview = row["input_preview"]
    if isinstance(preview, str):
        preview = json.loads(preview)
    assert preview["token"] == "[REDACTED]"
    assert "bad" not in row["output_preview"]


def test_classify_tool_result():
    assert classify_tool_result("all good") == ("success", None, None)
    status, failure_class, message = classify_tool_result(
        "Error calling tool search_memories: no auth"
    )
    assert status == "failed"
    assert failure_class == "tool_error"
    assert "search_memories" in message
    assert classify_tool_result("Tool write_file blocked by hook.")[0] == "blocked"
