"""API endpoint tests for group management router."""

from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.deps import CurrentUser, get_current_user


@pytest_asyncio.fixture
async def grp_prefix(db_pool):
    test_id = str(uuid4())[:8]
    prefix = f"test_grp_{test_id}_"
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
async def grp_org(db_pool, grp_prefix):
    from lucent.db import OrganizationRepository

    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{grp_prefix}org")


@pytest_asyncio.fixture
async def grp_admin(db_pool, grp_org, grp_prefix):
    from lucent.db import UserRepository

    repo = UserRepository(db_pool)
    user = await repo.create(
        external_id=f"{grp_prefix}admin",
        provider="local",
        organization_id=grp_org["id"],
        email=f"{grp_prefix}admin@test.com",
        display_name=f"{grp_prefix}Admin",
    )
    return await repo.update_role(user["id"], "admin")


@pytest_asyncio.fixture
async def grp_member(db_pool, grp_org, grp_prefix):
    from lucent.db import UserRepository

    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{grp_prefix}member",
        provider="local",
        organization_id=grp_org["id"],
        email=f"{grp_prefix}member@test.com",
        display_name=f"{grp_prefix}Member",
    )


def _make_app_for_user(user_dict: dict, role: str):
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
async def admin_client(db_pool, grp_admin):
    app = _make_app_for_user(grp_admin, role="admin")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def member_client(db_pool, grp_member):
    app = _make_app_for_user(grp_member, role="member")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class TestGroupCrudAndMembership:
    async def test_member_cannot_create_group(self, member_client):
        resp = await member_client.post("/api/groups", json={"name": "nope"})
        assert resp.status_code == 403

    async def test_admin_can_create_group(self, admin_client, grp_prefix):
        resp = await admin_client.post(
            "/api/groups",
            json={"name": f"{grp_prefix}eng", "description": "Engineering"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == f"{grp_prefix}eng"
        assert data["member_count"] == 0

    async def test_list_get_update_delete_group(self, admin_client, grp_prefix):
        create = await admin_client.post("/api/groups", json={"name": f"{grp_prefix}ops"})
        group_id = create.json()["id"]

        listed = await admin_client.get("/api/groups")
        assert listed.status_code == 200
        assert any(item["id"] == group_id for item in listed.json()["items"])

        fetched = await admin_client.get(f"/api/groups/{group_id}")
        assert fetched.status_code == 200
        assert fetched.json()["group"]["id"] == group_id
        assert fetched.json()["members"] == []

        updated = await admin_client.put(
            f"/api/groups/{group_id}",
            json={"description": "Ops team"},
        )
        assert updated.status_code == 200
        assert updated.json()["description"] == "Ops team"

        deleted = await admin_client.delete(f"/api/groups/{group_id}")
        assert deleted.status_code == 200
        assert deleted.json()["success"] is True

    async def test_add_list_remove_member(self, admin_client, grp_prefix, grp_member):
        create = await admin_client.post("/api/groups", json={"name": f"{grp_prefix}dev"})
        group_id = create.json()["id"]

        added = await admin_client.post(
            f"/api/groups/{group_id}/members",
            json={"user_id": str(grp_member["id"]), "role": "member"},
        )
        assert added.status_code == 201
        assert added.json()["user_id"] == str(grp_member["id"])

        members = await admin_client.get(f"/api/groups/{group_id}/members")
        assert members.status_code == 200
        assert members.json()["total_count"] == 1

        removed = await admin_client.delete(f"/api/groups/{group_id}/members/{grp_member['id']}")
        assert removed.status_code == 200
        assert removed.json()["success"] is True

    async def test_group_admin_can_update_and_manage_members(
        self, admin_client, member_client, grp_prefix, grp_member
    ):
        create = await admin_client.post("/api/groups", json={"name": f"{grp_prefix}sec"})
        group_id = create.json()["id"]

        make_admin = await admin_client.post(
            f"/api/groups/{group_id}/members",
            json={"user_id": str(grp_member["id"]), "role": "admin"},
        )
        assert make_admin.status_code == 201

        update = await member_client.put(
            f"/api/groups/{group_id}",
            json={"description": "Security"},
        )
        assert update.status_code == 200
        assert update.json()["description"] == "Security"

    async def test_member_cannot_manage_group_without_group_admin(
        self, admin_client, member_client, grp_prefix, grp_member
    ):
        create = await admin_client.post("/api/groups", json={"name": f"{grp_prefix}qa"})
        group_id = create.json()["id"]

        update = await member_client.put(
            f"/api/groups/{group_id}",
            json={"description": "Blocked"},
        )
        assert update.status_code == 403

        add_member = await member_client.post(
            f"/api/groups/{group_id}/members",
            json={"user_id": str(grp_member["id"]), "role": "member"},
        )
        assert add_member.status_code == 403
