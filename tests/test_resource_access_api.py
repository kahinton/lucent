"""API tests for /api/access-grants/{resource_type}/{resource_id} endpoints.

Verifies the REST resource access-grant surface is secured identically to the
web and MCP surfaces: only the managing owner or an org admin/owner may manage
grants, scoped API-key contexts are blocked, and grant principals must belong to
the caller's organization.
"""

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import (
    DefinitionRepository,
    GroupRepository,
    OrganizationRepository,
    UserRepository,
)


@pytest_asyncio.fixture
async def rag_env(db_pool):
    """Provision an org with an owner, admin, member, a group, and an owned agent."""
    test_id = str(uuid4())[:8]
    prefix = f"test_rag_{test_id}_"
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    org_id = str(org["id"])

    users = UserRepository(db_pool)
    owner = await users.create(
        external_id=f"{prefix}owner", provider="local",
        organization_id=org["id"], email=f"{prefix}owner@test.com",
        display_name="Owner",
    )
    admin = await users.create(
        external_id=f"{prefix}admin", provider="local",
        organization_id=org["id"], email=f"{prefix}admin@test.com",
        display_name="Admin",
    )
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET role = 'admin' WHERE id = $1", admin["id"]
        )
    member = await users.create(
        external_id=f"{prefix}member", provider="local",
        organization_id=org["id"], email=f"{prefix}member@test.com",
        display_name="Member",
    )
    group = await GroupRepository(db_pool).create_group(
        name=f"{prefix}group", org_id=org_id, created_by=str(owner["id"]),
    )
    agent = await DefinitionRepository(db_pool).create_agent(
        name=f"{prefix}agent", description="", content="# Agent",
        org_id=org_id, created_by=str(owner["id"]), owner_user_id=str(owner["id"]),
    )

    yield {
        "prefix": prefix,
        "org_id": org_id,
        "owner": owner,
        "admin": admin,
        "member": member,
        "group": group,
        "agent_id": str(agent["id"]),
    }

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM resource_access_grants WHERE organization_id = $1", org["id"]
        )
        await conn.execute("DELETE FROM agent_definitions WHERE organization_id = $1", org["id"])
        await conn.execute("DELETE FROM groups WHERE organization_id = $1", org["id"])
        await conn.execute("DELETE FROM users WHERE organization_id = $1", org["id"])
        await conn.execute("DELETE FROM organizations WHERE id = $1", org["id"])


def _client(user: dict, *, role: str, memory_scope: str | None = None):
    """Build an ASGI client authenticated as the given user."""
    app = create_app()
    fake = CurrentUser(
        id=user["id"],
        organization_id=user["organization_id"],
        role=role,
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
        memory_scope=memory_scope,
        memory_scope_user_id=user["id"] if memory_scope else None,
    )

    async def override():
        return fake

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), app


@pytest.mark.asyncio
async def test_owner_grants_user_then_lists_and_revokes(rag_env):
    client, app = _client(rag_env["owner"], role="member")
    agent_id = rag_env["agent_id"]
    member_id = str(rag_env["member"]["id"])
    async with client as c:
        grant = await c.post(
            f"/api/access-grants/agent/{agent_id}/grant",
            json={"principal_type": "user", "principal_id": member_id},
        )
        assert grant.status_code == 201, grant.text
        assert grant.json()["status"] == "granted"

        listed = await c.get(f"/api/access-grants/agent/{agent_id}")
        assert listed.status_code == 200
        principals = {
            (g["principal_type"], g["principal_id"]) for g in listed.json()["grants"]
        }
        assert ("user", member_id) in principals

        revoke = await c.post(
            f"/api/access-grants/agent/{agent_id}/revoke",
            json={"principal_type": "user", "principal_id": member_id},
        )
        assert revoke.status_code == 200
        assert revoke.json()["status"] == "revoked"

        listed2 = await c.get(f"/api/access-grants/agent/{agent_id}")
        assert all(g["principal_id"] != member_id for g in listed2.json()["grants"])
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_can_grant_org(rag_env):
    client, app = _client(rag_env["admin"], role="admin")
    agent_id = rag_env["agent_id"]
    async with client as c:
        grant = await c.post(
            f"/api/access-grants/agent/{agent_id}/grant",
            json={"principal_type": "org"},
        )
        assert grant.status_code == 201, grant.text
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_member_non_owner_forbidden(rag_env):
    client, app = _client(rag_env["member"], role="member")
    agent_id = rag_env["agent_id"]
    async with client as c:
        grant = await c.post(
            f"/api/access-grants/agent/{agent_id}/grant",
            json={"principal_type": "org"},
        )
        assert grant.status_code == 403
        listed = await c.get(f"/api/access-grants/agent/{agent_id}")
        assert listed.status_code == 403
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_scoped_key_forbidden(rag_env):
    client, app = _client(rag_env["owner"], role="member", memory_scope="user")
    agent_id = rag_env["agent_id"]
    async with client as c:
        grant = await c.post(
            f"/api/access-grants/agent/{agent_id}/grant",
            json={"principal_type": "org"},
        )
        assert grant.status_code == 403
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unknown_resource_type(rag_env):
    client, app = _client(rag_env["owner"], role="member")
    async with client as c:
        grant = await c.post(
            "/api/access-grants/banana/x/grant",
            json={"principal_type": "org"},
        )
        assert grant.status_code == 404
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_missing_principal_id(rag_env):
    client, app = _client(rag_env["owner"], role="member")
    agent_id = rag_env["agent_id"]
    async with client as c:
        grant = await c.post(
            f"/api/access-grants/agent/{agent_id}/grant",
            json={"principal_type": "user"},
        )
        assert grant.status_code == 400
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_cross_org_principal_rejected(rag_env, db_pool):
    other_org = await OrganizationRepository(db_pool).create(name="rag-rest-foreign")
    foreign = await UserRepository(db_pool).create(
        external_id="rag-rest-foreign", provider="local",
        organization_id=other_org["id"], email="rag-rest-foreign@test.com",
        display_name="Foreign",
    )
    client, app = _client(rag_env["owner"], role="member")
    agent_id = rag_env["agent_id"]
    try:
        async with client as c:
            grant = await c.post(
                f"/api/access-grants/agent/{agent_id}/grant",
                json={"principal_type": "user", "principal_id": str(foreign["id"])},
            )
            assert grant.status_code == 400
        app.dependency_overrides.clear()
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", foreign["id"])
            await conn.execute("DELETE FROM organizations WHERE id = $1", other_org["id"])
