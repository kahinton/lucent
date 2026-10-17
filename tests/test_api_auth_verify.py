"""Auth-verification endpoint tests for ``GET /api/auth/verify``.

This is the cheap, always-registered endpoint the daemon's periodic key
probe targets. It must return 200 for a good key and 401 for a
missing/invalid/expired/revoked key (and 403 for a suspended organization)
so the daemon's ``classify_api_key()`` keeps working unchanged.

These tests exercise the *real* auth dependency (no override), so they rely
on the ``db_pool`` fixture and are auto-marked ``integration``.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.db import ApiKeyRepository, OrganizationRepository, UserRepository


@pytest_asyncio.fixture
async def auth_prefix(db_pool):
    """Namespace + cleanup for auth-verify test data."""
    prefix = f"test_authverify_{str(uuid4())[:8]}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def auth_user(db_pool, auth_prefix):
    """A real user + organization to hang API keys off."""
    org = await OrganizationRepository(db_pool).create(name=f"{auth_prefix}org")
    user = await UserRepository(db_pool).create(
        external_id=f"{auth_prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{auth_prefix}user@test.com",
        display_name=f"{auth_prefix}User",
    )
    return user


@pytest_asyncio.fixture
async def make_key(db_pool, auth_user, auth_prefix):
    """Factory that mints an API key for the test user."""
    counter = {"n": 0}

    async def _make(*, expires_at: datetime | None = None) -> tuple[dict, str]:
        counter["n"] += 1
        repo = ApiKeyRepository(db_pool)
        record, plain = await repo.create(
            user_id=auth_user["id"],
            organization_id=auth_user["organization_id"],
            name=f"{auth_prefix}key-{counter['n']}",
            scopes=["read", "write"],
            expires_at=expires_at,
        )
        return record, plain

    return _make


@pytest_asyncio.fixture
async def raw_client(db_pool):
    """Client that uses the real auth path (no dependency override)."""
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_valid_key_returns_200(raw_client, make_key):
    _record, plain = await make_key()
    resp = await raw_client.get(
        "/api/auth/verify", headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}


async def test_missing_key_returns_401(raw_client):
    resp = await raw_client.get("/api/auth/verify")
    assert resp.status_code == 401


async def test_invalid_key_returns_401(raw_client):
    resp = await raw_client.get(
        "/api/auth/verify",
        headers={"Authorization": "Bearer hs_definitelynotarealkey00"},
    )
    assert resp.status_code == 401


async def test_non_prefixed_key_returns_401(raw_client):
    resp = await raw_client.get(
        "/api/auth/verify", headers={"Authorization": "Bearer not-an-hs-key"}
    )
    assert resp.status_code == 401


async def test_revoked_key_returns_401(raw_client, db_pool, auth_user, make_key):
    record, plain = await make_key()
    await ApiKeyRepository(db_pool).revoke(
        key_id=record["id"],
        user_id=auth_user["id"],
        organization_id=auth_user["organization_id"],
    )
    resp = await raw_client.get(
        "/api/auth/verify", headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 401


async def test_expired_key_returns_401(raw_client, make_key):
    _record, plain = await make_key(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    resp = await raw_client.get(
        "/api/auth/verify", headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 401


async def test_suspended_org_returns_403(raw_client, db_pool, auth_user, make_key):
    _record, plain = await make_key()
    await OrganizationRepository(db_pool).set_suspended(
        auth_user["organization_id"], suspended=True
    )
    try:
        resp = await raw_client.get(
            "/api/auth/verify", headers={"Authorization": f"Bearer {plain}"}
        )
        assert resp.status_code == 403
    finally:
        await OrganizationRepository(db_pool).set_suspended(
            auth_user["organization_id"], suspended=False
        )
