"""Integration tests for group management web routes.

Tests:
- GET  /groups                                 (list groups, team mode only)
- GET  /groups/{group_id}                      (group detail)
- POST /groups/create                          (create group, admin/owner only)
- POST /groups/{group_id}/edit                 (edit group)
- POST /groups/{group_id}/delete               (delete group, admin/owner only)
- POST /groups/{group_id}/members/add          (add member)
- POST /groups/{group_id}/members/{id}/remove  (remove member)

Uses real DB sessions + CSRF tokens through the full ASGI stack.
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
from lucent.db import GroupRepository, OrganizationRepository, UserRepository

TEST_PASSWORD = "TestPass1"


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for web group tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_webgrp_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_groups WHERE group_id IN "
            "(SELECT id FROM groups WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM groups WHERE name LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def owner_user(db_pool, web_prefix):
    """Create an owner user + org for web tests."""
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
    """Create a member user in the same org."""
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
    """httpx client with owner session + CSRF cookies."""
    _user, _org, session_token = owner_user
    csrf_token = "test-csrf-token-grp123"
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
    """httpx client with member session + CSRF cookies."""
    _user, _org, session_token = member_user
    csrf_token = "test-csrf-token-mbr456"
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
    data = {CSRF_FIELD_NAME: client._csrf_token}  # type: ignore[attr-defined]
    if extra:
        data.update(extra)
    return data


# ============================================================================
# GET /groups — list
# ============================================================================


@pytest.mark.asyncio
async def test_groups_list_renders(client):
    """GET /groups returns 200 with HTML (available in all modes)."""
    resp = await client.get("/groups")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Groups" in resp.text


# ============================================================================
# POST /groups/create
# ============================================================================


@pytest.mark.asyncio
async def test_create_group_as_owner(client, web_prefix):
    """Owner can create a group; expect 303 redirect."""
    resp = await client.post(
        "/groups/create",
        data=_csrf_data(client, {"name": f"{web_prefix}testgroup", "description": "A test group"}),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/groups/" in resp.headers["location"]


@pytest.mark.asyncio
async def test_create_group_as_member_returns_403(member_client, web_prefix):
    """Members cannot create groups; expect 403."""
    resp = await member_client.post(
        "/groups/create",
        data=_csrf_data(member_client, {"name": f"{web_prefix}sneaky"}),
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ============================================================================
# GET /groups/{group_id} — detail
# ============================================================================


@pytest.mark.asyncio
async def test_group_detail_renders(client, db_pool, owner_user, web_prefix):
    """GET /groups/{id} returns 200 for a valid group."""
    _, org, _ = owner_user
    repo = GroupRepository(db_pool)
    group = await repo.create_group(
        name=f"{web_prefix}detail_test",
        org_id=str(org["id"]),
    )
    resp = await client.get(f"/groups/{group['id']}")
    assert resp.status_code == 200
    assert group["name"] in resp.text


@pytest.mark.asyncio
async def test_group_detail_not_found(client):
    """GET /groups/{nonexistent} returns 404."""
    resp = await client.get(f"/groups/{uuid4()}")
    assert resp.status_code == 404


# ============================================================================
# POST /groups/{group_id}/edit
# ============================================================================


@pytest.mark.asyncio
async def test_edit_group(client, db_pool, owner_user, web_prefix):
    """Owner can edit a group; expect 303 redirect."""
    _, org, _ = owner_user
    repo = GroupRepository(db_pool)
    group = await repo.create_group(
        name=f"{web_prefix}edit_test",
        org_id=str(org["id"]),
    )
    resp = await client.post(
        f"/groups/{group['id']}/edit",
        data=_csrf_data(client, {"name": f"{web_prefix}edited", "description": "Updated"}),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert f"/groups/{group['id']}" in resp.headers["location"]


# ============================================================================
# POST /groups/{group_id}/delete
# ============================================================================


@pytest.mark.asyncio
async def test_delete_group(client, db_pool, owner_user, web_prefix):
    """Owner can delete a group; expect redirect to /groups."""
    _, org, _ = owner_user
    repo = GroupRepository(db_pool)
    group = await repo.create_group(
        name=f"{web_prefix}delete_test",
        org_id=str(org["id"]),
    )
    resp = await client.post(
        f"/groups/{group['id']}/delete",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/groups" in resp.headers["location"]


@pytest.mark.asyncio
async def test_delete_group_as_member_returns_403(
    member_client, db_pool, owner_user, web_prefix,
):
    """Members cannot delete groups; expect 403."""
    _, org, _ = owner_user
    repo = GroupRepository(db_pool)
    group = await repo.create_group(
        name=f"{web_prefix}nodelete",
        org_id=str(org["id"]),
    )
    resp = await member_client.post(
        f"/groups/{group['id']}/delete",
        data=_csrf_data(member_client),
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ============================================================================
# POST /groups/{group_id}/members/add
# ============================================================================


@pytest.mark.asyncio
async def test_add_member(client, db_pool, owner_user, member_user, web_prefix):
    """Owner can add a member to a group."""
    _, org, _ = owner_user
    target, _, _ = member_user
    repo = GroupRepository(db_pool)
    group = await repo.create_group(
        name=f"{web_prefix}addmember",
        org_id=str(org["id"]),
    )
    resp = await client.post(
        f"/groups/{group['id']}/members/add",
        data=_csrf_data(client, {"user_id": str(target["id"]), "role": "member"}),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert f"/groups/{group['id']}" in resp.headers["location"]

    # Verify member was added
    members = await repo.list_members(str(group["id"]), str(org["id"]))
    assert any(str(m["user_id"]) == str(target["id"]) for m in members)


# ============================================================================
# POST /groups/{group_id}/members/{member_id}/remove
# ============================================================================


@pytest.mark.asyncio
async def test_remove_member(client, db_pool, owner_user, member_user, web_prefix):
    """Owner can remove a member from a group."""
    _, org, _ = owner_user
    target, _, _ = member_user
    repo = GroupRepository(db_pool)
    group = await repo.create_group(
        name=f"{web_prefix}rmmember",
        org_id=str(org["id"]),
    )
    await repo.add_member(str(group["id"]), str(target["id"]))

    resp = await client.post(
        f"/groups/{group['id']}/members/{target['id']}/remove",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Verify member was removed
    members = await repo.list_members(str(group["id"]), str(org["id"]))
    assert not any(str(m["user_id"]) == str(target["id"]) for m in members)
