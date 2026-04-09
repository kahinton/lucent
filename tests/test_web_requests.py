"""Integration tests for request/activity web routes in web/routes.py.

Tests the HTML-serving endpoints:
- GET  /activity                          (list with filtering)
- GET  /requests                          (redirect to /activity)
- GET  /activity/{id}                     (request detail page)
- GET  /requests/{id}                     (request detail alias)
- POST /requests/tasks/{task_id}/retry    (retry a failed task)

Uses real DB sessions + CSRF tokens through the full ASGI stack.
"""

from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    create_session,
)
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.requests import RequestRepository

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for web request tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_webreq_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        # Clean task events and memory links first (FK deps)
        await conn.execute(
            "DELETE FROM task_events WHERE task_id IN "
            "(SELECT id FROM tasks WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM task_memories WHERE task_id IN "
            "(SELECT id FROM tasks WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM tasks WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM requests WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
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
async def web_user(db_pool, web_prefix):
    """Create user + org for web tests and return (user, org, session_token)."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{web_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{web_prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{web_prefix}user@test.com",
        display_name=f"{web_prefix}User",
    )
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def client(db_pool, web_user):
    """httpx client with session + CSRF cookies pre-set."""
    _user, _org, session_token = web_user
    csrf_token = "test-csrf-token-req123"

    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={
            SESSION_COOKIE_NAME: session_token,
            CSRF_COOKIE_NAME: csrf_token,
        },
    ) as c:
        c._csrf_token = csrf_token  # type: ignore[attr-defined]
        yield c


@pytest_asyncio.fixture
async def sample_request(db_pool, web_user):
    """Create a sample request and return it."""
    _user, org, _token = web_user
    repo = RequestRepository(db_pool)
    return await repo.create_request(
        title="Web Test Request",
        org_id=str(org["id"]),
        description="A request created for web route testing",
        source="user",
        priority="medium",
    )


@pytest_asyncio.fixture
async def request_with_task(db_pool, web_user, sample_request):
    """Create a request with one task attached and return (request, task)."""
    _user, org, _token = web_user
    repo = RequestRepository(db_pool)
    task = await repo.create_task(
        request_id=str(sample_request["id"]),
        title="Web Test Task",
        org_id=str(org["id"]),
        description="A task for testing",
        agent_type="code",
    )
    return sample_request, task


@pytest_asyncio.fixture
async def failed_task(db_pool, request_with_task):
    """Create a request with a failed task."""
    req, task = request_with_task
    repo = RequestRepository(db_pool)
    await repo.claim_task(str(task["id"]), "inst-test")
    await repo.fail_task(str(task["id"]), "Simulated failure for testing")
    return req, task


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    """Build form data dict with CSRF token included."""
    data = {CSRF_FIELD_NAME: client._csrf_token}  # type: ignore[attr-defined]
    if extra:
        data.update(extra)
    return data


# ============================================================================
# GET /activity — list
# ============================================================================


class TestActivityList:
    async def test_list_returns_html(self, client, sample_request):
        resp = await client.get("/activity")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_list_contains_request_title(self, client, sample_request):
        resp = await client.get("/activity")
        assert "Web Test Request" in resp.text

    async def test_list_filter_by_status(self, client, sample_request):
        resp = await client.get("/activity", params={"status": "pending"})
        assert resp.status_code == 200

    async def test_list_filter_by_source(self, client, sample_request):
        resp = await client.get("/activity", params={"source": "user"})
        assert resp.status_code == 200
        assert "Web Test Request" in resp.text

    async def test_list_filter_nonmatching_source(self, client, sample_request):
        resp = await client.get("/activity", params={"source": "daemon"})
        assert resp.status_code == 200
        # Request was created with source="user", should not appear
        assert "Web Test Request" not in resp.text

    async def test_list_unauthenticated_redirects(self, db_pool):
        """No session cookie → redirect to login."""
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/activity", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")


# ============================================================================
# GET /requests — redirect
# ============================================================================


class TestRequestsRedirect:
    async def test_redirect_to_activity(self, client):
        resp = await client.get("/requests", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers.get("location") == "/activity"

    async def test_redirect_preserves_query_string(self, client):
        resp = await client.get("/requests", params={"status": "pending"}, follow_redirects=False)
        assert resp.status_code == 301
        location = resp.headers.get("location", "")
        assert location.startswith("/activity?")
        assert "status=pending" in location


# ============================================================================
# GET /activity/{request_id} — detail
# ============================================================================


class TestRequestDetail:
    async def test_detail_returns_html(self, client, sample_request):
        resp = await client.get(f"/activity/{sample_request['id']}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_detail_contains_title(self, client, sample_request):
        resp = await client.get(f"/activity/{sample_request['id']}")
        assert "Web Test Request" in resp.text

    async def test_detail_via_requests_path(self, client, sample_request):
        """The /requests/{id} alias also works."""
        resp = await client.get(f"/requests/{sample_request['id']}")
        assert resp.status_code == 200
        assert "Web Test Request" in resp.text

    async def test_detail_shows_task(self, client, request_with_task):
        req, task = request_with_task
        resp = await client.get(f"/activity/{req['id']}")
        assert resp.status_code == 200
        assert "Web Test Task" in resp.text

    async def test_detail_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.get(f"/activity/{fake_id}")
        assert resp.status_code == 404

    async def test_detail_unauthenticated_redirects(self, db_pool, sample_request):
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(f"/activity/{sample_request['id']}", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")


# ============================================================================
# POST /requests/tasks/{task_id}/retry
# ============================================================================


class TestRetryTask:
    async def test_retry_failed_task_redirects(self, client, failed_task):
        req, task = failed_task
        resp = await client.post(
            f"/requests/tasks/{task['id']}/retry",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert f"/requests/{req['id']}" in resp.headers.get("location", "")

    async def test_retry_resets_task_to_pending(self, client, failed_task, db_pool):
        _req, task = failed_task
        repo = RequestRepository(db_pool)

        await client.post(
            f"/requests/tasks/{task['id']}/retry",
            data=_csrf_data(client),
            follow_redirects=False,
        )

        updated = await repo.get_task(str(task["id"]))
        assert updated is not None
        assert updated["status"] == "pending"

    async def test_retry_non_failed_task_returns_409(self, client, request_with_task):
        """Only failed tasks can be retried."""
        _req, task = request_with_task
        resp = await client.post(
            f"/requests/tasks/{task['id']}/retry",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 409

    async def test_retry_no_csrf_fails(self, client, failed_task):
        _req, task = failed_task
        resp = await client.post(
            f"/requests/tasks/{task['id']}/retry",
            follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_retry_unauthenticated_redirects(self, db_pool, failed_task):
        _req, task = failed_task
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                f"/requests/tasks/{task['id']}/retry",
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")
