"""Integration tests for user management web routes in web/routes.py.

Tests:
- GET  /users                          (list users, team mode only)
- POST /users/create                   (create user, admin/owner only)
- POST /users/{user_id}/impersonate    (start impersonation, admin/owner only)
- POST /users/stop-impersonation       (stop impersonation)

Uses real DB sessions + CSRF tokens through the full ASGI stack.
"""

from unittest.mock import patch
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
from lucent.db import OrganizationRepository, UserRepository

TEST_PASSWORD = "TestPass1"


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for web user tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_webusr_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
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
async def owner_user(db_pool, web_prefix):
    """Create an owner user + org for web tests and return (user, org, session_token)."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{web_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{web_prefix}owner",
        provider="basic",
        organization_id=org["id"],
        email=f"{web_prefix}owner@test.com",
        display_name=f"{web_prefix}Owner",
    )
    await user_repo.update_role(user["id"], "owner")
    await set_user_password(db_pool, user["id"], TEST_PASSWORD)
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def member_user(db_pool, web_prefix, owner_user):
    """Create a member user in the same org as the owner."""
    _, org, _ = owner_user
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{web_prefix}member",
        provider="basic",
        organization_id=org["id"],
        email=f"{web_prefix}member@test.com",
        display_name=f"{web_prefix}Member",
    )
    await set_user_password(db_pool, user["id"], TEST_PASSWORD)
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def client(db_pool, owner_user):
    """httpx client with owner session + CSRF cookies pre-set."""
    _user, _org, session_token = owner_user
    csrf_token = "test-csrf-token-usr123"

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
async def member_client(db_pool, member_user):
    """httpx client with member session + CSRF cookies pre-set."""
    _user, _org, session_token = member_user
    csrf_token = "test-csrf-token-mbr123"

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


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    """Build form data dict with CSRF token included."""
    data = {CSRF_FIELD_NAME: client._csrf_token}  # type: ignore[attr-defined]
    if extra:
        data.update(extra)
    return data


# ============================================================================
# GET /users — list
# ============================================================================


@pytest.mark.asyncio
@patch("lucent.web.routes.admin.is_team_mode", return_value=True)
async def test_users_list_in_team_mode(_mock_tm, client):
    """With team mode on and an owner user, GET /users returns 200."""
    resp = await client.get("/users")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_users_list_not_team_mode_returns_404(client):
    """Without team mode, GET /users returns 404."""
    resp = await client.get("/users")
    assert resp.status_code == 404


# ============================================================================
# POST /users/create
# ============================================================================


@pytest.mark.asyncio
@patch("secrets.token_urlsafe", return_value="TempPass1safe")
@patch("lucent.web.routes.admin.is_team_mode", return_value=True)
async def test_create_user_as_owner(_mock_tm, _mock_pw, client):
    """Owner can create a new user; expect 303 redirect to /users?success=."""
    resp = await client.post(
        "/users/create",
        data=_csrf_data(
            client,
            {
                "display_name": "New User",
                "email": "newuser@test.com",
                "role": "member",
            },
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/users" in resp.headers["location"]
    assert "success=" in resp.headers["location"]


@pytest.mark.asyncio
@patch("lucent.web.routes.admin.is_team_mode", return_value=True)
async def test_create_user_without_permission_returns_403(_mock_tm, member_client):
    """A member user cannot create users; expect 403."""
    resp = await member_client.post(
        "/users/create",
        data=_csrf_data(
            member_client,
            {
                "display_name": "Sneaky User",
                "email": "sneaky@test.com",
                "role": "member",
            },
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ============================================================================
# POST /users/{user_id}/impersonate
# ============================================================================


@pytest.mark.asyncio
@patch("lucent.web.routes.admin.is_team_mode", return_value=True)
async def test_impersonate_user(_mock_tm, client, member_user):
    """Owner can impersonate a member; expect 303 redirect."""
    target_user, _, _ = member_user
    resp = await client.post(
        f"/users/{target_user['id']}/impersonate",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "impersonating" in resp.headers["location"]


@pytest.mark.asyncio
@patch("lucent.web.routes.admin.is_team_mode", return_value=True)
async def test_impersonate_self_redirects_with_error(_mock_tm, client, owner_user):
    """Owner cannot impersonate themselves; expect redirect to /users?error=."""
    owner, _, _ = owner_user
    resp = await client.post(
        f"/users/{owner['id']}/impersonate",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


# ============================================================================
# POST /users/stop-impersonation
# ============================================================================


@pytest.mark.asyncio
@patch("lucent.web.routes.admin.is_team_mode", return_value=True)
async def test_stop_impersonation(_mock_tm, client):
    """POST /users/stop-impersonation returns 303 redirect to /users."""
    resp = await client.post(
        "/users/stop-impersonation",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/users" in resp.headers["location"]
