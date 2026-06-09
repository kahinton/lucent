"""Tests for operational tool-call audit logging."""

import json
from uuid import uuid4

import pytest
import pytest_asyncio

from lucent.db.definitions import DefinitionRepository
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
        await conn.execute(
            "DELETE FROM skill_definitions WHERE organization_id = $1 AND name LIKE 'audit-test-%'",
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


def test_classify_tool_result_distinguishes_auth_403_429():
    """Pattern 2: 401 vs 403 vs 429 produce distinct failure classes so
    operators can route them differently (re-mint vs grant vs backoff)."""
    s, fc, _ = classify_tool_result(
        "Error calling tool search_memories: Unauthorized: Invalid or expired credentials"
    )
    assert (s, fc) == ("failed", "auth_error")

    s, fc, _ = classify_tool_result("Error calling tool x: HTTP 403 Forbidden")
    assert (s, fc) == ("failed", "forbidden")

    s, fc, _ = classify_tool_result(
        "Error calling tool x: status_code=429 Too Many Requests"
    )
    assert (s, fc) == ("failed", "rate_limited")


def test_classify_tool_result_bash_exit_zero_is_not_auth_error():
    """Pattern 2 / Fix F: investigative bash scripts often print other tools'
    error corpora — substrings like 'Unauthorized' in stdout must NOT be
    classified as auth_error when the runner reports success (exit 0)."""
    output = (
        "Investigating failures... found row: "
        "{'error': 'Unauthorized: Invalid or expired credentials', 'http': 401}"
    )
    # exit 0 — NOT a failure
    s, fc, _ = classify_tool_result(output, tool_name="bash", exit_code=0)
    assert (s, fc) == ("success", None)
    # status='failed' from runner overrides
    s, fc, _ = classify_tool_result(
        output, tool_name="bash", exit_code=0, runner_status="failed"
    )
    assert (s, fc) == ("failed", "auth_error")
    # exit != 0 — real bash failure
    s, fc, _ = classify_tool_result(output, tool_name="bash", exit_code=1)
    assert (s, fc) == ("failed", "auth_error")
    # Non-bash tools unchanged — same string still classifies as auth_error
    s, fc, _ = classify_tool_result(output, tool_name="search_memories")
    assert (s, fc) == ("failed", "auth_error")


def test_classify_tool_result_bash_error_text_without_exit_code_is_not_auth_error():
    output = "Error: Unauthorized: Invalid or expired credentials"

    s, fc, _ = classify_tool_result(output, tool_name="bash")
    assert (s, fc) == ("success", None)

    s, fc, _ = classify_tool_result(output, tool_name="bash", runner_status="failed")
    assert (s, fc) == ("failed", "auth_error")


def test_classify_tool_result_bash_rate_limited_only_on_failure():
    """Pattern 2: 429 substrings in bash stdout are also gated on exit code."""
    output = "Rate limit hit: HTTP 429 Too Many Requests"
    s, fc, _ = classify_tool_result(output, tool_name="bash", exit_code=0)
    assert (s, fc) == ("success", None)
    s, fc, _ = classify_tool_result(output, tool_name="bash", exit_code=1)
    assert (s, fc) == ("failed", "rate_limited")


@pytest.mark.asyncio
async def test_tool_audit_analyzes_repeated_agent_tool_failures(db_pool, test_user):
    repo = ToolAuditRepository(db_pool)
    for idx in range(3):
        await repo.log_tool_call(
            tool_name="run_tests",
            status="failed",
            source="test",
            input_payload={"command": "pytest", "attempt": idx},
            output_payload="Error calling tool run_tests: missing working directory",
            failure_class="tool_error",
            error_message="missing working directory",
            context={
                "organization_id": str(test_user["organization_id"]),
                "user_id": str(test_user["id"]),
                "agent_type": "code",
                "skill_names": ["dev-workflow"],
                "model": "gpt-5.1",
            },
        )

    result = await repo.analyze_failure_patterns(
        org_id=test_user["organization_id"],
        since_days=7,
        min_failures=3,
    )

    pattern = next(
        p for p in result["patterns"]
        if p["dimension"] == "agent" and p["tool_name"] == "run_tests"
    )
    assert pattern["failure_count"] == 3
    assert pattern["target"] == "code"
    assert pattern["proposal_evidence"]["source"] == "tool_call_audit_log"
    assert pattern["proposal_evidence"]["affected_models"] == ["gpt-5.1"]
    assert "focused skill" in pattern["recommended_action"]


@pytest.mark.asyncio
async def test_definition_proposals_preserve_review_evidence(db_pool, test_user):
    repo = DefinitionRepository(db_pool)
    evidence = {
        "source": "tool_call_audit_log",
        "tool_name": "run_tests",
        "failure_count": 4,
        "recommended_agent_type": "code",
    }
    created = await repo.create_skill(
        name=f"audit-test-run-tests-{uuid4().hex[:8]}",
        description="Better run_tests usage for code agents",
        content="# Run Tests Skill\n\nUse the run_tests tool with a working directory.",
        org_id=str(test_user["organization_id"]),
        created_by=str(test_user["id"]),
        proposal_reason="code agents repeatedly failed run_tests without working_dir",
        proposal_evidence=evidence,
    )

    proposals = await repo.get_pending_proposals(str(test_user["organization_id"]))
    skill = next(s for s in proposals["skills"] if s["id"] == created["id"])
    assert skill["proposal_reason"].startswith("code agents repeatedly failed")
    proposal_evidence = skill["proposal_evidence"]
    if isinstance(proposal_evidence, str):
        proposal_evidence = json.loads(proposal_evidence)
    assert proposal_evidence["tool_name"] == "run_tests"
    assert proposal_evidence["failure_count"] == 4
