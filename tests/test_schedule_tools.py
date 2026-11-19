"""Tests for MCP tools in src/lucent/tools/schedules.py.

Covers: create_schedule, list_schedules, toggle_schedule, get_schedule_details.
Tests auth context enforcement, validation, JSON serialization, and error handling.
"""

import json
from datetime import datetime

import pytest
import pytest_asyncio
from mcp.server import MCPServer as FastMCP

from lucent.auth import set_current_user
from lucent.db.definitions import DefinitionRepository
from lucent.db.schedules import ScheduleRepository
from lucent.tools.schedules import FALLBACK_WORKFLOW_AGENT_TYPES, register_schedule_tools

_GATE_CONTRACT = {
    "json_schema": {
        "type": "object",
        "required": ["should_act"],
        "properties": {"should_act": {"type": "boolean"}},
    },
    "gate": {"path": "should_act", "stop_value": False, "action": "stop_flow"},
}

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def mcp(db_pool):
    """Create a FastMCP instance with schedule tools registered."""
    m = FastMCP("test")
    register_schedule_tools(m)
    return m


@pytest_asyncio.fixture
async def auth_user(test_user):
    """Set auth context to the test user."""
    set_current_user(
        {
            "id": test_user["id"],
            "organization_id": test_user["organization_id"],
            "role": "member",
            "display_name": "Test User",
            "email": "test@test.com",
        }
    )
    yield test_user
    set_current_user(None)


@pytest_asyncio.fixture
async def schedule_repo(db_pool):
    return ScheduleRepository(db_pool)


@pytest_asyncio.fixture(autouse=True)
async def cleanup_schedules(db_pool, test_organization):
    """Clean up schedule data after each test."""
    yield
    org_id = test_organization["id"]
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM schedules WHERE organization_id = $1", org_id)


async def _call(mcp, tool_name: str, args: dict | None = None) -> dict | list:
    """Call an MCP tool and parse the JSON response."""
    result = await mcp._tool_manager.call_tool(tool_name, args or {}, None)
    return json.loads(result)


# ============================================================================
# create_schedule
# ============================================================================


class TestCreateSchedule:
    @pytest.mark.asyncio
    async def test_create_once(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_schedule",
            {"title": "One-time job", "schedule_type": "once"},
        )
        assert "id" in result
        assert result["title"] == "One-time job"

    @pytest.mark.asyncio
    async def test_create_interval(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_schedule",
            {
                "title": "Hourly job",
                "schedule_type": "interval",
                "interval_seconds": 3600,
            },
        )
        assert "id" in result

    @pytest.mark.asyncio
    async def test_create_cron(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_schedule",
            {
                "title": "Weekly job",
                "schedule_type": "cron",
                "cron_expression": "0 9 * * 1",
            },
        )
        assert "id" in result

    @pytest.mark.asyncio
    async def test_invalid_schedule_type(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_schedule",
            {"title": "Bad Type", "schedule_type": "hourly"},
        )
        assert "error" in result
        assert "schedule_type" in result["error"]

    @pytest.mark.asyncio
    async def test_cron_without_expression(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_schedule",
            {"title": "Missing Cron", "schedule_type": "cron"},
        )
        assert "error" in result
        assert "cron_expression" in result["error"]

    @pytest.mark.asyncio
    async def test_interval_too_small(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_schedule",
            {
                "title": "Too Fast",
                "schedule_type": "interval",
                "interval_seconds": 10,
            },
        )
        assert "error" in result
        assert "60" in result["error"]

    @pytest.mark.asyncio
    async def test_interval_missing(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_schedule",
            {"title": "No Interval", "schedule_type": "interval"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_invalid_priority(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_schedule",
            {"title": "Bad Priority", "schedule_type": "once", "priority": "super"},
        )
        assert "error" in result
        assert "priority" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_cron_format(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_schedule",
            {
                "title": "Bad Cron",
                "schedule_type": "cron",
                "cron_expression": "not a cron",
            },
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_with_model(self, mcp, auth_user, monkeypatch):
        """Known model from hardcoded registry is accepted in strict mode."""
        from lucent import model_registry
        from lucent.model_registry import MODELS

        monkeypatch.setattr(model_registry, "_db_models", None)
        monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {m.id: m for m in MODELS})
        result = await _call(
            mcp,
            "create_schedule",
            {
                "title": "With Model",
                "schedule_type": "once",
                "model": "claude-sonnet-4.6",
            },
        )
        assert "id" in result

    @pytest.mark.asyncio
    async def test_unknown_model_rejected(self, mcp, auth_user):
        """Unknown model ID is rejected with helpful error message."""
        result = await _call(
            mcp,
            "create_schedule",
            {
                "title": "Bad Model Schedule",
                "schedule_type": "once",
                "model": "totally-fake-model-xyz",
            },
        )
        assert "error" in result
        assert "Unknown model" in result["error"]

    @pytest.mark.asyncio
    async def test_no_auth(self, mcp, test_user):
        set_current_user(None)
        result = await _call(
            mcp,
            "create_schedule",
            {"title": "No Auth", "schedule_type": "once"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_with_max_runs(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_schedule",
            {"title": "Limited", "schedule_type": "once", "max_runs": 5},
        )
        assert "id" in result


# ============================================================================
# create_workflow
# ============================================================================


class TestCreateWorkflow:
    def test_fallback_agent_types_exclude_chat_only_composer(self):
        assert "workflow-composer" not in FALLBACK_WORKFLOW_AGENT_TYPES

    @pytest.mark.asyncio
    async def test_create_workflow_rejects_unknown_action_agent(self, mcp, auth_user, db_pool):
        repo = DefinitionRepository(db_pool)
        await repo.create_agent(
            name="code",
            description="Code agent",
            content="# Code",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            status="active",
        )

        result = await _call(
            mcp,
            "create_workflow",
            {
                "title": "Bad workflow agent",
                "trigger_type": "manual",
                "actions_json": json.dumps(
                    [
                        {
                            "action_type": "task",
                            "title": "Do generic work",
                            "agent_type": "general-purpose",
                        }
                    ]
                ),
            },
        )

        assert result["error"] == "Unknown workflow action agent_type"
        assert result["invalid_agent_types"] == ["general-purpose"]

    @pytest.mark.asyncio
    async def test_create_workflow_rejects_script_action_without_template(
        self, mcp, auth_user, capable_organization
    ):
        """The MCP authoring surface refuses an unrunnable script action."""
        result = await _call(
            mcp,
            "create_workflow",
            {
                "title": "Script without template",
                "trigger_type": "manual",
                "actions_json": json.dumps(
                    [
                        {
                            "action_type": "task",
                            "title": "Compute the report",
                            "task_kind": "script",
                            "execution_config": {
                                "source": "inline",
                                "script": "print('{}')",
                            },
                        }
                    ]
                ),
            },
        )

        assert "sandbox_template_id" in result["error"]

    @pytest.mark.asyncio
    async def test_create_workflow_round_trips_gate_spec(
        self, mcp, auth_user, db_pool
    ):
        """A flow gate authored on a workflow action is stored byte-for-byte."""
        repo = DefinitionRepository(db_pool)
        await repo.create_agent(
            name="code",
            description="Code agent",
            content="# Code",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            status="active",
        )

        result = await _call(
            mcp,
            "create_workflow",
            {
                "title": "Gated workflow",
                "trigger_type": "manual",
                "actions_json": json.dumps(
                    [
                        {
                            "action_type": "task",
                            "title": "Decide whether to act",
                            "agent_type": "code",
                            "output_contract": _GATE_CONTRACT,
                        }
                    ]
                ),
            },
        )

        assert "error" not in result, result
        actions = result["actions"]
        if isinstance(actions, str):
            actions = json.loads(actions)
        assert actions[0]["output_contract"] == _GATE_CONTRACT

    @pytest.mark.asyncio
    async def test_create_workflow_rejects_malformed_gate(self, mcp, auth_user):
        malformed = {
            **_GATE_CONTRACT,
            "gate": {"path": "should_act", "stop_value": False, "action": "ignore"},
        }

        result = await _call(
            mcp,
            "create_workflow",
            {
                "title": "Bad gate workflow",
                "trigger_type": "manual",
                "actions_json": json.dumps(
                    [
                        {
                            "action_type": "task",
                            "title": "Decide",
                            "agent_type": "code",
                            "output_contract": malformed,
                        }
                    ]
                ),
            },
        )

        assert "Invalid output_contract.gate.action 'ignore'" in result["error"]
        assert "Must be one of: stop_flow" in result["error"]

    @pytest.mark.asyncio
    async def test_create_workflow_rejects_server_function_actions(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_workflow",
            {
                "title": "Bad server workflow",
                "trigger_type": "manual",
                "actions_json": json.dumps(
                    [
                        {
                            "action_type": "server_function",
                            "title": "Run internal thing",
                            "function": "release_stale_tasks",
                        }
                    ]
                ),
            },
        )

        assert "server_function actions are reserved" in result["error"]

    @pytest.mark.asyncio
    async def test_create_workflow_normalizes_action_aliases(self, mcp, auth_user, db_pool):
        repo = DefinitionRepository(db_pool)
        await repo.create_agent(
            name="code",
            description="Code agent",
            content="# Code",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            status="active",
        )

        result = await _call(
            mcp,
            "create_workflow",
            {
                "title": "Alias workflow",
                "trigger_type": "manual",
                "actions_json": json.dumps(
                    [
                        {
                            "type": "task",
                            "title": "Record output",
                            "agent_type": "code",
                            "instructions": "Record a result.",
                        }
                    ]
                ),
            },
        )

        assert result["id"]
        actions = result["actions"]
        if isinstance(actions, str):
            actions = json.loads(actions)
        assert actions[0]["action_type"] == "task"
        assert actions[0]["description"] == "Record a result."
        assert actions[0]["sequence_order"] == 0
        assert "type" not in actions[0]
        assert "instructions" not in actions[0]


# ============================================================================
# list_schedules
# ============================================================================


class TestListSchedules:
    @pytest.mark.asyncio
    async def test_empty_list(self, mcp, auth_user):
        result = await _call(mcp, "list_schedules")
        assert isinstance(result, dict)
        assert result["items"] == []
        assert result["total_count"] == 0

    @pytest.mark.asyncio
    async def test_returns_schedules(self, mcp, auth_user, schedule_repo, test_organization):
        await schedule_repo.create_schedule(
            title="Listed Schedule",
            org_id=str(test_organization["id"]),
            schedule_type="once",
            created_by=str(auth_user["id"]),
        )
        result = await _call(mcp, "list_schedules")
        assert isinstance(result, dict)
        items = result["items"]
        assert len(items) >= 1
        titles = [s["title"] for s in items]
        assert "Listed Schedule" in titles

    @pytest.mark.asyncio
    async def test_filter_by_status(self, mcp, auth_user, schedule_repo, test_organization):
        await schedule_repo.create_schedule(
            title="Active Schedule",
            org_id=str(test_organization["id"]),
            schedule_type="once",
        )
        result = await _call(mcp, "list_schedules", {"status": "active"})
        assert isinstance(result, dict)
        assert isinstance(result["items"], list)

    @pytest.mark.asyncio
    async def test_enabled_only(self, mcp, auth_user, schedule_repo, test_organization):
        await schedule_repo.create_schedule(
            title="Enabled Schedule",
            org_id=str(test_organization["id"]),
            schedule_type="once",
        )
        result = await _call(mcp, "list_schedules", {"enabled_only": True})
        assert isinstance(result, dict)
        assert isinstance(result["items"], list)

    @pytest.mark.asyncio
    async def test_no_auth(self, mcp, test_user):
        set_current_user(None)
        result = await _call(mcp, "list_schedules")
        assert "error" in result


# ============================================================================
# toggle_schedule
# ============================================================================


class TestToggleSchedule:
    @pytest_asyncio.fixture
    async def schedule_id(self, schedule_repo, test_organization, auth_user):
        s = await schedule_repo.create_schedule(
            title="Toggle Me",
            org_id=str(test_organization["id"]),
            schedule_type="once",
            created_by=str(auth_user["id"]),
        )
        return str(s["id"])

    @pytest.mark.asyncio
    async def test_disable(self, mcp, auth_user, schedule_id):
        result = await _call(
            mcp,
            "toggle_schedule",
            {"schedule_id": schedule_id, "enabled": False},
        )
        assert "id" in result

    @pytest.mark.asyncio
    async def test_enable(self, mcp, auth_user, schedule_id):
        # Disable first, then re-enable
        await _call(mcp, "toggle_schedule", {"schedule_id": schedule_id, "enabled": False})
        result = await _call(
            mcp,
            "toggle_schedule",
            {"schedule_id": schedule_id, "enabled": True},
        )
        assert "id" in result

    @pytest.mark.asyncio
    async def test_not_found(self, mcp, auth_user):
        result = await _call(
            mcp,
            "toggle_schedule",
            {
                "schedule_id": "00000000-0000-0000-0000-000000000000",
                "enabled": False,
            },
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth(self, mcp, test_user):
        set_current_user(None)
        result = await _call(
            mcp,
            "toggle_schedule",
            {
                "schedule_id": "00000000-0000-0000-0000-000000000000",
                "enabled": False,
            },
        )
        assert "error" in result


# ============================================================================
# get_schedule_details
# ============================================================================


class TestGetScheduleDetails:
    @pytest_asyncio.fixture
    async def schedule_id(self, schedule_repo, test_organization, auth_user):
        s = await schedule_repo.create_schedule(
            title="Details Test",
            org_id=str(test_organization["id"]),
            schedule_type="interval",
            interval_seconds=3600,
            description="Test schedule with details",
            created_by=str(auth_user["id"]),
        )
        return str(s["id"])

    @pytest.mark.asyncio
    async def test_get_details(self, mcp, auth_user, schedule_id):
        result = await _call(mcp, "get_schedule_details", {"schedule_id": schedule_id})
        assert result["title"] == "Details Test"

    @pytest.mark.asyncio
    async def test_not_found(self, mcp, auth_user):
        result = await _call(
            mcp,
            "get_schedule_details",
            {"schedule_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth(self, mcp, test_user):
        set_current_user(None)
        result = await _call(
            mcp,
            "get_schedule_details",
            {"schedule_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result


# ============================================================================
# create_workflow decision steps
# ============================================================================


class TestCreateWorkflowDecisionSteps:
    """Decision-step actions on create_workflow (agent_type 'decision-step')."""

    def test_fallback_agent_types_include_decision_step(self):
        assert "decision-step" in FALLBACK_WORKFLOW_AGENT_TYPES

    def _decision_agent_sql(self, org_id, user_id):
        return (
            "INSERT INTO agent_definitions "
            "(name, organization_id, content, status, owner_user_id) "
            "VALUES ('decision-step', $1, 'test decision definition', "
            "'active', $2) "
            "ON CONFLICT (name, organization_id) DO UPDATE SET "
            "status = 'active', owner_user_id = EXCLUDED.owner_user_id",
            (str(org_id), str(user_id)),
        )

    @pytest.mark.asyncio
    async def test_create_workflow_accepts_decision_step(self, mcp, auth_user, db_pool):
        sql, params = self._decision_agent_sql(auth_user["organization_id"], auth_user["id"])
        async with db_pool.acquire() as conn:
            await conn.execute(sql, *params)

        result = await _call(
            mcp,
            "create_workflow",
            {
                "title": "Decision workflow",
                "trigger_type": "manual",
                "actions_json": json.dumps(
                    [
                        {
                            "action_type": "task",
                            "title": "Gate the audit",
                            "agent_type": "decision-step",
                            "sequence_order": 1,
                            "decision_config": {
                                "questions": [
                                    {
                                        "name": "report_complete",
                                        "type": "noul",
                                        "instructions": "The audit is complete.",
                                    }
                                ],
                                "threshold_policy": {
                                    "questions": {"report_complete": {"min": 0.6}},
                                    "on_fail": "fail_task",
                                },
                            },
                        }
                    ]
                ),
            },
        )
        assert "id" in result, result
        actions = result["actions"]
        if isinstance(actions, str):
            actions = json.loads(actions)
        assert actions[0]["agent_type"] == "decision-step"
        assert actions[0]["decision_config"]["questions"][0]["name"] == ("report_complete")

    @pytest.mark.asyncio
    async def test_create_workflow_rejects_bad_decision_config(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_workflow",
            {
                "title": "Bad decision workflow",
                "trigger_type": "manual",
                "actions_json": json.dumps(
                    [
                        {
                            "action_type": "task",
                            "title": "Gate it",
                            "agent_type": "decision-step",
                            "decision_config": {
                                "questions": [
                                    {
                                        "name": "q",
                                        "type": "noul",
                                        "instructions": "check",
                                    }
                                ],
                                "threshold_policy": {"questions": {"missing": {"min": 0.5}}},
                            },
                        }
                    ]
                ),
            },
        )
        assert "error" in result
        assert "unknown question" in result["error"].lower()


# ============================================================================
# Workflow detail / update / delete
# ============================================================================


@pytest_asyncio.fixture
async def owner_auth(auth_user):
    """Owner context: exercises the admin branch of the detail/update paths."""
    set_current_user(
        {
            "id": auth_user["id"],
            "organization_id": auth_user["organization_id"],
            "role": "owner",
            "display_name": "Owner",
            "email": "owner@test.com",
        }
    )
    yield auth_user
    set_current_user(None)


@pytest_asyncio.fixture
async def workflow_id(schedule_repo, test_organization, auth_user):
    wf = await schedule_repo.create_schedule(
        title="Daily digest",
        org_id=str(test_organization["id"]),
        schedule_type="cron",
        cron_expression="0 9 * * *",
        description="Original description",
        priority="high",
        review_instructions="Check output",
        created_by=str(auth_user["id"]),
        actions=[
            {
                "action_type": "task",
                "title": "Summarize",
                "agent_type": "code",
                "description": "Summarize the day",
                "sequence_order": 0,
            }
        ],
        request_template={"title": "Digest", "title_prefix": "[Scheduled]"},
    )
    return str(wf["id"])


class TestGetWorkflowDetails:
    @pytest.mark.asyncio
    async def test_details_returns_fields_and_run_history(
        self, mcp, owner_auth, workflow_id, schedule_repo, test_organization
    ):
        """Regression: the admin branch used to raise TenantScopeError because
        get_schedule_with_runs called list_runs() without org_id. It must now
        return real workflow fields plus a structured run list."""
        org = str(test_organization["id"])
        await schedule_repo.mark_schedule_run(workflow_id, force=True, org_id=org)

        result = await _call(mcp, "get_workflow_details", {"workflow_id": workflow_id})

        assert "error" not in result, result
        assert result["title"] == "Daily digest"
        assert result["priority"] == "high"
        assert result["schedule_type"] == "cron"
        assert isinstance(result["actions"], list)
        assert result["actions"][0]["title"] == "Summarize"
        assert isinstance(result["runs"], list)
        assert len(result["runs"]) == 1
        assert result["runs"][0]["schedule_id"] == workflow_id

    @pytest.mark.asyncio
    async def test_member_details_returns_structured_runs(
        self, mcp, auth_user, workflow_id, schedule_repo, test_organization
    ):
        org = str(test_organization["id"])
        await schedule_repo.mark_schedule_run(workflow_id, force=True, org_id=org)

        result = await _call(mcp, "get_schedule_details", {"schedule_id": workflow_id})

        assert "error" not in result, result
        assert isinstance(result["runs"], list)
        assert result["runs"][0]["schedule_id"] == workflow_id


class TestUpdateWorkflow:
    @pytest.mark.asyncio
    async def test_updates_only_supplied_fields(
        self, mcp, auth_user, workflow_id, schedule_repo, test_organization
    ):
        org = str(test_organization["id"])
        before = await schedule_repo.get_schedule_with_runs(
            workflow_id, org, created_by=str(auth_user["id"])
        )

        result = await _call(
            mcp,
            "update_workflow",
            {"workflow_id": workflow_id, "description": "Updated description"},
        )

        assert "error" not in result, result
        assert result["description"] == "Updated description"
        for key in (
            "title",
            "priority",
            "enabled",
            "review_instructions",
            "schedule_type",
            "cron_expression",
            "interval_seconds",
            "request_template",
            "actions",
            "trigger_config",
        ):
            assert result[key] == before[key], key
        # No schedule field changed, so next_run_at must not be touched.
        assert str(result["next_run_at"]) == str(before["next_run_at"])

    @pytest.mark.asyncio
    async def test_omitted_enabled_preserves_state(
        self, mcp, auth_user, workflow_id, schedule_repo, test_organization
    ):
        org = str(test_organization["id"])
        await schedule_repo.update_schedule(workflow_id, org, enabled=False)

        updated_description = await _call(
            mcp, "update_workflow", {"workflow_id": workflow_id, "description": "x"}
        )
        assert updated_description["enabled"] is False

        disabled = await _call(
            mcp, "update_workflow", {"workflow_id": workflow_id, "enabled": False}
        )
        assert disabled["enabled"] is False

    @pytest.mark.asyncio
    async def test_cron_change_recomputes_next_run_at(
        self, mcp, auth_user, workflow_id
    ):
        result = await _call(
            mcp,
            "update_workflow",
            {"workflow_id": workflow_id, "cron_expression": "30 14 * * *"},
        )

        assert "error" not in result, result
        assert result["cron_expression"] == "30 14 * * *"
        next_run = datetime.fromisoformat(result["next_run_at"])
        assert (next_run.hour, next_run.minute) == (14, 30)
        # Trigger metadata stays in step with the column.
        assert result["trigger_config"]["cron_expression"] == "30 14 * * *"

    @pytest.mark.asyncio
    async def test_actions_json_replaces_full_list(
        self, mcp, auth_user, db_pool, workflow_id, test_organization
    ):
        """A single-action edit is made by re-sending the full, edited list."""
        from lucent.db.definitions import DefinitionRepository

        await DefinitionRepository(db_pool).create_agent(
            name="code",
            description="Code agent",
            content="# Code",
            org_id=str(test_organization["id"]),
            created_by=str(auth_user["id"]),
            status="active",
        )
        result = await _call(
            mcp,
            "update_workflow",
            {
                "workflow_id": workflow_id,
                "actions_json": json.dumps(
                    [
                        {
                            "action_type": "task",
                            "title": "Summarize",
                            "agent_type": "code",
                            "description": "New action description",
                            "sequence_order": 0,
                        },
                        {
                            "action_type": "task",
                            "title": "Notify",
                            "agent_type": "code",
                            "description": "Send the digest",
                            "sequence_order": 1,
                        },
                    ]
                ),
            },
        )

        assert "error" not in result, result
        assert [a["title"] for a in result["actions"]] == ["Summarize", "Notify"]
        assert result["actions"][0]["description"] == "New action description"

    @pytest.mark.asyncio
    async def test_update_nonexistent_refused(self, mcp, auth_user):
        result = await _call(
            mcp,
            "update_workflow",
            {
                "workflow_id": "00000000-0000-0000-0000-000000000000",
                "title": "nope",
            },
        )
        assert result.get("error") == "Workflow not found"

    @pytest.mark.asyncio
    async def test_update_foreign_workflow_refused(
        self, mcp, auth_user, schedule_repo, test_organization
    ):
        # created_by NULL has no owner clearance, so a member cannot see it.
        foreign = await schedule_repo.create_schedule(
            title="Foreign workflow",
            org_id=str(test_organization["id"]),
            schedule_type="once",
        )
        foreign_id = str(foreign["id"])

        result = await _call(
            mcp, "update_workflow", {"workflow_id": foreign_id, "title": "hijacked"}
        )

        assert result.get("error") == "Workflow not found"
        still = await schedule_repo.get_schedule(
            foreign_id, str(test_organization["id"])
        )
        assert still["title"] == "Foreign workflow"


class TestDeleteWorkflow:
    @pytest.mark.asyncio
    async def test_delete_removes_workflow_and_run_history(
        self, mcp, auth_user, workflow_id, schedule_repo, test_organization, db_pool
    ):
        org = str(test_organization["id"])
        await schedule_repo.mark_schedule_run(workflow_id, force=True, org_id=org)
        assert (
            await schedule_repo.list_runs(workflow_id, org_id=org)
        )["total_count"] == 1

        result = await _call(mcp, "delete_workflow", {"workflow_id": workflow_id})

        assert result.get("deleted") is True, result
        assert result["title"] == "Daily digest"
        assert result["run_history_deleted"] == 1
        assert (
            await schedule_repo.get_schedule(
                workflow_id, org, created_by=str(auth_user["id"])
            )
            is None
        )
        async with db_pool.acquire() as conn:
            remaining = await conn.fetchval(
                "SELECT count(*) FROM schedule_runs WHERE schedule_id = $1::uuid",
                workflow_id,
            )
        assert remaining == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_refused(self, mcp, auth_user):
        result = await _call(
            mcp,
            "delete_workflow",
            {"workflow_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert result.get("error") == "Workflow not found"

    @pytest.mark.asyncio
    async def test_delete_foreign_workflow_refused(
        self, mcp, auth_user, schedule_repo, test_organization
    ):
        foreign = await schedule_repo.create_schedule(
            title="Foreign workflow",
            org_id=str(test_organization["id"]),
            schedule_type="once",
        )
        foreign_id = str(foreign["id"])

        result = await _call(mcp, "delete_workflow", {"workflow_id": foreign_id})

        assert result.get("error") == "Workflow not found"
        assert (
            await schedule_repo.get_schedule(foreign_id, str(test_organization["id"]))
        ) is not None
