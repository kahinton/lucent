"""API endpoint tests for the organizations management router.

Tests /api/organizations endpoints (team-mode only):
- GET /api/organizations/current (get current org)
- PATCH /api/organizations/current (update current org — owner)
- POST /api/organizations (create org — owner)
- GET /api/organizations/{organization_id} (get org by id)
- GET /api/organizations (list orgs — owner)
- DELETE /api/organizations/current (delete current org — owner)
- POST /api/organizations/current/transfer (transfer ownership — owner)
"""

import pytest
import pytest_asyncio
from unittest.mock import patch
from uuid import uuid4, UUID

import httpx
from httpx import ASGITransport

from lucent.api.deps import CurrentUser, get_current_user


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def org_prefix(db_pool):
    """Create and clean up test data."""
    test_id = str(uuid4())[:8]
    prefix = f"test_org_{test_id}_"
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
async def org_org(db_pool, org_prefix):
    """Create a test organization."""
    from lucent.db import OrganizationRepository
    org_repo = OrganizationRepository(db_pool)
    return await org_repo.create(name=f"{org_prefix}org")


@pytest_asyncio.fixture
async def org_owner(db_pool, org_org, org_prefix):
    """Create a test user with owner role."""
    from lucent.db import UserRepository
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{org_prefix}owner",
        provider="local",
        organization_id=org_org["id"],
        email=f"{org_prefix}owner@test.com",
        display_name=f"{org_prefix}Owner",
    )
    user = await user_repo.update_role(user["id"], "owner")
    return user


@pytest_asyncio.fixture
async def org_admin(db_pool, org_org, org_prefix):
    """Create a test user with admin role."""
    from lucent.db import UserRepository
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{org_prefix}admin",
        provider="local",
        organization_id=org_org["id"],
        email=f"{org_prefix}admin@test.com",
        display_name=f"{org_prefix}Admin",
    )
    user = await user_repo.update_role(user["id"], "admin")
    return user


@pytest_asyncio.fixture
async def org_member(db_pool, org_org, org_prefix):
    """Create a test user with member role."""
    from lucent.db import UserRepository
    user_repo = UserRepository(db_pool)
    return await user_repo.create(
        external_id=f"{org_prefix}member",
        provider="local",
        organization_id=org_org["id"],
        email=f"{org_prefix}member@test.com",
        display_name=f"{org_prefix}Member",
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
async def owner_client(db_pool, org_owner):
    """AsyncClient authenticated as owner."""
    app = _build_app_with_team_mode(org_owner, role="owner")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_client(db_pool, org_admin):
    """AsyncClient authenticated as admin."""
    app = _build_app_with_team_mode(org_admin, role="admin")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def member_client(db_pool, org_member):
    """AsyncClient authenticated as member."""
    app = _build_app_with_team_mode(org_member, role="member")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ============================================================================
# GET /api/organizations/current — get current organization
# ============================================================================


class TestGetCurrentOrganization:

    async def test_get_current_org_as_owner(self, owner_client, org_org):
        resp = await owner_client.get("/api/organizations/current")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(org_org["id"])
        assert data["name"] == org_org["name"]
        assert "created_at" in data
        assert "updated_at" in data

    async def test_get_current_org_as_admin(self, admin_client, org_org):
        resp = await admin_client.get("/api/organizations/current")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(org_org["id"])

    async def test_get_current_org_as_member(self, member_client, org_org):
        resp = await member_client.get("/api/organizations/current")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(org_org["id"])

    async def test_get_current_org_no_org(self, db_pool, org_prefix):
        """User without organization gets 400."""
        from lucent.db import UserRepository, OrganizationRepository
        # Create a user with no org
        org_repo = OrganizationRepository(db_pool)
        temp_org = await org_repo.create(name=f"{org_prefix}temp_org")
        user_repo = UserRepository(db_pool)
        user = await user_repo.create(
            external_id=f"{org_prefix}no_org_user",
            provider="local",
            organization_id=temp_org["id"],
            email=f"{org_prefix}noorg@test.com",
        )
        # Build a client with organization_id=None
        user_dict = dict(user)
        user_dict["organization_id"] = None
        app = _build_app_with_team_mode(user_dict, role="member")
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/api/organizations/current")
        assert resp.status_code == 400
        assert "not part of an organization" in resp.json()["detail"]
        app.dependency_overrides.clear()


# ============================================================================
# PATCH /api/organizations/current — update current organization
# ============================================================================


class TestUpdateCurrentOrganization:

    @pytest.mark.xfail(
        reason="Bug: organizations.py:89 passes organization_id= but "
               "OrganizationRepository.update() expects org_id=",
        strict=True,
    )
    async def test_update_org_as_owner(self, owner_client, org_prefix):
        new_name = f"{org_prefix}updated"
        resp = await owner_client.patch(
            "/api/organizations/current",
            json={"name": new_name},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == new_name

    async def test_update_org_admin_forbidden(self, admin_client, org_prefix):
        """Admins cannot update the organization (requires owner)."""
        resp = await admin_client.patch(
            "/api/organizations/current",
            json={"name": f"{org_prefix}nope"},
        )
        assert resp.status_code == 403

    async def test_update_org_member_forbidden(self, member_client, org_prefix):
        """Members cannot update the organization (requires owner)."""
        resp = await member_client.patch(
            "/api/organizations/current",
            json={"name": f"{org_prefix}nope"},
        )
        assert resp.status_code == 403

    async def test_update_org_no_org(self, db_pool, org_prefix):
        """Owner without org_id gets 400."""
        from lucent.db import UserRepository, OrganizationRepository
        org_repo = OrganizationRepository(db_pool)
        temp_org = await org_repo.create(name=f"{org_prefix}temp_upd_org")
        user_repo = UserRepository(db_pool)
        user = await user_repo.create(
            external_id=f"{org_prefix}no_org_owner",
            provider="local",
            organization_id=temp_org["id"],
            email=f"{org_prefix}noorgowner@test.com",
        )
        user_dict = dict(user)
        user_dict["organization_id"] = None
        app = _build_app_with_team_mode(user_dict, role="owner")
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.patch(
                "/api/organizations/current",
                json={"name": f"{org_prefix}impossible"},
            )
        assert resp.status_code == 400
        assert "not part of an organization" in resp.json()["detail"]
        app.dependency_overrides.clear()


# ============================================================================
# POST /api/organizations — create organization
# ============================================================================


class TestCreateOrganization:

    async def test_create_org_as_owner(self, owner_client, org_prefix):
        resp = await owner_client.post(
            "/api/organizations",
            json={"name": f"{org_prefix}new_org"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == f"{org_prefix}new_org"
        assert "id" in data
        assert "created_at" in data

    async def test_create_org_duplicate_name(self, owner_client, org_org):
        """Cannot create org with an existing name."""
        resp = await owner_client.post(
            "/api/organizations",
            json={"name": org_org["name"]},
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    async def test_create_org_admin_forbidden(self, admin_client, org_prefix):
        """Admins cannot create organizations (requires owner)."""
        resp = await admin_client.post(
            "/api/organizations",
            json={"name": f"{org_prefix}admin_org"},
        )
        assert resp.status_code == 403

    async def test_create_org_member_forbidden(self, member_client, org_prefix):
        """Members cannot create organizations (requires owner)."""
        resp = await member_client.post(
            "/api/organizations",
            json={"name": f"{org_prefix}member_org"},
        )
        assert resp.status_code == 403


# ============================================================================
# GET /api/organizations/{organization_id} — get org by ID
# ============================================================================


class TestGetOrganizationById:

    async def test_get_own_org(self, owner_client, org_org):
        resp = await owner_client.get(f"/api/organizations/{org_org['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(org_org["id"])
        assert resp.json()["name"] == org_org["name"]

    async def test_get_other_org_forbidden(self, owner_client):
        """Cannot view an organization you don't belong to."""
        fake_id = str(uuid4())
        resp = await owner_client.get(f"/api/organizations/{fake_id}")
        assert resp.status_code == 403
        assert "own organization" in resp.json()["detail"]

    async def test_member_can_view_own_org(self, member_client, org_org):
        """Members can view their own organization."""
        resp = await member_client.get(f"/api/organizations/{org_org['id']}")
        assert resp.status_code == 200
        assert resp.json()["name"] == org_org["name"]


# ============================================================================
# GET /api/organizations — list organizations
# ============================================================================


class TestListOrganizations:

    async def test_list_orgs_as_owner(self, owner_client):
        resp = await owner_client.get("/api/organizations")
        assert resp.status_code == 200
        data = resp.json()
        assert "organizations" in data
        assert "total_count" in data
        assert "offset" in data
        assert "limit" in data
        assert "has_more" in data
        assert data["total_count"] >= 1

    async def test_list_orgs_pagination(self, owner_client):
        resp = await owner_client.get(
            "/api/organizations",
            params={"offset": 0, "limit": 1},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 1
        assert len(data["organizations"]) <= 1

    async def test_list_orgs_admin_forbidden(self, admin_client):
        """Admins cannot list all organizations (requires owner)."""
        resp = await admin_client.get("/api/organizations")
        assert resp.status_code == 403

    async def test_list_orgs_member_forbidden(self, member_client):
        """Members cannot list all organizations (requires owner)."""
        resp = await member_client.get("/api/organizations")
        assert resp.status_code == 403


# ============================================================================
# DELETE /api/organizations/current — delete current organization
# ============================================================================


class TestDeleteCurrentOrganization:

    async def test_delete_org_as_owner(self, db_pool, org_prefix):
        """Owner can delete their organization."""
        from lucent.db import OrganizationRepository, UserRepository
        # Create a throwaway org+user for this destructive test
        org_repo = OrganizationRepository(db_pool)
        temp_org = await org_repo.create(name=f"{org_prefix}delete_me")
        user_repo = UserRepository(db_pool)
        user = await user_repo.create(
            external_id=f"{org_prefix}delete_owner",
            provider="local",
            organization_id=temp_org["id"],
            email=f"{org_prefix}delowner@test.com",
            display_name=f"{org_prefix}Delete Owner",
        )
        user = await user_repo.update_role(user["id"], "owner")

        app = _build_app_with_team_mode(user, role="owner")
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.delete("/api/organizations/current")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        app.dependency_overrides.clear()

        # Verify org is actually gone
        deleted = await org_repo.get_by_id(temp_org["id"])
        assert deleted is None

    async def test_delete_org_admin_forbidden(self, admin_client):
        """Admins cannot delete the organization (requires owner)."""
        resp = await admin_client.delete("/api/organizations/current")
        assert resp.status_code == 403

    async def test_delete_org_member_forbidden(self, member_client):
        """Members cannot delete the organization (requires owner)."""
        resp = await member_client.delete("/api/organizations/current")
        assert resp.status_code == 403

    async def test_delete_org_no_org(self, db_pool, org_prefix):
        """Owner without org_id gets 400."""
        from lucent.db import UserRepository, OrganizationRepository
        org_repo = OrganizationRepository(db_pool)
        temp_org = await org_repo.create(name=f"{org_prefix}temp_del_org")
        user_repo = UserRepository(db_pool)
        user = await user_repo.create(
            external_id=f"{org_prefix}no_org_del",
            provider="local",
            organization_id=temp_org["id"],
            email=f"{org_prefix}noorgdel@test.com",
        )
        user_dict = dict(user)
        user_dict["organization_id"] = None
        app = _build_app_with_team_mode(user_dict, role="owner")
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.delete("/api/organizations/current")
        assert resp.status_code == 400
        assert "not part of an organization" in resp.json()["detail"]
        app.dependency_overrides.clear()


# ============================================================================
# POST /api/organizations/current/transfer — transfer ownership
# ============================================================================


class TestTransferOwnership:

    @pytest.mark.xfail(
        reason="Bug: organizations.py:279 promotes new owner before demoting old, "
               "violating idx_one_owner_per_org unique constraint",
        strict=True,
    )
    async def test_transfer_to_member(self, db_pool, org_prefix):
        """Owner can transfer ownership to another member."""
        from lucent.db import OrganizationRepository, UserRepository
        org_repo = OrganizationRepository(db_pool)
        temp_org = await org_repo.create(name=f"{org_prefix}transfer_org")
        user_repo = UserRepository(db_pool)

        owner = await user_repo.create(
            external_id=f"{org_prefix}xfer_owner",
            provider="local",
            organization_id=temp_org["id"],
            email=f"{org_prefix}xfer_owner@test.com",
            display_name=f"{org_prefix}Transfer Owner",
        )
        owner = await user_repo.update_role(owner["id"], "owner")

        new_owner = await user_repo.create(
            external_id=f"{org_prefix}xfer_member",
            provider="local",
            organization_id=temp_org["id"],
            email=f"{org_prefix}xfer_member@test.com",
            display_name=f"{org_prefix}Transfer Member",
        )

        app = _build_app_with_team_mode(owner, role="owner")
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/api/organizations/current/transfer",
                params={"new_owner_id": str(new_owner["id"])},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert str(new_owner["id"]) in data["message"]
        app.dependency_overrides.clear()

        # Verify roles changed
        updated_new = await user_repo.get_by_id(new_owner["id"])
        assert updated_new["role"] == "owner"
        updated_old = await user_repo.get_by_id(owner["id"])
        assert updated_old["role"] == "admin"

    async def test_transfer_to_self_fails(self, owner_client, org_owner):
        """Cannot transfer ownership to yourself."""
        resp = await owner_client.post(
            "/api/organizations/current/transfer",
            params={"new_owner_id": str(org_owner["id"])},
        )
        assert resp.status_code == 400
        assert "already the owner" in resp.json()["detail"]

    async def test_transfer_to_nonexistent_user(self, owner_client):
        """Cannot transfer to a user that doesn't exist."""
        fake_id = str(uuid4())
        resp = await owner_client.post(
            "/api/organizations/current/transfer",
            params={"new_owner_id": fake_id},
        )
        assert resp.status_code == 404
        assert "User not found" in resp.json()["detail"]

    async def test_transfer_to_user_in_other_org(self, owner_client, db_pool, org_prefix):
        """Cannot transfer to a user in a different organization."""
        from lucent.db import OrganizationRepository, UserRepository
        org_repo = OrganizationRepository(db_pool)
        other_org = await org_repo.create(name=f"{org_prefix}other_org")
        user_repo = UserRepository(db_pool)
        other_user = await user_repo.create(
            external_id=f"{org_prefix}other_user",
            provider="local",
            organization_id=other_org["id"],
            email=f"{org_prefix}other@test.com",
        )

        resp = await owner_client.post(
            "/api/organizations/current/transfer",
            params={"new_owner_id": str(other_user["id"])},
        )
        assert resp.status_code == 400
        assert "not part of your organization" in resp.json()["detail"]

    async def test_transfer_admin_forbidden(self, admin_client, org_member):
        """Admins cannot transfer ownership (requires owner)."""
        resp = await admin_client.post(
            "/api/organizations/current/transfer",
            params={"new_owner_id": str(org_member["id"])},
        )
        assert resp.status_code == 403

    async def test_transfer_member_forbidden(self, member_client, org_owner):
        """Members cannot transfer ownership (requires owner)."""
        resp = await member_client.post(
            "/api/organizations/current/transfer",
            params={"new_owner_id": str(org_owner["id"])},
        )
        assert resp.status_code == 403

    async def test_transfer_no_org(self, db_pool, org_prefix):
        """Owner without org_id gets 400."""
        from lucent.db import UserRepository, OrganizationRepository
        org_repo = OrganizationRepository(db_pool)
        temp_org = await org_repo.create(name=f"{org_prefix}temp_xfer_org")
        user_repo = UserRepository(db_pool)
        user = await user_repo.create(
            external_id=f"{org_prefix}no_org_xfer",
            provider="local",
            organization_id=temp_org["id"],
            email=f"{org_prefix}noorgxfer@test.com",
        )
        user_dict = dict(user)
        user_dict["organization_id"] = None
        app = _build_app_with_team_mode(user_dict, role="owner")
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        fake_id = str(uuid4())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/api/organizations/current/transfer",
                params={"new_owner_id": fake_id},
            )
        assert resp.status_code == 400
        assert "not part of an organization" in resp.json()["detail"]
        app.dependency_overrides.clear()
