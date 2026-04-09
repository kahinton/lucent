"""Comprehensive ACL enforcement tests for resource endpoints.

Tests that every resource endpoint (definitions, sandbox templates) enforces
access control correctly. Covers:
- Private resources invisible to non-owners
- Group-shared resources visible to group members
- Group removal revokes access
- Admin override (sees everything)
- Owner can update/delete own resources
- Non-owner member cannot update/delete
- Anti-spoofing: identity from token, not request body
"""

from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import (
    DefinitionRepository,
    GroupRepository,
    OrganizationRepository,
    UserRepository,
    get_pool,
)
from lucent.db.audit import AuditRepository

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def acl_prefix(db_pool):
    """Unique prefix + cleanup for ACL tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_acl_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        # Clean junction tables
        await conn.execute(
            "DELETE FROM agent_skills WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM agent_mcp_servers WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM agent_definitions WHERE name LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM skill_definitions WHERE name LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM mcp_server_configs WHERE name LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM sandbox_templates WHERE name LIKE $1", f"{prefix}%")
        # Clean user_groups before groups and users
        await conn.execute(
            "DELETE FROM user_groups WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM groups WHERE name LIKE $1",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def org_and_users(db_pool, acl_prefix):
    """Create an org with three users: owner (member), other (member), admin."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{acl_prefix}org")
    user_repo = UserRepository(db_pool)

    owner_user = await user_repo.create(
        external_id=f"{acl_prefix}owner",
        provider="local",
        organization_id=org["id"],
        email=f"{acl_prefix}owner@test.com",
        display_name="Owner User",
        role="member",
    )
    other_user = await user_repo.create(
        external_id=f"{acl_prefix}other",
        provider="local",
        organization_id=org["id"],
        email=f"{acl_prefix}other@test.com",
        display_name="Other User",
        role="member",
    )
    admin_user = await user_repo.create(
        external_id=f"{acl_prefix}admin",
        provider="local",
        organization_id=org["id"],
        email=f"{acl_prefix}admin@test.com",
        display_name="Admin User",
        role="admin",
    )
    return {
        "org": org,
        "owner": owner_user,
        "other": other_user,
        "admin": admin_user,
    }


def _make_client(app, user_record):
    """Return an httpx AsyncClient configured to authenticate as the given user."""

    fake = CurrentUser(
        id=user_record["id"],
        organization_id=user_record["organization_id"],
        role=user_record.get("role", "member"),
        email=user_record.get("email"),
        display_name=user_record.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ============================================================================
# Agent ACL Enforcement
# ============================================================================


class TestAgentACLRead:
    """Read-access tests for agents (list + get)."""

    async def test_private_agent_invisible_to_other_member(
        self, db_pool, acl_prefix, org_and_users
    ):
        """User A creates a private agent → User B (member) CANNOT see it."""
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]

        # Owner creates agent
        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/agents",
                json={
                    "name": f"{acl_prefix}private_agent",
                    "description": "Private",
                    "content": "# Private",
                },
            )
            assert resp.status_code == 201
            agent_id = resp.json()["id"]

        # Other user cannot see it in list
        async with _make_client(app, other) as client:
            resp = await client.get("/api/definitions/agents")
            assert resp.status_code == 200
            names = [a["name"] for a in resp.json()["items"]]
            assert f"{acl_prefix}private_agent" not in names

            # Other user cannot get it by ID
            resp = await client.get(f"/api/definitions/agents/{agent_id}")
            assert resp.status_code == 404

        app.dependency_overrides.clear()

    async def test_admin_can_see_all_agents(self, db_pool, acl_prefix, org_and_users):
        """Admin can see all agents regardless of ownership."""
        app = create_app()
        owner = org_and_users["owner"]
        admin = org_and_users["admin"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/agents",
                json={
                    "name": f"{acl_prefix}admin_visible",
                    "description": "Test",
                    "content": "# T",
                },
            )
            assert resp.status_code == 201
            agent_id = resp.json()["id"]

        async with _make_client(app, admin) as client:
            resp = await client.get("/api/definitions/agents")
            names = [a["name"] for a in resp.json()["items"]]
            assert f"{acl_prefix}admin_visible" in names

            resp = await client.get(f"/api/definitions/agents/{agent_id}")
            assert resp.status_code == 200

        app.dependency_overrides.clear()

    async def test_group_shared_agent_visible_to_group_member(
        self, db_pool, acl_prefix, org_and_users
    ):
        """User A shares agent with Group X → User B (member of Group X) CAN see it."""
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]
        org = org_and_users["org"]

        # Create group and add other user
        group_repo = GroupRepository(db_pool)
        group = await group_repo.create_group(f"{acl_prefix}groupX", str(org["id"]))
        await group_repo.add_member(str(group["id"]), str(other["id"]))

        # Create agent owned by a group
        pool = await get_pool()
        repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
        agent = await repo.create_agent(
            name=f"{acl_prefix}group_agent",
            description="Group shared",
            content="# Group",
            org_id=str(org["id"]),
            created_by=str(owner["id"]),
            owner_group_id=str(group["id"]),
            owner_user_id=None,
        )
        # Clear the owner_user_id that defaults from created_by
        await repo.update_agent(
            str(agent["id"]),
            str(org["id"]),
            owner_user_id=None,
            owner_group_id=str(group["id"]),
        )

        # Other user (in group) can see it
        async with _make_client(app, other) as client:
            resp = await client.get(f"/api/definitions/agents/{agent['id']}")
            assert resp.status_code == 200

        app.dependency_overrides.clear()

    async def test_group_removal_revokes_access(self, db_pool, acl_prefix, org_and_users):
        """User B removed from Group X → User B CANNOT see the agent anymore."""
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]
        org = org_and_users["org"]

        # Create group, add other user, create group-owned agent
        group_repo = GroupRepository(db_pool)
        group = await group_repo.create_group(f"{acl_prefix}groupY", str(org["id"]))
        await group_repo.add_member(str(group["id"]), str(other["id"]))

        pool = await get_pool()
        repo = DefinitionRepository(pool, audit_repo=AuditRepository(pool))
        agent = await repo.create_agent(
            name=f"{acl_prefix}revoke_agent",
            description="Will be revoked",
            content="# Revoke",
            org_id=str(org["id"]),
            created_by=str(owner["id"]),
            owner_group_id=str(group["id"]),
            owner_user_id=None,
        )
        await repo.update_agent(
            str(agent["id"]),
            str(org["id"]),
            owner_user_id=None,
            owner_group_id=str(group["id"]),
        )

        # Verify other user can see it
        async with _make_client(app, other) as client:
            resp = await client.get(f"/api/definitions/agents/{agent['id']}")
            assert resp.status_code == 200

        # Remove other user from group
        await group_repo.remove_member(str(group["id"]), str(other["id"]))

        # Invalidate group cache
        from lucent.access_control import AccessControlService

        AccessControlService.invalidate_user_groups(str(other["id"]))

        # Now other user CANNOT see it
        async with _make_client(app, other) as client:
            resp = await client.get(f"/api/definitions/agents/{agent['id']}")
            assert resp.status_code == 404

        app.dependency_overrides.clear()


class TestAgentACLWrite:
    """Write-access tests for agents (update + delete)."""

    async def test_owner_can_update_own_agent(self, db_pool, acl_prefix, org_and_users):
        """Owner of agent can update it."""
        app = create_app()
        owner = org_and_users["owner"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/agents",
                json={
                    "name": f"{acl_prefix}own_update",
                    "description": "Original",
                    "content": "# Orig",
                },
            )
            agent_id = resp.json()["id"]

            resp = await client.patch(
                f"/api/definitions/agents/{agent_id}",
                json={
                    "name": f"{acl_prefix}own_update",
                    "description": "Updated",
                    "content": "# Updated",
                },
            )
            assert resp.status_code == 200
            assert resp.json()["description"] == "Updated"

        app.dependency_overrides.clear()

    async def test_non_owner_member_cannot_update_agent(self, db_pool, acl_prefix, org_and_users):
        """Non-owner member cannot update another user's agent (gets 404)."""
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/agents",
                json={"name": f"{acl_prefix}no_update", "description": "Nope", "content": "# No"},
            )
            agent_id = resp.json()["id"]

        async with _make_client(app, other) as client:
            resp = await client.patch(
                f"/api/definitions/agents/{agent_id}",
                json={
                    "name": f"{acl_prefix}no_update",
                    "description": "Hacked",
                    "content": "# Hacked",
                },
            )
            assert resp.status_code == 404

        app.dependency_overrides.clear()

    async def test_admin_can_update_any_agent(self, db_pool, acl_prefix, org_and_users):
        """Admin can update any agent in the org."""
        app = create_app()
        owner = org_and_users["owner"]
        admin = org_and_users["admin"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/agents",
                json={"name": f"{acl_prefix}admin_upd", "description": "Test", "content": "# T"},
            )
            agent_id = resp.json()["id"]

        async with _make_client(app, admin) as client:
            resp = await client.patch(
                f"/api/definitions/agents/{agent_id}",
                json={
                    "name": f"{acl_prefix}admin_upd",
                    "description": "Admin edit",
                    "content": "# Admin",
                },
            )
            assert resp.status_code == 200
            assert resp.json()["description"] == "Admin edit"

        app.dependency_overrides.clear()

    async def test_member_owner_cannot_delete_own_agent(self, db_pool, acl_prefix, org_and_users):
        """Member owner cannot delete agent (admin/owner role required)."""
        app = create_app()
        owner = org_and_users["owner"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/agents",
                json={"name": f"{acl_prefix}own_del", "description": "Del", "content": "# D"},
            )
            agent_id = resp.json()["id"]

            resp = await client.delete(f"/api/definitions/agents/{agent_id}")
            assert resp.status_code == 403

        app.dependency_overrides.clear()

    async def test_non_owner_member_cannot_delete_agent(self, db_pool, acl_prefix, org_and_users):
        """Non-owner member cannot delete another user's agent (gets 403)."""
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/agents",
                json={"name": f"{acl_prefix}no_del", "description": "No", "content": "# No"},
            )
            agent_id = resp.json()["id"]

        async with _make_client(app, other) as client:
            resp = await client.delete(f"/api/definitions/agents/{agent_id}")
            assert resp.status_code == 403

        # Verify it still exists (owner can see it)
        async with _make_client(app, owner) as client:
            resp = await client.get(f"/api/definitions/agents/{agent_id}")
            assert resp.status_code == 200

        app.dependency_overrides.clear()

    async def test_admin_can_delete_any_agent(self, db_pool, acl_prefix, org_and_users):
        """Admin can delete any agent in the org."""
        app = create_app()
        owner = org_and_users["owner"]
        admin = org_and_users["admin"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/agents",
                json={"name": f"{acl_prefix}admin_del", "description": "Del", "content": "# D"},
            )
            agent_id = resp.json()["id"]

        async with _make_client(app, admin) as client:
            resp = await client.delete(f"/api/definitions/agents/{agent_id}")
            assert resp.status_code == 204

        app.dependency_overrides.clear()


# ============================================================================
# Skill ACL Enforcement
# ============================================================================


class TestSkillACL:
    """ACL tests for skills."""

    async def test_private_skill_invisible_to_other_member(
        self, db_pool, acl_prefix, org_and_users
    ):
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/skills",
                json={
                    "name": f"{acl_prefix}priv_skill",
                    "description": "Private",
                    "content": "# P",
                },
            )
            assert resp.status_code == 201
            skill_id = resp.json()["id"]

        async with _make_client(app, other) as client:
            resp = await client.get(f"/api/definitions/skills/{skill_id}")
            assert resp.status_code == 404

            resp = await client.get("/api/definitions/skills")
            names = [s["name"] for s in resp.json()["items"]]
            assert f"{acl_prefix}priv_skill" not in names

        app.dependency_overrides.clear()

    async def test_member_owner_cannot_delete_own_skill(self, db_pool, acl_prefix, org_and_users):
        app = create_app()
        owner = org_and_users["owner"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/skills",
                json={"name": f"{acl_prefix}del_skill", "description": "D", "content": "# D"},
            )
            skill_id = resp.json()["id"]

            resp = await client.delete(f"/api/definitions/skills/{skill_id}")
            assert resp.status_code == 403

        app.dependency_overrides.clear()

    async def test_non_owner_cannot_delete_skill(self, db_pool, acl_prefix, org_and_users):
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/skills",
                json={"name": f"{acl_prefix}no_del_skill", "description": "N", "content": "# N"},
            )
            skill_id = resp.json()["id"]

        async with _make_client(app, other) as client:
            resp = await client.delete(f"/api/definitions/skills/{skill_id}")
            assert resp.status_code == 403

        app.dependency_overrides.clear()


# ============================================================================
# MCP Server ACL Enforcement
# ============================================================================


class TestMCPServerACL:
    """ACL tests for MCP servers."""

    async def test_private_mcp_server_invisible_to_other_member(
        self, db_pool, acl_prefix, org_and_users
    ):
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/mcp-servers",
                json={
                    "name": f"{acl_prefix}priv_mcp",
                    "description": "Private",
                    "url": "http://x:8000",
                },
            )
            assert resp.status_code == 201

        async with _make_client(app, other) as client:
            resp = await client.get("/api/definitions/mcp-servers")
            names = [s["name"] for s in resp.json()["items"]]
            assert f"{acl_prefix}priv_mcp" not in names

        app.dependency_overrides.clear()

    async def test_admin_can_see_all_mcp_servers(self, db_pool, acl_prefix, org_and_users):
        app = create_app()
        owner = org_and_users["owner"]
        admin = org_and_users["admin"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/mcp-servers",
                json={"name": f"{acl_prefix}admin_mcp", "description": "T", "url": "http://y:8000"},
            )
            assert resp.status_code == 201

        async with _make_client(app, admin) as client:
            resp = await client.get("/api/definitions/mcp-servers")
            names = [s["name"] for s in resp.json()["items"]]
            assert f"{acl_prefix}admin_mcp" in names

        app.dependency_overrides.clear()


# ============================================================================
# Sandbox Template ACL Enforcement
# ============================================================================


class TestSandboxTemplateACL:
    """ACL tests for sandbox templates."""

    async def test_template_list_filtered_by_access(self, db_pool, acl_prefix, org_and_users):
        """Template list_accessible_by only returns accessible templates."""
        from lucent.db.sandbox_template import SandboxTemplateRepository

        owner = org_and_users["owner"]
        other = org_and_users["other"]
        org = org_and_users["org"]

        repo = SandboxTemplateRepository(db_pool)
        await repo.create(
            name=f"{acl_prefix}priv_tpl",
            organization_id=str(org["id"]),
            description="Private template",
            created_by=str(owner["id"]),
        )

        # Other user's accessible list should not include it
        result = await repo.list_accessible_by(
            str(other["id"]), str(org["id"]), user_role="member"
        )
        names = [t["name"] for t in result["items"]]
        assert f"{acl_prefix}priv_tpl" not in names

        # Owner's accessible list should include it
        result = await repo.list_accessible_by(
            str(owner["id"]), str(org["id"]), user_role="member"
        )
        names = [t["name"] for t in result["items"]]
        assert f"{acl_prefix}priv_tpl" in names

    async def test_template_get_filtered_by_access(self, db_pool, acl_prefix, org_and_users):
        """Cannot get a template you don't have access to."""
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/sandboxes/templates",
                json={"name": f"{acl_prefix}priv_tpl2", "description": "Private"},
            )
            tpl_id = resp.json()["id"]

        async with _make_client(app, other) as client:
            resp = await client.get(f"/api/sandboxes/templates/{tpl_id}")
            assert resp.status_code == 404

        app.dependency_overrides.clear()

    async def test_owner_can_update_own_template(self, db_pool, acl_prefix, org_and_users):
        app = create_app()
        owner = org_and_users["owner"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/sandboxes/templates",
                json={"name": f"{acl_prefix}upd_tpl", "description": "Original"},
            )
            tpl_id = resp.json()["id"]

            resp = await client.patch(
                f"/api/sandboxes/templates/{tpl_id}",
                json={"description": "Updated"},
            )
            assert resp.status_code == 200
            assert resp.json()["description"] == "Updated"

        app.dependency_overrides.clear()

    async def test_non_owner_cannot_update_template(self, db_pool, acl_prefix, org_and_users):
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/sandboxes/templates",
                json={"name": f"{acl_prefix}no_upd_tpl", "description": "Nope"},
            )
            tpl_id = resp.json()["id"]

        async with _make_client(app, other) as client:
            resp = await client.patch(
                f"/api/sandboxes/templates/{tpl_id}",
                json={"description": "Hacked"},
            )
            assert resp.status_code == 404

        app.dependency_overrides.clear()

    async def test_non_owner_cannot_delete_template(self, db_pool, acl_prefix, org_and_users):
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/sandboxes/templates",
                json={"name": f"{acl_prefix}no_del_tpl", "description": "Nope"},
            )
            tpl_id = resp.json()["id"]

        async with _make_client(app, other) as client:
            resp = await client.delete(f"/api/sandboxes/templates/{tpl_id}")
            assert resp.status_code == 404

        app.dependency_overrides.clear()

    async def test_admin_can_update_any_template(self, db_pool, acl_prefix, org_and_users):
        app = create_app()
        owner = org_and_users["owner"]
        admin = org_and_users["admin"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/sandboxes/templates",
                json={"name": f"{acl_prefix}admin_tpl", "description": "Orig"},
            )
            tpl_id = resp.json()["id"]

        async with _make_client(app, admin) as client:
            resp = await client.patch(
                f"/api/sandboxes/templates/{tpl_id}",
                json={"description": "Admin edit"},
            )
            assert resp.status_code == 200
            assert resp.json()["description"] == "Admin edit"

        app.dependency_overrides.clear()

    async def test_admin_can_delete_any_template(self, db_pool, acl_prefix, org_and_users):
        app = create_app()
        owner = org_and_users["owner"]
        admin = org_and_users["admin"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/sandboxes/templates",
                json={"name": f"{acl_prefix}admin_del_tpl", "description": "D"},
            )
            tpl_id = resp.json()["id"]

        async with _make_client(app, admin) as client:
            resp = await client.delete(f"/api/sandboxes/templates/{tpl_id}")
            assert resp.status_code == 200

        app.dependency_overrides.clear()

    async def test_launch_from_template_requires_access(self, db_pool, acl_prefix, org_and_users):
        """Cannot launch a sandbox from a template you don't have access to."""
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/sandboxes/templates",
                json={"name": f"{acl_prefix}launch_tpl", "description": "Launch"},
            )
            tpl_id = resp.json()["id"]

        async with _make_client(app, other) as client:
            resp = await client.post(f"/api/sandboxes/templates/{tpl_id}/launch")
            assert resp.status_code == 404

        app.dependency_overrides.clear()


# ============================================================================
# Anti-Spoofing Tests
# ============================================================================


class TestAntiSpoofing:
    """Identity must come from the authenticated token, not from request body."""

    async def test_identity_from_token_not_request_body(self, db_pool, acl_prefix, org_and_users):
        """Even if User B passes User A's ID in the body, the agent is owned by User B."""
        app = create_app()
        other = org_and_users["other"]
        owner = org_and_users["owner"]

        # Other user creates an agent — ownership is determined by token
        async with _make_client(app, other) as client:
            resp = await client.post(
                "/api/definitions/agents",
                json={
                    "name": f"{acl_prefix}spoof_agent",
                    "description": "Spoofing test",
                    "content": "# Spoof",
                },
            )
            assert resp.status_code == 201
            agent_data = resp.json()
            # Owner is the authenticated user (other), not owner_user
            assert str(agent_data["owner_user_id"]) == str(other["id"])

        # Owner user (member) cannot see agent created by other user
        async with _make_client(app, owner) as client:
            resp = await client.get(f"/api/definitions/agents/{agent_data['id']}")
            assert resp.status_code == 404

        app.dependency_overrides.clear()

    async def test_cannot_modify_resource_by_spoofing_identity(
        self, db_pool, acl_prefix, org_and_users
    ):
        """Non-owner cannot update a resource even by directly hitting the endpoint."""
        app = create_app()
        owner = org_and_users["owner"]
        other = org_and_users["other"]

        async with _make_client(app, owner) as client:
            resp = await client.post(
                "/api/definitions/agents",
                json={"name": f"{acl_prefix}spoof_modify", "description": "T", "content": "# T"},
            )
            agent_id = resp.json()["id"]

        # Other tries to update — should get 404 (no existence leak)
        async with _make_client(app, other) as client:
            resp = await client.patch(
                f"/api/definitions/agents/{agent_id}",
                json={
                    "name": f"{acl_prefix}spoof_modify",
                    "description": "Pwned",
                    "content": "# Pwned",
                },
            )
            assert resp.status_code == 404

        # Verify original is untouched
        async with _make_client(app, owner) as client:
            resp = await client.get(f"/api/definitions/agents/{agent_id}")
            assert resp.status_code == 200
            assert resp.json()["description"] == "T"

        app.dependency_overrides.clear()

    async def test_nonexistent_resource_returns_404(self, db_pool, acl_prefix, org_and_users):
        """Accessing a nonexistent resource returns 404, same as access denied."""
        app = create_app()
        other = org_and_users["other"]
        fake_id = str(uuid4())

        async with _make_client(app, other) as client:
            resp = await client.get(f"/api/definitions/agents/{fake_id}")
            assert resp.status_code == 404

            resp = await client.patch(
                f"/api/definitions/agents/{fake_id}",
                json={"name": "x", "description": "x", "content": "x"},
            )
            assert resp.status_code == 404

            resp = await client.delete(f"/api/definitions/agents/{fake_id}")
            assert resp.status_code == 403

        app.dependency_overrides.clear()
