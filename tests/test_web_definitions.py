"""Integration tests for definition web routes in web/routes.py.

Tests the HTML-serving endpoints:
- GET  /definitions                              (list with tabs)
- GET  /definitions/agents/{id}                  (agent detail)
- GET  /definitions/skills/{id}                  (skill detail)
- GET  /definitions/mcp-servers/{id}             (MCP server detail)
- POST /definitions/agents/create                (create agent)
- POST /definitions/agents/{id}/update           (update agent)
- POST /definitions/agents/{id}/delete           (delete agent)
- POST /definitions/agents/{id}/approve          (approve agent)
- POST /definitions/agents/{id}/reject           (reject agent)
- POST /definitions/skills/create                (create skill)
- POST /definitions/skills/{id}/update           (update skill)
- POST /definitions/skills/{id}/delete           (delete skill)
- POST /definitions/skills/{id}/approve          (approve skill)
- POST /definitions/skills/{id}/reject           (reject skill)
- POST /definitions/mcp-servers/create           (create MCP server)
- POST /definitions/mcp-servers/{id}/update      (update MCP server)
- POST /definitions/mcp-servers/{id}/delete      (delete MCP server)
- POST /definitions/mcp-servers/{id}/approve     (approve MCP server)
- POST /definitions/mcp-servers/{id}/reject      (reject MCP server)
- POST /definitions/agents/{id}/grant-skill      (grant skill to agent)
- POST /definitions/agents/{id}/revoke-skill/{s} (revoke skill)
- POST /definitions/agents/{id}/grant-mcp        (grant MCP to agent)
- POST /definitions/agents/{id}/revoke-mcp/{s}   (revoke MCP)
- POST /definitions/agents/{id}/mcp-tools/{s}    (update tool grants)

Uses real DB sessions + CSRF tokens through the full ASGI stack.
"""

from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    create_session,
)
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.definitions import DefinitionRepository

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for web definition tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_webdef_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        # Clean grants first (FK constraints)
        await conn.execute(
            "DELETE FROM agent_skills WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM agent_mcp_servers WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM agent_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM skill_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM mcp_server_configs WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
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
async def web_user(db_pool, web_prefix):
    """Create user + org for web tests and return (user, org, session_token)."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{web_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{web_prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{web_prefix}user@test.com",
        display_name=f"{web_prefix}User",
        role="admin",
    )
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def client(db_pool, web_user):
    """httpx client with session + CSRF cookies pre-set."""
    _user, _org, session_token = web_user
    csrf_token = "test-csrf-token-def123"

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
async def agent_def(db_pool, web_user):
    """Create a test agent definition."""
    _user, org, _token = web_user
    repo = DefinitionRepository(db_pool)
    return await repo.create_agent(
        name="Test Agent",
        description="A test agent",
        content="# Test Agent\nDoes testing.",
        org_id=str(org["id"]),
        created_by=str(_user["id"]),
    )


@pytest_asyncio.fixture
async def skill_def(db_pool, web_user):
    """Create a test skill definition."""
    _user, org, _token = web_user
    repo = DefinitionRepository(db_pool)
    return await repo.create_skill(
        name="Test Skill",
        description="A test skill",
        content="# Test Skill\nDoes testing.",
        org_id=str(org["id"]),
        created_by=str(_user["id"]),
    )


@pytest_asyncio.fixture
async def mcp_def(db_pool, web_user):
    """Create a test MCP server definition."""
    _user, org, _token = web_user
    repo = DefinitionRepository(db_pool)
    return await repo.create_mcp_server(
        name="Test MCP",
        description="A test MCP server",
        server_type="http",
        url="http://localhost:9999",
        org_id=str(org["id"]),
        created_by=str(_user["id"]),
    )


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    """Build form data dict with CSRF token included."""
    data = {CSRF_FIELD_NAME: client._csrf_token}  # type: ignore[attr-defined]
    if extra:
        data.update(extra)
    return data


# ============================================================================
# GET /definitions — list
# ============================================================================


class TestDefinitionsList:
    async def test_list_returns_html(self, client, agent_def):
        resp = await client.get("/definitions")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_list_contains_agent_name(self, client, agent_def):
        resp = await client.get("/definitions")
        assert "Test Agent" in resp.text

    async def test_list_tab_agents(self, client, agent_def):
        resp = await client.get("/definitions", params={"tab": "agents"})
        assert resp.status_code == 200

    async def test_list_tab_skills(self, client, skill_def):
        resp = await client.get("/definitions", params={"tab": "skills"})
        assert resp.status_code == 200

    async def test_list_tab_mcp(self, client, mcp_def):
        resp = await client.get("/definitions", params={"tab": "mcp"})
        assert resp.status_code == 200

    async def test_list_unauthenticated_redirects(self, db_pool):
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/definitions", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")


# ============================================================================
# GET /definitions/agents/{id} — agent detail
# ============================================================================


class TestAgentDetail:
    async def test_detail_returns_html(self, client, agent_def):
        resp = await client.get(f"/definitions/agents/{agent_def['id']}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_detail_contains_name(self, client, agent_def):
        resp = await client.get(f"/definitions/agents/{agent_def['id']}")
        assert "Test Agent" in resp.text

    async def test_detail_not_found(self, client):
        resp = await client.get(f"/definitions/agents/{uuid4()}")
        assert resp.status_code == 404


# ============================================================================
# GET /definitions/skills/{id} — skill detail
# ============================================================================


class TestSkillDetail:
    async def test_detail_returns_html(self, client, skill_def):
        resp = await client.get(f"/definitions/skills/{skill_def['id']}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_detail_contains_name(self, client, skill_def):
        resp = await client.get(f"/definitions/skills/{skill_def['id']}")
        assert "Test Skill" in resp.text

    async def test_detail_not_found(self, client):
        resp = await client.get(f"/definitions/skills/{uuid4()}")
        assert resp.status_code == 404


# ============================================================================
# GET /definitions/mcp-servers/{id} — MCP server detail
# ============================================================================


class TestMcpServerDetail:
    async def test_detail_returns_html(self, client, mcp_def):
        resp = await client.get(f"/definitions/mcp-servers/{mcp_def['id']}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_detail_contains_name(self, client, mcp_def):
        resp = await client.get(f"/definitions/mcp-servers/{mcp_def['id']}")
        assert "Test MCP" in resp.text

    async def test_detail_not_found(self, client):
        resp = await client.get(f"/definitions/mcp-servers/{uuid4()}")
        assert resp.status_code == 404


# ============================================================================
# POST /definitions/agents/create
# ============================================================================


class TestAgentCreate:
    async def test_create_redirects(self, client):
        resp = await client.post(
            "/definitions/agents/create",
            data=_csrf_data(
                client,
                {
                    "name": "Created Agent",
                    "description": "Created via test",
                    "content": "# Created\nContent here.",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/definitions" in resp.headers["location"]

    async def test_create_persists(self, client, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            "/definitions/agents/create",
            data=_csrf_data(
                client,
                {
                    "name": "Persisted Agent",
                    "description": "Check persistence",
                    "content": "# Persisted",
                },
            ),
        )
        repo = DefinitionRepository(db_pool)
        agents = await repo.list_agents(str(org["id"]))
        names = [a["name"] for a in agents]
        assert "Persisted Agent" in names

    async def test_create_no_csrf_fails(self, client):
        resp = await client.post(
            "/definitions/agents/create",
            data={"name": "No CSRF", "description": "x", "content": "x"},
        )
        assert resp.status_code in (403, 400)


# ============================================================================
# POST /definitions/agents/{id}/update
# ============================================================================


class TestAgentUpdate:
    async def test_update_redirects(self, client, agent_def):
        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/update",
            data=_csrf_data(
                client,
                {
                    "name": "Updated Agent",
                    "description": "Updated desc",
                    "content": "# Updated",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert f"/definitions/agents/{agent_def['id']}" in resp.headers["location"]

    async def test_update_persists(self, client, agent_def, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/definitions/agents/{agent_def['id']}/update",
            data=_csrf_data(
                client,
                {
                    "name": "Renamed Agent",
                    "description": "New desc",
                    "content": "# New content",
                },
            ),
        )
        repo = DefinitionRepository(db_pool)
        agent = await repo.get_agent(str(agent_def["id"]), str(org["id"]))
        assert agent["name"] == "Renamed Agent"


# ============================================================================
# POST /definitions/agents/{id}/delete
# ============================================================================


class TestAgentDelete:
    async def test_delete_redirects(self, client, agent_def):
        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/definitions" in resp.headers["location"]

    async def test_delete_removes(self, client, agent_def, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/definitions/agents/{agent_def['id']}/delete",
            data=_csrf_data(client),
        )
        repo = DefinitionRepository(db_pool)
        agent = await repo.get_agent(str(agent_def["id"]), str(org["id"]))
        assert agent is None


# ============================================================================
# POST /definitions/agents/{id}/approve & reject
# ============================================================================


class TestAgentApproveReject:
    async def test_approve_redirects(self, client, agent_def):
        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/approve",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/definitions" in resp.headers["location"]

    async def test_approve_changes_status(self, client, agent_def, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/definitions/agents/{agent_def['id']}/approve",
            data=_csrf_data(client),
        )
        repo = DefinitionRepository(db_pool)
        agent = await repo.get_agent(str(agent_def["id"]), str(org["id"]))
        assert agent["status"] == "active"

    async def test_reject_redirects(self, client, agent_def):
        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/reject",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_reject_changes_status(self, client, agent_def, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/definitions/agents/{agent_def['id']}/reject",
            data=_csrf_data(client),
        )
        repo = DefinitionRepository(db_pool)
        agent = await repo.get_agent(str(agent_def["id"]), str(org["id"]))
        assert agent["status"] == "rejected"


# ============================================================================
# Skill CRUD
# ============================================================================


class TestSkillCreate:
    async def test_create_redirects(self, client):
        resp = await client.post(
            "/definitions/skills/create",
            data=_csrf_data(
                client,
                {
                    "name": "Created Skill",
                    "description": "Created via test",
                    "content": "# Created Skill",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/definitions" in resp.headers["location"]

    async def test_create_no_csrf_fails(self, client):
        resp = await client.post(
            "/definitions/skills/create",
            data={"name": "No CSRF", "description": "x", "content": "x"},
        )
        assert resp.status_code in (403, 400)


class TestSkillUpdate:
    async def test_update_redirects(self, client, skill_def):
        resp = await client.post(
            f"/definitions/skills/{skill_def['id']}/update",
            data=_csrf_data(
                client,
                {
                    "name": "Updated Skill",
                    "description": "Updated",
                    "content": "# Updated",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_update_persists(self, client, skill_def, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/definitions/skills/{skill_def['id']}/update",
            data=_csrf_data(
                client,
                {
                    "name": "Renamed Skill",
                    "description": "New",
                    "content": "# New",
                },
            ),
        )
        repo = DefinitionRepository(db_pool)
        skill = await repo.get_skill(str(skill_def["id"]), str(org["id"]))
        assert skill["name"] == "Renamed Skill"


class TestSkillDelete:
    async def test_delete_redirects(self, client, skill_def):
        resp = await client.post(
            f"/definitions/skills/{skill_def['id']}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_delete_removes(self, client, skill_def, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/definitions/skills/{skill_def['id']}/delete",
            data=_csrf_data(client),
        )
        repo = DefinitionRepository(db_pool)
        skill = await repo.get_skill(str(skill_def["id"]), str(org["id"]))
        assert skill is None


class TestSkillApproveReject:
    async def test_approve_changes_status(self, client, skill_def, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/definitions/skills/{skill_def['id']}/approve",
            data=_csrf_data(client),
        )
        repo = DefinitionRepository(db_pool)
        skill = await repo.get_skill(str(skill_def["id"]), str(org["id"]))
        assert skill["status"] == "active"

    async def test_reject_changes_status(self, client, skill_def, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/definitions/skills/{skill_def['id']}/reject",
            data=_csrf_data(client),
        )
        repo = DefinitionRepository(db_pool)
        skill = await repo.get_skill(str(skill_def["id"]), str(org["id"]))
        assert skill["status"] == "rejected"


# ============================================================================
# MCP Server CRUD
# ============================================================================


class TestMcpCreate:
    async def test_create_redirects(self, client):
        resp = await client.post(
            "/definitions/mcp-servers/create",
            data=_csrf_data(
                client,
                {
                    "name": "Created MCP",
                    "description": "Created via test",
                    "server_type": "http",
                    "url": "http://localhost:8080",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/definitions" in resp.headers["location"]

    async def test_create_with_headers(self, client, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            "/definitions/mcp-servers/create",
            data=_csrf_data(
                client,
                {
                    "name": "MCP With Headers",
                    "description": "Has headers",
                    "server_type": "http",
                    "url": "http://localhost:8080",
                    "headers": '{"Authorization": "Bearer test"}',
                },
            ),
        )
        repo = DefinitionRepository(db_pool)
        servers = await repo.list_mcp_servers(str(org["id"]))
        found = [s for s in servers if s["name"] == "MCP With Headers"]
        assert len(found) == 1

    async def test_create_with_invalid_headers_json(self, client):
        """Invalid JSON in headers should not crash — falls back to empty dict."""
        resp = await client.post(
            "/definitions/mcp-servers/create",
            data=_csrf_data(
                client,
                {
                    "name": "Bad Headers MCP",
                    "description": "Invalid JSON",
                    "server_type": "http",
                    "url": "http://localhost:8080",
                    "headers": "not-json{{{",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_create_no_csrf_fails(self, client):
        resp = await client.post(
            "/definitions/mcp-servers/create",
            data={"name": "No CSRF", "description": "x", "server_type": "http"},
        )
        assert resp.status_code in (403, 400)


class TestMcpUpdate:
    async def test_update_redirects(self, client, mcp_def):
        resp = await client.post(
            f"/definitions/mcp-servers/{mcp_def['id']}/update",
            data=_csrf_data(
                client,
                {
                    "name": "Updated MCP",
                    "description": "Updated",
                    "server_type": "http",
                    "url": "http://localhost:9090",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_update_persists(self, client, mcp_def, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/definitions/mcp-servers/{mcp_def['id']}/update",
            data=_csrf_data(
                client,
                {
                    "name": "Renamed MCP",
                    "description": "New",
                    "server_type": "http",
                    "url": "http://localhost:7777",
                },
            ),
        )
        repo = DefinitionRepository(db_pool)
        server = await repo.get_mcp_server(str(mcp_def["id"]), str(org["id"]))
        assert server["name"] == "Renamed MCP"


class TestMcpDelete:
    async def test_delete_redirects(self, client, mcp_def):
        resp = await client.post(
            f"/definitions/mcp-servers/{mcp_def['id']}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_delete_removes(self, client, mcp_def, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/definitions/mcp-servers/{mcp_def['id']}/delete",
            data=_csrf_data(client),
        )
        repo = DefinitionRepository(db_pool)
        server = await repo.get_mcp_server(str(mcp_def["id"]), str(org["id"]))
        assert server is None


class TestMcpApproveReject:
    async def test_approve_changes_status(self, client, mcp_def, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/definitions/mcp-servers/{mcp_def['id']}/approve",
            data=_csrf_data(client),
        )
        repo = DefinitionRepository(db_pool)
        server = await repo.get_mcp_server(str(mcp_def["id"]), str(org["id"]))
        assert server["status"] == "active"

    async def test_reject_changes_status(self, client, mcp_def, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/definitions/mcp-servers/{mcp_def['id']}/reject",
            data=_csrf_data(client),
        )
        repo = DefinitionRepository(db_pool)
        server = await repo.get_mcp_server(str(mcp_def["id"]), str(org["id"]))
        assert server["status"] == "rejected"


# ============================================================================
# Grant management (skill/MCP ↔ agent)
# ============================================================================


class TestGrantManagement:
    async def test_grant_skill_to_agent(self, client, agent_def, skill_def, db_pool, web_user):
        # Approve the skill first — get_agent_skills filters by status='active'
        _user, org, _token = web_user
        repo = DefinitionRepository(db_pool)
        await repo.approve_skill(str(skill_def["id"]), str(org["id"]), str(_user["id"]))

        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/grant-skill",
            data=_csrf_data(client, {"skill_id": str(skill_def["id"])}),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        skills = await repo.get_agent_skills(str(agent_def["id"]))
        skill_ids = [str(s["id"]) for s in skills]
        assert str(skill_def["id"]) in skill_ids

    async def test_grant_skill_empty_id_skips(self, client, agent_def):
        """Empty skill_id should still redirect without error."""
        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/grant-skill",
            data=_csrf_data(client, {"skill_id": ""}),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_revoke_skill_from_agent(self, client, agent_def, skill_def, db_pool):
        repo = DefinitionRepository(db_pool)
        await repo.grant_skill(str(agent_def["id"]), str(skill_def["id"]))
        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/revoke-skill/{skill_def['id']}",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        skills = await repo.get_agent_skills(str(agent_def["id"]))
        skill_ids = [str(s["id"]) for s in skills]
        assert str(skill_def["id"]) not in skill_ids

    async def test_grant_mcp_to_agent(self, client, agent_def, mcp_def, db_pool, web_user):
        # Approve the MCP server first — get_agent_mcp_servers filters by status='active'
        _user, org, _token = web_user
        repo = DefinitionRepository(db_pool)
        await repo.approve_mcp_server(str(mcp_def["id"]), str(org["id"]), str(_user["id"]))

        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/grant-mcp",
            data=_csrf_data(client, {"mcp_server_id": str(mcp_def["id"])}),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        servers = await repo.get_agent_mcp_servers(str(agent_def["id"]))
        server_ids = [str(s["id"]) for s in servers]
        assert str(mcp_def["id"]) in server_ids

    async def test_grant_mcp_empty_id_skips(self, client, agent_def):
        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/grant-mcp",
            data=_csrf_data(client, {"mcp_server_id": ""}),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_revoke_mcp_from_agent(self, client, agent_def, mcp_def, db_pool):
        repo = DefinitionRepository(db_pool)
        await repo.grant_mcp_server(str(agent_def["id"]), str(mcp_def["id"]))
        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/revoke-mcp/{mcp_def['id']}",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        servers = await repo.get_agent_mcp_servers(str(agent_def["id"]))
        server_ids = [str(s["id"]) for s in servers]
        assert str(mcp_def["id"]) not in server_ids

    async def test_update_mcp_tools(self, client, agent_def, mcp_def, db_pool):
        repo = DefinitionRepository(db_pool)
        await repo.grant_mcp_server(str(agent_def["id"]), str(mcp_def["id"]))
        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/mcp-tools/{mcp_def['id']}",
            data=_csrf_data(client, {"allowed_tools": "tool1, tool2, tool3"}),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_update_mcp_tools_empty_clears(self, client, agent_def, mcp_def, db_pool):
        repo = DefinitionRepository(db_pool)
        await repo.grant_mcp_server(str(agent_def["id"]), str(mcp_def["id"]))
        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/mcp-tools/{mcp_def['id']}",
            data=_csrf_data(client, {"allowed_tools": ""}),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_grant_no_csrf_fails(self, client, agent_def, skill_def):
        resp = await client.post(
            f"/definitions/agents/{agent_def['id']}/grant-skill",
            data={"skill_id": str(skill_def["id"])},
        )
        assert resp.status_code in (403, 400)


# ============================================================================
# Member-role users get 403 on all mutating endpoints
# ============================================================================


@pytest_asyncio.fixture
async def member_user(db_pool, web_prefix):
    """Create a member-role user in the same org as web_user."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{web_prefix}member_org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{web_prefix}member",
        provider="local",
        organization_id=org["id"],
        email=f"{web_prefix}member@test.com",
        display_name=f"{web_prefix}Member",
        role="member",
    )
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def member_client(db_pool, member_user):
    """httpx client authenticated as a member-role user."""
    _user, _org, session_token = member_user
    csrf_token = "test-csrf-token-member456"

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
async def member_agent_def(db_pool, member_user):
    """Agent definition in the member user's org."""
    _user, org, _token = member_user
    repo = DefinitionRepository(db_pool)
    return await repo.create_agent(
        name="Member Test Agent",
        description="Agent for member tests",
        content="# Member Test Agent",
        org_id=str(org["id"]),
        created_by=str(_user["id"]),
    )


@pytest_asyncio.fixture
async def member_skill_def(db_pool, member_user):
    """Skill definition in the member user's org."""
    _user, org, _token = member_user
    repo = DefinitionRepository(db_pool)
    return await repo.create_skill(
        name="Member Test Skill",
        description="Skill for member tests",
        content="# Member Test Skill",
        org_id=str(org["id"]),
        created_by=str(_user["id"]),
    )


@pytest_asyncio.fixture
async def member_mcp_def(db_pool, member_user):
    """MCP server definition in the member user's org."""
    _user, org, _token = member_user
    repo = DefinitionRepository(db_pool)
    return await repo.create_mcp_server(
        name="Member Test MCP",
        description="MCP for member tests",
        server_type="http",
        url="http://localhost:9999",
        org_id=str(org["id"]),
        created_by=str(_user["id"]),
    )


class TestMemberRoleBlocked:
    """Member-role users must get 403 on all mutating definition endpoints."""

    # --- Agent CRUD ---

    async def test_member_cannot_create_agent(self, member_client):
        resp = await member_client.post(
            "/definitions/agents/create",
            data=_csrf_data(member_client, {"name": "X", "description": "X", "content": "X"}),
        )
        assert resp.status_code == 403

    async def test_member_cannot_update_agent(self, member_client, member_agent_def):
        resp = await member_client.post(
            f"/definitions/agents/{member_agent_def['id']}/update",
            data=_csrf_data(
                member_client, {"name": "X", "description": "X", "content": "X"}
            ),
        )
        assert resp.status_code == 403

    async def test_member_cannot_delete_agent(self, member_client, member_agent_def):
        resp = await member_client.post(
            f"/definitions/agents/{member_agent_def['id']}/delete",
            data=_csrf_data(member_client),
        )
        assert resp.status_code == 403

    async def test_member_cannot_approve_agent(self, member_client, member_agent_def):
        resp = await member_client.post(
            f"/definitions/agents/{member_agent_def['id']}/approve",
            data=_csrf_data(member_client),
        )
        assert resp.status_code == 403

    async def test_member_cannot_reject_agent(self, member_client, member_agent_def):
        resp = await member_client.post(
            f"/definitions/agents/{member_agent_def['id']}/reject",
            data=_csrf_data(member_client),
        )
        assert resp.status_code == 403

    # --- Skill CRUD ---

    async def test_member_cannot_create_skill(self, member_client):
        resp = await member_client.post(
            "/definitions/skills/create",
            data=_csrf_data(member_client, {"name": "X", "description": "X", "content": "X"}),
        )
        assert resp.status_code == 403

    async def test_member_cannot_update_skill(self, member_client, member_skill_def):
        resp = await member_client.post(
            f"/definitions/skills/{member_skill_def['id']}/update",
            data=_csrf_data(
                member_client, {"name": "X", "description": "X", "content": "X"}
            ),
        )
        assert resp.status_code == 403

    async def test_member_cannot_delete_skill(self, member_client, member_skill_def):
        resp = await member_client.post(
            f"/definitions/skills/{member_skill_def['id']}/delete",
            data=_csrf_data(member_client),
        )
        assert resp.status_code == 403

    async def test_member_cannot_approve_skill(self, member_client, member_skill_def):
        resp = await member_client.post(
            f"/definitions/skills/{member_skill_def['id']}/approve",
            data=_csrf_data(member_client),
        )
        assert resp.status_code == 403

    async def test_member_cannot_reject_skill(self, member_client, member_skill_def):
        resp = await member_client.post(
            f"/definitions/skills/{member_skill_def['id']}/reject",
            data=_csrf_data(member_client),
        )
        assert resp.status_code == 403

    # --- MCP Server CRUD ---

    async def test_member_cannot_create_mcp(self, member_client):
        resp = await member_client.post(
            "/definitions/mcp-servers/create",
            data=_csrf_data(
                member_client,
                {"name": "X", "description": "X", "command": "echo", "args": "hi"},
            ),
        )
        assert resp.status_code == 403

    async def test_member_cannot_update_mcp(self, member_client, member_mcp_def):
        resp = await member_client.post(
            f"/definitions/mcp-servers/{member_mcp_def['id']}/update",
            data=_csrf_data(
                member_client,
                {"name": "X", "description": "X", "command": "echo", "args": "hi"},
            ),
        )
        assert resp.status_code == 403

    async def test_member_cannot_delete_mcp(self, member_client, member_mcp_def):
        resp = await member_client.post(
            f"/definitions/mcp-servers/{member_mcp_def['id']}/delete",
            data=_csrf_data(member_client),
        )
        assert resp.status_code == 403

    async def test_member_cannot_approve_mcp(self, member_client, member_mcp_def):
        resp = await member_client.post(
            f"/definitions/mcp-servers/{member_mcp_def['id']}/approve",
            data=_csrf_data(member_client),
        )
        assert resp.status_code == 403

    async def test_member_cannot_reject_mcp(self, member_client, member_mcp_def):
        resp = await member_client.post(
            f"/definitions/mcp-servers/{member_mcp_def['id']}/reject",
            data=_csrf_data(member_client),
        )
        assert resp.status_code == 403

    # --- Grant management ---

    async def test_member_cannot_grant_skill(
        self, member_client, member_agent_def, member_skill_def
    ):
        resp = await member_client.post(
            f"/definitions/agents/{member_agent_def['id']}/grant-skill",
            data=_csrf_data(member_client, {"skill_id": str(member_skill_def["id"])}),
        )
        assert resp.status_code == 403

    async def test_member_cannot_revoke_skill(
        self, member_client, member_agent_def, member_skill_def
    ):
        resp = await member_client.post(
            f"/definitions/agents/{member_agent_def['id']}/revoke-skill/{member_skill_def['id']}",
            data=_csrf_data(member_client),
        )
        assert resp.status_code == 403

    async def test_member_cannot_grant_mcp(
        self, member_client, member_agent_def, member_mcp_def
    ):
        resp = await member_client.post(
            f"/definitions/agents/{member_agent_def['id']}/grant-mcp",
            data=_csrf_data(member_client, {"server_id": str(member_mcp_def["id"])}),
        )
        assert resp.status_code == 403

    async def test_member_cannot_revoke_mcp(
        self, member_client, member_agent_def, member_mcp_def
    ):
        resp = await member_client.post(
            f"/definitions/agents/{member_agent_def['id']}/revoke-mcp/{member_mcp_def['id']}",
            data=_csrf_data(member_client),
        )
        assert resp.status_code == 403

    async def test_member_cannot_update_mcp_tools(
        self, member_client, member_agent_def, member_mcp_def
    ):
        resp = await member_client.post(
            f"/definitions/agents/{member_agent_def['id']}/mcp-tools/{member_mcp_def['id']}",
            data=_csrf_data(member_client, {"allowed_tools": "tool1"}),
        )
        assert resp.status_code == 403

    # --- Read-only endpoints remain accessible to members ---

    async def test_member_can_list_definitions(self, member_client):
        resp = await member_client.get("/definitions")
        assert resp.status_code == 200

    async def test_member_can_view_agent_detail(self, member_client, member_agent_def):
        resp = await member_client.get(f"/definitions/agents/{member_agent_def['id']}")
        assert resp.status_code == 200

    async def test_member_can_view_skill_detail(self, member_client, member_skill_def):
        resp = await member_client.get(f"/definitions/skills/{member_skill_def['id']}")
        assert resp.status_code == 200

    async def test_member_can_view_mcp_detail(self, member_client, member_mcp_def):
        resp = await member_client.get(f"/definitions/mcp-servers/{member_mcp_def['id']}")
        assert resp.status_code == 200
