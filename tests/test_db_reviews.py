"""Tests for db/reviews.py — ReviewRepository.

Covers: CRUD operations, organization scoping, filtering, pagination,
foreign key constraints, edge cases, and aggregate summaries.
"""

from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from lucent.db import OrganizationRepository, UserRepository
from lucent.db.requests import RequestRepository
from lucent.db.reviews import ReviewRepository

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def rv_prefix(db_pool):
    """Unique prefix and cleanup for review DB tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_rv_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        org_ids = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id FROM organizations WHERE name LIKE $1", f"{prefix}%"
            )
        ]
        for oid in org_ids:
            await conn.execute("DELETE FROM reviews WHERE organization_id = $1", oid)
            await conn.execute("DELETE FROM task_events WHERE task_id IN "
                               "(SELECT id FROM tasks WHERE organization_id = $1)", oid)
            await conn.execute("DELETE FROM task_memories WHERE task_id IN "
                               "(SELECT id FROM tasks WHERE organization_id = $1)", oid)
            await conn.execute("DELETE FROM tasks WHERE organization_id = $1", oid)
            await conn.execute("DELETE FROM requests WHERE organization_id = $1", oid)
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def rv_org(db_pool, rv_prefix):
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{rv_prefix}org")


@pytest_asyncio.fixture
async def rv_user(db_pool, rv_org, rv_prefix):
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{rv_prefix}user",
        provider="local",
        organization_id=rv_org["id"],
        email=f"{rv_prefix}user@test.com",
        display_name=f"{rv_prefix}Reviewer",
    )


@pytest_asyncio.fixture
async def other_org(db_pool, rv_prefix):
    """Second org for cross-org isolation tests."""
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{rv_prefix}other_org")


@pytest_asyncio.fixture
async def other_user(db_pool, other_org, rv_prefix):
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{rv_prefix}other_user",
        provider="local",
        organization_id=other_org["id"],
        email=f"{rv_prefix}other@test.com",
        display_name=f"{rv_prefix}Other User",
    )


@pytest_asyncio.fixture
def repo(db_pool):
    return ReviewRepository(db_pool)


@pytest_asyncio.fixture
def req_repo(db_pool):
    return RequestRepository(db_pool)


@pytest_asyncio.fixture
def org_id(rv_org):
    return str(rv_org["id"])


@pytest_asyncio.fixture
def user_id(rv_user):
    return str(rv_user["id"])


@pytest_asyncio.fixture
async def sample_request(req_repo, org_id):
    """Create a test request."""
    return await req_repo.create_request(title="Review Test Request", org_id=org_id)


@pytest_asyncio.fixture
async def task(req_repo, sample_request, org_id):
    """Create a test task in the test request."""
    return await req_repo.create_task(
        request_id=str(sample_request["id"]),
        title="Review Test Task",
        org_id=org_id,
    )


# ── Helpers ──────────────────────────────────────────────────────────────


async def _make_review(repo, request_id, org_id, **kwargs):
    """Create a review with sensible defaults."""
    defaults = dict(
        request_id=request_id,
        organization_id=org_id,
        status="approved",
        source="human",
    )
    defaults.update(kwargs)
    return await repo.create_review(**defaults)


# ── Create Review ────────────────────────────────────────────────────────


class TestCreateReview:
    async def test_create_approved(self, repo, sample_request, org_id, user_id):
        review = await repo.create_review(
            request_id=str(sample_request["id"]),
            organization_id=org_id,
            status="approved",
            reviewer_user_id=user_id,
            reviewer_display_name="Reviewer",
            comments="Looks good",
            source="human",
        )
        assert isinstance(review["id"], UUID)
        assert review["status"] == "approved"
        assert review["source"] == "human"
        assert review["comments"] == "Looks good"
        assert review["request_id"] == sample_request["id"]
        assert review["reviewer_display_name"] == "Reviewer"
        assert review["created_at"] is not None

    async def test_create_rejected(self, repo, sample_request, org_id, user_id):
        review = await repo.create_review(
            request_id=str(sample_request["id"]),
            organization_id=org_id,
            status="rejected",
            reviewer_user_id=user_id,
            comments="Needs rework",
            source="human",
        )
        assert review["status"] == "rejected"
        assert review["comments"] == "Needs rework"

    async def test_create_with_task_id(self, repo, sample_request, task, org_id):
        review = await repo.create_review(
            request_id=str(sample_request["id"]),
            organization_id=org_id,
            status="approved",
            task_id=str(task["id"]),
        )
        assert review["task_id"] == task["id"]

    async def test_create_without_optional_fields(self, repo, sample_request, org_id):
        review = await repo.create_review(
            request_id=str(sample_request["id"]),
            organization_id=org_id,
            status="approved",
        )
        assert review["task_id"] is None
        assert review["reviewer_user_id"] is None
        assert review["reviewer_display_name"] is None
        assert review["comments"] is None
        assert review["source"] == "human"

    async def test_create_daemon_source(self, repo, sample_request, org_id):
        review = await _make_review(
            repo, str(sample_request["id"]), org_id, source="daemon"
        )
        assert review["source"] == "daemon"

    async def test_create_agent_source(self, repo, sample_request, org_id):
        review = await _make_review(
            repo, str(sample_request["id"]), org_id, source="agent"
        )
        assert review["source"] == "agent"

    async def test_invalid_status_raises(self, repo, sample_request, org_id):
        with pytest.raises(ValueError, match="Invalid review status"):
            await repo.create_review(
                request_id=str(sample_request["id"]),
                organization_id=org_id,
                status="maybe",
            )

    async def test_invalid_source_raises(self, repo, sample_request, org_id):
        with pytest.raises(ValueError, match="Invalid review source"):
            await repo.create_review(
                request_id=str(sample_request["id"]),
                organization_id=org_id,
                status="approved",
                source="bot",
            )

    async def test_nonexistent_request_raises(self, repo, org_id):
        """FK constraint: request_id must reference a valid request."""
        fake_id = str(uuid4())
        with pytest.raises(Exception):  # asyncpg.ForeignKeyViolationError
            await repo.create_review(
                request_id=fake_id,
                organization_id=org_id,
                status="approved",
            )

    async def test_null_comments_on_approval(self, repo, sample_request, org_id):
        """Null comments are acceptable on approval."""
        review = await repo.create_review(
            request_id=str(sample_request["id"]),
            organization_id=org_id,
            status="approved",
            comments=None,
        )
        assert review["comments"] is None

    async def test_multiple_reviews_for_same_request(self, repo, sample_request, org_id):
        """Multiple reviews can be created for the same request."""
        r1 = await _make_review(repo, str(sample_request["id"]), org_id, status="rejected",
                                comments="Try again")
        r2 = await _make_review(repo, str(sample_request["id"]), org_id, status="approved")
        assert r1["id"] != r2["id"]


# ── Get Review ───────────────────────────────────────────────────────────


class TestGetReview:
    async def test_get_existing(self, repo, sample_request, org_id):
        created = await _make_review(repo, str(sample_request["id"]), org_id,
                                     comments="Good work")
        fetched = await repo.get_review(str(created["id"]), org_id)
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["comments"] == "Good work"
        # Should include request_title from the JOIN
        assert fetched["request_title"] == "Review Test Request"

    async def test_get_nonexistent(self, repo, org_id):
        result = await repo.get_review(str(uuid4()), org_id)
        assert result is None

    async def test_get_wrong_org(self, repo, sample_request, org_id, other_org):
        """Reviews are org-scoped: can't access with wrong org_id."""
        created = await _make_review(repo, str(sample_request["id"]), org_id)
        result = await repo.get_review(str(created["id"]), str(other_org["id"]))
        assert result is None


# ── List Reviews ─────────────────────────────────────────────────────────


class TestListReviews:
    async def test_list_returns_org_reviews(self, repo, sample_request, org_id):
        await _make_review(repo, str(sample_request["id"]), org_id, comments="Review A")
        await _make_review(repo, str(sample_request["id"]), org_id, comments="Review B")
        result = await repo.list_reviews(org_id)
        assert result["total_count"] >= 2
        comments = {r["comments"] for r in result["items"]}
        assert "Review A" in comments
        assert "Review B" in comments

    async def test_list_org_scoping(self, repo, req_repo, sample_request, org_id, other_org):
        """Only reviews from the queried org are returned."""
        await _make_review(repo, str(sample_request["id"]), org_id, comments="Mine")
        # Other org's request/review
        other_req = await req_repo.create_request(
            title="Other Req", org_id=str(other_org["id"])
        )
        await _make_review(repo, str(other_req["id"]), str(other_org["id"]),
                           comments="Theirs")

        mine = await repo.list_reviews(org_id)
        theirs = await repo.list_reviews(str(other_org["id"]))
        my_comments = {r["comments"] for r in mine["items"]}
        their_comments = {r["comments"] for r in theirs["items"]}
        assert "Mine" in my_comments
        assert "Theirs" not in my_comments
        assert "Theirs" in their_comments

    async def test_filter_by_request_id(self, repo, req_repo, sample_request, org_id):
        req2 = await req_repo.create_request(title="Req 2", org_id=org_id)
        await _make_review(repo, str(sample_request["id"]), org_id, comments="For req 1")
        await _make_review(repo, str(req2["id"]), org_id, comments="For req 2")

        result = await repo.list_reviews(org_id, request_id=str(sample_request["id"]))
        assert all(r["request_id"] == sample_request["id"] for r in result["items"])
        assert any(r["comments"] == "For req 1" for r in result["items"])

    async def test_filter_by_task_id(self, repo, sample_request, task, org_id):
        await _make_review(repo, str(sample_request["id"]), org_id,
                           task_id=str(task["id"]), comments="Task-level")
        await _make_review(repo, str(sample_request["id"]), org_id, comments="Request-level")

        result = await repo.list_reviews(org_id, task_id=str(task["id"]))
        assert all(r["task_id"] == task["id"] for r in result["items"])
        assert result["total_count"] == 1

    async def test_filter_by_status(self, repo, sample_request, org_id):
        await _make_review(repo, str(sample_request["id"]), org_id, status="approved")
        await _make_review(repo, str(sample_request["id"]), org_id, status="rejected",
                           comments="Nope")

        approved = await repo.list_reviews(org_id, status="approved")
        rejected = await repo.list_reviews(org_id, status="rejected")
        assert all(r["status"] == "approved" for r in approved["items"])
        assert all(r["status"] == "rejected" for r in rejected["items"])

    async def test_filter_by_source(self, repo, sample_request, org_id):
        await _make_review(repo, str(sample_request["id"]), org_id, source="human")
        await _make_review(repo, str(sample_request["id"]), org_id, source="daemon")
        await _make_review(repo, str(sample_request["id"]), org_id, source="agent")

        human = await repo.list_reviews(org_id, source="human")
        daemon = await repo.list_reviews(org_id, source="daemon")
        assert all(r["source"] == "human" for r in human["items"])
        assert all(r["source"] == "daemon" for r in daemon["items"])

    async def test_pagination(self, repo, sample_request, org_id):
        for i in range(5):
            await _make_review(repo, str(sample_request["id"]), org_id,
                               comments=f"Review {i}")

        page1 = await repo.list_reviews(org_id, limit=2, offset=0)
        page2 = await repo.list_reviews(org_id, limit=2, offset=2)
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 2
        assert page1["items"][0]["id"] != page2["items"][0]["id"]
        assert page1["total_count"] >= 5
        assert page1["has_more"] is True

    async def test_pagination_last_page(self, repo, sample_request, org_id):
        for i in range(3):
            await _make_review(repo, str(sample_request["id"]), org_id,
                               comments=f"Review {i}")

        result = await repo.list_reviews(org_id, limit=10, offset=0)
        assert result["has_more"] is False
        assert result["total_count"] >= 3

    async def test_combined_filters(self, repo, sample_request, org_id):
        await _make_review(repo, str(sample_request["id"]), org_id,
                           status="approved", source="daemon")
        await _make_review(repo, str(sample_request["id"]), org_id,
                           status="rejected", source="daemon", comments="No")
        await _make_review(repo, str(sample_request["id"]), org_id,
                           status="approved", source="human")

        result = await repo.list_reviews(
            org_id, status="approved", source="daemon"
        )
        assert all(
            r["status"] == "approved" and r["source"] == "daemon"
            for r in result["items"]
        )

    async def test_response_format(self, repo, sample_request, org_id):
        await _make_review(repo, str(sample_request["id"]), org_id)
        result = await repo.list_reviews(org_id)
        assert "items" in result
        assert "total_count" in result
        assert "offset" in result
        assert "limit" in result
        assert "has_more" in result
        # Each item should include the request_title from the JOIN
        for item in result["items"]:
            assert "request_title" in item

    async def test_order_by_created_at_desc(self, repo, sample_request, org_id):
        """Reviews should be ordered newest first."""
        r1 = await _make_review(repo, str(sample_request["id"]), org_id, comments="First")
        r2 = await _make_review(repo, str(sample_request["id"]), org_id, comments="Second")
        result = await repo.list_reviews(org_id)
        ids = [r["id"] for r in result["items"]]
        # r2 was created later, should appear first
        assert ids.index(r2["id"]) < ids.index(r1["id"])


# ── Get Reviews for Request/Task ─────────────────────────────────────────


class TestGetReviewsForRequest:
    async def test_returns_all_reviews(self, repo, sample_request, org_id):
        r1 = await _make_review(repo, str(sample_request["id"]), org_id, status="rejected",
                                comments="Fix it")
        r2 = await _make_review(repo, str(sample_request["id"]), org_id, status="approved")
        reviews = await repo.get_reviews_for_request(str(sample_request["id"]), org_id)
        assert len(reviews) == 2
        ids = {r["id"] for r in reviews}
        assert r1["id"] in ids
        assert r2["id"] in ids

    async def test_org_scoping(self, repo, sample_request, org_id, other_org):
        await _make_review(repo, str(sample_request["id"]), org_id)
        # Query from other org — should not see reviews
        reviews = await repo.get_reviews_for_request(
            str(sample_request["id"]), str(other_org["id"])
        )
        assert len(reviews) == 0

    async def test_empty_request(self, repo, org_id):
        reviews = await repo.get_reviews_for_request(str(uuid4()), org_id)
        assert reviews == []


class TestGetReviewsForTask:
    async def test_returns_task_reviews(self, repo, sample_request, task, org_id):
        await _make_review(repo, str(sample_request["id"]), org_id,
                           task_id=str(task["id"]), comments="Task review")
        await _make_review(repo, str(sample_request["id"]), org_id,
                           comments="Request-level only")
        reviews = await repo.get_reviews_for_task(str(task["id"]), org_id)
        assert len(reviews) == 1
        assert reviews[0]["task_id"] == task["id"]

    async def test_org_scoping(self, repo, sample_request, task, org_id, other_org):
        await _make_review(repo, str(sample_request["id"]), org_id,
                           task_id=str(task["id"]))
        reviews = await repo.get_reviews_for_task(
            str(task["id"]), str(other_org["id"])
        )
        assert len(reviews) == 0


# ── Review Summary ───────────────────────────────────────────────────────


class TestReviewSummary:
    async def test_summary_all_types(self, repo, sample_request, org_id):
        await _make_review(repo, str(sample_request["id"]), org_id,
                           status="approved", source="human")
        await _make_review(repo, str(sample_request["id"]), org_id,
                           status="approved", source="daemon")
        await _make_review(repo, str(sample_request["id"]), org_id,
                           status="rejected", source="agent", comments="Bad")

        summary = await repo.get_review_summary(org_id)
        assert summary["total"] >= 3
        assert summary["approved"] >= 2
        assert summary["rejected"] >= 1
        assert summary["human_reviews"] >= 1
        assert summary["daemon_reviews"] >= 1
        assert summary["agent_reviews"] >= 1

    async def test_summary_empty_org(self, repo, other_org):
        summary = await repo.get_review_summary(str(other_org["id"]))
        assert summary["total"] == 0
        assert summary["approved"] == 0
        assert summary["rejected"] == 0

    async def test_summary_org_scoping(self, repo, req_repo, sample_request, org_id, other_org):
        """Summary only counts reviews in the specified org."""
        await _make_review(repo, str(sample_request["id"]), org_id, status="approved")
        other_req = await req_repo.create_request(
            title="Other", org_id=str(other_org["id"])
        )
        await _make_review(repo, str(other_req["id"]), str(other_org["id"]),
                           status="rejected", comments="No")

        my_summary = await repo.get_review_summary(org_id)
        their_summary = await repo.get_review_summary(str(other_org["id"]))
        assert my_summary["rejected"] == 0
        assert their_summary["approved"] == 0


# ── FK Cascade Tests ─────────────────────────────────────────────────────


class TestForeignKeyCascades:
    async def test_delete_request_cascades_reviews(self, repo, req_repo, org_id):
        """Deleting a request should cascade-delete its reviews."""
        req = await req_repo.create_request(title="Cascade Test", org_id=org_id)
        await _make_review(repo, str(req["id"]), org_id, comments="To be deleted")

        # Delete the request directly
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM requests WHERE id = $1",
                req["id"],
            )

        # Review should be gone
        result = await repo.list_reviews(org_id)
        comments = {r.get("comments") for r in result["items"]}
        assert "To be deleted" not in comments

    async def test_delete_task_nullifies_review(self, repo, req_repo, sample_request, task, org_id):
        """Deleting a task should SET NULL on review.task_id (not cascade-delete)."""
        review = await _make_review(repo, str(sample_request["id"]), org_id,
                                    task_id=str(task["id"]))
        # Delete the task directly
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM task_events WHERE task_id = $1", task["id"]
            )
            await conn.execute("DELETE FROM tasks WHERE id = $1", task["id"])

        # Review should still exist but with task_id = NULL
        fetched = await repo.get_review(str(review["id"]), org_id)
        assert fetched is not None
        assert fetched["task_id"] is None

    async def test_delete_org_cascades_reviews(self, repo, req_repo, db_pool, rv_prefix):
        """Deleting an org should cascade-delete reviews."""
        org_repo = OrganizationRepository(db_pool)
        temp_org = await org_repo.create(name=f"{rv_prefix}temp_org")
        temp_org_id = str(temp_org["id"])

        req = await req_repo.create_request(title="Temp Req", org_id=temp_org_id)
        review = await _make_review(repo, str(req["id"]), temp_org_id)

        # Delete the org (cascades to reviews via requests)
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM requests WHERE organization_id = $1",
                               temp_org["id"])
            await conn.execute("DELETE FROM organizations WHERE id = $1",
                               temp_org["id"])

        # Review should be gone
        fetched = await repo.get_review(str(review["id"]), temp_org_id)
        assert fetched is None
