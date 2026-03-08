"""API endpoint tests for the access log router.

Tests /api/access endpoints (team-mode only):
- GET /api/access/memory/{memory_id} (memory access history)
- GET /api/access/memory/{memory_id}/searches (search history)
- GET /api/access/user/{user_id} (user activity)
- GET /api/access/most-accessed (most accessed memories)
- GET /api/access/organization/activity (org activity — 501)
"""

import pytest
import pytest_asyncio
from unittest.mock import patch
from uuid import uuid4, UUID

import httpx
from httpx import ASGITransport

from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import MemoryRepository, AccessRepository


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def acc_prefix(db_pool):
    """Create and clean up test data."""
    test_id = str(uuid4())[:8]
    prefix = f"test_acc_{test_id}_"
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
async def acc_org(db_pool, acc_prefix):
    from lucent.db import OrganizationRepository
    org_repo = OrganizationRepository(db_pool)
    return await org_repo.create(name=f"{acc_prefix}org")


@pytest_asyncio.fixture
async def acc_admin(db_pool, acc_org, acc_prefix):
    from lucent.db import UserRepository
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{acc_prefix}admin",
        provider="local",
        organization_id=acc_org["id"],
        email=f"{acc_prefix}admin@test.com",
        display_name=f"{acc_prefix}Admin",
    )
    user = await user_repo.update_role(user["id"], "admin")
    return user


@pytest_asyncio.fixture
async def acc_member(db_pool, acc_org, acc_prefix):
    from lucent.db import UserRepository
    user_repo = UserRepository(db_pool)
    return await user_repo.create(
        external_id=f"{acc_prefix}member",
        provider="local",
        organization_id=acc_org["id"],
        email=f"{acc_prefix}member@test.com",
        display_name=f"{acc_prefix}Member",
    )


@pytest_asyncio.fixture
async def acc_memory(db_pool, acc_member, acc_prefix):
    """Create a test memory owned by acc_member."""
    repo = MemoryRepository(db_pool)
    return await repo.create(
        username=f"{acc_prefix}member",
        type="experience",
        content=f"{acc_prefix}Test memory for access log",
        tags=["test"],
        importance=5,
        user_id=acc_member["id"],
        organization_id=acc_member["organization_id"],
    )


def _build_app_with_team_mode(user_dict, role="member"):
    """Create app with team mode enabled and auth overridden."""
    with patch("lucent.api.app.is_team_mode", return_value=True):
        from lucent.api.app import create_app
        app = create_app()
    fake = CurrentUser(
        id=user_dict["id"],
        organization_id=user_dict.get("organization_id"),
        role=role,
        email=user_dict.get("email"),
        display_name=user_dict.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake

    app.dependency_overrides[get_current_user] = override
    return app


@pytest_asyncio.fixture
async def admin_client(db_pool, acc_admin):
    app = _build_app_with_team_mode(acc_admin, role="admin")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def member_client(db_pool, acc_member):
    app = _build_app_with_team_mode(acc_member, role="member")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ============================================================================
# GET /api/access/memory/{memory_id} — memory access history
# ============================================================================


class TestMemoryAccessHistory:

    async def test_get_access_history_empty(self, member_client, acc_memory):
        resp = await member_client.get(f"/api/access/memory/{acc_memory['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "total_count" in data

    async def test_get_access_history_after_view(self, member_client, db_pool, acc_memory, acc_member):
        """After logging access, it should appear in history."""
        access_repo = AccessRepository(db_pool)
        await access_repo.log_access(
            memory_id=acc_memory["id"],
            access_type="view",
            user_id=acc_member["id"],
            organization_id=acc_member["organization_id"],
        )

        resp = await member_client.get(f"/api/access/memory/{acc_memory['id']}")
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        # Member should see their own access
        view_entries = [e for e in entries if e["access_type"] == "view"]
        assert len(view_entries) >= 1

    async def test_get_access_history_pagination(self, member_client, acc_memory):
        resp = await member_client.get(
            f"/api/access/memory/{acc_memory['id']}",
            params={"offset": 0, "limit": 10},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["offset"] == 0
        assert data["limit"] == 10


# ============================================================================
# GET /api/access/memory/{memory_id}/searches — search history
# ============================================================================


class TestMemorySearchHistory:

    async def test_search_history_empty(self, member_client, acc_memory):
        resp = await member_client.get(f"/api/access/memory/{acc_memory['id']}/searches")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    async def test_search_history_after_search(self, member_client, db_pool, acc_memory, acc_member):
        """After logging a search_result access, it should appear."""
        access_repo = AccessRepository(db_pool)
        await access_repo.log_access(
            memory_id=acc_memory["id"],
            access_type="search_result",
            user_id=acc_member["id"],
            organization_id=acc_member["organization_id"],
            context={"query": "test search"},
        )

        resp = await member_client.get(f"/api/access/memory/{acc_memory['id']}/searches")
        assert resp.status_code == 200
        entries = resp.json()
        search_entries = [e for e in entries if e["access_type"] == "search_result"]
        assert len(search_entries) >= 1


# ============================================================================
# GET /api/access/user/{user_id} — user activity
# ============================================================================


class TestUserAccessActivity:

    async def test_get_own_activity(self, member_client, acc_member):
        resp = await member_client.get(f"/api/access/user/{acc_member['id']}")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_get_other_user_activity_member_forbidden(self, member_client, acc_admin):
        """Members cannot view other users' access activity."""
        resp = await member_client.get(f"/api/access/user/{acc_admin['id']}")
        assert resp.status_code == 403

    async def test_admin_can_view_other_activity(self, admin_client, acc_member):
        """Admins can view any user's activity."""
        resp = await admin_client.get(f"/api/access/user/{acc_member['id']}")
        assert resp.status_code == 200


# ============================================================================
# GET /api/access/most-accessed — most accessed memories
# ============================================================================


class TestMostAccessed:

    async def test_most_accessed_personal(self, member_client):
        resp = await member_client.get("/api/access/most-accessed")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_most_accessed_org_wide_member_forbidden(self, member_client):
        """Members cannot access org-wide stats."""
        resp = await member_client.get(
            "/api/access/most-accessed", params={"organization_wide": "true"}
        )
        assert resp.status_code == 403

    async def test_most_accessed_org_wide_admin(self, admin_client):
        """Admins can access org-wide stats."""
        resp = await admin_client.get(
            "/api/access/most-accessed", params={"organization_wide": "true"}
        )
        assert resp.status_code == 200

    async def test_most_accessed_with_limit(self, member_client):
        resp = await member_client.get(
            "/api/access/most-accessed", params={"limit": 5}
        )
        assert resp.status_code == 200


# ============================================================================
# GET /api/access/organization/activity — org activity (not implemented)
# ============================================================================


class TestOrgActivity:

    async def test_org_activity_returns_501(self, admin_client):
        """Organization activity endpoint is not yet implemented."""
        resp = await admin_client.get("/api/access/organization/activity")
        assert resp.status_code == 501

    async def test_org_activity_member_forbidden(self, member_client):
        """Members cannot access org activity (should fail before 501)."""
        resp = await member_client.get("/api/access/organization/activity")
        assert resp.status_code == 403
