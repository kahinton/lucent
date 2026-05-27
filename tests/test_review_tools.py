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
    async def test_approval_decisions_rejected_for_mcp(self, mcp, auth_user, test_request):
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
            "comments": "Looks good",
        })
        assert result.get("error") == (
            "MCP create_review cannot submit 'approved' or 'rejected' decisions; "
            "use the REST API /api/reviews"
        )

    async def test_rejection_decisions_rejected_for_mcp(self, mcp, auth_user, test_request):
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "rejected",
            "comments": "Needs more tests",
        })
        assert result.get("error") == (
            "MCP create_review cannot submit 'approved' or 'rejected' decisions; "
            "use the REST API /api/reviews"
        )

    async def test_create_with_task_id_rejected_for_mcp(
        self, mcp, auth_user, test_request, test_task
    ):
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "task_id": str(test_task["id"]),
            "status": "approved",
        })
        assert result.get("error") == (
            "MCP create_review cannot submit 'approved' or 'rejected' decisions; "
            "use the REST API /api/reviews"
        )

    async def test_create_with_custom_source_still_rejected(
        self, mcp, auth_user, test_request
    ):
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
            "source": "daemon",
        })
        assert result.get("error") == (
            "MCP create_review cannot submit 'approved' or 'rejected' decisions; "
            "use the REST API /api/reviews"
        )

    async def test_source_effectively_agent_only_for_decisions(
        self, mcp, auth_user, test_request
    ):
        """Passing source='human' cannot spoof a decision-capable review path."""
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
            "source": "human",
        })
        assert result.get("error") == (
            "MCP create_review cannot submit 'approved' or 'rejected' decisions; "
            "use the REST API /api/reviews"
        )

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
        assert result.get("error") == (
            "MCP create_review cannot submit 'approved' or 'rejected' decisions; "
            "use the REST API /api/reviews"
        )

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

    async def test_cross_org_request_returns_not_found(
        self, mcp, auth_user, repo, db_pool
    ):
        """MCP create_review cannot access requests from another organization."""
        from lucent.db import OrganizationRepository, UserRepository

        org_repo = OrganizationRepository(db_pool)
        user_repo = UserRepository(db_pool)
        other_org = await org_repo.create(name=f"mcp_other_{uuid4().hex[:8]}")
        other_user = await user_repo.create(
            external_id=f"mcp_other_user_{uuid4().hex[:8]}",
            provider="local",
            organization_id=other_org["id"],
            email=f"mcp_other_{uuid4().hex[:8]}@test.com",
            display_name="MCP Other User",
        )
        other_req = await repo.create_request(
            title="Other org request",
            org_id=str(other_org["id"]),
            created_by=str(other_user["id"]),
        )

        result = await _call(mcp, "create_review", {
            "request_id": str(other_req["id"]),
            "status": "approved",
        })
        assert result.get("error") == "Request not found"

        # Cleanup cross-org test data (cascade deletes memories and requests)
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM requests WHERE organization_id = $1", other_org["id"])
            await conn.execute("DELETE FROM users WHERE id = $1", other_user["id"])
            await conn.execute("DELETE FROM organizations WHERE id = $1", other_org["id"])

    async def test_task_wrong_request_returns_error(self, mcp, auth_user, test_request, repo, test_organization, test_task):
        other_request = await repo.create_request(
            title="Different request",
            org_id=str(test_organization["id"]),
        )
        result = await _call(mcp, "create_review", {
            "request_id": str(other_request["id"]),
            "task_id": str(test_task["id"]),
            "status": "approved",
        })
        assert result.get("error") == "Task does not belong to the specified request"

    async def test_invalid_source_returns_error(self, mcp, auth_user, test_request):
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
            "source": "bot",
        })
        assert result.get("error") == "source must be one of: 'human', 'daemon', 'agent'"

    async def test_db_exceptions_are_not_leaked(self, mcp, auth_user, monkeypatch, test_request):
        from lucent.db.requests import RequestRepository

        async def boom(*args, **kwargs):
            raise RuntimeError("SQLSTATE 23505 duplicate key details")

        monkeypatch.setattr(RequestRepository, "get_request", boom)
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "approved",
        })
        assert result.get("error") == "Failed to create review"
        assert "SQLSTATE" not in json.dumps(result)

    async def test_self_review_prevention(self, mcp, auth_user, repo, test_organization):
        """Request creators cannot submit reviews for their own requests."""
        own_request = await repo.create_request(
            title="Self review forbidden",
            org_id=str(test_organization["id"]),
            created_by=str(auth_user["id"]),
        )
        result = await _call(mcp, "create_review", {
            "request_id": str(own_request["id"]),
            "status": "approved",
        })
        assert result.get("error") == "Request creators cannot review their own requests"

    async def test_comments_max_length_enforced(self, mcp, auth_user, test_request):
        result = await _call(mcp, "create_review", {
            "request_id": str(test_request["id"]),
            "status": "note",
            "comments": "x" * 10001,
        })
        assert result.get("error") == "comments must be at most 10000 characters"


# ── list_reviews tool ────────────────────────────────────────────────────


class TestListReviewsTool:
    async def test_list_empty(self, mcp, auth_user):
        result = await _call(mcp, "list_reviews", {})
        assert "items" in result
        assert "total_count" in result

    async def test_list_after_create(
        self, mcp, auth_user, test_request, review_repo, test_organization
    ):
        await review_repo.create_review(
            request_id=str(test_request["id"]),
            organization_id=str(test_organization["id"]),
            status="approved",
            comments="Listed review",
            source="agent",
        )
        result = await _call(mcp, "list_reviews", {})
        assert result["total_count"] >= 1
        comments = [r.get("comments") for r in result["items"]]
        assert "Listed review" in comments

    async def test_filter_by_request_id(
        self, mcp, auth_user, test_request, repo, review_repo, test_organization
    ):
        req2 = await repo.create_request(
            title="Other Req", org_id=str(test_organization["id"])
        )
        await review_repo.create_review(
            request_id=str(test_request["id"]),
            organization_id=str(test_organization["id"]),
            status="approved",
            source="agent",
        )
        await review_repo.create_review(
            request_id=str(req2["id"]),
            organization_id=str(test_organization["id"]),
            status="rejected",
            comments="No",
            source="agent",
        )

        result = await _call(mcp, "list_reviews", {
            "request_id": str(test_request["id"]),
        })
        assert all(
            r["request_id"] == str(test_request["id"]) for r in result["items"]
        )

    async def test_filter_by_task_id(
        self, mcp, auth_user, test_request, test_task, repo, review_repo, test_organization
    ):
        req2 = await repo.create_request(
            title="Other Req", org_id=str(test_organization["id"])
        )
        task2 = await repo.create_task(
            request_id=str(req2["id"]),
            title="Other task",
            org_id=str(test_organization["id"]),
        )
        await review_repo.create_review(
            request_id=str(test_request["id"]),
            task_id=str(test_task["id"]),
            organization_id=str(test_organization["id"]),
            status="approved",
            source="agent",
        )
        await review_repo.create_review(
            request_id=str(req2["id"]),
            task_id=str(task2["id"]),
            organization_id=str(test_organization["id"]),
            status="approved",
            source="agent",
        )

        result = await _call(mcp, "list_reviews", {"task_id": str(test_task["id"])})
        assert result["total_count"] >= 1
        assert all(r["task_id"] == str(test_task["id"]) for r in result["items"])

    async def test_filter_by_multiple_fields(
        self, mcp, auth_user, test_request, test_task, repo, review_repo, test_organization
    ):
        await review_repo.create_review(
            request_id=str(test_request["id"]),
            task_id=str(test_task["id"]),
            organization_id=str(test_organization["id"]),
            status="approved",
            source="agent",
            comments="matching",
        )
        await review_repo.create_review(
            request_id=str(test_request["id"]),
            task_id=str(test_task["id"]),
            organization_id=str(test_organization["id"]),
            status="rejected",
            source="agent",
            comments="non-matching-status",
        )
        req2 = await repo.create_request(
            title="Other Req Multi", org_id=str(test_organization["id"])
        )
        await review_repo.create_review(
            request_id=str(req2["id"]),
            organization_id=str(test_organization["id"]),
            status="approved",
            source="agent",
            comments="non-matching-request",
        )

        result = await _call(
            mcp,
            "list_reviews",
            {
                "request_id": str(test_request["id"]),
                "task_id": str(test_task["id"]),
                "status": "approved",
                "source": "agent",
            },
        )
        assert result["total_count"] == 1
        assert len(result["items"]) == 1
        item = result["items"][0]
        assert item["request_id"] == str(test_request["id"])
        assert item["task_id"] == str(test_task["id"])
        assert item["status"] == "approved"
        assert item["source"] == "agent"

    async def test_filter_by_status(
        self, mcp, auth_user, test_request, review_repo, test_organization
    ):
        await review_repo.create_review(
            request_id=str(test_request["id"]),
            organization_id=str(test_organization["id"]),
            status="approved",
            source="agent",
        )
        await review_repo.create_review(
            request_id=str(test_request["id"]),
            organization_id=str(test_organization["id"]),
            status="rejected",
            comments="Nah",
            source="agent",
        )

        approved = await _call(mcp, "list_reviews", {"status": "approved"})
        assert all(r["status"] == "approved" for r in approved["items"])

    async def test_filter_by_source(
        self, mcp, auth_user, test_request, review_repo, test_organization
    ):
        await review_repo.create_review(
            request_id=str(test_request["id"]),
            organization_id=str(test_organization["id"]),
            status="approved",
            source="daemon",
        )
        result = await _call(mcp, "list_reviews", {"source": "daemon"})
        assert all(r["source"] == "daemon" for r in result["items"])

    async def test_pagination(self, mcp, auth_user, test_request, review_repo, test_organization):
        for _ in range(5):
            await review_repo.create_review(
                request_id=str(test_request["id"]),
                organization_id=str(test_organization["id"]),
                status="approved",
                source="agent",
            )
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

    async def test_cross_org_reviews_not_visible(
        self, mcp, auth_user, repo, review_repo, db_pool
    ):
        from lucent.db import OrganizationRepository

        org_repo = OrganizationRepository(db_pool)
        other_org = await org_repo.create(name=f"mcp_other_list_{uuid4().hex[:8]}")
        other_req = await repo.create_request(
            title="Other org hidden request",
            org_id=str(other_org["id"]),
        )
        await review_repo.create_review(
            request_id=str(other_req["id"]),
            organization_id=str(other_org["id"]),
            status="approved",
            comments="hidden",
            source="agent",
        )

        result = await _call(mcp, "list_reviews", {})
        comments = [r.get("comments") for r in result["items"]]
        assert "hidden" not in comments

        # Cleanup cross-org test data
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM reviews WHERE organization_id = $1", other_org["id"])
            await conn.execute("DELETE FROM requests WHERE organization_id = $1", other_org["id"])
            await conn.execute("DELETE FROM organizations WHERE id = $1", other_org["id"])


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
