"""API tests for /api/admin/lifecycle/vitality-stats."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.deps import CurrentUser, get_current_user


@pytest_asyncio.fixture
async def lifecycle_prefix(db_pool):
    test_id = str(uuid4())[:8]
    prefix = f"test_lifecycle_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE content LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%"
        )


async def _make_client(db_pool, prefix: str, role: str):
    from lucent.api.app import create_app
    from lucent.db import OrganizationRepository, UserRepository

    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org_{role}")
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}{role}",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}{role}@test.com",
        display_name=f"{prefix}{role}",
    )
    app = create_app()
    fake_user = CurrentUser(
        id=user["id"],
        organization_id=user["organization_id"],
        role=role,
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake_user

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, app, org, user


@pytest.mark.asyncio
async def test_vitality_stats_returns_distribution_and_flags(
    db_pool, lifecycle_prefix, monkeypatch
):
    """Admin can read vitality histogram + lifecycle stage counts for their org."""
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", "true")
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ALPHA", "0.2")

    client, app, org, user = await _make_client(db_pool, lifecycle_prefix, "admin")
    try:
        # Seed three memories with different vitality scores via MemoryRepository
        # (which fills in username and lifecycle defaults), then patch the
        # vitality scores so the histogram has known buckets.
        from lucent.db import MemoryRepository

        repo = MemoryRepository(db_pool)
        seeded_ids: list[str] = []
        for i in range(3):
            mem = await repo.create(
                username=f"{lifecycle_prefix}admin",
                type="technical",
                content=f"{lifecycle_prefix}memory_{i}",
                user_id=user["id"],
                organization_id=org["id"],
            )
            seeded_ids.append(str(mem["id"]))

        async with db_pool.acquire() as conn:
            for mem_id, vitality in zip(seeded_ids, [None, 0.05, 0.95]):
                await conn.execute(
                    "UPDATE memories SET vitality_score = $2 WHERE id = $1",
                    mem_id,
                    vitality,
                )

        resp = await client.get("/api/admin/lifecycle/vitality-stats")
        assert resp.status_code == 200, resp.text
        body = resp.json()
    finally:
        app.dependency_overrides.clear()
        await client.aclose()

    assert "vitality_histogram" in body
    assert "stage_distribution" in body
    assert "total_memories" in body
    hist = body["vitality_histogram"]
    assert hist["unscored"] >= 1
    assert hist["0.0-0.1"] >= 1
    assert hist["0.9-1.0"] >= 1

    assert body["organization_id"] == str(org["id"])
    assert body["flags"]["vitality_boost_enabled"] is True
    assert body["flags"]["vitality_boost_alpha"] == 0.2
    assert body["flags"]["vitality_boost_log_sample_rate"] == 0.0
    assert body["flags"]["vitality_boost_log_top_n"] == 10


@pytest.mark.asyncio
async def test_vitality_stats_requires_admin_role(db_pool, lifecycle_prefix):
    """Non-admin members must be rejected with 403."""
    client, app, _org, _user = await _make_client(db_pool, lifecycle_prefix, "member")
    try:
        resp = await client.get("/api/admin/lifecycle/vitality-stats")
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.clear()
        await client.aclose()
