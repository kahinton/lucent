"""Integration tests for daemon web routes in web/routes.py.

Tests:
- GET  /daemon                          (redirect to activity)
- GET  /daemon/review                   (review queue)
- GET  /daemon/tasks                    (legacy redirect)
- GET  /daemon/tasks/new                (legacy redirect)
- GET  /daemon/tasks/{task_id}          (legacy redirect)
"""

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    create_session,
    set_user_password,
)
from lucent.db import MemoryRepository, OrganizationRepository, UserRepository
from lucent.db.requests import RequestRepository

TEST_PASSWORD = "TestPass1"


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for web daemon tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_webdmn_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM reviews WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
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
    """Create user + org with a password set for web daemon tests."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{web_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{web_prefix}user",
        provider="basic",
        organization_id=org["id"],
        email=f"{web_prefix}user@test.com",
        display_name=f"{web_prefix}User",
    )
    await set_user_password(db_pool, user["id"], TEST_PASSWORD)
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def client(db_pool, web_user):
    """httpx client with session + CSRF cookies pre-set."""
    _user, _org, session_token = web_user
    csrf_token = "test-csrf-token-dmn123"

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
async def review_memory(db_pool, web_user):
    """Create a memory tagged for daemon review."""
    user, org, _ = web_user
    repo = MemoryRepository(db_pool)
    memory = await repo.create(
        username=user["display_name"],
        type="experience",
        content="Test daemon work for review",
        tags=["daemon", "needs-review"],
        importance=5,
        user_id=user["id"],
        organization_id=org["id"],
    )
    return memory


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    """Build form data dict with CSRF token included."""
    data = {CSRF_FIELD_NAME: client._csrf_token}  # type: ignore[attr-defined]
    if extra:
        data.update(extra)
    return data


# ============================================================================
# Retired bookmark routes — /activity is the only entry point now
# ============================================================================


@pytest.mark.asyncio
async def test_daemon_bookmark_route_is_retired(client):
    resp = await client.get("/daemon", follow_redirects=False)
    assert resp.status_code == 404


# ============================================================================
# GET /daemon/review — retired bookmark route
# ============================================================================


@pytest.mark.asyncio
async def test_daemon_review_bookmark_route_is_retired(client):
    resp = await client.get("/daemon/review", follow_redirects=False)
    assert resp.status_code == 404


# ============================================================================
# POST /daemon/review/{request_id}/action — approve / reject
# ============================================================================


@pytest.mark.asyncio
async def test_daemon_review_reject_escapes_comment_html(client, db_pool, web_user):
    user, org, _ = web_user

    # Route requires admin/daemon privileges.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET role = 'admin' WHERE id = $1",
            user["id"],
        )

    req_repo = RequestRepository(db_pool)
    req = await req_repo.create_request(
        title="XSS review test",
        description="Verify rejection comment escaping",
        source="user",
        priority="medium",
        org_id=str(org["id"]),
    )
    await req_repo.update_request_status(str(req["id"]), "review", str(org["id"]))

    malicious_comment = "<script>alert(1)</script>"
    resp = await client.post(
        f"/daemon/review/{req['id']}/action",
        data=_csrf_data(
            client,
            {"action": "reject", "comment": malicious_comment},
        ),
    )

    assert resp.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text
    assert "<script>alert(1)</script>" not in resp.text
