"""Tests for MCP tools in src/lucent/tools/requests.py.

Covers: create_request, create_task, log_task_event, link_task_memory,
get_request_details, list_pending_requests, list_pending_tasks.
Tests auth context enforcement, JSON serialization, and error handling.
"""

import json

import pytest
import pytest_asyncio
from mcp.server.fastmcp import FastMCP

from lucent.auth import set_current_user
from lucent.db.requests import RequestRepository
from lucent.tools.requests import register_request_tools

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def mcp(db_pool):
    """Create a FastMCP instance with request tools registered."""
    m = FastMCP("test")
    register_request_tools(m)
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
async def repo(db_pool):
    return RequestRepository(db_pool)


@pytest_asyncio.fixture(autouse=True)
async def cleanup_requests(db_pool, test_organization):
    """Clean up request tracking data after each test."""
    yield
    org_id = test_organization["id"]
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM requests WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM agent_definitions WHERE organization_id = $1", org_id)


async def _call(mcp, tool_name: str, args: dict | None = None) -> dict | list:
    """Call an MCP tool and parse the JSON response."""
    result = await mcp._tool_manager.call_tool(tool_name, args or {})
    return json.loads(result)


# ============================================================================
# create_request
# ============================================================================


class TestCreateRequest:
    @pytest.mark.asyncio
    async def test_create_basic(self, mcp, auth_user):
        result = await _call(mcp, "create_request", {"title": "Test Request"})
        assert "id" in result
        assert result["title"] == "Test Request"
        assert result["status"] == "pending"

    @pytest.mark.asyncio
    async def test_create_with_all_fields(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_request",
            {
                "title": "Full Request",
                "description": "A detailed description",
                "source": "user",
                "priority": "high",
            },
        )
        assert result["title"] == "Full Request"
        assert result["status"] == "pending"

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp, test_user):
        """Without auth context, should return org error."""
        set_current_user(None)
        result = await _call(mcp, "create_request", {"title": "No Auth"})
        assert "error" in result


# ============================================================================
# create_task
# ============================================================================


class TestCreateTask:
    @pytest_asyncio.fixture
    async def request_id(self, mcp, auth_user):
        result = await _call(mcp, "create_request", {"title": "Parent Request"})
        return result["id"]

    @pytest.mark.asyncio
    async def test_create_basic_task(self, mcp, auth_user, request_id, db_pool):
        # Need an active agent definition for validation
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_definitions (name, organization_id, content, status, owner_user_id)
                   VALUES ($1, $2, $3, 'active', $4)
                   ON CONFLICT (name, organization_id) DO UPDATE SET status = 'active', owner_user_id = EXCLUDED.owner_user_id""",
                "code",
                auth_user["organization_id"],
                "test definition",
                auth_user["id"],
            )

        result = await _call(
            mcp,
            "create_task",
            {"request_id": request_id, "title": "Test Task", "agent_type": "code"},
        )
        assert "id" in result
        assert result["title"] == "Test Task"
        assert result["agent_type"] == "code"
        assert result["status"] == "pending"

    @pytest.mark.asyncio
    async def test_invalid_agent_type(self, mcp, auth_user, request_id):
        result = await _call(
            mcp,
            "create_task",
            {
                "request_id": request_id,
                "title": "Bad Agent",
                "agent_type": "nonexistent_agent_xyz",
            },
        )
        assert "error" in result
        assert "nonexistent_agent_xyz" in result["error"]

    @pytest.mark.asyncio
    async def test_with_model(self, mcp, auth_user, request_id, db_pool, monkeypatch):
        """Known model from hardcoded registry is accepted in strict mode."""
        from lucent import model_registry
        from lucent.model_registry import MODELS

        monkeypatch.setattr(model_registry, "_db_models", None)
        monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {m.id: m for m in MODELS})
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_definitions (name, organization_id, content, status, owner_user_id)
                   VALUES ($1, $2, $3, 'active', $4)
                   ON CONFLICT (name, organization_id) DO UPDATE SET status = 'active', owner_user_id = EXCLUDED.owner_user_id""",
                "code",
                auth_user["organization_id"],
                "test definition",
                auth_user["id"],
            )
        result = await _call(
            mcp,
            "create_task",
            {
                "request_id": request_id,
                "title": "With Model",
                "agent_type": "code",
                "model": "claude-sonnet-4.6",
            },
        )
        assert "id" in result
        assert result["model"] == "claude-sonnet-4.6"

    @pytest.mark.asyncio
    async def test_unknown_model_rejected(self, mcp, auth_user, request_id, db_pool):
        """Unknown model ID is rejected with helpful error message."""
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_definitions (name, organization_id, content, status, owner_user_id)
                   VALUES ($1, $2, $3, 'active', $4)
                   ON CONFLICT (name, organization_id) DO UPDATE SET status = 'active', owner_user_id = EXCLUDED.owner_user_id""",
                "code",
                auth_user["organization_id"],
                "test definition",
                auth_user["id"],
            )
        result = await _call(
            mcp,
            "create_task",
            {
                "request_id": request_id,
                "title": "Bad Model",
                "agent_type": "code",
                "model": "totally-fake-model-xyz",
            },
        )
        assert "error" in result
        assert "Unknown model" in result["error"]
        assert "list_available_models" in result["error"]
        set_current_user(None)
        result = await _call(
            mcp,
            "create_task",
            {"request_id": "fake-id", "title": "No Auth"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_negative_sequence_order_rejected(self, mcp, auth_user, request_id):
        result = await _call(
            mcp,
            "create_task",
            {
                "request_id": request_id,
                "title": "Negative Seq",
                "sequence_order": -1,
            },
        )
        assert "error" in result
        assert "sequence_order" in result["error"]

    @pytest.mark.asyncio
    async def test_sequence_order_zero_accepted(self, mcp, auth_user, request_id, db_pool):
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_definitions (name, organization_id, content, status, owner_user_id)
                   VALUES ($1, $2, $3, 'active', $4)
                   ON CONFLICT (name, organization_id) DO UPDATE SET status = 'active', owner_user_id = EXCLUDED.owner_user_id""",
                "code",
                auth_user["organization_id"],
                "test definition",
                auth_user["id"],
            )
        result = await _call(
            mcp,
            "create_task",
            {
                "request_id": request_id,
                "title": "Zero Seq",
                "agent_type": "code",
                "sequence_order": 0,
            },
        )
        assert "id" in result

    @pytest.mark.asyncio
    async def test_create_task_inherits_requesting_user_id(self, mcp, auth_user, request_id, db_pool):
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_definitions
                   (name, organization_id, content, status, owner_user_id)
                   VALUES ($1, $2, $3, 'active', $4)
                   ON CONFLICT (name, organization_id) DO UPDATE SET status = 'active', owner_user_id = EXCLUDED.owner_user_id""",
                "code",
                auth_user["organization_id"],
                "test definition",
                auth_user["id"],
            )
        result = await _call(
            mcp,
            "create_task",
            {"request_id": request_id, "title": "Trace requester", "agent_type": "code"},
        )
        assert "id" in result
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT requesting_user_id FROM tasks WHERE id = $1",
                result["id"],
            )
        assert row is not None
        assert row["requesting_user_id"] == auth_user["id"]


# ============================================================================
# log_task_event
# ============================================================================


class TestLogTaskEvent:
    @pytest_asyncio.fixture
    async def task_id(self, mcp, auth_user, repo, test_organization, db_pool):
        req = await repo.create_request(
            title="Event Test Request",
            org_id=str(test_organization["id"]),
        )
        task = await repo.create_task(
            request_id=str(req["id"]),
            title="Event Test Task",
            org_id=str(test_organization["id"]),
        )
        return str(task["id"])

    @pytest.mark.asyncio
    async def test_log_event(self, mcp, auth_user, task_id):
        result = await _call(
            mcp,
            "log_task_event",
            {"task_id": task_id, "event_type": "progress", "detail": "50% done"},
        )
        assert "id" in result
        assert result["event_type"] == "progress"

    @pytest.mark.asyncio
    async def test_log_event_minimal(self, mcp, auth_user, task_id):
        result = await _call(
            mcp,
            "log_task_event",
            {"task_id": task_id, "event_type": "info"},
        )
        assert result["event_type"] == "info"


# ============================================================================
# link_task_memory
# ============================================================================


class TestLinkTaskMemory:
    @pytest_asyncio.fixture
    async def task_id(self, mcp, auth_user, repo, test_organization):
        req = await repo.create_request(
            title="Link Test Request",
            org_id=str(test_organization["id"]),
        )
        task = await repo.create_task(
            request_id=str(req["id"]),
            title="Link Test Task",
            org_id=str(test_organization["id"]),
        )
        return str(task["id"])

    @pytest.mark.asyncio
    async def test_link_memory(self, mcp, auth_user, task_id, test_memory):
        result = await _call(
            mcp,
            "link_task_memory",
            {
                "task_id": task_id,
                "memory_id": str(test_memory["id"]),
                "relation": "created",
            },
        )
        assert result["status"] == "linked"
        assert result["task_id"] == task_id
        assert result["memory_id"] == str(test_memory["id"])

    @pytest.mark.asyncio
    async def test_link_with_read_relation(self, mcp, auth_user, task_id, test_memory):
        result = await _call(
            mcp,
            "link_task_memory",
            {
                "task_id": task_id,
                "memory_id": str(test_memory["id"]),
                "relation": "read",
            },
        )
        assert result["status"] == "linked"


# ============================================================================
# get_request_details
# ============================================================================


class TestGetRequestDetails:
    @pytest_asyncio.fixture
    async def request_id(self, repo, test_organization, auth_user):
        req = await repo.create_request(
            title="Details Test",
            org_id=str(test_organization["id"]),
            description="Testing details endpoint",
        )
        return str(req["id"])

    @pytest.mark.asyncio
    async def test_get_details(self, mcp, auth_user, request_id):
        result = await _call(mcp, "get_request_details", {"request_id": request_id})
        assert result["title"] == "Details Test"

    @pytest.mark.asyncio
    async def test_not_found(self, mcp, auth_user):
        result = await _call(
            mcp,
            "get_request_details",
            {"request_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth(self, mcp, test_user):
        set_current_user(None)
        result = await _call(
            mcp,
            "get_request_details",
            {"request_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result


# ============================================================================
# list_pending_requests
# ============================================================================


class TestListPendingRequests:
    @pytest.mark.asyncio
    async def test_empty_list(self, mcp, auth_user):
        result = await _call(mcp, "list_pending_requests")
        assert isinstance(result, dict)
        assert result["items"] == []

    @pytest.mark.asyncio
    async def test_returns_pending(self, mcp, auth_user, repo, test_organization):
        await repo.create_request(
            title="Pending One",
            org_id=str(test_organization["id"]),
        )
        result = await _call(mcp, "list_pending_requests")
        assert isinstance(result, dict)
        titles = [r["title"] for r in result["items"]]
        assert "Pending One" in titles

    @pytest.mark.asyncio
    async def test_no_auth(self, mcp, test_user):
        set_current_user(None)
        result = await _call(mcp, "list_pending_requests")
        assert "error" in result


# ============================================================================
# list_available_models
# ============================================================================


class TestListAvailableModels:
    @pytest.mark.asyncio
    async def test_returns_models(self, mcp, auth_user):
        result = await _call(mcp, "list_available_models")
        assert "models" in result
        assert isinstance(result["models"], list)
        assert len(result["models"]) > 0

    @pytest.mark.asyncio
    async def test_model_fields(self, mcp, auth_user):
        result = await _call(mcp, "list_available_models")
        m = result["models"][0]
        for field in ("id", "name", "provider", "category", "supports_tools", "notes", "tags"):
            assert field in m

    @pytest.mark.asyncio
    async def test_category_filter(self, mcp, auth_user):
        result = await _call(mcp, "list_available_models", {"category": "fast"})
        assert all(m["category"] == "fast" for m in result["models"])

    @pytest.mark.asyncio
    async def test_unknown_category_returns_empty(self, mcp, auth_user):
        result = await _call(mcp, "list_available_models", {"category": "nonexistent"})
        assert result["models"] == []

    @pytest.mark.asyncio
    async def test_recommended_included_when_agent_type_given(self, mcp, auth_user):
        result = await _call(mcp, "list_available_models", {"agent_type": "code"})
        assert "recommended" in result
        assert isinstance(result["recommended"], str)
        assert len(result["recommended"]) > 0

    @pytest.mark.asyncio
    async def test_no_recommended_without_agent_type(self, mcp, auth_user):
        result = await _call(mcp, "list_available_models")
        assert "recommended" not in result


# ============================================================================
# list_pending_tasks
# ============================================================================


class TestListPendingTasks:
    @pytest.mark.asyncio
    async def test_empty_list(self, mcp, auth_user):
        result = await _call(mcp, "list_pending_tasks")
        assert isinstance(result, dict)
        assert result["items"] == []

    @pytest.mark.asyncio
    async def test_returns_tasks(self, mcp, auth_user, repo, test_organization):
        req = await repo.create_request(
            title="Task List Test",
            org_id=str(test_organization["id"]),
        )
        await repo.create_task(
            request_id=str(req["id"]),
            title="Queued Task",
            org_id=str(test_organization["id"]),
        )
        result = await _call(mcp, "list_pending_tasks")
        assert isinstance(result, dict)
        titles = [t["title"] for t in result["items"]]
        assert "Queued Task" in titles

    @pytest.mark.asyncio
    async def test_no_auth(self, mcp, test_user):
        set_current_user(None)
        result = await _call(mcp, "list_pending_tasks")
        assert "error" in result
