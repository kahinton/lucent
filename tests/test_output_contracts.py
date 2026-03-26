"""Tests for the structured task output contract system.

Covers:
  1. Database migration — new columns exist with correct types/defaults
  2. Output validation module — extraction, parsing, schema validation
  3. DB layer — create_task with output_contract, complete_task with structured results
  4. API endpoints — creating/completing/retrieving tasks with structured contracts
  5. End-to-end lifecycle — full request → task → structured completion → retrieval
"""

import json
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.requests import RequestRepository


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

_SIMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "score": {"type": "integer"},
    },
    "required": ["summary", "score"],
}

_SIMPLE_CONTRACT = {
    "json_schema": _SIMPLE_SCHEMA,
    "on_failure": "fallback",
    "max_retries": 1,
}


@pytest_asyncio.fixture
async def oc_prefix(db_pool):
    """Unique prefix and cleanup for output-contract tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_oc_{test_id}_"
    yield prefix

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM task_events WHERE task_id IN ("
            "SELECT id FROM tasks WHERE organization_id IN ("
            "SELECT id FROM organizations WHERE name LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM task_memories WHERE task_id IN ("
            "SELECT id FROM tasks WHERE organization_id IN ("
            "SELECT id FROM organizations WHERE name LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM tasks WHERE organization_id IN ("
            "SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM requests WHERE organization_id IN ("
            "SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM agent_definitions WHERE organization_id IN ("
            "SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memory_audit_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memory_access_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM memories WHERE username LIKE $1", f"{prefix}%")
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def oc_org(db_pool, oc_prefix):
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{oc_prefix}org")


@pytest_asyncio.fixture
async def oc_user(db_pool, oc_org, oc_prefix):
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{oc_prefix}user",
        provider="local",
        organization_id=oc_org["id"],
        email=f"{oc_prefix}user@test.com",
        display_name=f"{oc_prefix}User",
    )


@pytest_asyncio.fixture
async def oc_repo(db_pool):
    return RequestRepository(db_pool)


@pytest_asyncio.fixture
async def oc_request(oc_repo, oc_org, oc_user):
    """Create a test request."""
    return await oc_repo.create_request(
        title="OC Test Request",
        org_id=str(oc_org["id"]),
        description="Testing output contracts",
        source="user",
        priority="medium",
        created_by=str(oc_user["id"]),
    )


async def _create_active_agent_definition(db_pool, org_id, name="code"):
    """Insert a minimal active agent definition for task creation."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_definitions (name, organization_id, content, status, scope)
               VALUES ($1, $2, $3, 'active', 'built-in')
               ON CONFLICT DO NOTHING""",
            name,
            org_id,
            f"Agent {name}",
        )


async def _make_client(user, scopes=None):
    """Create an httpx test client with mocked auth."""
    app = create_app()
    fake_user = CurrentUser(
        id=user["id"],
        organization_id=user["organization_id"],
        role=user.get("role", "member"),
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=scopes or ["read", "write", "daemon-tasks"],
        external_id=user.get("external_id"),
    )

    async def override():
        return fake_user

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, app


@pytest_asyncio.fixture
async def oc_client(oc_user):
    client, app = await _make_client(oc_user)
    async with client:
        yield client
    app.dependency_overrides.clear()


# ============================================================================
# 1. DATABASE MIGRATION TESTS
# ============================================================================


class TestMigrationColumns:
    """Verify migration 045 added the expected columns with correct types."""

    @pytest.mark.asyncio
    async def test_output_contract_column_exists(self, db_pool):
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT column_name, data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_name = 'tasks' AND column_name = 'output_contract'"""
            )
        assert row is not None, "output_contract column missing"
        assert row["data_type"] == "jsonb"

    @pytest.mark.asyncio
    async def test_result_structured_column_exists(self, db_pool):
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT column_name, data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_name = 'tasks' AND column_name = 'result_structured'"""
            )
        assert row is not None, "result_structured column missing"
        assert row["data_type"] == "jsonb"

    @pytest.mark.asyncio
    async def test_result_summary_column_exists(self, db_pool):
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT column_name, data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_name = 'tasks' AND column_name = 'result_summary'"""
            )
        assert row is not None, "result_summary column missing"
        assert row["data_type"] == "text"

    @pytest.mark.asyncio
    async def test_validation_status_column_exists(self, db_pool):
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT column_name, data_type, column_default, is_nullable
                   FROM information_schema.columns
                   WHERE table_name = 'tasks' AND column_name = 'validation_status'"""
            )
        assert row is not None, "validation_status column missing"
        assert row["data_type"] == "character varying"
        assert row["is_nullable"] == "NO"
        # Default should be 'not_applicable'
        assert "not_applicable" in (row["column_default"] or "")

    @pytest.mark.asyncio
    async def test_validation_errors_column_exists(self, db_pool):
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT column_name, data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_name = 'tasks' AND column_name = 'validation_errors'"""
            )
        assert row is not None, "validation_errors column missing"
        assert row["data_type"] == "jsonb"

    @pytest.mark.asyncio
    async def test_legacy_tasks_have_default_validation_status(
        self, oc_repo, oc_request, oc_org
    ):
        """Tasks created without contracts get 'not_applicable' by default."""
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Legacy task",
            org_id=str(oc_org["id"]),
        )
        assert task["validation_status"] == "not_applicable"
        assert task["output_contract"] is None
        assert task["result_structured"] is None


# ============================================================================
# 2. UNIT TESTS FOR OUTPUT VALIDATION MODULE
# ============================================================================


class TestValidateContractSchema:
    """Tests for validate_contract_schema() in output_validation.py."""

    def _import(self):
        import sys
        import os

        daemon_path = os.path.join(os.path.dirname(__file__), "..", "daemon")
        if daemon_path not in sys.path:
            sys.path.insert(0, daemon_path)
        from output_validation import validate_contract_schema

        return validate_contract_schema

    def test_none_contract_returns_no_errors(self):
        fn = self._import()
        assert fn(None) == []

    def test_valid_contract_returns_no_errors(self):
        fn = self._import()
        assert fn(_SIMPLE_CONTRACT) == []

    def test_non_dict_contract_returns_error(self):
        fn = self._import()
        errors = fn("not a dict")
        assert len(errors) == 1
        assert "JSON object" in errors[0]

    def test_missing_json_schema_key(self):
        fn = self._import()
        errors = fn({"on_failure": "fail"})
        assert any("json_schema" in e for e in errors)

    def test_json_schema_not_dict(self):
        fn = self._import()
        errors = fn({"json_schema": "string"})
        assert any("JSON object" in e for e in errors)

    def test_invalid_json_schema(self):
        fn = self._import()
        errors = fn({"json_schema": {"type": "not_a_type"}})
        assert any("Invalid JSON Schema" in e for e in errors)

    def test_invalid_on_failure_policy(self):
        fn = self._import()
        errors = fn({
            "json_schema": {"type": "object"},
            "on_failure": "explode",
        })
        assert any("on_failure" in e for e in errors)

    def test_valid_on_failure_policies(self):
        fn = self._import()
        for policy in ("fail", "fallback", "retry_then_fallback"):
            errors = fn({
                "json_schema": {"type": "object"},
                "on_failure": policy,
            })
            assert errors == [], f"Policy '{policy}' should be valid"

    def test_negative_max_retries(self):
        fn = self._import()
        errors = fn({
            "json_schema": {"type": "object"},
            "max_retries": -1,
        })
        assert any("max_retries" in e for e in errors)

    def test_non_integer_max_retries(self):
        fn = self._import()
        errors = fn({
            "json_schema": {"type": "object"},
            "max_retries": 1.5,
        })
        assert any("max_retries" in e for e in errors)

    def test_defaults_for_on_failure_and_max_retries(self):
        fn = self._import()
        # Only json_schema provided — defaults should be valid
        errors = fn({"json_schema": {"type": "object"}})
        assert errors == []


class TestProcessTaskOutput:
    """Tests for process_task_output() in output_validation.py."""

    def _import(self):
        import sys
        import os

        daemon_path = os.path.join(os.path.dirname(__file__), "..", "daemon")
        if daemon_path not in sys.path:
            sys.path.insert(0, daemon_path)
        from output_validation import process_task_output

        return process_task_output

    # --- No contract (backward compat) ---

    def test_no_contract_returns_not_applicable(self):
        fn = self._import()
        result = fn("some text", None)
        assert result["validation_status"] == "not_applicable"
        assert result["result_structured"] is None
        assert result["validation_errors"] is None

    def test_empty_contract_returns_not_applicable(self):
        fn = self._import()
        result = fn("some text", {})
        # Empty dict is falsy, treated same as None (no contract)
        assert result["validation_status"] == "not_applicable"
        assert result["validation_errors"] is None

    def test_contract_with_only_on_failure_is_invalid(self):
        fn = self._import()
        # Non-empty dict with no json_schema key → invalid contract
        result = fn("some text", {"on_failure": "fail"})
        assert result["validation_status"] == "invalid"
        assert result["validation_errors"] is not None

    # --- Valid structured output ---

    def test_valid_output_extracted_and_validated(self):
        fn = self._import()
        text = (
            "Here is my analysis.\n\n"
            "<task_output>\n"
            '{"summary": "All good", "score": 95}\n'
            "</task_output>\n"
            "Some closing remarks."
        )
        result = fn(text, _SIMPLE_CONTRACT)
        assert result["validation_status"] == "valid"
        assert result["result_structured"] == {"summary": "All good", "score": 95}
        assert result["result_summary"] == "All good"
        assert result["validation_errors"] is None

    def test_summary_extracted_from_parsed_output(self):
        fn = self._import()
        text = '<task_output>{"summary": "Brief", "score": 1}</task_output>'
        result = fn(text, _SIMPLE_CONTRACT)
        assert result["result_summary"] == "Brief"

    def test_summary_truncated_to_2000_chars(self):
        fn = self._import()
        long_summary = "x" * 5000
        text = f'<task_output>{{"summary": "{long_summary}", "score": 1}}</task_output>'
        result = fn(text, _SIMPLE_CONTRACT)
        assert result["validation_status"] == "valid"
        assert len(result["result_summary"]) == 2000

    def test_no_summary_field_returns_none(self):
        fn = self._import()
        contract = {
            "json_schema": {"type": "object", "properties": {"count": {"type": "integer"}}},
        }
        text = "<task_output>{\"count\": 42}</task_output>"
        result = fn(text, contract)
        assert result["validation_status"] == "valid"
        assert result["result_summary"] is None

    # --- Extraction failures ---

    def test_no_task_output_block(self):
        fn = self._import()
        result = fn("Just plain text, no tags.", _SIMPLE_CONTRACT)
        assert result["validation_status"] == "extraction_failed"
        assert "No <task_output>" in result["validation_errors"][0]

    def test_empty_result_text(self):
        fn = self._import()
        result = fn("", _SIMPLE_CONTRACT)
        assert result["validation_status"] == "extraction_failed"

    def test_none_result_text(self):
        fn = self._import()
        result = fn(None, _SIMPLE_CONTRACT)
        assert result["validation_status"] == "extraction_failed"

    # --- Invalid JSON ---

    def test_invalid_json_in_output_block(self):
        fn = self._import()
        text = "<task_output>{not valid json}</task_output>"
        result = fn(text, _SIMPLE_CONTRACT)
        assert result["validation_status"] == "invalid"
        assert any("Invalid JSON" in e for e in result["validation_errors"])

    # --- Schema validation failures ---

    def test_missing_required_field(self):
        fn = self._import()
        text = '<task_output>{"summary": "only summary"}</task_output>'
        result = fn(text, _SIMPLE_CONTRACT)
        assert result["validation_status"] == "invalid"
        assert result["result_structured"] is None
        assert any("score" in e for e in result["validation_errors"])

    def test_wrong_type(self):
        fn = self._import()
        text = '<task_output>{"summary": "ok", "score": "not_int"}</task_output>'
        result = fn(text, _SIMPLE_CONTRACT)
        assert result["validation_status"] == "invalid"

    def test_extra_fields_allowed_by_default(self):
        fn = self._import()
        text = '<task_output>{"summary": "ok", "score": 5, "extra": true}</task_output>'
        result = fn(text, _SIMPLE_CONTRACT)
        assert result["validation_status"] == "valid"
        assert result["result_structured"]["extra"] is True

    def test_extra_fields_rejected_with_additional_properties_false(self):
        fn = self._import()
        strict_contract = {
            "json_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "score": {"type": "integer"},
                },
                "required": ["summary", "score"],
                "additionalProperties": False,
            },
        }
        text = '<task_output>{"summary": "ok", "score": 5, "extra": true}</task_output>'
        result = fn(text, strict_contract)
        assert result["validation_status"] == "invalid"

    # --- Edge cases ---

    def test_multiline_task_output(self):
        fn = self._import()
        text = (
            "Preamble\n"
            "<task_output>\n"
            "{\n"
            '  "summary": "multi\\nline",\n'
            '  "score": 10\n'
            "}\n"
            "</task_output>\n"
            "Epilogue"
        )
        result = fn(text, _SIMPLE_CONTRACT)
        assert result["validation_status"] == "valid"
        assert result["result_structured"]["score"] == 10

    def test_whitespace_around_json(self):
        fn = self._import()
        text = '<task_output>   {"summary": "ok", "score": 1}   </task_output>'
        result = fn(text, _SIMPLE_CONTRACT)
        assert result["validation_status"] == "valid"

    def test_array_output(self):
        fn = self._import()
        contract = {"json_schema": {"type": "array", "items": {"type": "integer"}}}
        text = "<task_output>[1, 2, 3]</task_output>"
        result = fn(text, contract)
        assert result["validation_status"] == "valid"
        assert result["result_structured"] == [1, 2, 3]
        # Arrays don't have a .get("summary") so summary should be None
        assert result["result_summary"] is None

    def test_invalid_contract_schema_returns_invalid(self):
        fn = self._import()
        bad_contract = {"json_schema": {"type": "not_real"}}
        text = '<task_output>{"a": 1}</task_output>'
        result = fn(text, bad_contract)
        assert result["validation_status"] == "invalid"
        assert any("Invalid JSON Schema" in e for e in result["validation_errors"])

    def test_first_task_output_block_is_used(self):
        fn = self._import()
        text = (
            '<task_output>{"summary": "first", "score": 1}</task_output>\n'
            '<task_output>{"summary": "second", "score": 2}</task_output>'
        )
        result = fn(text, _SIMPLE_CONTRACT)
        assert result["validation_status"] == "valid"
        assert result["result_structured"]["summary"] == "first"


# ============================================================================
# 3. DATABASE LAYER TESTS — create_task / complete_task with contracts
# ============================================================================


class TestCreateTaskWithContract:
    """Test RequestRepository.create_task with output_contract."""

    @pytest.mark.asyncio
    async def test_create_with_valid_contract(self, oc_repo, oc_request, oc_org):
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Contracted Task",
            org_id=str(oc_org["id"]),
            output_contract=_SIMPLE_CONTRACT,
        )
        assert task["output_contract"] is not None
        stored = task["output_contract"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        assert stored["json_schema"] == _SIMPLE_SCHEMA
        assert task["validation_status"] == "not_applicable"

    @pytest.mark.asyncio
    async def test_create_without_contract_backward_compat(
        self, oc_repo, oc_request, oc_org
    ):
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="No Contract Task",
            org_id=str(oc_org["id"]),
        )
        assert task["output_contract"] is None
        assert task["validation_status"] == "not_applicable"

    @pytest.mark.asyncio
    async def test_create_with_invalid_contract_raises(
        self, oc_repo, oc_request, oc_org
    ):
        with pytest.raises(ValueError, match="json_schema"):
            await oc_repo.create_task(
                request_id=str(oc_request["id"]),
                title="Bad Contract",
                org_id=str(oc_org["id"]),
                output_contract={"on_failure": "fail"},  # missing json_schema
            )

    @pytest.mark.asyncio
    async def test_create_with_invalid_on_failure_raises(
        self, oc_repo, oc_request, oc_org
    ):
        with pytest.raises(ValueError, match="on_failure"):
            await oc_repo.create_task(
                request_id=str(oc_request["id"]),
                title="Bad Policy",
                org_id=str(oc_org["id"]),
                output_contract={
                    "json_schema": {"type": "object"},
                    "on_failure": "panic",
                },
            )

    @pytest.mark.asyncio
    async def test_create_with_non_dict_contract_raises(
        self, oc_repo, oc_request, oc_org
    ):
        with pytest.raises(ValueError, match="object"):
            await oc_repo.create_task(
                request_id=str(oc_request["id"]),
                title="String Contract",
                org_id=str(oc_org["id"]),
                output_contract="not a dict",
            )


class TestCompleteTaskWithStructuredOutput:
    """Test RequestRepository.complete_task with structured result fields."""

    @pytest.mark.asyncio
    async def test_complete_with_structured_output(self, oc_repo, oc_request, oc_org):
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Structured Complete",
            org_id=str(oc_org["id"]),
            output_contract=_SIMPLE_CONTRACT,
        )
        await oc_repo.claim_task(str(task["id"]), "test-daemon")

        completed = await oc_repo.complete_task(
            str(task["id"]),
            "Text result goes here",
            result_structured={"summary": "Done", "score": 100},
            result_summary="Done",
            validation_status="valid",
        )
        assert completed is not None
        assert completed["status"] == "completed"
        assert completed["result"] == "Text result goes here"
        assert completed["validation_status"] == "valid"
        assert completed["result_summary"] == "Done"

        # Verify structured data persisted
        stored = completed["result_structured"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        assert stored["summary"] == "Done"
        assert stored["score"] == 100

    @pytest.mark.asyncio
    async def test_complete_without_structured_output_backward_compat(
        self, oc_repo, oc_request, oc_org
    ):
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Plain Complete",
            org_id=str(oc_org["id"]),
        )
        await oc_repo.claim_task(str(task["id"]), "test-daemon")

        completed = await oc_repo.complete_task(str(task["id"]), "Just text")
        assert completed is not None
        assert completed["status"] == "completed"
        assert completed["result"] == "Just text"
        assert completed["validation_status"] == "not_applicable"
        assert completed["result_structured"] is None

    @pytest.mark.asyncio
    async def test_complete_with_fallback_used(self, oc_repo, oc_request, oc_org):
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Fallback",
            org_id=str(oc_org["id"]),
            output_contract=_SIMPLE_CONTRACT,
        )
        await oc_repo.claim_task(str(task["id"]), "test-daemon")

        completed = await oc_repo.complete_task(
            str(task["id"]),
            "Text-only fallback result",
            validation_status="fallback_used",
            validation_errors=["No <task_output> block found"],
        )
        assert completed["validation_status"] == "fallback_used"
        assert completed["result_structured"] is None

        errors = completed["validation_errors"]
        if isinstance(errors, str):
            errors = json.loads(errors)
        assert "<task_output>" in errors[0]

    @pytest.mark.asyncio
    async def test_complete_with_repair_succeeded(self, oc_repo, oc_request, oc_org):
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Repaired",
            org_id=str(oc_org["id"]),
            output_contract=_SIMPLE_CONTRACT,
        )
        await oc_repo.claim_task(str(task["id"]), "test-daemon")

        completed = await oc_repo.complete_task(
            str(task["id"]),
            "Repaired output",
            result_structured={"summary": "Fixed", "score": 80},
            result_summary="Fixed",
            validation_status="repair_succeeded",
        )
        assert completed["validation_status"] == "repair_succeeded"

        stored = completed["result_structured"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        assert stored["score"] == 80

    @pytest.mark.asyncio
    async def test_complete_with_invalid_validation_status_raises(
        self, oc_repo, oc_request, oc_org
    ):
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Bad Status",
            org_id=str(oc_org["id"]),
        )
        await oc_repo.claim_task(str(task["id"]), "test-daemon")

        with pytest.raises(ValueError, match="validation_status"):
            await oc_repo.complete_task(
                str(task["id"]),
                "result",
                validation_status="totally_wrong",
            )

    @pytest.mark.asyncio
    async def test_complete_stores_all_validation_statuses(
        self, oc_repo, oc_request, oc_org
    ):
        """Verify every valid validation_status is accepted."""
        valid_statuses = [
            "not_applicable",
            "valid",
            "invalid",
            "extraction_failed",
            "fallback_used",
            "repair_succeeded",
        ]
        for status in valid_statuses:
            task = await oc_repo.create_task(
                request_id=str(oc_request["id"]),
                title=f"Status {status}",
                org_id=str(oc_org["id"]),
            )
            await oc_repo.claim_task(str(task["id"]), "test-daemon")
            completed = await oc_repo.complete_task(
                str(task["id"]),
                f"result for {status}",
                validation_status=status,
            )
            assert completed is not None, f"Status '{status}' should be accepted"
            assert completed["validation_status"] == status


class TestGetTaskReturnsStructuredFields:
    """Verify get_task / list_tasks return all structured output fields."""

    @pytest.mark.asyncio
    async def test_get_task_includes_contract_and_structured_fields(
        self, oc_repo, oc_request, oc_org
    ):
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Retrieve Test",
            org_id=str(oc_org["id"]),
            output_contract=_SIMPLE_CONTRACT,
        )
        await oc_repo.claim_task(str(task["id"]), "d1")
        await oc_repo.complete_task(
            str(task["id"]),
            "text",
            result_structured={"summary": "ok", "score": 1},
            result_summary="ok",
            validation_status="valid",
        )

        fetched = await oc_repo.get_task(str(task["id"]))
        assert fetched is not None
        assert fetched["output_contract"] is not None
        assert fetched["validation_status"] == "valid"
        assert fetched["result_summary"] == "ok"

        structured = fetched["result_structured"]
        if isinstance(structured, str):
            structured = json.loads(structured)
        assert structured["score"] == 1

    @pytest.mark.asyncio
    async def test_get_request_with_tasks_includes_structured_fields(
        self, oc_repo, oc_request, oc_org
    ):
        """get_request_with_tasks should include structured output on each task."""
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Tree Test",
            org_id=str(oc_org["id"]),
            output_contract=_SIMPLE_CONTRACT,
        )
        await oc_repo.claim_task(str(task["id"]), "d1")
        await oc_repo.complete_task(
            str(task["id"]),
            "text result",
            result_structured={"summary": "tree", "score": 42},
            validation_status="valid",
        )

        detail = await oc_repo.get_request_with_tasks(
            str(oc_request["id"]), str(oc_org["id"])
        )
        assert detail is not None
        tasks = detail["tasks"]
        assert len(tasks) >= 1

        completed_task = next(t for t in tasks if str(t["id"]) == str(task["id"]))
        assert completed_task["validation_status"] == "valid"

        structured = completed_task["result_structured"]
        if isinstance(structured, str):
            structured = json.loads(structured)
        assert structured["score"] == 42


# ============================================================================
# 4. API ENDPOINT TESTS
# ============================================================================


class TestApiCreateTaskWithContract:
    """Test POST /api/requests/{id}/tasks with output_contract."""

    @pytest.mark.asyncio
    async def test_create_with_output_contract(
        self, oc_client, oc_repo, oc_org, oc_request, db_pool
    ):
        await _create_active_agent_definition(db_pool, oc_org["id"], "code")

        resp = await oc_client.post(
            f"/api/requests/{oc_request['id']}/tasks",
            json={
                "title": "API Contracted Task",
                "agent_type": "code",
                "output_contract": _SIMPLE_CONTRACT,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["title"] == "API Contracted Task"

        # Verify contract was stored
        task = await oc_repo.get_task(str(data["id"]))
        assert task["output_contract"] is not None

    @pytest.mark.asyncio
    async def test_create_with_output_schema_compat(
        self, oc_client, oc_repo, oc_org, oc_request, db_pool
    ):
        """output_schema is a backward-compatible alias that wraps into output_contract."""
        await _create_active_agent_definition(db_pool, oc_org["id"], "code")

        resp = await oc_client.post(
            f"/api/requests/{oc_request['id']}/tasks",
            json={
                "title": "Schema Alias Task",
                "agent_type": "code",
                "output_schema": _SIMPLE_SCHEMA,
            },
        )
        assert resp.status_code == 200, resp.text

        task = await oc_repo.get_task(str(resp.json()["id"]))
        contract = task["output_contract"]
        if isinstance(contract, str):
            contract = json.loads(contract)
        assert contract["json_schema"] == _SIMPLE_SCHEMA
        assert contract["on_failure"] == "fallback"
        assert contract["max_retries"] == 1

    @pytest.mark.asyncio
    async def test_create_with_both_contract_and_schema_rejected(
        self, oc_client, oc_org, oc_request, db_pool
    ):
        await _create_active_agent_definition(db_pool, oc_org["id"], "code")

        resp = await oc_client.post(
            f"/api/requests/{oc_request['id']}/tasks",
            json={
                "title": "Both",
                "agent_type": "code",
                "output_contract": _SIMPLE_CONTRACT,
                "output_schema": _SIMPLE_SCHEMA,
            },
        )
        assert resp.status_code == 422
        assert "either" in resp.json()["detail"].lower() or "both" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_with_invalid_contract_rejected(
        self, oc_client, oc_org, oc_request, db_pool
    ):
        await _create_active_agent_definition(db_pool, oc_org["id"], "code")

        resp = await oc_client.post(
            f"/api/requests/{oc_request['id']}/tasks",
            json={
                "title": "Invalid Contract",
                "agent_type": "code",
                "output_contract": {"on_failure": "fail"},  # missing json_schema
            },
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_without_contract(
        self, oc_client, oc_org, oc_request, db_pool
    ):
        await _create_active_agent_definition(db_pool, oc_org["id"], "code")

        resp = await oc_client.post(
            f"/api/requests/{oc_request['id']}/tasks",
            json={
                "title": "No Contract",
                "agent_type": "code",
            },
        )
        assert resp.status_code == 200


class TestApiCompleteTaskWithStructuredOutput:
    """Test POST /api/requests/tasks/{id}/complete with structured result."""

    @pytest.mark.asyncio
    async def test_complete_with_structured_result(
        self, oc_client, oc_repo, oc_org, oc_request, db_pool
    ):
        await _create_active_agent_definition(db_pool, oc_org["id"], "code")

        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="API Complete",
            org_id=str(oc_org["id"]),
            output_contract=_SIMPLE_CONTRACT,
            agent_type="code",
        )
        await oc_repo.claim_task(str(task["id"]), "test-daemon")

        resp = await oc_client.post(
            f"/api/requests/tasks/{task['id']}/complete",
            json={
                "result": "Analysis complete.",
                "result_structured": {"summary": "API Done", "score": 99},
                "result_summary": "API Done",
                "validation_status": "valid",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "completed"
        assert data["validation_status"] == "valid"

    @pytest.mark.asyncio
    async def test_complete_valid_status_without_structured_result_rejected(
        self, oc_client, oc_repo, oc_org, oc_request, db_pool
    ):
        """When contract exists and status=valid, result_structured is required."""
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Missing Structured",
            org_id=str(oc_org["id"]),
            output_contract=_SIMPLE_CONTRACT,
        )
        await oc_repo.claim_task(str(task["id"]), "test-daemon")

        resp = await oc_client.post(
            f"/api/requests/tasks/{task['id']}/complete",
            json={
                "result": "text only",
                "validation_status": "valid",
                # result_structured missing
            },
        )
        assert resp.status_code == 422
        assert "result_structured" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_complete_schema_mismatch_rejected(
        self, oc_client, oc_repo, oc_org, oc_request, db_pool
    ):
        """result_structured must validate against the task's contract schema."""
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Schema Mismatch",
            org_id=str(oc_org["id"]),
            output_contract=_SIMPLE_CONTRACT,
        )
        await oc_repo.claim_task(str(task["id"]), "test-daemon")

        resp = await oc_client.post(
            f"/api/requests/tasks/{task['id']}/complete",
            json={
                "result": "text",
                "result_structured": {"summary": "ok", "score": "not_an_int"},
                "validation_status": "valid",
            },
        )
        assert resp.status_code == 422
        assert "schema validation" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_complete_fallback_allows_no_structured_result(
        self, oc_client, oc_repo, oc_org, oc_request, db_pool
    ):
        """fallback_used status should accept completion without result_structured."""
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Fallback Complete",
            org_id=str(oc_org["id"]),
            output_contract=_SIMPLE_CONTRACT,
        )
        await oc_repo.claim_task(str(task["id"]), "test-daemon")

        resp = await oc_client.post(
            f"/api/requests/tasks/{task['id']}/complete",
            json={
                "result": "text fallback",
                "validation_status": "fallback_used",
                "validation_errors": ["No structured output found"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["validation_status"] == "fallback_used"

    @pytest.mark.asyncio
    async def test_complete_legacy_task_without_contract(
        self, oc_client, oc_repo, oc_org, oc_request, db_pool
    ):
        """Tasks without contract accept plain result (backward compat)."""
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Legacy Complete",
            org_id=str(oc_org["id"]),
        )
        await oc_repo.claim_task(str(task["id"]), "test-daemon")

        resp = await oc_client.post(
            f"/api/requests/tasks/{task['id']}/complete",
            json={"result": "legacy text output"},
        )
        assert resp.status_code == 200
        assert resp.json()["validation_status"] == "not_applicable"

    @pytest.mark.asyncio
    async def test_complete_invalid_validation_status_rejected(
        self, oc_client, oc_repo, oc_org, oc_request, db_pool
    ):
        """API should reject unknown validation_status values."""
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Bad Status",
            org_id=str(oc_org["id"]),
        )
        await oc_repo.claim_task(str(task["id"]), "test-daemon")

        resp = await oc_client.post(
            f"/api/requests/tasks/{task['id']}/complete",
            json={
                "result": "text",
                "validation_status": "made_up_status",
            },
        )
        assert resp.status_code == 422


class TestApiRetrieveStructuredResults:
    """Test GET endpoints return structured output fields."""

    @pytest.mark.asyncio
    async def test_get_request_detail_includes_structured_task_fields(
        self, oc_client, oc_repo, oc_org, oc_request, db_pool
    ):
        task = await oc_repo.create_task(
            request_id=str(oc_request["id"]),
            title="Detail Task",
            org_id=str(oc_org["id"]),
            output_contract=_SIMPLE_CONTRACT,
        )
        await oc_repo.claim_task(str(task["id"]), "d1")
        await oc_repo.complete_task(
            str(task["id"]),
            "text",
            org_id=str(oc_org["id"]),
            result_structured={"summary": "detail", "score": 7},
            result_summary="detail",
            validation_status="valid",
        )

        resp = await oc_client.get(f"/api/requests/{oc_request['id']}")
        assert resp.status_code == 200
        data = resp.json()
        tasks = data.get("tasks", [])
        matching = [t for t in tasks if str(t["id"]) == str(task["id"])]
        assert len(matching) == 1

        t = matching[0]
        assert t["validation_status"] == "valid"
        assert t["result_summary"] == "detail"

        structured = t["result_structured"]
        if isinstance(structured, str):
            structured = json.loads(structured)
        assert structured["score"] == 7


# ============================================================================
# 5. END-TO-END LIFECYCLE TEST
# ============================================================================


class TestEndToEndLifecycle:
    """Full lifecycle: create request → create task with schema → claim → complete
    with structured output → verify stored and retrievable."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_with_structured_output(
        self, oc_client, oc_repo, oc_org, oc_request, db_pool
    ):
        await _create_active_agent_definition(db_pool, oc_org["id"], "code")

        org_id = str(oc_org["id"])
        req_id = str(oc_request["id"])

        # Step 1: Create task with output contract via API
        schema = {
            "type": "object",
            "properties": {
                "analysis": {"type": "string"},
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "summary": {"type": "string"},
            },
            "required": ["analysis", "findings", "confidence"],
        }
        contract = {
            "json_schema": schema,
            "on_failure": "retry_then_fallback",
            "max_retries": 1,
        }

        resp = await oc_client.post(
            f"/api/requests/{req_id}/tasks",
            json={
                "title": "E2E Analysis Task",
                "agent_type": "code",
                "description": "Perform analysis",
                "output_contract": contract,
                "sequence_order": 0,
            },
        )
        assert resp.status_code == 200, resp.text
        task_id = str(resp.json()["id"])

        # Step 2: Claim the task (via repo since that's what daemon does)
        claimed = await oc_repo.claim_task(task_id, "test-daemon-e2e")
        assert claimed is not None

        # Step 3: Complete with structured output via API
        structured_result = {
            "analysis": "The system is well-designed",
            "findings": ["Finding A", "Finding B", "Finding C"],
            "confidence": 0.92,
            "summary": "Well-designed system with 3 findings",
        }
        resp = await oc_client.post(
            f"/api/requests/tasks/{task_id}/complete",
            json={
                "result": "Full text of the analysis goes here.",
                "result_structured": structured_result,
                "result_summary": "Well-designed system with 3 findings",
                "validation_status": "valid",
            },
        )
        assert resp.status_code == 200, resp.text
        completed = resp.json()
        assert completed["status"] == "completed"
        assert completed["validation_status"] == "valid"

        # Step 4: Verify retrieval via GET request detail
        resp = await oc_client.get(f"/api/requests/{req_id}")
        assert resp.status_code == 200
        detail = resp.json()
        tasks = detail.get("tasks", [])
        e2e_task = next(t for t in tasks if str(t["id"]) == task_id)

        assert e2e_task["validation_status"] == "valid"
        assert e2e_task["result_summary"] == "Well-designed system with 3 findings"

        structured = e2e_task["result_structured"]
        if isinstance(structured, str):
            structured = json.loads(structured)
        assert structured["confidence"] == 0.92
        assert len(structured["findings"]) == 3

        # Step 5: Verify via direct DB retrieval
        db_task = await oc_repo.get_task(task_id)
        assert db_task["validation_status"] == "valid"
        stored = db_task["result_structured"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        assert stored["analysis"] == "The system is well-designed"

    @pytest.mark.asyncio
    async def test_mixed_structured_and_unstructured_tasks(
        self, oc_client, oc_repo, oc_org, oc_request, db_pool
    ):
        """A request with both legacy text tasks and structured-contract tasks."""
        await _create_active_agent_definition(db_pool, oc_org["id"], "code")

        org_id = str(oc_org["id"])
        req_id = str(oc_request["id"])

        # Task 1: Legacy (no contract)
        t1 = await oc_repo.create_task(
            request_id=req_id,
            title="Legacy Text Task",
            org_id=org_id,
            sequence_order=0,
        )
        await oc_repo.claim_task(str(t1["id"]), "d1")
        await oc_repo.complete_task(str(t1["id"]), "Plain text result")

        # Task 2: With contract
        t2 = await oc_repo.create_task(
            request_id=req_id,
            title="Structured Task",
            org_id=org_id,
            output_contract=_SIMPLE_CONTRACT,
            sequence_order=1,
        )
        await oc_repo.claim_task(str(t2["id"]), "d2")
        await oc_repo.complete_task(
            str(t2["id"]),
            "Text plus structured",
            result_structured={"summary": "Mixed", "score": 50},
            result_summary="Mixed",
            validation_status="valid",
        )

        # Retrieve the full request
        detail = await oc_repo.get_request_with_tasks(req_id, org_id)
        assert detail is not None
        assert len(detail["tasks"]) == 2

        # Verify each task has appropriate fields
        tasks_by_title = {t["title"]: t for t in detail["tasks"]}

        legacy = tasks_by_title["Legacy Text Task"]
        assert legacy["validation_status"] == "not_applicable"
        assert legacy["result_structured"] is None
        assert legacy["result"] == "Plain text result"

        structured = tasks_by_title["Structured Task"]
        assert structured["validation_status"] == "valid"
        result_data = structured["result_structured"]
        if isinstance(result_data, str):
            result_data = json.loads(result_data)
        assert result_data["score"] == 50

    @pytest.mark.asyncio
    async def test_subtask_with_contract(
        self, oc_client, oc_repo, oc_org, oc_request, db_pool
    ):
        """Sub-tasks can also have output contracts."""
        org_id = str(oc_org["id"])
        req_id = str(oc_request["id"])

        parent = await oc_repo.create_task(
            request_id=req_id,
            title="Parent Task",
            org_id=org_id,
            sequence_order=0,
        )

        child = await oc_repo.create_task(
            request_id=req_id,
            title="Child Task",
            org_id=org_id,
            parent_task_id=str(parent["id"]),
            output_contract=_SIMPLE_CONTRACT,
        )
        assert child["parent_task_id"] == parent["id"]
        assert child["output_contract"] is not None

        # Complete the child with structured output
        await oc_repo.claim_task(str(child["id"]), "d1")
        completed = await oc_repo.complete_task(
            str(child["id"]),
            "child result",
            result_structured={"summary": "child done", "score": 10},
            validation_status="valid",
        )
        assert completed["validation_status"] == "valid"

    @pytest.mark.asyncio
    async def test_sequence_gating_works_with_contracted_tasks(
        self, oc_repo, oc_org, oc_request
    ):
        """Tasks with contracts still respect sequence_order gating."""
        org_id = str(oc_org["id"])
        req_id = str(oc_request["id"])

        t0 = await oc_repo.create_task(
            request_id=req_id,
            title="Seq0 Contracted",
            org_id=org_id,
            output_contract=_SIMPLE_CONTRACT,
            sequence_order=0,
        )
        t1 = await oc_repo.create_task(
            request_id=req_id,
            title="Seq1 Contracted",
            org_id=org_id,
            output_contract=_SIMPLE_CONTRACT,
            sequence_order=1,
        )

        # t1 should not be pending yet
        pending = await oc_repo.list_pending_tasks(org_id)
        pending_ids = [str(t["id"]) for t in pending["items"]]
        assert str(t0["id"]) in pending_ids
        assert str(t1["id"]) not in pending_ids

        # Complete t0
        await oc_repo.claim_task(str(t0["id"]), "d1")
        await oc_repo.complete_task(
            str(t0["id"]),
            "done",
            result_structured={"summary": "done", "score": 1},
            validation_status="valid",
        )

        # Now t1 should be pending
        pending = await oc_repo.list_pending_tasks(org_id)
        pending_ids = [str(t["id"]) for t in pending["items"]]
        assert str(t1["id"]) in pending_ids
