"""Tests for review-related MCP tools in src/lucent/tools/requests.py.

Covers: create_review tool, list_reviews tool, get_request_details includes reviews.
Tests auth context enforcement, JSON serialization, validation, and error handling.
"""

import json
from uuid import uuid4

import pytest
import pytest_asyncio
from mcp.server.fastmcp import FastMCP

from lucent.auth import set_current_user
from lucent.db.requests import RequestRepository
from lucent.db.reviews import ReviewRepository
from lucent.tools.requests import register_request_tools

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def mcp(db_pool):
    """Create a FastMCP instance with request tools registered."""
    m = FastMCP("test-reviews")
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


@pytest_asyncio.fixture
async def review_repo(db_pool):
    return ReviewRepository(db_pool)


@pytest_asyncio.fixture(autouse=True)
async def cleanup_data(db_pool, test_organization):
    """Clean up reviews and requests after each test."""
    yield
    org_id = test_organization["id"]
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM reviews WHERE organization_id = $1", org_id
        )
        await conn.execute(
            "DELETE FROM requests WHERE organization_id = $1", org_id
        )


async def _call(mcp, tool_name: str, args: dict | None = None) -> dict | list:
    """Call an MCP tool and parse the JSON response."""
    result = await mcp._tool_manager.call_tool(tool_name, args or {})
    return json.loads(result)


@pytest_asyncio.fixture
async def test_request(repo, test_organization):
    """Create a request for testing."""
    return await repo.create_request(
        title="MCP Review Request",
        org_id=str(test_organization["id"]),
    )


@pytest_asyncio.fixture
async def test_task(repo, test_request, test_organization):
    """Create a task for testing."""
    return await repo.create_task(
        request_id=str(test_request["id"]),
        title="MCP Review Task",
        org_id=str(test_organization["id"]),
    )


# ── create_review tool ───────────────────────────────────────────────────


class TestCreateReviewTool:
    async def test_create_approval(self, mcp, auth_user, test_request):
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
            "comments": "Looks good",
        })
        assert "id" in result
        assert result["status"] == "approved"
        assert result["comments"] == "Looks good"
        assert result["source"] == "agent"  # default for MCP tool

    async def test_create_rejection_with_comments(self, mcp, auth_user, test_request):
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "rejected",
            "comments": "Needs more tests",
        })
        assert result["status"] == "rejected"
        assert result["comments"] == "Needs more tests"

    async def test_create_with_task_id(self, mcp, auth_user, test_request, test_task):
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "task_id": str(test_task["id"]),
            "status": "approved",
        })
        assert result["task_id"] == str(test_task["id"])

    async def test_create_with_custom_source(self, mcp, auth_user, test_request):
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
            "source": "daemon",
        })
        assert result["source"] == "daemon"

    async def test_invalid_status_returns_error(self, mcp, auth_user, test_request):
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "maybe",
        })
        assert "error" in result

    async def test_rejection_without_comments_returns_error(
        self, mcp, auth_user, test_request
    ):
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "rejected",
        })
        assert "error" in result

    async def test_no_auth_returns_error(self, mcp, test_request):
        """Without auth context, should return error."""
        set_current_user(None)
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
        })
        assert "error" in result

    async def test_nonexistent_request_returns_error(self, mcp, auth_user):
        result = await _call(mcp, "create_review", {
            "request_id": str(uuid4()),
            "status": "approved",
        })
        assert "error" in result

    async def test_json_serialization(self, mcp, auth_user, test_request):
        """Verify UUID and datetime are properly serialized to strings."""
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
        })
        # All UUIDs should be strings
        assert isinstance(result["id"], str)
        assert isinstance(result["request_id"], str)
        assert isinstance(result["organization_id"], str)
        # created_at should be ISO format string
        assert isinstance(result["created_at"], str)


# ── list_reviews tool ────────────────────────────────────────────────────


class TestListReviewsTool:
    async def test_list_empty(self, mcp, auth_user):
        result = await _call(mcp, "list_reviews", {})
        assert "items" in result
        assert "total_count" in result

    async def test_list_after_create(self, mcp, auth_user, test_request):
        await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
            "comments": "Listed review",
        })
        result = await _call(mcp, "list_reviews", {})
        assert result["total_count"] >= 1
        comments = [r.get("comments") for r in result["items"]]
        assert "Listed review" in comments

    async def test_filter_by_request_id(self, mcp, auth_user, test_request, repo, test_organization):
        req2 = await repo.create_request(
            title="Other Req", org_id=str(test_organization["id"])
        )
        await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
        })
        await _call(mcp, "create_review", {
            "request_id": str(req2["id"]),
            "status": "rejected",
            "comments": "No",
        })

        result = await _call(mcp, "list_reviews", {
            "request_id": str(test_request["id"]),
        })
        assert all(
            r["request_id"] == str(test_request["id"]) for r in result["items"]
        )

    async def test_filter_by_status(self, mcp, auth_user, test_request):
        await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
        })
        await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "rejected",
            "comments": "Nah",
        })

        approved = await _call(mcp, "list_reviews", {"status": "approved"})
        assert all(r["status"] == "approved" for r in approved["items"])

    async def test_filter_by_source(self, mcp, auth_user, test_request):
        await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
            "source": "daemon",
        })
        result = await _call(mcp, "list_reviews", {"source": "daemon"})
        assert all(r["source"] == "daemon" for r in result["items"])

    async def test_pagination(self, mcp, auth_user, test_request):
        for _ in range(5):
            await _call(mcp, "create_review", {
                "request_id": str(test_request["id"]),
                "status": "approved",
            })
        result = await _call(mcp, "list_reviews", {"limit": 2, "offset": 0})
        assert len(result["items"]) == 2
        assert result["has_more"] is True

    async def test_limit_capped_at_100(self, mcp, auth_user, test_request):
        """limit > 100 should be capped to 100."""
        result = await _call(mcp, "list_reviews", {"limit": 200})
        # Should succeed without error
        assert "items" in result

    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(mcp, "list_reviews", {})
        assert "error" in result


# ── get_request_details includes reviews ─────────────────────────────────


class TestGetRequestDetailsWithReviews:
    async def test_includes_reviews_key(self, mcp, auth_user, test_request):
        """get_request_details should include review history."""
        result = await _call(mcp, "get_request_details", {
            "request_id": str(test_request["id"]),
        })
        assert "reviews" in result

    async def test_reviews_populated(
        self, mcp, auth_user, test_request, review_repo, test_organization
    ):
        """Reviews created for a request show up in get_request_details."""
        await review_repo.create_review(
            request_id=str(test_request["id"]),
            organization_id=str(test_organization["id"]),
            status="approved",
            comments="Included in details",
        )
        result = await _call(mcp, "get_request_details", {
            "request_id": str(test_request["id"]),
        })
        assert len(result["reviews"]) >= 1
        comments = [r.get("comments") for r in result["reviews"]]
        assert "Included in details" in comments

    async def test_reviews_empty_for_new_request(self, mcp, auth_user, test_request):
        result = await _call(mcp, "get_request_details", {
            "request_id": str(test_request["id"]),
        })
        assert result["reviews"] == []
