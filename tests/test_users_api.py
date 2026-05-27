"""API endpoint tests for the users management router.

Tests /api/users endpoints (team-mode only):
- GET /api/users/me (get current user)
- PATCH /api/users/me (update current user)
- GET /api/users (list org users)
- GET /api/users/{user_id} (get user)
- POST /api/users (create user — admin)
- PATCH /api/users/{user_id} (update user — admin)
- PATCH /api/users/{user_id}/role (update role — admin)
- DELETE /api/users/{user_id} (delete user — admin)
"""

from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.deps import CurrentUser, get_current_user

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def usr_prefix(db_pool):
    """Create and clean up test data."""
    test_id = str(uuid4())[:8]
    prefix = f"test_usr_{test_id}_"
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
async def usr_org(db_pool, usr_prefix):
    """Create a test organization."""
    from lucent.db import OrganizationRepository

    org_repo = OrganizationRepository(db_pool)
    return await org_repo.create(name=f"{usr_prefix}org")


@pytest_asyncio.fixture
async def usr_owner(db_pool, usr_org, usr_prefix):
    """Create a test user with owner role."""
    from lucent.db import UserRepository

    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{usr_prefix}owner",
        provider="local",
        organization_id=usr_org["id"],
        email=f"{usr_prefix}owner@test.com",
        display_name=f"{usr_prefix}Owner",
    )
    user = await user_repo.update_role(user["id"], "owner")
    return user


@pytest_asyncio.fixture
async def usr_admin(db_pool, usr_org, usr_prefix):
    """Create a test user with admin role."""
    from lucent.db import UserRepository

    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{usr_prefix}admin",
        provider="local",
        organization_id=usr_org["id"],
        email=f"{usr_prefix}admin@test.com",
        display_name=f"{usr_prefix}Admin",
    )
    user = await user_repo.update_role(user["id"], "admin")
    return user


@pytest_asyncio.fixture
async def usr_member(db_pool, usr_org, usr_prefix):
    """Create a test user with member role."""
    from lucent.db import UserRepository

    user_repo = UserRepository(db_pool)
    return await user_repo.create(
        external_id=f"{usr_prefix}member",
        provider="local",
        organization_id=usr_org["id"],
        email=f"{usr_prefix}member@test.com",
        display_name=f"{usr_prefix}Member",
    )


def _make_client(app, user_dict, role="member"):
    """Build a fake CurrentUser and override the dependency."""
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


@pytest_asyncio.fixture
async def admin_client(db_pool, usr_admin):
    """AsyncClient authenticated as admin."""
    with patch("lucent.api.app.is_team_mode", return_value=True):
        from lucent.api.app import create_app

        app = create_app()
    _make_client(app, usr_admin, role="admin")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def owner_client(db_pool, usr_owner):
    """AsyncClient authenticated as owner."""
    with patch("lucent.api.app.is_team_mode", return_value=True):
        from lucent.api.app import create_app

        app = create_app()
    _make_client(app, usr_owner, role="owner")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def member_client(db_pool, usr_member):
    """AsyncClient authenticated as member."""
    with patch("lucent.api.app.is_team_mode", return_value=True):
        from lucent.api.app import create_app

        app = create_app()
    _make_client(app, usr_member, role="member")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ============================================================================
# GET /api/users/me
# ============================================================================


class TestGetCurrentUser:
    async def test_get_current_user(self, member_client, usr_member):
        resp = await member_client.get("/api/users/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(usr_member["id"])
        assert data["email"] == usr_member["email"]

    async def test_get_current_user_admin(self, admin_client, usr_admin):
        resp = await admin_client.get("/api/users/me")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(usr_admin["id"])


# ============================================================================
# PATCH /api/users/me
# ============================================================================


class TestUpdateCurrentUser:
    async def test_update_display_name(self, member_client):
        resp = await member_client.patch(
            "/api/users/me",
            json={
                "display_name": "New Name",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "New Name"

    async def test_update_email(self, member_client):
        resp = await member_client.patch(
            "/api/users/me",
            json={
                "email": "new@example.com",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "new@example.com"


# ============================================================================
# GET /api/users (list org users)
# ============================================================================


class TestListUsers:
    async def test_list_org_users(self, admin_client):
        resp = await admin_client.get("/api/users")
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        assert "total_count" in data
        assert data["total_count"] >= 1

    async def test_list_users_member_has_permission(self, member_client):
        """Members can view org users."""
        resp = await member_client.get("/api/users")
        assert resp.status_code == 200

    async def test_list_users_filter_by_role(self, admin_client):
        resp = await admin_client.get("/api/users", params={"role": "admin"})
        assert resp.status_code == 200
        for u in resp.json()["users"]:
            assert u["role"] == "admin"


# ============================================================================
# GET /api/users/{user_id}
# ============================================================================


class TestGetUser:
    async def test_get_user_same_org(self, admin_client, usr_member):
        resp = await admin_client.get(f"/api/users/{usr_member['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(usr_member["id"])

    async def test_get_user_not_found(self, admin_client):
        fake_id = str(uuid4())
        resp = await admin_client.get(f"/api/users/{fake_id}")
        assert resp.status_code == 404

    async def test_get_user_cross_org_returns_404(self, admin_client, db_pool, usr_prefix):
        """Users from other orgs should appear as 'not found'."""
        from lucent.db import OrganizationRepository, UserRepository

        org_repo = OrganizationRepository(db_pool)
        other_org = await org_repo.create(name=f"{usr_prefix}other_org")
        user_repo = UserRepository(db_pool)
        other_user = await user_repo.create(
            external_id=f"{usr_prefix}other",
            provider="local",
            organization_id=other_org["id"],
            email=f"{usr_prefix}other@test.com",
        )

        resp = await admin_client.get(f"/api/users/{other_user['id']}")
        assert resp.status_code == 404


# ============================================================================
# POST /api/users (create user — admin)
# ============================================================================


class TestCreateUser:
    async def test_create_user_as_admin(self, admin_client, usr_prefix):
        resp = await admin_client.post(
            "/api/users",
            json={
                "external_id": f"{usr_prefix}newuser",
                "provider": "local",
                "email": f"{usr_prefix}new@test.com",
                "display_name": "New User",
                "role": "member",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["external_id"] == f"{usr_prefix}newuser"
        assert data["role"] == "member"

    async def test_create_user_member_forbidden(self, member_client, usr_prefix):
        """Members cannot create users (requires admin)."""
        resp = await member_client.post(
            "/api/users",
            json={
                "external_id": f"{usr_prefix}blocked",
                "provider": "local",
            },
        )
        assert resp.status_code == 403

    async def test_create_duplicate_user(self, admin_client, usr_prefix, usr_member):
        """Cannot create user with same external_id + provider."""
        resp = await admin_client.post(
            "/api/users",
            json={
                "external_id": usr_member["external_id"],
                "provider": "local",
            },
        )
        assert resp.status_code == 409

    async def test_admin_cannot_create_owner(self, admin_client, usr_prefix):
        """Admins cannot assign owner role."""
        resp = await admin_client.post(
            "/api/users",
            json={
                "external_id": f"{usr_prefix}wannabe_owner",
                "provider": "local",
                "role": "owner",
            },
        )
        assert resp.status_code == 403


# ============================================================================
# PATCH /api/users/{user_id} (update user — admin)
# ============================================================================


class TestUpdateUser:
    async def test_update_user_as_admin(self, admin_client, usr_member):
        resp = await admin_client.patch(
            f"/api/users/{usr_member['id']}",
            json={
                "display_name": "Updated Member",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Updated Member"

    async def test_update_user_not_found(self, admin_client):
        fake_id = str(uuid4())
        resp = await admin_client.patch(
            f"/api/users/{fake_id}",
            json={
                "display_name": "Ghost",
            },
        )
        assert resp.status_code == 404

    async def test_member_cannot_update_others(self, member_client, usr_admin):
        """Members cannot update other users."""
        resp = await member_client.patch(
            f"/api/users/{usr_admin['id']}",
            json={
                "display_name": "Hacked",
            },
        )
        assert resp.status_code == 403


# ============================================================================
# PATCH /api/users/{user_id}/role (update role — admin)
# ============================================================================


class TestUpdateUserRole:
    async def test_owner_can_promote_to_admin(self, owner_client, usr_member):
        resp = await owner_client.patch(
            f"/api/users/{usr_member['id']}/role",
            json={
                "role": "admin",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    async def test_admin_cannot_promote_to_owner(self, admin_client, usr_member):
        """Admins can only set member role."""
        resp = await admin_client.patch(
            f"/api/users/{usr_member['id']}/role",
            json={
                "role": "owner",
            },
        )
        assert resp.status_code == 403

    async def test_update_role_not_found(self, admin_client):
        fake_id = str(uuid4())
        resp = await admin_client.patch(
            f"/api/users/{fake_id}/role",
            json={
                "role": "member",
            },
        )
        assert resp.status_code == 404

    async def test_update_role_invalid_value(self, admin_client, usr_member):
        resp = await admin_client.patch(
            f"/api/users/{usr_member['id']}/role",
            json={
                "role": "superadmin",
            },
        )
        assert resp.status_code in (400, 403)


# ============================================================================
# DELETE /api/users/{user_id} (delete user — admin)
# ============================================================================


class TestDeleteUser:
    async def test_delete_user_as_admin(self, admin_client, usr_member):
        resp = await admin_client.delete(f"/api/users/{usr_member['id']}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_delete_self_forbidden(self, admin_client, usr_admin):
        """Users cannot delete themselves."""
        resp = await admin_client.delete(f"/api/users/{usr_admin['id']}")
        assert resp.status_code == 400
        assert "yourself" in resp.json()["detail"].lower()

    async def test_delete_not_found(self, admin_client):
        fake_id = str(uuid4())
        resp = await admin_client.delete(f"/api/users/{fake_id}")
        assert resp.status_code == 404

    async def test_member_cannot_delete(self, member_client, usr_admin):
        """Members cannot delete users."""
        resp = await member_client.delete(f"/api/users/{usr_admin['id']}")
        assert resp.status_code == 403
