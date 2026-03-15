"""API endpoint tests for the definitions router.

Tests /api/definitions endpoints:
- Agents: CRUD, approve/reject workflow
- Skills: CRUD, approve/reject workflow
- MCP Servers: CRUD, approve/reject workflow
- Access Grants: agent↔skill, agent↔mcp-server
- Proposals: list pending proposals
"""

from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import OrganizationRepository, UserRepository

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def def_prefix(db_pool):
    """Create and clean up test data for definitions API tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_def_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        # Clean up junction tables first (FK constraints)
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
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def def_user(db_pool, def_prefix):
    """Create a test user for definitions API tests."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{def_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{def_prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{def_prefix}user@test.com",
        display_name=f"{def_prefix}User",
        role="admin",
    )
    return user


@pytest_asyncio.fixture
async def client(db_pool, def_user):
    """Create an httpx AsyncClient with auth dependency overridden."""
    app = create_app()

    fake_user = CurrentUser(
        id=def_user["id"],
        organization_id=def_user["organization_id"],
        role=def_user.get("role", "admin"),
        email=def_user.get("email"),
        display_name=def_user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override_get_current_user():
        return fake_user

    app.dependency_overrides[get_current_user] = override_get_current_user

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()


# ============================================================================
# Agent Endpoint Tests
# ============================================================================


class TestAgentCRUD:
    """CRUD operations on /api/definitions/agents."""

    async def test_create_agent(self, client, def_prefix):
        resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}research",
                "description": "A research agent",
                "content": "# Research Agent\nYou are a research agent.",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == f"{def_prefix}research"
        assert data["status"] == "proposed"
        assert data["scope"] == "instance"

    async def test_list_agents(self, client, def_prefix):
        # Create two agents
        for name in ["alpha", "beta"]:
            await client.post(
                "/api/definitions/agents",
                json={
                    "name": f"{def_prefix}{name}",
                    "description": f"Agent {name}",
                    "content": f"# {name}",
                },
            )
        resp = await client.get("/api/definitions/agents")
        assert resp.status_code == 200
        agents = resp.json()
        names = [a["name"] for a in agents]
        assert f"{def_prefix}alpha" in names
        assert f"{def_prefix}beta" in names

    async def test_list_agents_filter_by_status(self, client, def_prefix):
        resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}filtered",
                "description": "Test",
                "content": "# Test",
            },
        )
        assert resp.status_code == 201

        # Filter for proposed
        resp = await client.get("/api/definitions/agents?status=proposed")
        assert resp.status_code == 200
        agents = resp.json()
        assert all(a["status"] == "proposed" for a in agents)

        # Filter for active (should not include our new agent)
        resp = await client.get("/api/definitions/agents?status=active")
        assert resp.status_code == 200
        names = [a["name"] for a in resp.json()]
        assert f"{def_prefix}filtered" not in names

    async def test_get_agent(self, client, def_prefix):
        create_resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}get_test",
                "description": "For get test",
                "content": "# Get test",
            },
        )
        agent_id = create_resp.json()["id"]

        resp = await client.get(f"/api/definitions/agents/{agent_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == f"{def_prefix}get_test"

    async def test_get_agent_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.get(f"/api/definitions/agents/{fake_id}")
        assert resp.status_code == 404

    async def test_update_agent(self, client, def_prefix):
        create_resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}to_update",
                "description": "Original",
                "content": "# Original",
            },
        )
        agent_id = create_resp.json()["id"]

        resp = await client.patch(
            f"/api/definitions/agents/{agent_id}",
            json={
                "name": f"{def_prefix}to_update",
                "description": "Updated",
                "content": "# Updated content",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "Updated"

    async def test_delete_agent(self, client, def_prefix):
        create_resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}to_delete",
                "description": "Delete me",
                "content": "# Delete me",
            },
        )
        agent_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/definitions/agents/{agent_id}")
        assert resp.status_code == 204

        # Verify deleted
        resp = await client.get(f"/api/definitions/agents/{agent_id}")
        assert resp.status_code == 404

    async def test_delete_agent_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.delete(f"/api/definitions/agents/{fake_id}")
        assert resp.status_code == 404


# ============================================================================
# Agent Approval Workflow Tests
# ============================================================================


class TestAgentApproval:
    """Approve/reject workflow for agents."""

    async def test_approve_agent(self, client, def_prefix):
        create_resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}approvable",
                "description": "To approve",
                "content": "# Approve me",
            },
        )
        agent_id = create_resp.json()["id"]
        assert create_resp.json()["status"] == "proposed"

        resp = await client.post(f"/api/definitions/agents/{agent_id}/approve")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        assert data["approved_by"] is not None

    async def test_reject_agent(self, client, def_prefix):
        create_resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}rejectable",
                "description": "To reject",
                "content": "# Reject me",
            },
        )
        agent_id = create_resp.json()["id"]

        resp = await client.post(f"/api/definitions/agents/{agent_id}/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    async def test_approve_already_active_fails(self, client, def_prefix):
        create_resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}double_approve",
                "description": "Test",
                "content": "# Test",
            },
        )
        agent_id = create_resp.json()["id"]

        # Approve once
        await client.post(f"/api/definitions/agents/{agent_id}/approve")

        # Try to approve again
        resp = await client.post(f"/api/definitions/agents/{agent_id}/approve")
        assert resp.status_code == 404

    async def test_approve_nonexistent_agent(self, client):
        fake_id = str(uuid4())
        resp = await client.post(f"/api/definitions/agents/{fake_id}/approve")
        assert resp.status_code == 404


# ============================================================================
# Skill Endpoint Tests
# ============================================================================


class TestSkillCRUD:
    """CRUD operations on /api/definitions/skills."""

    async def test_create_skill(self, client, def_prefix):
        resp = await client.post(
            "/api/definitions/skills",
            json={
                "name": f"{def_prefix}memory-init",
                "description": "Initialize memory context",
                "content": "# Memory Init Skill",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == f"{def_prefix}memory-init"
        assert data["status"] == "proposed"

    async def test_list_skills(self, client, def_prefix):
        await client.post(
            "/api/definitions/skills",
            json={
                "name": f"{def_prefix}skill_a",
                "description": "Skill A",
                "content": "# A",
            },
        )
        resp = await client.get("/api/definitions/skills")
        assert resp.status_code == 200
        names = [s["name"] for s in resp.json()]
        assert f"{def_prefix}skill_a" in names

    async def test_get_skill(self, client, def_prefix):
        create_resp = await client.post(
            "/api/definitions/skills",
            json={
                "name": f"{def_prefix}get_skill",
                "description": "For get",
                "content": "# Get skill",
            },
        )
        skill_id = create_resp.json()["id"]

        resp = await client.get(f"/api/definitions/skills/{skill_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == f"{def_prefix}get_skill"

    async def test_get_skill_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.get(f"/api/definitions/skills/{fake_id}")
        assert resp.status_code == 404

    async def test_approve_skill(self, client, def_prefix):
        create_resp = await client.post(
            "/api/definitions/skills",
            json={
                "name": f"{def_prefix}approve_skill",
                "description": "To approve",
                "content": "# Approve",
            },
        )
        skill_id = create_resp.json()["id"]

        resp = await client.post(f"/api/definitions/skills/{skill_id}/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    async def test_reject_skill(self, client, def_prefix):
        create_resp = await client.post(
            "/api/definitions/skills",
            json={
                "name": f"{def_prefix}reject_skill",
                "description": "To reject",
                "content": "# Reject",
            },
        )
        skill_id = create_resp.json()["id"]

        resp = await client.post(f"/api/definitions/skills/{skill_id}/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    async def test_delete_skill(self, client, def_prefix):
        create_resp = await client.post(
            "/api/definitions/skills",
            json={
                "name": f"{def_prefix}del_skill",
                "description": "Delete",
                "content": "# Del",
            },
        )
        skill_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/definitions/skills/{skill_id}")
        assert resp.status_code == 204


# ============================================================================
# MCP Server Endpoint Tests
# ============================================================================


class TestMCPServerCRUD:
    """CRUD operations on /api/definitions/mcp-servers."""

    async def test_create_mcp_server(self, client, def_prefix):
        resp = await client.post(
            "/api/definitions/mcp-servers",
            json={
                "name": f"{def_prefix}lucent",
                "description": "Lucent memory server",
                "server_type": "http",
                "url": "http://localhost:8766/mcp",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == f"{def_prefix}lucent"
        assert data["status"] == "proposed"

    async def test_list_mcp_servers(self, client, def_prefix):
        await client.post(
            "/api/definitions/mcp-servers",
            json={
                "name": f"{def_prefix}server_a",
                "description": "Server A",
                "url": "http://a:8000",
            },
        )
        resp = await client.get("/api/definitions/mcp-servers")
        assert resp.status_code == 200
        names = [s["name"] for s in resp.json()]
        assert f"{def_prefix}server_a" in names

    async def test_approve_mcp_server(self, client, def_prefix):
        create_resp = await client.post(
            "/api/definitions/mcp-servers",
            json={
                "name": f"{def_prefix}approve_srv",
                "description": "To approve",
                "url": "http://approve:8000",
            },
        )
        server_id = create_resp.json()["id"]

        resp = await client.post(f"/api/definitions/mcp-servers/{server_id}/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    async def test_reject_mcp_server(self, client, def_prefix):
        create_resp = await client.post(
            "/api/definitions/mcp-servers",
            json={
                "name": f"{def_prefix}reject_srv",
                "description": "To reject",
                "url": "http://reject:8000",
            },
        )
        server_id = create_resp.json()["id"]

        resp = await client.post(f"/api/definitions/mcp-servers/{server_id}/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"


# ============================================================================
# Access Grant Tests
# ============================================================================


class TestAccessGrants:
    """Agent ↔ skill and agent ↔ MCP server access grants."""

    async def test_grant_skill_to_agent(self, client, def_prefix):
        # Create and approve agent
        agent_resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}grant_agent",
                "description": "Agent for grants",
                "content": "# Grant agent",
            },
        )
        agent_id = agent_resp.json()["id"]
        await client.post(f"/api/definitions/agents/{agent_id}/approve")

        # Create and approve skill
        skill_resp = await client.post(
            "/api/definitions/skills",
            json={
                "name": f"{def_prefix}grant_skill",
                "description": "Skill for grants",
                "content": "# Grant skill",
            },
        )
        skill_id = skill_resp.json()["id"]
        await client.post(f"/api/definitions/skills/{skill_id}/approve")

        # Grant skill to agent
        resp = await client.post(
            f"/api/definitions/agents/{agent_id}/skills",
            json={"target_id": skill_id},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "granted"

    async def test_revoke_skill_from_agent(self, client, def_prefix):
        # Create, approve, and grant
        agent_resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}revoke_agent",
                "description": "Agent",
                "content": "# Agent",
            },
        )
        agent_id = agent_resp.json()["id"]
        await client.post(f"/api/definitions/agents/{agent_id}/approve")

        skill_resp = await client.post(
            "/api/definitions/skills",
            json={
                "name": f"{def_prefix}revoke_skill",
                "description": "Skill",
                "content": "# Skill",
            },
        )
        skill_id = skill_resp.json()["id"]
        await client.post(f"/api/definitions/skills/{skill_id}/approve")

        await client.post(
            f"/api/definitions/agents/{agent_id}/skills",
            json={"target_id": skill_id},
        )

        # Revoke
        resp = await client.delete(f"/api/definitions/agents/{agent_id}/skills/{skill_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"

    async def test_grant_mcp_server_to_agent(self, client, def_prefix):
        agent_resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}mcp_agent",
                "description": "Agent for MCP",
                "content": "# MCP agent",
            },
        )
        agent_id = agent_resp.json()["id"]
        await client.post(f"/api/definitions/agents/{agent_id}/approve")

        server_resp = await client.post(
            "/api/definitions/mcp-servers",
            json={
                "name": f"{def_prefix}mcp_srv",
                "description": "MCP server",
                "url": "http://mcp:8766",
            },
        )
        server_id = server_resp.json()["id"]
        await client.post(f"/api/definitions/mcp-servers/{server_id}/approve")

        resp = await client.post(
            f"/api/definitions/agents/{agent_id}/mcp-servers",
            json={"target_id": server_id},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "granted"

    async def test_revoke_mcp_server_from_agent(self, client, def_prefix):
        agent_resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}revoke_mcp_agent",
                "description": "Agent",
                "content": "# Agent",
            },
        )
        agent_id = agent_resp.json()["id"]
        await client.post(f"/api/definitions/agents/{agent_id}/approve")

        server_resp = await client.post(
            "/api/definitions/mcp-servers",
            json={
                "name": f"{def_prefix}revoke_mcp_srv",
                "description": "Server",
                "url": "http://srv:8766",
            },
        )
        server_id = server_resp.json()["id"]
        await client.post(f"/api/definitions/mcp-servers/{server_id}/approve")

        await client.post(
            f"/api/definitions/agents/{agent_id}/mcp-servers",
            json={"target_id": server_id},
        )

        resp = await client.delete(f"/api/definitions/agents/{agent_id}/mcp-servers/{server_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"


# ============================================================================
# Proposals Endpoint Tests
# ============================================================================


class TestProposals:
    """GET /api/definitions/proposals."""

    async def test_list_proposals(self, client, def_prefix):
        # Create some proposed items
        await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}proposed_agent",
                "description": "Proposed",
                "content": "# Proposed",
            },
        )
        await client.post(
            "/api/definitions/skills",
            json={
                "name": f"{def_prefix}proposed_skill",
                "description": "Proposed",
                "content": "# Proposed",
            },
        )

        resp = await client.get("/api/definitions/proposals")
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert "skills" in data
        assert "mcp_servers" in data
        assert "total" in data
        assert data["total"] >= 2

    async def test_approved_items_not_in_proposals(self, client, def_prefix):
        # Create and approve an agent
        create_resp = await client.post(
            "/api/definitions/agents",
            json={
                "name": f"{def_prefix}approved_not_proposed",
                "description": "Test",
                "content": "# Test",
            },
        )
        agent_id = create_resp.json()["id"]
        await client.post(f"/api/definitions/agents/{agent_id}/approve")

        resp = await client.get("/api/definitions/proposals")
        agent_names = [a["name"] for a in resp.json()["agents"]]
        assert f"{def_prefix}approved_not_proposed" not in agent_names
