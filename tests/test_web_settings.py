"""Integration tests for settings web routes in web/routes.py.

Tests:
- GET  /settings                           (settings page)
- POST /settings/api-keys                  (create API key)
- POST /settings/api-keys/{key_id}/revoke  (revoke API key)
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
from lucent.db import ApiKeyRepository, OrganizationRepository, UserRepository

TEST_PASSWORD = "TestPass1"


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    test_id = str(uuid4())[:8]
    prefix = f"test_webset_{test_id}_"
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
        await conn.execute(
            "DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%"
        )


@pytest_asyncio.fixture
async def web_user(db_pool, web_prefix):
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
    _user, _org, session_token = web_user
    csrf_token = "test-csrf-token-set123"
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: session_token, CSRF_COOKIE_NAME: csrf_token},
    ) as c:
        c._csrf_token = csrf_token
        yield c


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    data = {CSRF_FIELD_NAME: client._csrf_token}
    if extra:
        data.update(extra)
    return data


# ---------------------------------------------------------------------------
# GET /settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_page_returns_200(client):
    resp = await client.get("/settings", follow_redirects=True)
    assert resp.status_code == 200
    assert "Settings" in resp.text


@pytest.mark.asyncio
async def test_settings_unauthenticated_redirects(db_pool):
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/settings", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/login" in resp.headers.get("location", "")


# ---------------------------------------------------------------------------
# POST /settings/api-keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_api_key(client):
    resp = await client.post(
        "/settings/api-keys",
        data=_csrf_data(client, {"name": "integration-test-key"}),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "/settings" in location
    assert "new_key=" in location


@pytest.mark.asyncio
async def test_create_api_key_without_csrf_fails(client):
    resp = await client.post(
        "/settings/api-keys",
        data={"name": "no-csrf-key"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /settings/api-keys/{key_id}/revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_api_key(client, db_pool, web_user):
    user, org, _token = web_user
    api_key_repo = ApiKeyRepository(db_pool)
    key_record, _plain_key = await api_key_repo.create(
        user_id=user["id"],
        organization_id=org["id"],
        name="revoke-me",
    )

    resp = await client.post(
        f"/settings/api-keys/{key_record['id']}/revoke",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/settings" in resp.headers["location"]


@pytest.mark.asyncio
async def test_revoke_nonexistent_key_returns_404(client):
    fake_id = str(uuid4())
    resp = await client.post(
        f"/settings/api-keys/{fake_id}/revoke",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 404
