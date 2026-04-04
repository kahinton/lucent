"""Integration tests for memory usage analytics web route."""

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    create_session,
    set_user_password,
)
from lucent.db import MemoryRepository, OrganizationRepository, UserRepository
from lucent.db.access import AccessRepository

TEST_PASSWORD = "TestPass1"


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for usage analytics web tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_webusage_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
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
    """Create user + org with a password set for usage analytics tests."""
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
    csrf_token = "test-csrf-token-usage123"

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
        yield c


@pytest.mark.asyncio
async def test_usage_analytics_returns_html(client):
    resp = await client.get("/memories/analytics")
    assert resp.status_code == 200
    assert "Memory Usage Analytics" in resp.text


@pytest.mark.asyncio
async def test_usage_analytics_shows_least_accessed_and_chart(
    client, db_pool, web_user, web_prefix
):
    user, org, _token = web_user
    repo = MemoryRepository(db_pool)
    access_repo = AccessRepository(db_pool)

    memory = await repo.create(
        username=f"{web_prefix}User",
        type="experience",
        content="Usage analytics test memory content",
        tags=["test", "usage"],
        importance=5,
        user_id=user["id"],
        organization_id=org["id"],
    )

    await access_repo.log_access(
        memory_id=memory["id"],
        access_type="view",
        user_id=user["id"],
        organization_id=org["id"],
    )

    resp = await client.get("/memories/analytics?bucket=day")
    assert resp.status_code == 200
    assert "Least-Accessed Memories" in resp.text
    assert "Access Frequency" in resp.text
    assert "Usage analytics test memory content" in resp.text


@pytest.mark.asyncio
async def test_usage_analytics_unauthenticated_redirects(db_pool):
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/memories/analytics", follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in resp.headers.get("location", "")
