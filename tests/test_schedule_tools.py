"""Tests for MCP tools in src/lucent/tools/schedules.py.

Covers: create_schedule, list_schedules, toggle_schedule, get_schedule_details.
Tests auth context enforcement, validation, JSON serialization, and error handling.
"""

import json

import pytest
import pytest_asyncio
from mcp.server.fastmcp import FastMCP

from lucent.auth import set_current_user
from lucent.db.schedules import ScheduleRepository
from lucent.tools.schedules import register_schedule_tools

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
    result = await mcp._tool_manager.call_tool(tool_name, args or {})
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
    async def schedule_id(self, schedule_repo, test_organization):
        s = await schedule_repo.create_schedule(
            title="Toggle Me",
            org_id=str(test_organization["id"]),
            schedule_type="once",
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
    async def schedule_id(self, schedule_repo, test_organization):
        s = await schedule_repo.create_schedule(
            title="Details Test",
            org_id=str(test_organization["id"]),
            schedule_type="interval",
            interval_seconds=3600,
            description="Test schedule with details",
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
