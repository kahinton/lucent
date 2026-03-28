"""Tests for the /api/reviews REST endpoints.

Covers: CRUD, authentication, authorization, input validation,
pagination, HTTP status codes, cross-org isolation, and side effects.
"""

from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.requests import RequestRepository
from lucent.db.reviews import ReviewRepository

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def rv_api_prefix(db_pool):
    """Unique prefix and cleanup for review API tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_rva_{test_id}_"
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
            await conn.execute(
                "DELETE FROM task_events WHERE task_id IN "
                "(SELECT id FROM tasks WHERE organization_id = $1)", oid
            )
            await conn.execute(
                "DELETE FROM task_memories WHERE task_id IN "
                "(SELECT id FROM tasks WHERE organization_id = $1)", oid
            )
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
async def rv_api_org(db_pool, rv_api_prefix):
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{rv_api_prefix}org")


@pytest_asyncio.fixture
async def rv_api_user(db_pool, rv_api_org, rv_api_prefix):
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{rv_api_prefix}user",
        provider="local",
        organization_id=rv_api_org["id"],
        email=f"{rv_api_prefix}user@test.com",
        display_name=f"{rv_api_prefix}Reviewer",
    )


@pytest_asyncio.fixture
async def rv_api_other_org(db_pool, rv_api_prefix):
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{rv_api_prefix}other_org")


@pytest_asyncio.fixture
async def rv_api_other_user(db_pool, rv_api_other_org, rv_api_prefix):
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{rv_api_prefix}other_user",
        provider="local",
        organization_id=rv_api_other_org["id"],
        email=f"{rv_api_prefix}other@test.com",
        display_name=f"{rv_api_prefix}Other User",
    )


async def _make_api_client(user, scopes=None):
    """Build httpx client with dependency-override auth."""
    app = create_app()
    fake_user = CurrentUser(
        id=user["id"],
        organization_id=user["organization_id"],
        role=user.get("role", "member"),
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=scopes or ["read", "write"],
    )

    async def override():
        return fake_user

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, app


@pytest_asyncio.fixture
async def client(rv_api_user):
    c, app = await _make_api_client(rv_api_user)
    async with c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def other_client(rv_api_other_user):
    c, app = await _make_api_client(rv_api_other_user)
    async with c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
def req_repo(db_pool):
    return RequestRepository(db_pool)


@pytest_asyncio.fixture
def review_repo(db_pool):
    return ReviewRepository(db_pool)


@pytest_asyncio.fixture
def org_id(rv_api_org):
    return str(rv_api_org["id"])


@pytest_asyncio.fixture
async def api_request(req_repo, org_id):
    """Test request for API tests."""
    return await req_repo.create_request(title="API Review Request", org_id=org_id)


@pytest_asyncio.fixture
async def api_task(req_repo, api_request, org_id):
    """Test task for API tests."""
    return await req_repo.create_task(
        request_id=str(api_request["id"]),
        title="API Review Task",
        org_id=org_id,
    )


# ── Create Review Endpoint ───────────────────────────────────────────────


class TestCreateReviewEndpoint:
    async def test_create_approved_201(self, client, api_request):
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "approved",
            "comments": "Ship it",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "approved"
        assert data["comments"] == "Ship it"
        assert data["request_id"] == str(api_request["id"])
        assert "id" in data
        assert "created_at" in data

    async def test_create_rejected_201(self, client, api_request):
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "rejected",
            "comments": "Needs more work",
        })
        assert resp.status_code == 201
        assert resp.json()["status"] == "rejected"

    async def test_create_with_task_id(self, client, api_request, api_task):
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "task_id": str(api_task["id"]),
            "status": "approved",
        })
        assert resp.status_code == 201
        assert resp.json()["task_id"] == str(api_task["id"])

    async def test_create_with_source(self, client, api_request):
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "approved",
            "source": "daemon",
        })
        assert resp.status_code == 201
        assert resp.json()["source"] == "daemon"

    async def test_rejection_without_comments_422(self, client, api_request):
        """Rejections require comments."""
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "rejected",
        })
        assert resp.status_code == 422

    async def test_invalid_status_422(self, client, api_request):
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "maybe",
        })
        assert resp.status_code == 422

    async def test_invalid_source_422(self, client, api_request):
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "approved",
            "source": "bot",
        })
        assert resp.status_code == 422

    async def test_nonexistent_request_404(self, client):
        resp = await client.post("/api/reviews", json={
            "request_id": str(uuid4()),
            "status": "approved",
        })
        assert resp.status_code == 404

    async def test_nonexistent_task_404(self, client, api_request):
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "task_id": str(uuid4()),
            "status": "approved",
        })
        assert resp.status_code == 404

    async def test_task_wrong_request_422(self, client, req_repo, api_request, api_task, org_id):
        """Task must belong to the specified request."""
        req2 = await req_repo.create_request(title="Different Req", org_id=org_id)
        resp = await client.post("/api/reviews", json={
            "request_id": str(req2["id"]),
            "task_id": str(api_task["id"]),
            "status": "approved",
        })
        assert resp.status_code == 422

    async def test_missing_request_id_422(self, client):
        resp = await client.post("/api/reviews", json={
            "status": "approved",
        })
        assert resp.status_code == 422

    async def test_missing_status_422(self, client, api_request):
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
        })
        assert resp.status_code == 422

    async def test_reviewer_info_populated(self, client, api_request, rv_api_user):
        """Review should have the authenticated user as reviewer."""
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "approved",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["reviewer_user_id"] == str(rv_api_user["id"])


# ── Authentication ───────────────────────────────────────────────────────


class TestReviewAuthentication:
    async def test_unauthenticated_create_401(self, api_request):
        """No auth header → 401."""
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as unauth_client:
            resp = await unauth_client.post("/api/reviews", json={
                "request_id": str(api_request["id"]),
                "status": "approved",
            })
        assert resp.status_code == 401

    async def test_unauthenticated_list_401(self):
        """No auth header on list → 401."""
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as unauth_client:
            resp = await unauth_client.get("/api/reviews")
        assert resp.status_code == 401

    async def test_unauthenticated_get_401(self):
        """No auth header on single get → 401."""
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as unauth_client:
            resp = await unauth_client.get(f"/api/reviews/{uuid4()}")
        assert resp.status_code == 401


# ── Authorization (Cross-Org Isolation) ──────────────────────────────────


class TestReviewAuthorization:
    async def test_cross_org_create_404(
        self, other_client, api_request
    ):
        """User from org B cannot create review on org A's request (gets 404)."""
        resp = await other_client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "approved",
        })
        # Request doesn't exist in org B, so 404
        assert resp.status_code == 404

    async def test_cross_org_get_review_404(
        self, client, other_client, api_request, org_id, review_repo
    ):
        """User from org B cannot get review from org A."""
        review = await review_repo.create_review(
            request_id=str(api_request["id"]),
            organization_id=org_id,
            status="approved",
        )
        resp = await other_client.get(f"/api/reviews/{review['id']}")
        assert resp.status_code == 404

    async def test_cross_org_list_empty(
        self, client, other_client, api_request, org_id, review_repo
    ):
        """User from org B's list should not include org A's reviews."""
        await review_repo.create_review(
            request_id=str(api_request["id"]),
            organization_id=org_id,
            status="approved",
        )
        resp = await other_client.get("/api/reviews")
        assert resp.status_code == 200
        # Should see 0 reviews (or at least none from org A)
        data = resp.json()
        org_a_reviews = [
            r for r in data["items"]
            if r["request_id"] == str(api_request["id"])
        ]
        assert len(org_a_reviews) == 0


# ── List Reviews Endpoint ────────────────────────────────────────────────


class TestListReviewsEndpoint:
    async def test_list_200(self, client, api_request, org_id, review_repo):
        await review_repo.create_review(
            request_id=str(api_request["id"]),
            organization_id=org_id,
            status="approved",
        )
        resp = await client.get("/api/reviews")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total_count" in data
        assert "offset" in data
        assert "limit" in data
        assert "has_more" in data

    async def test_filter_by_request_id(self, client, req_repo, api_request, org_id, review_repo):
        req2 = await req_repo.create_request(title="Req 2", org_id=org_id)
        await review_repo.create_review(
            request_id=str(api_request["id"]),
            organization_id=org_id,
            status="approved",
        )
        await review_repo.create_review(
            request_id=str(req2["id"]),
            organization_id=org_id,
            status="rejected",
            comments="No",
        )

        resp = await client.get(
            "/api/reviews", params={"request_id": str(api_request["id"])}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(
            r["request_id"] == str(api_request["id"]) for r in data["items"]
        )

    async def test_filter_by_status(self, client, api_request, org_id, review_repo):
        await review_repo.create_review(
            request_id=str(api_request["id"]),
            organization_id=org_id,
            status="approved",
        )
        await review_repo.create_review(
            request_id=str(api_request["id"]),
            organization_id=org_id,
            status="rejected",
            comments="Nope",
        )

        resp = await client.get("/api/reviews", params={"status": "approved"})
        assert resp.status_code == 200
        assert all(r["status"] == "approved" for r in resp.json()["items"])

    async def test_filter_by_source(self, client, api_request, org_id, review_repo):
        await review_repo.create_review(
            request_id=str(api_request["id"]),
            organization_id=org_id,
            status="approved",
            source="daemon",
        )
        resp = await client.get("/api/reviews", params={"source": "daemon"})
        assert resp.status_code == 200
        assert all(r["source"] == "daemon" for r in resp.json()["items"])

    async def test_pagination_limit_offset(self, client, api_request, org_id, review_repo):
        for _ in range(5):
            await review_repo.create_review(
                request_id=str(api_request["id"]),
                organization_id=org_id,
                status="approved",
            )
        resp = await client.get("/api/reviews", params={"limit": 2, "offset": 0})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["has_more"] is True

    async def test_invalid_status_filter_422(self, client):
        resp = await client.get("/api/reviews", params={"status": "invalid"})
        assert resp.status_code == 422

    async def test_invalid_source_filter_422(self, client):
        resp = await client.get("/api/reviews", params={"source": "bot"})
        assert resp.status_code == 422


# ── Get Review Endpoint ──────────────────────────────────────────────────


class TestGetReviewEndpoint:
    async def test_get_existing_200(self, client, api_request, org_id, review_repo):
        review = await review_repo.create_review(
            request_id=str(api_request["id"]),
            organization_id=org_id,
            status="approved",
            comments="Nice work",
        )
        resp = await client.get(f"/api/reviews/{review['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(review["id"])
        assert data["comments"] == "Nice work"
        assert data["request_title"] == "API Review Request"

    async def test_get_nonexistent_404(self, client):
        resp = await client.get(f"/api/reviews/{uuid4()}")
        assert resp.status_code == 404


# ── Get Reviews by Request/Task ──────────────────────────────────────────


class TestGetReviewsByRequest:
    async def test_by_request_200(self, client, api_request, org_id, review_repo):
        await review_repo.create_review(
            request_id=str(api_request["id"]),
            organization_id=org_id,
            status="approved",
        )
        resp = await client.get(f"/api/reviews/by-request/{api_request['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    async def test_by_request_empty(self, client, req_repo, org_id):
        req = await req_repo.create_request(title="No Reviews", org_id=org_id)
        resp = await client.get(f"/api/reviews/by-request/{req['id']}")
        assert resp.status_code == 200
        assert resp.json() == []


class TestGetReviewsByTask:
    async def test_by_task_200(self, client, api_request, api_task, org_id, review_repo):
        await review_repo.create_review(
            request_id=str(api_request["id"]),
            organization_id=org_id,
            status="approved",
            task_id=str(api_task["id"]),
        )
        resp = await client.get(f"/api/reviews/by-task/{api_task['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    async def test_by_task_empty(self, client, api_task):
        resp = await client.get(f"/api/reviews/by-task/{api_task['id']}")
        assert resp.status_code == 200
        assert resp.json() == []


# ── Review Summary Endpoint ──────────────────────────────────────────────


class TestReviewSummaryEndpoint:
    async def test_summary_200(self, client, api_request, org_id, review_repo):
        await review_repo.create_review(
            request_id=str(api_request["id"]),
            organization_id=org_id,
            status="approved",
            source="human",
        )
        await review_repo.create_review(
            request_id=str(api_request["id"]),
            organization_id=org_id,
            status="rejected",
            source="daemon",
            comments="Bad",
        )
        resp = await client.get("/api/reviews/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "approved" in data
        assert "rejected" in data
        assert "human_reviews" in data
        assert "daemon_reviews" in data
        assert "agent_reviews" in data
        assert data["total"] >= 2


# ── Side Effects ─────────────────────────────────────────────────────────


class TestReviewSideEffects:
    async def test_approval_transitions_request_to_completed(
        self, client, req_repo, api_request, org_id
    ):
        """Approving a request in 'review' status auto-transitions to 'completed'."""
        # Put request in review status
        task = await req_repo.create_task(
            request_id=str(api_request["id"]),
            title="Side effect task",
            org_id=org_id,
        )
        await req_repo.claim_task(str(task["id"]), "test")
        await req_repo.complete_task(str(task["id"]), "Done")

        # Verify request is in review
        req = await req_repo.get_request(str(api_request["id"]), org_id)
        assert req["status"] == "review"

        # Create approval review via API
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "approved",
            "comments": "Ship it",
        })
        assert resp.status_code == 201

        # Check request transitioned to completed
        req = await req_repo.get_request(str(api_request["id"]), org_id)
        assert req["status"] == "completed"

    async def test_rejection_transitions_request_to_needs_rework(
        self, client, req_repo, api_request, org_id
    ):
        """Rejecting a request in 'review' status auto-transitions to 'needs_rework'."""
        task = await req_repo.create_task(
            request_id=str(api_request["id"]),
            title="Side effect task",
            org_id=org_id,
        )
        await req_repo.claim_task(str(task["id"]), "test")
        await req_repo.complete_task(str(task["id"]), "Done")

        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "rejected",
            "comments": "Needs improvement",
        })
        assert resp.status_code == 201

        req = await req_repo.get_request(str(api_request["id"]), org_id)
        assert req["status"] == "needs_rework"
        assert req["review_feedback"] == "Needs improvement"
        assert req["review_count"] == 1

    async def test_approval_on_pending_request_no_transition(
        self, client, api_request, req_repo, org_id
    ):
        """Approving a request NOT in 'review' status should NOT transition it."""
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "approved",
        })
        assert resp.status_code == 201

        # Request should still be pending
        req = await req_repo.get_request(str(api_request["id"]), org_id)
        assert req["status"] == "pending"

    async def test_approval_creates_tracked_request(
        self, client, req_repo, api_request, org_id, db_pool
    ):
        """Approving a request in 'review' status should create a tracked request
        from the approved content so it enters the processing pipeline."""
        # Put request in review status
        task = await req_repo.create_task(
            request_id=str(api_request["id"]),
            title="Approval side effect task",
            org_id=org_id,
        )
        await req_repo.claim_task(str(task["id"]), "test")
        await req_repo.complete_task(str(task["id"]), "Done")

        # Verify request is in review
        req = await req_repo.get_request(str(api_request["id"]), org_id)
        assert req["status"] == "review"

        # Count existing requests before approval
        async with db_pool.acquire() as conn:
            before_count = await conn.fetchval(
                "SELECT COUNT(*) FROM requests WHERE organization_id = $1",
                UUID(org_id),
            )

        # Create approval review via API
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "approved",
            "comments": "Ship it, create a follow-up",
        })
        assert resp.status_code == 201

        # Verify a new tracked request was created
        async with db_pool.acquire() as conn:
            after_count = await conn.fetchval(
                "SELECT COUNT(*) FROM requests WHERE organization_id = $1",
                UUID(org_id),
            )
            # Should have one more request (the auto-created one)
            assert after_count == before_count + 1

            # Find the auto-created request
            auto_req = await conn.fetchrow(
                "SELECT * FROM requests WHERE organization_id = $1 "
                "AND title LIKE 'Approved: %' "
                "ORDER BY created_at DESC LIMIT 1",
                UUID(org_id),
            )
            assert auto_req is not None
            assert "API Review Request" in auto_req["title"]
            assert auto_req["source"] == "api"

    async def test_approval_tracked_request_is_idempotent(
        self, client, req_repo, api_request, org_id, db_pool
    ):
        """Retrying the same approval should not create duplicate requests
        because the fingerprint-based dedup prevents it."""
        # Put request in review status
        task = await req_repo.create_task(
            request_id=str(api_request["id"]),
            title="Idempotent approval task",
            org_id=org_id,
        )
        await req_repo.claim_task(str(task["id"]), "test")
        await req_repo.complete_task(str(task["id"]), "Done")

        # First approval
        resp1 = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "approved",
            "comments": "First approval",
        })
        assert resp1.status_code == 201

        # Count requests with the approved title
        async with db_pool.acquire() as conn:
            count_after_first = await conn.fetchval(
                "SELECT COUNT(*) FROM requests WHERE organization_id = $1 "
                "AND title LIKE 'Approved: %'",
                UUID(org_id),
            )

        # Second approval (retry) — request is now 'completed', so the side
        # effect won't fire again (status != 'review'). But even if it did,
        # the fingerprint dedup in create_request prevents duplicate inserts.
        resp2 = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "approved",
            "comments": "Second approval (retry)",
        })
        assert resp2.status_code == 201

        async with db_pool.acquire() as conn:
            count_after_second = await conn.fetchval(
                "SELECT COUNT(*) FROM requests WHERE organization_id = $1 "
                "AND title LIKE 'Approved: %'",
                UUID(org_id),
            )
        # Should not have created another request since the original
        # request is no longer in 'review' status
        assert count_after_second == count_after_first

    async def test_rejection_creates_learning_memory(
        self, client, req_repo, api_request, org_id, db_pool
    ):
        """Rejecting a request in 'review' status should create a memory
        tagged with 'rejection-lesson' and 'learning-extraction' and 'daemon'."""
        # Put request in review status
        task = await req_repo.create_task(
            request_id=str(api_request["id"]),
            title="Rejection learning task",
            org_id=org_id,
        )
        await req_repo.claim_task(str(task["id"]), "test")
        await req_repo.complete_task(str(task["id"]), "Done")

        # Verify request is in review
        req = await req_repo.get_request(str(api_request["id"]), org_id)
        assert req["status"] == "review"

        # Count existing rejection-lesson memories before rejection
        async with db_pool.acquire() as conn:
            before_count = await conn.fetchval(
                "SELECT COUNT(*) FROM memories "
                "WHERE organization_id = $1 AND 'rejection-lesson' = ANY(tags) "
                "AND deleted_at IS NULL",
                UUID(org_id),
            )

        # Create rejection review via API
        rejection_reason = "Missing error handling and needs more tests"
        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "rejected",
            "comments": rejection_reason,
        })
        assert resp.status_code == 201

        # Verify a rejection-lesson memory was created
        async with db_pool.acquire() as conn:
            after_count = await conn.fetchval(
                "SELECT COUNT(*) FROM memories "
                "WHERE organization_id = $1 AND 'rejection-lesson' = ANY(tags) "
                "AND deleted_at IS NULL",
                UUID(org_id),
            )
            assert after_count == before_count + 1

            # Verify the memory has the right tags and content
            lesson_memory = await conn.fetchrow(
                "SELECT * FROM memories "
                "WHERE organization_id = $1 AND 'rejection-lesson' = ANY(tags) "
                "AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                UUID(org_id),
            )
            assert lesson_memory is not None
            assert "learning-extraction" in lesson_memory["tags"]
            assert "daemon" in lesson_memory["tags"]
            assert rejection_reason in lesson_memory["content"]
            assert lesson_memory["type"] == "experience"

    async def test_rejection_on_pending_request_no_memory(
        self, client, api_request, req_repo, org_id, db_pool
    ):
        """Rejecting a request NOT in 'review' status should NOT create
        a rejection-lesson memory (side effects only fire for review→rejected)."""
        async with db_pool.acquire() as conn:
            before_count = await conn.fetchval(
                "SELECT COUNT(*) FROM memories "
                "WHERE organization_id = $1 AND 'rejection-lesson' = ANY(tags) "
                "AND deleted_at IS NULL",
                UUID(org_id),
            )

        resp = await client.post("/api/reviews", json={
            "request_id": str(api_request["id"]),
            "status": "rejected",
            "comments": "Not in review status",
        })
        assert resp.status_code == 201

        async with db_pool.acquire() as conn:
            after_count = await conn.fetchval(
                "SELECT COUNT(*) FROM memories "
                "WHERE organization_id = $1 AND 'rejection-lesson' = ANY(tags) "
                "AND deleted_at IS NULL",
                UUID(org_id),
            )
        assert after_count == before_count  # No new memory created
