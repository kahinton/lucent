"""Tests for MCP tools in src/lucent/tools/definitions.py.

Covers: list_agent_definitions, get_agent_definition, list_skill_definitions,
get_skill_definition, list_proposals, create_agent_definition,
create_skill_definition, grant_skill_to_agent, update_agent_definition,
approve_agent_definition, reject_agent_definition, delete_agent_definition,
revoke_skill_from_agent, grant_mcp_server_to_agent, revoke_mcp_server_from_agent,
approve_skill_definition, reject_skill_definition, delete_skill_definition,
list_mcp_server_definitions, create_mcp_server_definition,
update_mcp_server_definition, approve_mcp_server_definition,
reject_mcp_server_definition.
Tests auth context enforcement, JSON serialization, and error handling.
"""

import json

import pytest
import pytest_asyncio
from mcp.server.fastmcp import FastMCP

from lucent.auth import set_current_user
from lucent.db.definitions import DefinitionRepository
from lucent.tools.definitions import register_definition_tools

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def mcp(db_pool):
    """Create a FastMCP instance with definition tools registered."""
    m = FastMCP("test")
    register_definition_tools(m)
    return m


@pytest_asyncio.fixture
async def auth_user(test_user):
    """Set auth context to the test user (admin role)."""
    set_current_user(
        {
            "id": test_user["id"],
            "organization_id": test_user["organization_id"],
            "role": "admin",
            "display_name": "Test User",
            "email": "test@test.com",
        }
    )
    yield test_user
    set_current_user(None)


@pytest_asyncio.fixture
async def repo(db_pool):
    return DefinitionRepository(db_pool)


@pytest_asyncio.fixture(autouse=True)
async def cleanup_definitions(db_pool, test_organization):
    """Clean up definition data after each test."""
    yield
    org_id = test_organization["id"]
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_skills WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE organization_id = $1)",
            org_id,
        )
        await conn.execute(
            "DELETE FROM agent_mcp_servers WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE organization_id = $1)",
            org_id,
        )
        await conn.execute("DELETE FROM agent_definitions WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM skill_definitions WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM mcp_server_configs WHERE organization_id = $1", org_id)


async def _call(mcp, tool_name: str, args: dict | None = None) -> dict | list:
    """Call an MCP tool and parse the JSON response."""
    result = await mcp._tool_manager.call_tool(tool_name, args or {})
    return json.loads(result)


# ============================================================================
# list_agent_definitions
# ============================================================================


class TestListAgentDefinitions:
    @pytest.mark.asyncio
    async def test_list_empty(self, mcp, auth_user):
        result = await _call(mcp, "list_agent_definitions")
        assert "items" in result
        assert result["total_count"] == 0

    @pytest.mark.asyncio
    async def test_list_with_agents(self, mcp, auth_user, repo):
        await repo.create_agent(
            name="test-agent",
            description="A test agent",
            content="# Test Agent",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(mcp, "list_agent_definitions")
        assert result["total_count"] >= 1
        names = [a["name"] for a in result["items"]]
        assert "test-agent" in names

    @pytest.mark.asyncio
    async def test_filter_by_status(self, mcp, auth_user, repo):
        await repo.create_agent(
            name="proposed-agent",
            description="",
            content="# Proposed",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
            status="proposed",
        )
        result = await _call(mcp, "list_agent_definitions", {"status": "active"})
        names = [a["name"] for a in result["items"]]
        assert "proposed-agent" not in names

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(mcp, "list_agent_definitions")
        assert "error" in result


# ============================================================================
# get_agent_definition
# ============================================================================


class TestGetAgentDefinition:
    @pytest.mark.asyncio
    async def test_get_existing(self, mcp, auth_user, repo):
        agent = await repo.create_agent(
            name="detail-agent",
            description="Detailed agent",
            content="# Detailed content",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(mcp, "get_agent_definition", {"agent_id": str(agent["id"])})
        assert result["name"] == "detail-agent"
        assert result["content"] == "# Detailed content"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, mcp, auth_user):
        result = await _call(
            mcp, "get_agent_definition", {"agent_id": "00000000-0000-0000-0000-000000000000"}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp, "get_agent_definition", {"agent_id": "00000000-0000-0000-0000-000000000000"}
        )
        assert "error" in result


# ============================================================================
# list_skill_definitions
# ============================================================================


class TestListSkillDefinitions:
    @pytest.mark.asyncio
    async def test_list_empty(self, mcp, auth_user):
        result = await _call(mcp, "list_skill_definitions")
        assert "items" in result
        assert result["total_count"] == 0

    @pytest.mark.asyncio
    async def test_list_with_skills(self, mcp, auth_user, repo):
        await repo.create_skill(
            name="test-skill",
            description="A test skill",
            content="# Test Skill",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(mcp, "list_skill_definitions")
        assert result["total_count"] >= 1
        names = [s["name"] for s in result["items"]]
        assert "test-skill" in names


# ============================================================================
# get_skill_definition
# ============================================================================


class TestGetSkillDefinition:
    @pytest.mark.asyncio
    async def test_get_existing(self, mcp, auth_user, repo):
        skill = await repo.create_skill(
            name="detail-skill",
            description="Detailed skill",
            content="# Skill content",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(mcp, "get_skill_definition", {"skill_id": str(skill["id"])})
        assert result["name"] == "detail-skill"
        assert result["content"] == "# Skill content"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, mcp, auth_user):
        result = await _call(
            mcp, "get_skill_definition", {"skill_id": "00000000-0000-0000-0000-000000000000"}
        )
        assert "error" in result


# ============================================================================
# list_proposals
# ============================================================================


class TestListProposals:
    @pytest.mark.asyncio
    async def test_list_empty(self, mcp, auth_user):
        result = await _call(mcp, "list_proposals")
        assert "agents" in result
        assert "skills" in result
        assert "mcp_servers" in result
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_list_with_proposals(self, mcp, auth_user, repo):
        await repo.create_agent(
            name="proposed-agent",
            description="",
            content="# Proposed",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
            status="proposed",
        )
        await repo.create_skill(
            name="proposed-skill",
            description="",
            content="# Proposed",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
            status="proposed",
        )
        result = await _call(mcp, "list_proposals")
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(mcp, "list_proposals")
        assert "error" in result


# ============================================================================
# create_agent_definition
# ============================================================================


class TestCreateAgentDefinition:
    @pytest.mark.asyncio
    async def test_create_basic(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_agent_definition",
            {"name": "new-agent", "description": "New agent", "content": "# Agent def"},
        )
        assert result["name"] == "new-agent"
        assert result["status"] == "proposed"

    @pytest.mark.asyncio
    async def test_missing_content_returns_error(self, mcp, auth_user):
        result = await _call(
            mcp, "create_agent_definition", {"name": "no-content", "description": ""}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_name_too_long(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_agent_definition",
            {"name": "x" * 65, "content": "# Content"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "create_agent_definition",
            {"name": "no-auth", "content": "# Content"},
        )
        assert "error" in result


# ============================================================================
# create_skill_definition
# ============================================================================


class TestCreateSkillDefinition:
    @pytest.mark.asyncio
    async def test_create_basic(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_skill_definition",
            {"name": "new-skill", "description": "New skill", "content": "# Skill def"},
        )
        assert result["name"] == "new-skill"
        assert result["status"] == "proposed"

    @pytest.mark.asyncio
    async def test_missing_content_returns_error(self, mcp, auth_user):
        result = await _call(
            mcp, "create_skill_definition", {"name": "no-content", "description": ""}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "create_skill_definition",
            {"name": "no-auth", "content": "# Content"},
        )
        assert "error" in result


# ============================================================================
# grant_skill_to_agent
# ============================================================================


class TestGrantSkillToAgent:
    @pytest.mark.asyncio
    async def test_grant_success(self, mcp, auth_user, repo):
        agent = await repo.create_agent(
            name="grantee-agent",
            description="",
            content="# Agent",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        skill = await repo.create_skill(
            name="granted-skill",
            description="",
            content="# Skill",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(
            mcp,
            "grant_skill_to_agent",
            {"agent_id": str(agent["id"]), "skill_id": str(skill["id"])},
        )
        assert result["status"] == "granted"

    @pytest.mark.asyncio
    async def test_grant_nonexistent_agent(self, mcp, auth_user, repo):
        skill = await repo.create_skill(
            name="orphan-skill",
            description="",
            content="# Skill",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(
            mcp,
            "grant_skill_to_agent",
            {
                "agent_id": "00000000-0000-0000-0000-000000000000",
                "skill_id": str(skill["id"]),
            },
        )
        assert "error" in result
        assert "Agent not found" in result["error"]

    @pytest.mark.asyncio
    async def test_grant_nonexistent_skill(self, mcp, auth_user, repo):
        agent = await repo.create_agent(
            name="lonely-agent",
            description="",
            content="# Agent",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(
            mcp,
            "grant_skill_to_agent",
            {
                "agent_id": str(agent["id"]),
                "skill_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert "error" in result
        assert "Skill not found" in result["error"]

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "grant_skill_to_agent",
            {
                "agent_id": "00000000-0000-0000-0000-000000000000",
                "skill_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert "error" in result


# ============================================================================
# update_agent_definition
# ============================================================================


class TestUpdateAgentDefinition:
    @pytest.mark.asyncio
    async def test_update_succeeds(self, mcp, auth_user, repo):
        agent = await repo.create_agent(
            name="update-agent",
            description="Original",
            content="# Original",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(
            mcp,
            "update_agent_definition",
            {"agent_id": str(agent["id"]), "description": "Updated", "content": "# Updated"},
        )
        assert result["description"] == "Updated"
        assert result["content"] == "# Updated"

    @pytest.mark.asyncio
    async def test_update_not_found(self, mcp, auth_user):
        result = await _call(
            mcp,
            "update_agent_definition",
            {"agent_id": "00000000-0000-0000-0000-000000000000", "content": "# x"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "update_agent_definition",
            {"agent_id": "00000000-0000-0000-0000-000000000000", "content": "# x"},
        )
        assert "error" in result


# ============================================================================
# approve_agent_definition
# ============================================================================


class TestApproveAgentDefinition:
    @pytest.mark.asyncio
    async def test_approve_succeeds(self, mcp, auth_user, repo):
        agent = await repo.create_agent(
            name="to-approve-agent",
            description="",
            content="# Agent",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
            status="proposed",
        )
        result = await _call(mcp, "approve_agent_definition", {"agent_id": str(agent["id"])})
        assert result["status"] == "active"

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, mcp, auth_user):
        set_current_user(
            {
                "id": auth_user["id"],
                "organization_id": auth_user["organization_id"],
                "role": "member",
                "display_name": "Member",
                "email": "member@test.com",
            }
        )
        result = await _call(
            mcp,
            "approve_agent_definition",
            {"agent_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result
        assert "Admin" in result["error"] or "admin" in result["error"]

    @pytest.mark.asyncio
    async def test_not_found(self, mcp, auth_user):
        result = await _call(
            mcp,
            "approve_agent_definition",
            {"agent_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "approve_agent_definition",
            {"agent_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result


# ============================================================================
# reject_agent_definition
# ============================================================================


class TestRejectAgentDefinition:
    @pytest.mark.asyncio
    async def test_reject_succeeds(self, mcp, auth_user, repo):
        agent = await repo.create_agent(
            name="to-reject-agent",
            description="",
            content="# Agent",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
            status="proposed",
        )
        result = await _call(mcp, "reject_agent_definition", {"agent_id": str(agent["id"])})
        assert result["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, mcp, auth_user):
        set_current_user(
            {
                "id": auth_user["id"],
                "organization_id": auth_user["organization_id"],
                "role": "member",
                "display_name": "Member",
                "email": "member@test.com",
            }
        )
        result = await _call(
            mcp,
            "reject_agent_definition",
            {"agent_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "reject_agent_definition",
            {"agent_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result


# ============================================================================
# delete_agent_definition
# ============================================================================


class TestDeleteAgentDefinition:
    @pytest.mark.asyncio
    async def test_delete_succeeds(self, mcp, auth_user, repo):
        agent = await repo.create_agent(
            name="to-delete-agent",
            description="",
            content="# Agent",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(mcp, "delete_agent_definition", {"agent_id": str(agent["id"])})
        assert result["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_delete_not_found(self, mcp, auth_user):
        result = await _call(
            mcp,
            "delete_agent_definition",
            {"agent_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "delete_agent_definition",
            {"agent_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result


# ============================================================================
# revoke_skill_from_agent
# ============================================================================


class TestRevokeSkillFromAgent:
    @pytest.mark.asyncio
    async def test_revoke_succeeds(self, mcp, auth_user, repo):
        agent = await repo.create_agent(
            name="revoke-skill-agent",
            description="",
            content="# Agent",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        skill = await repo.create_skill(
            name="revoke-me-skill",
            description="",
            content="# Skill",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        await repo.grant_skill(
            str(agent["id"]),
            str(skill["id"]),
            org_id=str(auth_user["organization_id"]),
            user_id=str(auth_user["id"]),
        )
        result = await _call(
            mcp,
            "revoke_skill_from_agent",
            {"agent_id": str(agent["id"]), "skill_id": str(skill["id"])},
        )
        assert result["status"] == "revoked"

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "revoke_skill_from_agent",
            {
                "agent_id": "00000000-0000-0000-0000-000000000000",
                "skill_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert "error" in result


# ============================================================================
# grant_mcp_server_to_agent
# ============================================================================


class TestGrantMcpServerToAgent:
    @pytest.mark.asyncio
    async def test_grant_succeeds(self, mcp, auth_user, repo):
        agent = await repo.create_agent(
            name="mcp-grantee-agent",
            description="",
            content="# Agent",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        server = await repo.create_mcp_server(
            name="test-mcp-server",
            description="",
            server_type="http",
            url="http://localhost:8080",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(
            mcp,
            "grant_mcp_server_to_agent",
            {"agent_id": str(agent["id"]), "definition_id": str(server["id"])},
        )
        assert result["status"] == "granted"

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "grant_mcp_server_to_agent",
            {
                "agent_id": "00000000-0000-0000-0000-000000000000",
                "definition_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert "error" in result


# ============================================================================
# revoke_mcp_server_from_agent
# ============================================================================


class TestRevokeMcpServerFromAgent:
    @pytest.mark.asyncio
    async def test_revoke_succeeds(self, mcp, auth_user, repo):
        agent = await repo.create_agent(
            name="mcp-revoke-agent",
            description="",
            content="# Agent",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        server = await repo.create_mcp_server(
            name="revoke-mcp-server",
            description="",
            server_type="http",
            url="http://localhost:9090",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        await repo.grant_mcp_server(
            str(agent["id"]),
            str(server["id"]),
            org_id=str(auth_user["organization_id"]),
            user_id=str(auth_user["id"]),
        )
        result = await _call(
            mcp,
            "revoke_mcp_server_from_agent",
            {"agent_id": str(agent["id"]), "server_id": str(server["id"])},
        )
        assert result["status"] == "revoked"

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "revoke_mcp_server_from_agent",
            {
                "agent_id": "00000000-0000-0000-0000-000000000000",
                "server_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert "error" in result


# ============================================================================
# approve_skill_definition
# ============================================================================


class TestApproveSkillDefinition:
    @pytest.mark.asyncio
    async def test_approve_succeeds(self, mcp, auth_user, repo):
        skill = await repo.create_skill(
            name="to-approve-skill",
            description="",
            content="# Skill",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
            status="proposed",
        )
        result = await _call(mcp, "approve_skill_definition", {"skill_id": str(skill["id"])})
        assert result["status"] == "active"

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, mcp, auth_user):
        set_current_user(
            {
                "id": auth_user["id"],
                "organization_id": auth_user["organization_id"],
                "role": "member",
                "display_name": "Member",
                "email": "member@test.com",
            }
        )
        result = await _call(
            mcp,
            "approve_skill_definition",
            {"skill_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "approve_skill_definition",
            {"skill_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result


# ============================================================================
# reject_skill_definition
# ============================================================================


class TestRejectSkillDefinition:
    @pytest.mark.asyncio
    async def test_reject_succeeds(self, mcp, auth_user, repo):
        skill = await repo.create_skill(
            name="to-reject-skill",
            description="",
            content="# Skill",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
            status="proposed",
        )
        result = await _call(mcp, "reject_skill_definition", {"skill_id": str(skill["id"])})
        assert result["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, mcp, auth_user):
        set_current_user(
            {
                "id": auth_user["id"],
                "organization_id": auth_user["organization_id"],
                "role": "member",
                "display_name": "Member",
                "email": "member@test.com",
            }
        )
        result = await _call(
            mcp,
            "reject_skill_definition",
            {"skill_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "reject_skill_definition",
            {"skill_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result


# ============================================================================
# delete_skill_definition
# ============================================================================


class TestDeleteSkillDefinition:
    @pytest.mark.asyncio
    async def test_delete_succeeds(self, mcp, auth_user, repo):
        skill = await repo.create_skill(
            name="to-delete-skill",
            description="",
            content="# Skill",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(mcp, "delete_skill_definition", {"skill_id": str(skill["id"])})
        assert result["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "delete_skill_definition",
            {"skill_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result


# ============================================================================
# list_mcp_server_definitions
# ============================================================================


class TestListMcpServerDefinitions:
    @pytest.mark.asyncio
    async def test_list_returns_items(self, mcp, auth_user, repo):
        await repo.create_mcp_server(
            name="list-test-server",
            description="",
            server_type="http",
            url="http://example.com",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(mcp, "list_mcp_server_definitions")
        assert "items" in result
        names = [s["name"] for s in result["items"]]
        assert "list-test-server" in names

    @pytest.mark.asyncio
    async def test_status_filter(self, mcp, auth_user, repo):
        await repo.create_mcp_server(
            name="proposed-mcp-server",
            description="",
            server_type="http",
            url="http://example.com",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
            status="proposed",
        )
        result = await _call(mcp, "list_mcp_server_definitions", {"status": "active"})
        names = [s["name"] for s in result["items"]]
        assert "proposed-mcp-server" not in names

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(mcp, "list_mcp_server_definitions")
        assert "error" in result


# ============================================================================
# create_mcp_server_definition
# ============================================================================


class TestCreateMcpServerDefinition:
    @pytest.mark.asyncio
    async def test_create_succeeds(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_mcp_server_definition",
            {"name": "new-mcp-server", "description": "New server", "url": "http://new.example.com"},
        )
        assert result["name"] == "new-mcp-server"
        assert result["status"] == "proposed"

    @pytest.mark.asyncio
    async def test_name_too_long_returns_error(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_mcp_server_definition",
            {"name": "x" * 65, "url": "http://example.com"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_invalid_args_json_returns_error(self, mcp, auth_user):
        result = await _call(
            mcp,
            "create_mcp_server_definition",
            {"name": "bad-args-server", "server_type": "stdio", "command": "mybin", "args": "not-json"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "create_mcp_server_definition",
            {"name": "no-auth-server", "url": "http://example.com"},
        )
        assert "error" in result


# ============================================================================
# update_mcp_server_definition
# ============================================================================


class TestUpdateMcpServerDefinition:
    @pytest.mark.asyncio
    async def test_update_succeeds(self, mcp, auth_user, repo):
        server = await repo.create_mcp_server(
            name="update-mcp-server",
            description="Original",
            server_type="http",
            url="http://original.example.com",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
        )
        result = await _call(
            mcp,
            "update_mcp_server_definition",
            {"server_id": str(server["id"]), "description": "Updated", "url": "http://updated.example.com"},
        )
        assert result["description"] == "Updated"
        assert result["url"] == "http://updated.example.com"

    @pytest.mark.asyncio
    async def test_update_not_found(self, mcp, auth_user):
        result = await _call(
            mcp,
            "update_mcp_server_definition",
            {"server_id": "00000000-0000-0000-0000-000000000000", "description": "x"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "update_mcp_server_definition",
            {"server_id": "00000000-0000-0000-0000-000000000000", "description": "x"},
        )
        assert "error" in result


# ============================================================================
# approve_mcp_server_definition
# ============================================================================


class TestApproveMcpServerDefinition:
    @pytest.mark.asyncio
    async def test_approve_succeeds(self, mcp, auth_user, repo):
        server = await repo.create_mcp_server(
            name="to-approve-mcp-server",
            description="",
            server_type="http",
            url="http://approve.example.com",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
            status="proposed",
        )
        result = await _call(
            mcp, "approve_mcp_server_definition", {"server_id": str(server["id"])}
        )
        assert result["status"] == "active"

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, mcp, auth_user):
        set_current_user(
            {
                "id": auth_user["id"],
                "organization_id": auth_user["organization_id"],
                "role": "member",
                "display_name": "Member",
                "email": "member@test.com",
            }
        )
        result = await _call(
            mcp,
            "approve_mcp_server_definition",
            {"server_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "approve_mcp_server_definition",
            {"server_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result


# ============================================================================
# reject_mcp_server_definition
# ============================================================================


class TestRejectMcpServerDefinition:
    @pytest.mark.asyncio
    async def test_reject_succeeds(self, mcp, auth_user, repo):
        server = await repo.create_mcp_server(
            name="to-reject-mcp-server",
            description="",
            server_type="http",
            url="http://reject.example.com",
            org_id=str(auth_user["organization_id"]),
            created_by=str(auth_user["id"]),
            owner_user_id=str(auth_user["id"]),
            status="proposed",
        )
        result = await _call(
            mcp, "reject_mcp_server_definition", {"server_id": str(server["id"])}
        )
        assert result["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, mcp, auth_user):
        set_current_user(
            {
                "id": auth_user["id"],
                "organization_id": auth_user["organization_id"],
                "role": "member",
                "display_name": "Member",
                "email": "member@test.com",
            }
        )
        result = await _call(
            mcp,
            "reject_mcp_server_definition",
            {"server_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp):
        set_current_user(None)
        result = await _call(
            mcp,
            "reject_mcp_server_definition",
            {"server_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert "error" in result
