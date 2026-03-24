"""Tests for MCP tools in src/lucent/tools/definitions.py.

Covers: list_agent_definitions, get_agent_definition, list_skill_definitions,
get_skill_definition, list_proposals, create_agent_definition,
create_skill_definition, grant_skill_to_agent.
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
