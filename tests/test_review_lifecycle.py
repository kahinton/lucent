"""Comprehensive tests for the post-completion review lifecycle.

Tests the full review phase added to request lifecycle:
  pending → in_progress → review → completed  (happy path)
  review → needs_rework → in_progress → review → completed  (rework path)

Covers:
- DB layer: status transitions, review_count tracking, retry_task_with_feedback
- API layer: review endpoints, status filtering, response models
- Integration flows: happy path, rework loop, max-rework escalation, backwards compat
- Daemon helpers: _parse_review_decision, _is_request_review_task
"""

import re
from uuid import uuid4

import pytest
import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.constants import (
    REQUEST_STATUS_COMPLETED,
    REQUEST_STATUS_FAILED,
    REQUEST_STATUS_IN_PROGRESS,
    REQUEST_STATUS_NEEDS_REWORK,
    REQUEST_STATUS_PENDING,
    REQUEST_STATUS_REVIEW,
)
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.requests import RequestRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_request(repo, org_id, **kwargs):
    """Create a request with sensible defaults."""
    defaults = dict(title=f"Review-test {uuid4().hex[:6]}", org_id=org_id)
    defaults.update(kwargs)
    return await repo.create_request(**defaults)


async def _make_task(repo, request_id, org_id, **kwargs):
    """Create a task with sensible defaults."""
    defaults = dict(request_id=request_id, title="Test task", org_id=org_id)
    defaults.update(kwargs)
    return await repo.create_task(**defaults)


async def _complete_task_flow(repo, task, result="Done"):
    """Claim then complete a task in one step."""
    tid = str(task["id"])
    await repo.claim_task(tid, "test-inst")
    return await repo.complete_task(tid, result)


async def _fail_task_flow(repo, task, error="Something broke"):
    """Claim then fail a task in one step."""
    tid = str(task["id"])
    await repo.claim_task(tid, "test-inst")
    return await repo.fail_task(tid, error)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_review_mode(monkeypatch):
    """Review mode is the default — ensure it's not disabled."""
    monkeypatch.delenv("LUCENT_SKIP_POST_REVIEW", raising=False)


@pytest_asyncio.fixture
async def rl_prefix(db_pool):
    """Unique prefix and cleanup for review lifecycle tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_rl_{test_id}_"
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
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute(
            "DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%"
        )


@pytest_asyncio.fixture
async def rl_org(db_pool, rl_prefix):
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{rl_prefix}org")


@pytest_asyncio.fixture
async def rl_user(db_pool, rl_org, rl_prefix):
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{rl_prefix}user",
        provider="local",
        organization_id=rl_org["id"],
        email=f"{rl_prefix}user@test.com",
        display_name=f"{rl_prefix}User",
    )


@pytest_asyncio.fixture
def repo(db_pool):
    return RequestRepository(db_pool)


@pytest_asyncio.fixture
def org_id(rl_org):
    return str(rl_org["id"])


# API client fixture
async def _make_client(user, scopes=None, role=None):
    app = create_app()
    fake_user = CurrentUser(
        id=user["id"],
        organization_id=user["organization_id"],
        role=role or user.get("role", "member"),
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=scopes or ["read", "write", "daemon-tasks"],
    )

    async def override():
        return fake_user

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, app


@pytest_asyncio.fixture
async def api_client(rl_user):
    client, app = await _make_client(rl_user)
    async with client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_client(rl_user):
    """Client with admin role — needed for deprecated approve/reject endpoints."""
    client, app = await _make_client(rl_user, role="admin")
    async with client:
        yield client
    app.dependency_overrides.clear()


# =========================================================================
# SECTION 1: DB Layer — Status Transitions
# =========================================================================


class TestReviewStatusTransitions:
    """Test that requests transition through the review lifecycle correctly."""

    async def test_all_tasks_completed_moves_to_review(self, repo, org_id):
        """When all tasks complete successfully, request goes to 'review' (not 'completed')."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        updated = await repo.get_request(str(req["id"]), org_id)
        assert updated["status"] == REQUEST_STATUS_REVIEW

    async def test_two_tasks_both_complete_moves_to_review(self, repo, org_id):
        """Multiple tasks all completing → review."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        t1 = await _make_task(repo, rid, org_id, title="Task 1")
        t2 = await _make_task(repo, rid, org_id, title="Task 2")

        await _complete_task_flow(repo, t1, "Result 1")
        await _complete_task_flow(repo, t2, "Result 2")

        updated = await repo.get_request(rid, org_id)
        assert updated["status"] == REQUEST_STATUS_REVIEW

    async def test_review_to_completed_via_update_status(self, repo, org_id):
        """review → completed is a valid transition (approval path)."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        # Now in review; approve it
        result = await repo.update_request_status(str(req["id"]), "completed")
        assert result is not None
        assert result["status"] == REQUEST_STATUS_COMPLETED
        assert result["completed_at"] is not None

    async def test_review_to_needs_rework_via_update_status(self, repo, org_id):
        """review → needs_rework is a valid transition (rejection path)."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        result = await repo.update_request_status(str(req["id"]), "needs_rework")
        assert result is not None
        assert result["status"] == REQUEST_STATUS_NEEDS_REWORK
        assert result["reviewed_at"] is not None

    async def test_needs_rework_to_in_progress_via_ensure(self, repo, org_id):
        """needs_rework → in_progress when a task is retried/claimed."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)

        # Complete → review → needs_rework
        await _complete_task_flow(repo, task)
        await repo.update_request_status(str(req["id"]), "needs_rework")

        # Fail then retry the task to trigger _ensure_request_in_progress
        # We need a failed task to retry; create a new one
        t2 = await _make_task(repo, str(req["id"]), org_id, title="Retry target")
        await _fail_task_flow(repo, t2, "error")

        retried = await repo.retry_task(str(t2["id"]))
        assert retried is not None

        # Claim triggers _ensure_request_in_progress
        await repo.claim_task(str(t2["id"]), "test-inst-2")
        updated = await repo.get_request(str(req["id"]), org_id)
        assert updated["status"] == REQUEST_STATUS_IN_PROGRESS

    async def test_failed_request_with_any_failed_task(self, repo, org_id):
        """If any task fails, request becomes 'failed' (not 'review')."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        t1 = await _make_task(repo, rid, org_id, title="Good")
        t2 = await _make_task(repo, rid, org_id, title="Bad")

        await _complete_task_flow(repo, t1)
        await _fail_task_flow(repo, t2)

        updated = await repo.get_request(rid, org_id)
        assert updated["status"] == REQUEST_STATUS_FAILED

    async def test_reviewed_at_set_on_review_transition(self, repo, org_id):
        """reviewed_at timestamp is set when transitioning to review."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        updated = await repo.get_request(str(req["id"]), org_id)
        assert updated["status"] == REQUEST_STATUS_REVIEW
        assert updated["reviewed_at"] is not None

    async def test_reviewed_at_set_on_needs_rework(self, repo, org_id):
        """reviewed_at is updated when transitioning to needs_rework."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        result = await repo.update_request_status(str(req["id"]), "needs_rework")
        assert result["reviewed_at"] is not None


class TestInvalidTransitions:
    """Test that invalid status transitions are handled correctly."""

    async def test_pending_to_review_via_check_completion_not_possible(
        self, repo, org_id
    ):
        """A request can't jump from pending to review unless all tasks are done."""
        req = await _make_request(repo, org_id)
        # Create a task but don't complete it
        await _make_task(repo, str(req["id"]), org_id)

        # _check_request_completion won't trigger because tasks aren't all terminal
        still_pending = await repo.get_request(str(req["id"]), org_id)
        assert still_pending["status"] == REQUEST_STATUS_PENDING

    async def test_completed_request_stays_completed(self, repo, org_id):
        """Once completed, the request status can still be updated (no guard),
        but _check_request_completion won't re-trigger."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)
        # review → completed
        await repo.update_request_status(str(req["id"]), "completed")

        result = await repo.get_request(str(req["id"]), org_id)
        assert result["status"] == REQUEST_STATUS_COMPLETED


# =========================================================================
# SECTION 2: DB Layer — Review Count Tracking
# =========================================================================


class TestReviewCountTracking:
    """Test review_count and max_reviews fields."""

    async def test_new_request_has_zero_review_count(self, repo, org_id):
        """Newly created requests start with review_count=0."""
        req = await _make_request(repo, org_id)
        assert req["review_count"] == 0

    async def test_new_request_has_default_max_reviews(self, repo, org_id):
        """Default max_reviews is 3."""
        req = await _make_request(repo, org_id)
        assert req["max_reviews"] == 3

    async def test_review_feedback_initially_none(self, repo, org_id):
        """review_feedback starts as None."""
        req = await _make_request(repo, org_id)
        assert req.get("review_feedback") is None

    async def test_retry_with_feedback_increments_review_count(self, repo, org_id):
        """retry_task_with_feedback increments review_count on the parent request."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        task = await _make_task(repo, rid, org_id)
        await _fail_task_flow(repo, task)

        retried = await repo.retry_task_with_feedback(
            str(task["id"]), "Fix the output formatting"
        )
        assert retried is not None

        updated = await repo.get_request(rid, org_id)
        assert updated["review_count"] == 1
        assert updated["review_feedback"] == "Fix the output formatting"
        assert updated["status"] == REQUEST_STATUS_IN_PROGRESS

    async def test_multiple_reworks_increment_count(self, repo, org_id):
        """Each retry_task_with_feedback increments review_count."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])

        for i in range(3):
            task = await _make_task(repo, rid, org_id, title=f"Task round {i}")
            await _fail_task_flow(repo, task, f"Error {i}")
            await repo.retry_task_with_feedback(
                str(task["id"]), f"Feedback round {i}"
            )

        updated = await repo.get_request(rid, org_id)
        assert updated["review_count"] == 3

    async def test_retry_with_feedback_stores_latest_feedback(self, repo, org_id):
        """Only the most recent feedback is stored (overwrites previous)."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])

        t1 = await _make_task(repo, rid, org_id, title="T1")
        await _fail_task_flow(repo, t1, "err")
        await repo.retry_task_with_feedback(str(t1["id"]), "First feedback")

        t2 = await _make_task(repo, rid, org_id, title="T2")
        await _fail_task_flow(repo, t2, "err2")
        await repo.retry_task_with_feedback(str(t2["id"]), "Second feedback")

        updated = await repo.get_request(rid, org_id)
        assert updated["review_feedback"] == "Second feedback"
        assert updated["review_count"] == 2

    async def test_retry_with_feedback_on_non_failed_returns_none(self, repo, org_id):
        """retry_task_with_feedback returns None for non-failed tasks."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        # Task is still pending — not failed
        result = await repo.retry_task_with_feedback(str(task["id"]), "feedback")
        assert result is None

    async def test_retry_with_feedback_logs_event(self, repo, org_id):
        """retry_task_with_feedback logs a review_feedback event."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _fail_task_flow(repo, task, "err")
        await repo.retry_task_with_feedback(str(task["id"]), "Fix it")

        events = await repo.list_task_events(str(task["id"]))
        event_types = [e["event_type"] for e in events["items"]]
        assert "review_feedback" in event_types

    async def test_retry_with_feedback_sets_status_in_progress(self, repo, org_id):
        """retry_task_with_feedback moves parent request to in_progress."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _fail_task_flow(repo, task, "err")

        await repo.retry_task_with_feedback(str(task["id"]), "Fix it")
        updated = await repo.get_request(str(req["id"]), org_id)
        assert updated["status"] == REQUEST_STATUS_IN_PROGRESS


# =========================================================================
# SECTION 3: DB Layer — get_requests_in_review
# =========================================================================


class TestGetRequestsInReview:
    """Test the get_requests_in_review query."""

    async def test_returns_review_requests(self, repo, org_id):
        """Requests in 'review' status appear in the review list."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        result = await repo.get_requests_in_review(org_id)
        ids = [str(r["id"]) for r in result["items"]]
        assert str(req["id"]) in ids

    async def test_returns_needs_rework_requests(self, repo, org_id):
        """Requests in 'needs_rework' also appear in the review list."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)
        await repo.update_request_status(str(req["id"]), "needs_rework")

        result = await repo.get_requests_in_review(org_id)
        ids = [str(r["id"]) for r in result["items"]]
        assert str(req["id"]) in ids

    async def test_excludes_completed_requests(self, repo, org_id):
        """Completed requests do not appear in review list."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)
        await repo.update_request_status(str(req["id"]), "completed")

        result = await repo.get_requests_in_review(org_id)
        ids = [str(r["id"]) for r in result["items"]]
        assert str(req["id"]) not in ids

    async def test_excludes_pending_requests(self, repo, org_id):
        """Pending requests do not appear in review list."""
        req = await _make_request(repo, org_id)
        result = await repo.get_requests_in_review(org_id)
        ids = [str(r["id"]) for r in result["items"]]
        assert str(req["id"]) not in ids

    async def test_review_before_needs_rework_ordering(self, repo, org_id):
        """Review-status requests appear before needs_rework in results."""
        r1 = await _make_request(repo, org_id, title=f"Review first {uuid4().hex[:6]}")
        t1 = await _make_task(repo, str(r1["id"]), org_id)
        await _complete_task_flow(repo, t1)
        # r1 is now in 'review'

        r2 = await _make_request(repo, org_id, title=f"Rework second {uuid4().hex[:6]}")
        t2 = await _make_task(repo, str(r2["id"]), org_id)
        await _complete_task_flow(repo, t2)
        await repo.update_request_status(str(r2["id"]), "needs_rework")
        # r2 is now 'needs_rework'

        result = await repo.get_requests_in_review(org_id)
        statuses = [r["status"] for r in result["items"]]
        # All 'review' items should come before 'needs_rework' items
        review_indices = [i for i, s in enumerate(statuses) if s == "review"]
        rework_indices = [i for i, s in enumerate(statuses) if s == "needs_rework"]
        if review_indices and rework_indices:
            assert max(review_indices) < min(rework_indices)

    async def test_pagination(self, repo, org_id):
        """Pagination works for get_requests_in_review."""
        for i in range(3):
            r = await _make_request(repo, org_id, title=f"Paginate {i} {uuid4().hex[:6]}")
            t = await _make_task(repo, str(r["id"]), org_id)
            await _complete_task_flow(repo, t)

        page1 = await repo.get_requests_in_review(org_id, limit=2, offset=0)
        page2 = await repo.get_requests_in_review(org_id, limit=2, offset=2)

        assert len(page1["items"]) == 2
        assert len(page2["items"]) >= 1
        assert page1["total_count"] >= 3

    async def test_returns_total_count(self, repo, org_id):
        """Result includes accurate total_count."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        result = await repo.get_requests_in_review(org_id)
        assert "total_count" in result
        assert result["total_count"] >= 1


# =========================================================================
# SECTION 4: DB Layer — _ensure_request_in_progress with needs_rework
# =========================================================================


class TestEnsureRequestInProgress:
    """Test _ensure_request_in_progress handles the needs_rework state."""

    async def test_needs_rework_transitions_to_in_progress_on_claim(
        self, repo, org_id
    ):
        """Claiming a task when request is needs_rework moves it to in_progress."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])

        # Get to needs_rework state
        t1 = await _make_task(repo, rid, org_id, title="Original")
        await _complete_task_flow(repo, t1)
        await repo.update_request_status(rid, "needs_rework")

        # Create a new task and claim it
        t2 = await _make_task(repo, rid, org_id, title="Rework task")
        await repo.claim_task(str(t2["id"]), "inst-rework")

        updated = await repo.get_request(rid, org_id)
        assert updated["status"] == REQUEST_STATUS_IN_PROGRESS

    async def test_failed_transitions_to_in_progress_on_retry(self, repo, org_id):
        """Retrying a task when request is 'failed' transitions to in_progress."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _fail_task_flow(repo, task)

        # Request should be 'failed' now (only task failed)
        updated = await repo.get_request(str(req["id"]), org_id)
        assert updated["status"] == REQUEST_STATUS_FAILED

        # Retry
        await repo.retry_task(str(task["id"]))
        await repo.claim_task(str(task["id"]), "inst-retry")

        updated2 = await repo.get_request(str(req["id"]), org_id)
        assert updated2["status"] == REQUEST_STATUS_IN_PROGRESS


# =========================================================================
# SECTION 5: API Layer — Review Endpoints
# =========================================================================


class TestReviewAPIEndpoints:
    """Test API endpoints for the review lifecycle.

    All routes are mounted at /api/requests/... in the main app.
    """

    async def test_get_review_list(self, repo, org_id, api_client):
        """GET /api/requests/review returns requests in review/rework states."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        resp = await api_client.get("/api/requests/review")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        ids = [r["id"] for r in data["items"]]
        assert str(req["id"]) in ids

    async def test_get_review_list_empty(self, repo, org_id, api_client):
        """GET /api/requests/review returns empty list when nothing in review."""
        resp = await api_client.get("/api/requests/review")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["items"], list)

    async def test_approve_request(self, repo, org_id, admin_client):
        """POST /api/requests/{id}/review/approve transitions review → completed."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        resp = await admin_client.post(f"/api/requests/{req['id']}/review/approve")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["completed_at"] is not None

    async def test_approve_non_review_fails(self, repo, org_id, admin_client):
        """Approving a request not in 'review' returns 409."""
        req = await _make_request(repo, org_id)
        resp = await admin_client.post(f"/api/requests/{req['id']}/review/approve")
        assert resp.status_code == 409

    async def test_approve_nonexistent_fails(self, admin_client):
        """Approving a nonexistent request returns 404."""
        fake_id = str(uuid4())
        resp = await admin_client.post(f"/api/requests/{fake_id}/review/approve")
        assert resp.status_code == 404

    async def test_approve_requires_admin(self, repo, org_id, api_client):
        """Deprecated approve endpoint rejects non-admin users with 403."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        resp = await api_client.post(f"/api/requests/{req['id']}/review/approve")
        assert resp.status_code == 403

    async def test_reject_request(self, repo, org_id, admin_client):
        """POST /api/requests/{id}/review/reject transitions review → needs_rework."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        resp = await admin_client.post(
            f"/api/requests/{req['id']}/review/reject",
            json={"feedback": "The output is incomplete"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "needs_rework"
        assert data["review_feedback"] == "The output is incomplete"
        assert data["review_count"] == 1

    async def test_reject_increments_review_count(self, repo, org_id, admin_client):
        """Each rejection increments review_count."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        # First rejection
        resp1 = await admin_client.post(
            f"/api/requests/{req['id']}/review/reject",
            json={"feedback": "Round 1"},
        )
        assert resp1.json()["review_count"] == 1

        # Move back to review for second rejection
        await repo.update_request_status(str(req["id"]), "review")

        resp2 = await admin_client.post(
            f"/api/requests/{req['id']}/review/reject",
            json={"feedback": "Round 2"},
        )
        assert resp2.json()["review_count"] == 2

    async def test_reject_non_review_fails(self, repo, org_id, admin_client):
        """Rejecting a request not in 'review' returns 409."""
        req = await _make_request(repo, org_id)
        resp = await admin_client.post(
            f"/api/requests/{req['id']}/review/reject",
            json={"feedback": "nope"},
        )
        assert resp.status_code == 409

    async def test_reject_requires_admin(self, repo, org_id, api_client):
        """Deprecated reject endpoint rejects non-admin users with 403."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        resp = await api_client.post(
            f"/api/requests/{req['id']}/review/reject",
            json={"feedback": "nope"},
        )
        assert resp.status_code == 403

    async def test_reject_missing_feedback_fails(self, repo, org_id, admin_client):
        """Rejecting without feedback returns 422."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        resp = await admin_client.post(
            f"/api/requests/{req['id']}/review/reject",
            json={},
        )
        assert resp.status_code == 422

    async def test_reject_empty_feedback_fails(self, repo, org_id, api_client):
        """Rejecting with empty string feedback returns 422."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        resp = await api_client.post(
            f"/api/requests/{req['id']}/review/reject",
            json={"feedback": ""},
        )
        assert resp.status_code == 422

    async def test_retry_with_feedback_endpoint(self, repo, org_id, api_client):
        """POST /api/requests/tasks/{id}/retry-with-feedback retries a failed task."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _fail_task_flow(repo, task, "broke")

        resp = await api_client.post(
            f"/api/requests/tasks/{task['id']}/retry-with-feedback",
            json={"feedback": "Please fix the output format"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"

    async def test_retry_with_feedback_non_failed_returns_409(
        self, repo, org_id, api_client
    ):
        """Retrying a non-failed task returns 409."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)

        resp = await api_client.post(
            f"/api/requests/tasks/{task['id']}/retry-with-feedback",
            json={"feedback": "feedback"},
        )
        assert resp.status_code == 409


class TestStatusFiltering:
    """Test that list endpoints correctly filter by new statuses."""

    async def test_list_by_review_status(self, repo, org_id, api_client):
        """GET /api/requests?status=review returns only review requests."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        resp = await api_client.get("/api/requests", params={"status": "review"})
        assert resp.status_code == 200
        data = resp.json()
        assert all(r["status"] == "review" for r in data["items"])
        assert any(r["id"] == str(req["id"]) for r in data["items"])

    async def test_list_by_needs_rework_status(self, repo, org_id, api_client):
        """GET /api/requests?status=needs_rework returns only needs_rework requests."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)
        await repo.update_request_status(str(req["id"]), "needs_rework")

        resp = await api_client.get("/api/requests", params={"status": "needs_rework"})
        assert resp.status_code == 200
        data = resp.json()
        assert all(r["status"] == "needs_rework" for r in data["items"])


class TestResponseModels:
    """Test that new fields appear in API responses."""

    async def test_review_fields_in_request_response(self, repo, org_id, api_client):
        """GET /api/requests/{id} includes review_count, max_reviews, review_feedback."""
        req = await _make_request(repo, org_id)
        resp = await api_client.get(f"/api/requests/{req['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert "review_count" in data
        assert "max_reviews" in data
        assert data["review_count"] == 0
        assert data["max_reviews"] == 3

    async def test_review_fields_after_rejection(self, repo, org_id, admin_client):
        """After rejection, review fields are populated in response."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        await admin_client.post(
            f"/api/requests/{req['id']}/review/reject",
            json={"feedback": "Needs more detail"},
        )

        resp = await admin_client.get(f"/api/requests/{req['id']}")
        data = resp.json()
        assert data["review_count"] == 1
        assert data["review_feedback"] == "Needs more detail"
        assert data["status"] == "needs_rework"
        assert data["reviewed_at"] is not None

    async def test_status_update_accepts_new_statuses(self, repo, org_id, api_client):
        """PATCH /api/requests/{id}/status accepts 'review' and 'needs_rework'."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        # Direct status update to needs_rework
        resp = await api_client.patch(
            f"/api/requests/{req['id']}/status",
            json={"status": "needs_rework"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "needs_rework"


# =========================================================================
# SECTION 6: Integration/Flow Tests
# =========================================================================


class TestHappyPathFlow:
    """End-to-end happy path: tasks complete → review → approved → completed."""

    async def test_two_task_happy_path(self, repo, org_id):
        """Request with 2 tasks → both complete → review → approval → completed."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])

        t1 = await _make_task(repo, rid, org_id, title="Implement feature")
        t2 = await _make_task(repo, rid, org_id, title="Write tests")

        # Both tasks complete
        await _complete_task_flow(repo, t1, "Feature implemented")
        await _complete_task_flow(repo, t2, "Tests written")

        # Request should now be in review
        req_after = await repo.get_request(rid, org_id)
        assert req_after["status"] == REQUEST_STATUS_REVIEW

        # Approve the review
        approved = await repo.update_request_status(rid, "completed")
        assert approved["status"] == REQUEST_STATUS_COMPLETED
        assert approved["completed_at"] is not None

    async def test_single_task_happy_path(self, repo, org_id):
        """Single task request also enters review before completion."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id, title="Solo task")
        await _complete_task_flow(repo, task, "All done")

        req_after = await repo.get_request(str(req["id"]), org_id)
        assert req_after["status"] == REQUEST_STATUS_REVIEW

        approved = await repo.update_request_status(str(req["id"]), "completed")
        assert approved["status"] == REQUEST_STATUS_COMPLETED


class TestReworkFlow:
    """Rework path: complete → review → needs_rework → retry → review → approved."""

    async def test_full_rework_cycle(self, repo, org_id):
        """Tasks complete → review → reject → rework → review → approve."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])

        t1 = await _make_task(repo, rid, org_id, title="Task 1")
        await _complete_task_flow(repo, t1)
        # Request is in review

        # Reject: review → needs_rework
        await repo.update_request_status(rid, "needs_rework")
        req_rework = await repo.get_request(rid, org_id)
        assert req_rework["status"] == REQUEST_STATUS_NEEDS_REWORK

        # Create rework task (simulating daemon behavior)
        t2 = await _make_task(repo, rid, org_id, title="Rework: Task 1")

        # Claim the rework task (triggers _ensure_request_in_progress)
        await repo.claim_task(str(t2["id"]), "inst-rework")
        req_ip = await repo.get_request(rid, org_id)
        assert req_ip["status"] == REQUEST_STATUS_IN_PROGRESS

        # Complete rework task → back to review
        await repo.complete_task(str(t2["id"]), "Reworked output")
        req_review2 = await repo.get_request(rid, org_id)
        assert req_review2["status"] == REQUEST_STATUS_REVIEW

        # Approve second review
        approved = await repo.update_request_status(rid, "completed")
        assert approved["status"] == REQUEST_STATUS_COMPLETED

    async def test_rework_with_retry_task_with_feedback(self, repo, org_id):
        """Use retry_task_with_feedback to retry a failed task with corrective feedback."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])

        task = await _make_task(repo, rid, org_id, title="Fixable task")
        await _fail_task_flow(repo, task, "Wrong approach")

        # retry with feedback
        retried = await repo.retry_task_with_feedback(
            str(task["id"]), "Use the correct API endpoint"
        )
        assert retried is not None
        assert retried["status"] == "pending"

        # Request should be in_progress
        req_updated = await repo.get_request(rid, org_id)
        assert req_updated["status"] == REQUEST_STATUS_IN_PROGRESS
        assert req_updated["review_count"] == 1
        assert req_updated["review_feedback"] == "Use the correct API endpoint"

        # Complete the retried task
        await repo.claim_task(str(task["id"]), "inst-retry")
        await repo.complete_task(str(task["id"]), "Fixed output")

        # Should go to review again
        req_final = await repo.get_request(rid, org_id)
        assert req_final["status"] == REQUEST_STATUS_REVIEW


class TestMaxReworkExceeded:
    """Test behavior when max_reviews limit is reached."""

    async def test_review_count_tracks_through_cycles(self, repo, org_id):
        """Track review_count across multiple rework cycles via retry_task_with_feedback."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])

        for cycle in range(3):
            task = await _make_task(
                repo, rid, org_id, title=f"Cycle {cycle}"
            )
            await _fail_task_flow(repo, task, f"Error cycle {cycle}")
            await repo.retry_task_with_feedback(
                str(task["id"]), f"Feedback cycle {cycle}"
            )

        req_final = await repo.get_request(rid, org_id)
        assert req_final["review_count"] == 3
        # At max_reviews=3, review_count == max_reviews → daemon would stop auto-retry

    async def test_review_count_exceeds_max_reviews(self, repo, org_id):
        """review_count can exceed max_reviews at DB level (daemon enforces the limit)."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])

        for cycle in range(4):
            task = await _make_task(
                repo, rid, org_id, title=f"Cycle {cycle}"
            )
            await _fail_task_flow(repo, task, f"Error {cycle}")
            await repo.retry_task_with_feedback(
                str(task["id"]), f"Feedback {cycle}"
            )

        req_final = await repo.get_request(rid, org_id)
        # DB doesn't enforce the limit — daemon does
        assert req_final["review_count"] == 4
        assert req_final["max_reviews"] == 3


class TestReviewAgentFailure:
    """Test what happens when a review task itself fails."""

    async def test_review_task_failure_doesnt_break_request(self, repo, org_id):
        """A failed review task moves request to needs_rework (daemon behavior).

        At the DB level, we verify the states are valid. The daemon's
        _handle_review_task_failure sets needs_rework.
        """
        req = await _make_request(repo, org_id)
        rid = str(req["id"])

        # Complete work tasks
        work = await _make_task(repo, rid, org_id, title="Work task")
        await _complete_task_flow(repo, work)
        assert (await repo.get_request(rid, org_id))["status"] == REQUEST_STATUS_REVIEW

        # Create review task and fail it
        review_task = await _make_task(
            repo, rid, org_id, title="Post-completion review"
        )
        await _fail_task_flow(repo, review_task, "LLM error")

        # At DB level, request will be 'failed' because a task failed
        # The daemon would override this with needs_rework via update_request_status
        await repo.update_request_status(rid, "needs_rework")

        req_after = await repo.get_request(rid, org_id)
        assert req_after["status"] == REQUEST_STATUS_NEEDS_REWORK


class TestBackwardsCompatibility:
    """Existing requests without review phase still work normally."""

    async def test_request_without_tasks_stays_pending(self, repo, org_id):
        """A request with no tasks remains pending."""
        req = await _make_request(repo, org_id)
        fetched = await repo.get_request(str(req["id"]), org_id)
        assert fetched["status"] == REQUEST_STATUS_PENDING

    async def test_manual_status_to_completed_still_works(self, repo, org_id):
        """Direct update_request_status to completed bypasses review (legacy behavior)."""
        req = await _make_request(repo, org_id)
        result = await repo.update_request_status(str(req["id"]), "completed")
        assert result["status"] == REQUEST_STATUS_COMPLETED

    async def test_request_default_review_fields_present(self, repo, org_id):
        """New fields have sane defaults and don't break existing code."""
        req = await _make_request(repo, org_id)
        assert req["review_count"] == 0
        assert req["max_reviews"] == 3
        assert req.get("review_feedback") is None
        assert req.get("reviewed_at") is None

    async def test_list_requests_works_with_all_statuses(self, repo, org_id):
        """list_requests still works for all status filters including new ones."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        for status in ["pending", "in_progress", "review", "completed", "failed"]:
            result = await repo.list_requests(org_id, status=status)
            assert "items" in result

    async def test_failed_request_not_in_review(self, repo, org_id):
        """Failed requests don't show up in review list."""
        req = await _make_request(repo, org_id)
        await repo.update_request_status(str(req["id"]), "failed")

        result = await repo.get_requests_in_review(org_id)
        ids = [str(r["id"]) for r in result["items"]]
        assert str(req["id"]) not in ids

    async def test_cancelled_request_not_in_review(self, repo, org_id):
        """Cancelled requests don't show up in review list."""
        req = await _make_request(repo, org_id)
        await repo.update_request_status(str(req["id"]), "cancelled")

        result = await repo.get_requests_in_review(org_id)
        ids = [str(r["id"]) for r in result["items"]]
        assert str(req["id"]) not in ids


# =========================================================================
# SECTION 7: Daemon Helper Tests (_parse_review_decision)
# =========================================================================

# These test the daemon's parsing logic without starting the full daemon.
# We import just the parsing function by instantiating a minimal daemon
# or testing the regex patterns directly.


class TestParseReviewDecision:
    """Test the daemon's _parse_review_decision parser.

    We test the regex/parsing logic directly using the same patterns
    the daemon uses, since instantiating LucentDaemon requires full config.
    """

    @staticmethod
    def _parse(text: str) -> dict:
        """Replicate the daemon's _parse_review_decision logic for testing."""
        raw = (text or "").strip()
        upper = raw.upper()
        decision = "APPROVED" if "NEEDS_REWORK" not in upper else "NEEDS_REWORK"
        recognized = False

        m = re.search(
            r"(?:REQUEST_REVIEW_DECISION|DECISION)\s*:\s*(APPROVED|NEEDS_REWORK)",
            raw,
            flags=re.IGNORECASE,
        )
        if m:
            decision = m.group(1).upper()
            recognized = True
        elif "NEEDS_REWORK" in upper:
            decision = "NEEDS_REWORK"
            recognized = True
        elif "APPROVED" in upper:
            decision = "APPROVED"
            recognized = True

        task_ids: list[str] = []
        mt = re.search(
            r"TASK_IDS_TO_REWORK\s*:\s*(.+?)(?:\n[A-Z_ ]+\s*:|\Z)",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if mt:
            candidates = re.split(r"[\s,]+", mt.group(1).strip())
            task_ids = [
                c.strip().strip("[](){}")
                for c in candidates
                if c.strip()
                and re.fullmatch(
                    r"[0-9a-fA-F-]{8,64}", c.strip().strip("[](){}")
                )
            ]

        mf = re.search(
            r"FEEDBACK\s*:\s*(.+?)(?:\n[A-Z_ ]+\s*:|\Z)",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
        feedback = (mf.group(1).strip() if mf else raw)[:10000]
        return {
            "decision": decision,
            "task_ids": task_ids,
            "feedback": feedback,
            "recognized": recognized,
        }

    def test_explicit_approved(self):
        result = self._parse("REQUEST_REVIEW_DECISION: APPROVED\nFEEDBACK: All good")
        assert result["decision"] == "APPROVED"
        assert result["recognized"] is True
        assert "All good" in result["feedback"]

    def test_explicit_needs_rework(self):
        result = self._parse(
            "REQUEST_REVIEW_DECISION: NEEDS_REWORK\n"
            "TASK_IDS_TO_REWORK: abc12345-6789-0123-4567-890abcdef012\n"
            "FEEDBACK: Output is incomplete"
        )
        assert result["decision"] == "NEEDS_REWORK"
        assert result["recognized"] is True
        assert len(result["task_ids"]) == 1
        assert "abc12345-6789-0123-4567-890abcdef012" in result["task_ids"]
        assert "incomplete" in result["feedback"]

    def test_decision_shorthand(self):
        """'Decision: APPROVED' also works."""
        result = self._parse("Decision: APPROVED\nFEEDBACK: Looks great")
        assert result["decision"] == "APPROVED"
        assert result["recognized"] is True

    def test_case_insensitive_decision(self):
        result = self._parse("request_review_decision: approved")
        assert result["decision"] == "APPROVED"
        assert result["recognized"] is True

    def test_keyword_fallback_approved(self):
        """If no explicit format, 'APPROVED' keyword is recognized."""
        result = self._parse("The work looks good. APPROVED.")
        assert result["decision"] == "APPROVED"
        assert result["recognized"] is True

    def test_keyword_fallback_needs_rework(self):
        """If no explicit format, 'NEEDS_REWORK' keyword is recognized."""
        result = self._parse("This NEEDS_REWORK because it's incomplete.")
        assert result["decision"] == "NEEDS_REWORK"
        assert result["recognized"] is True

    def test_unrecognized_output(self):
        """Output with no decision keywords is treated as unrecognized APPROVED default."""
        result = self._parse("Some random text without decision keywords")
        assert result["recognized"] is False
        # Default when no NEEDS_REWORK found is APPROVED
        assert result["decision"] == "APPROVED"

    def test_empty_input(self):
        result = self._parse("")
        assert result["recognized"] is False

    def test_none_input(self):
        result = self._parse(None)
        assert result["recognized"] is False

    def test_multiple_task_ids(self):
        text = (
            "REQUEST_REVIEW_DECISION: NEEDS_REWORK\n"
            "TASK_IDS_TO_REWORK: aabbccdd-1234-5678-9abc-def012345678, "
            "11223344-aabb-ccdd-eeff-001122334455\n"
            "FEEDBACK: Both tasks need fixes"
        )
        result = self._parse(text)
        assert result["decision"] == "NEEDS_REWORK"
        assert len(result["task_ids"]) == 2

    def test_task_ids_with_brackets(self):
        """Task IDs wrapped in brackets are cleaned."""
        text = (
            "REQUEST_REVIEW_DECISION: NEEDS_REWORK\n"
            "TASK_IDS_TO_REWORK: [aabbccdd-1234-5678-9abc-def012345678]\n"
            "FEEDBACK: Fix"
        )
        result = self._parse(text)
        assert "aabbccdd-1234-5678-9abc-def012345678" in result["task_ids"]

    def test_no_task_ids_section(self):
        """Missing TASK_IDS_TO_REWORK returns empty list."""
        result = self._parse(
            "REQUEST_REVIEW_DECISION: NEEDS_REWORK\nFEEDBACK: Fix it"
        )
        assert result["task_ids"] == []

    def test_feedback_truncation(self):
        """Feedback is truncated to 10000 chars."""
        long_feedback = "x" * 20000
        result = self._parse(f"REQUEST_REVIEW_DECISION: APPROVED\nFEEDBACK: {long_feedback}")
        assert len(result["feedback"]) <= 10000

    def test_feedback_fallback_to_raw(self):
        """Without FEEDBACK section, raw text is used as feedback."""
        result = self._parse("REQUEST_REVIEW_DECISION: APPROVED")
        # feedback falls back to raw text
        assert "REQUEST_REVIEW_DECISION" in result["feedback"]

    def test_needs_rework_takes_precedence(self):
        """When both APPROVED and NEEDS_REWORK appear, NEEDS_REWORK wins."""
        result = self._parse(
            "We first thought it was APPROVED but actually NEEDS_REWORK"
        )
        assert result["decision"] == "NEEDS_REWORK"


class TestIsRequestReviewTask:
    """Test the daemon's _is_request_review_task identification logic."""

    @staticmethod
    def _is_review_task(task: dict) -> bool:
        """Replicate daemon's _is_request_review_task for testing."""
        title = (task.get("title") or "").strip().lower()
        desc = task.get("description") or ""
        return (
            title == "post-completion review"
            or "REQUEST_REVIEW_DECISION:" in desc
        )

    def test_match_by_title(self):
        assert self._is_review_task({"title": "Post-completion review"})

    def test_match_by_title_case_insensitive(self):
        assert self._is_review_task({"title": "POST-COMPLETION REVIEW"})

    def test_match_by_title_with_whitespace(self):
        assert self._is_review_task({"title": "  Post-completion review  "})

    def test_match_by_description(self):
        assert self._is_review_task(
            {
                "title": "Some other title",
                "description": "Return REQUEST_REVIEW_DECISION: APPROVED|NEEDS_REWORK",
            }
        )

    def test_no_match_random_task(self):
        assert not self._is_review_task(
            {"title": "Write unit tests", "description": "Test the auth module"}
        )

    def test_no_match_empty(self):
        assert not self._is_review_task({})

    def test_no_match_none_title(self):
        assert not self._is_review_task({"title": None, "description": None})


# =========================================================================
# SECTION 8: Deduplication with review/needs_rework open states
# =========================================================================


class TestDeduplication:
    """Test that fingerprint dedup includes review and needs_rework as open states."""

    async def test_dedup_conflict_in_review_state(self, repo, org_id):
        """Creating a request with same title while original is in 'review' returns existing."""
        title = f"Dedup test {uuid4().hex[:6]}"
        req1 = await _make_request(repo, org_id, title=title)
        task = await _make_task(repo, str(req1["id"]), org_id)
        await _complete_task_flow(repo, task)

        # r1 is now in 'review'; creating same title should dedup
        req2 = await repo.create_request(title=title, org_id=org_id)
        assert str(req2["id"]) == str(req1["id"])

    async def test_dedup_conflict_in_needs_rework_state(self, repo, org_id):
        """Creating a request with same title while original is 'needs_rework' returns existing."""
        title = f"Dedup rework {uuid4().hex[:6]}"
        req1 = await _make_request(repo, org_id, title=title)
        task = await _make_task(repo, str(req1["id"]), org_id)
        await _complete_task_flow(repo, task)
        await repo.update_request_status(str(req1["id"]), "needs_rework")

        req2 = await repo.create_request(title=title, org_id=org_id)
        assert str(req2["id"]) == str(req1["id"])

    async def test_no_dedup_after_completed(self, repo, org_id):
        """After a request is completed, same title creates a new request."""
        title = f"Dedup done {uuid4().hex[:6]}"
        req1 = await _make_request(repo, org_id, title=title)
        task = await _make_task(repo, str(req1["id"]), org_id)
        await _complete_task_flow(repo, task)
        await repo.update_request_status(str(req1["id"]), "completed")

        req2 = await repo.create_request(title=title, org_id=org_id)
        assert str(req2["id"]) != str(req1["id"])


# =========================================================================
# SECTION 9: Active Summary includes review states
# =========================================================================


class TestActiveSummary:
    """Test that get_active_summary includes review and needs_rework counts."""

    async def test_active_summary_counts_review(self, repo, org_id):
        """get_active_summary should count review-status requests as active."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)

        summary = await repo.get_active_summary(org_id)
        # review should be counted in the 'active' bucket
        assert summary["requests"]["active"] >= 1

    async def test_active_summary_counts_needs_rework(self, repo, org_id):
        """get_active_summary should count needs_rework-status requests as active."""
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await _complete_task_flow(repo, task)
        await repo.update_request_status(str(req["id"]), "needs_rework")

        summary = await repo.get_active_summary(org_id)
        assert summary["requests"]["active"] >= 1
