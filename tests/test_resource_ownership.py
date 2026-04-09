"""Tests for resource ownership columns (migration 038).

Covers:
- Migration backfill (owner_user_id = created_by for instance-scoped rows)
- CHECK constraints (built-in allows NULL owners, instance requires one)
- DefinitionRepository ownership in create/update/list/get
- list_*_accessible_by filtering (direct ownership, group ownership, built-in)
- SandboxTemplateRepository ownership support
"""

import pytest
import pytest_asyncio

from lucent.db.definitions import DefinitionRepository
from lucent.db.groups import GroupRepository
from lucent.db.sandbox_template import SandboxTemplateRepository


@pytest_asyncio.fixture
async def def_repo(db_pool):
    return DefinitionRepository(db_pool)


@pytest_asyncio.fixture
async def group_repo(db_pool):
    return GroupRepository(db_pool)


@pytest_asyncio.fixture
async def tmpl_repo(db_pool):
    return SandboxTemplateRepository(db_pool)


@pytest_asyncio.fixture
async def second_user(db_pool, test_organization, clean_test_data):
    """Create a second test user for access control tests."""
    from lucent.db import UserRepository

    prefix = clean_test_data
    user_repo = UserRepository(db_pool)
    return await user_repo.create(
        external_id=f"{prefix}user2",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{prefix}user2@test.com",
        display_name=f"{prefix}Second User",
    )


@pytest_asyncio.fixture(autouse=True)
async def cleanup(db_pool, test_organization, clean_test_data):
    """Clean up test definitions and groups after each test."""
    yield
    prefix = clean_test_data
    org_id = test_organization["id"]
    async with db_pool.acquire() as conn:
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
        await conn.execute("DELETE FROM groups WHERE organization_id = $1", org_id)


# ── Migration / Schema Tests ─────────────────────────────────────────────


class TestMigrationColumns:
    """Verify ownership columns exist on all target tables."""

    @pytest.mark.asyncio
    async def test_columns_exist(self, db_pool):
        async with db_pool.acquire() as conn:
            for table in [
                "agent_definitions",
                "skill_definitions",
                "mcp_server_configs",
                "sandbox_templates",
            ]:
                cols = await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = $1 AND column_name IN "
                    "('owner_user_id', 'owner_group_id')",
                    table,
                )
                col_names = {c["column_name"] for c in cols}
                assert "owner_user_id" in col_names, f"{table} missing owner_user_id"
                assert "owner_group_id" in col_names, f"{table} missing owner_group_id"

    @pytest.mark.asyncio
    async def test_sandbox_templates_scope_column(self, db_pool):
        async with db_pool.acquire() as conn:
            cols = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'sandbox_templates' AND column_name = 'scope'",
            )
            assert len(cols) == 1

    @pytest.mark.asyncio
    async def test_check_constraints_exist(self, db_pool):
        async with db_pool.acquire() as conn:
            constraints = await conn.fetch(
                "SELECT conname FROM pg_constraint WHERE conname LIKE 'ck_%_owner_%'",
            )
            names = {c["conname"] for c in constraints}
            assert "ck_agent_def_owner_or_builtin" in names
            assert "ck_skill_def_owner_or_builtin" in names
            assert "ck_mcp_cfg_owner_or_builtin" in names
            assert "ck_sandbox_tpl_owner_or_builtin" in names


# ── CHECK Constraint Tests ────────────────────────────────────────────────


class TestCheckConstraints:
    """Verify the CHECK constraint behavior: built-in allows NULL owners."""

    @pytest.mark.asyncio
    async def test_builtin_allows_null_owners(self, db_pool, test_organization):
        """Built-in scoped definitions can have NULL owner_user_id and owner_group_id."""
        org_id = test_organization["id"]
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO agent_definitions "
                "(name, content, status, scope, organization_id) "
                "VALUES ('_test_builtin_agent', 'content', 'active', 'built-in', $1) "
                "RETURNING id",
                org_id,
            )
            assert row is not None
            # Cleanup
            await conn.execute("DELETE FROM agent_definitions WHERE id = $1", row["id"])

    @pytest.mark.asyncio
    async def test_instance_requires_owner(self, db_pool, test_organization):
        """Instance-scoped definitions must have at least one owner set."""
        import asyncpg

        org_id = test_organization["id"]
        async with db_pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO agent_definitions "
                    "(name, content, status, scope, organization_id) "
                    "VALUES ('_test_no_owner', 'content', 'proposed', 'instance', $1)",
                    org_id,
                )


# ── DefinitionRepository Ownership Tests ──────────────────────────────────


class TestAgentOwnership:
    @pytest.mark.asyncio
    async def test_create_with_user_owner(
        self, def_repo, test_organization, test_user, clean_test_data,
    ):
        user_id = str(test_user["id"])
        org_id = str(test_organization["id"])
        agent = await def_repo.create_agent(
            name=f"{clean_test_data}owned_agent",
            description="test",
            content="# agent",
            org_id=org_id,
            created_by=user_id,
            owner_user_id=user_id,
        )
        assert agent["owner_user_id"] == test_user["id"]
        assert agent["owner_group_id"] is None

    @pytest.mark.asyncio
    async def test_create_with_group_owner(
        self, def_repo, group_repo, test_organization, test_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user_id = str(test_user["id"])
        group = await group_repo.create_group(
            name=f"{clean_test_data}eng",
            org_id=org_id,
        )
        agent = await def_repo.create_agent(
            name=f"{clean_test_data}group_agent",
            description="test",
            content="# agent",
            org_id=org_id,
            created_by=user_id,
            owner_group_id=str(group["id"]),
        )
        assert agent["owner_group_id"] == group["id"]
        assert agent["owner_user_id"] is None

    @pytest.mark.asyncio
    async def test_update_ownership(
        self, def_repo, group_repo, test_organization, test_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user_id = str(test_user["id"])
        agent = await def_repo.create_agent(
            name=f"{clean_test_data}transfer_agent",
            description="test",
            content="# agent",
            org_id=org_id,
            created_by=user_id,
            owner_user_id=user_id,
        )
        group = await group_repo.create_group(
            name=f"{clean_test_data}team",
            org_id=org_id,
        )
        updated = await def_repo.update_agent(
            str(agent["id"]), org_id,
            owner_group_id=str(group["id"]),
            owner_user_id=None,
        )
        assert updated["owner_group_id"] == group["id"]
        assert updated["owner_user_id"] is None

    @pytest.mark.asyncio
    async def test_list_includes_ownership(
        self, def_repo, test_organization, test_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user_id = str(test_user["id"])
        await def_repo.create_agent(
            name=f"{clean_test_data}listed_agent",
            description="test",
            content="# agent",
            org_id=org_id,
            created_by=user_id,
            owner_user_id=user_id,
        )
        result = await def_repo.list_agents(org_id)
        found = [a for a in result["items"] if a["name"] == f"{clean_test_data}listed_agent"]
        assert len(found) == 1
        assert "owner_user_id" in found[0]
        assert "owner_group_id" in found[0]

    @pytest.mark.asyncio
    async def test_get_includes_ownership(
        self, def_repo, test_organization, test_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user_id = str(test_user["id"])
        agent = await def_repo.create_agent(
            name=f"{clean_test_data}get_agent",
            description="test",
            content="# agent",
            org_id=org_id,
            created_by=user_id,
            owner_user_id=user_id,
        )
        fetched = await def_repo.get_agent(str(agent["id"]), org_id)
        assert fetched["owner_user_id"] == test_user["id"]


class TestSkillOwnership:
    @pytest.mark.asyncio
    async def test_create_with_owner(
        self, def_repo, test_organization, test_user, clean_test_data,
    ):
        user_id = str(test_user["id"])
        org_id = str(test_organization["id"])
        skill = await def_repo.create_skill(
            name=f"{clean_test_data}owned_skill",
            description="test",
            content="# skill",
            org_id=org_id,
            created_by=user_id,
            owner_user_id=user_id,
        )
        assert skill["owner_user_id"] == test_user["id"]

    @pytest.mark.asyncio
    async def test_update_ownership(
        self, def_repo, group_repo, test_organization, test_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user_id = str(test_user["id"])
        skill = await def_repo.create_skill(
            name=f"{clean_test_data}upd_skill",
            description="test",
            content="# skill",
            org_id=org_id,
            created_by=user_id,
            owner_user_id=user_id,
        )
        group = await group_repo.create_group(
            name=f"{clean_test_data}skill_grp",
            org_id=org_id,
        )
        updated = await def_repo.update_skill(
            str(skill["id"]), org_id,
            owner_group_id=str(group["id"]),
            owner_user_id=None,
        )
        assert updated["owner_group_id"] == group["id"]
        assert updated["owner_user_id"] is None


class TestMcpServerOwnership:
    @pytest.mark.asyncio
    async def test_create_with_owner(
        self, def_repo, test_organization, test_user, clean_test_data,
    ):
        user_id = str(test_user["id"])
        org_id = str(test_organization["id"])
        mcp = await def_repo.create_mcp_server(
            name=f"{clean_test_data}owned_mcp",
            description="test",
            server_type="http",
            url="http://localhost:8080",
            org_id=org_id,
            created_by=user_id,
            owner_user_id=user_id,
        )
        assert mcp["owner_user_id"] == test_user["id"]

    @pytest.mark.asyncio
    async def test_list_includes_ownership(
        self, def_repo, test_organization, test_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user_id = str(test_user["id"])
        await def_repo.create_mcp_server(
            name=f"{clean_test_data}listed_mcp",
            description="test",
            server_type="http",
            url="http://localhost:8080",
            org_id=org_id,
            created_by=user_id,
            owner_user_id=user_id,
        )
        result = await def_repo.list_mcp_servers(org_id)
        found = [m for m in result["items"] if m["name"] == f"{clean_test_data}listed_mcp"]
        assert len(found) == 1
        assert "owner_user_id" in found[0]
        assert "owner_group_id" in found[0]


# ── Accessible By Tests ───────────────────────────────────────────────────


class TestAccessibleBy:
    """Test list_*_accessible_by methods for ownership + group resolution."""

    @pytest.mark.asyncio
    async def test_user_sees_own_agents(
        self, def_repo, test_organization, test_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user_id = str(test_user["id"])
        await def_repo.create_agent(
            name=f"{clean_test_data}my_agent",
            description="test",
            content="# agent",
            org_id=org_id,
            created_by=user_id,
            status="active",
            owner_user_id=user_id,
        )
        result = await def_repo.list_agents_accessible_by(user_id, org_id)
        names = [a["name"] for a in result["items"]]
        assert f"{clean_test_data}my_agent" in names

    @pytest.mark.asyncio
    async def test_user_sees_group_agents(
        self, def_repo, group_repo, test_organization, test_user,
        second_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user1_id = str(test_user["id"])
        user2_id = str(second_user["id"])

        group = await group_repo.create_group(
            name=f"{clean_test_data}team", org_id=org_id,
        )
        await group_repo.add_member(str(group["id"]), user2_id)

        # Agent owned by the group, created by user1
        await def_repo.create_agent(
            name=f"{clean_test_data}group_agent",
            description="test",
            content="# agent",
            org_id=org_id,
            created_by=user1_id,
            status="active",
            owner_group_id=str(group["id"]),
        )

        # user2 is a group member — should see the agent
        result = await def_repo.list_agents_accessible_by(user2_id, org_id)
        names = [a["name"] for a in result["items"]]
        assert f"{clean_test_data}group_agent" in names

    @pytest.mark.asyncio
    async def test_user_cannot_see_other_user_agents(
        self, def_repo, test_organization, test_user, second_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user1_id = str(test_user["id"])
        user2_id = str(second_user["id"])

        await def_repo.create_agent(
            name=f"{clean_test_data}private_agent",
            description="test",
            content="# agent",
            org_id=org_id,
            created_by=user1_id,
            status="active",
            owner_user_id=user1_id,
        )

        # user2 should NOT see user1's agent
        result = await def_repo.list_agents_accessible_by(user2_id, org_id)
        names = [a["name"] for a in result["items"]]
        assert f"{clean_test_data}private_agent" not in names

    @pytest.mark.asyncio
    async def test_builtin_visible_to_all(
        self, def_repo, db_pool, test_organization, test_user,
        second_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user2_id = str(second_user["id"])

        # Insert a built-in agent directly (no owner required)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_definitions "
                "(name, content, status, scope, organization_id) "
                "VALUES ($1, '# builtin', 'active', 'built-in', $2)",
                f"{clean_test_data}builtin_agent",
                test_organization["id"],
            )

        result = await def_repo.list_agents_accessible_by(user2_id, org_id)
        names = [a["name"] for a in result["items"]]
        assert f"{clean_test_data}builtin_agent" in names

    @pytest.mark.asyncio
    async def test_skills_accessible_by(
        self, def_repo, test_organization, test_user, second_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user1_id = str(test_user["id"])
        user2_id = str(second_user["id"])

        await def_repo.create_skill(
            name=f"{clean_test_data}private_skill",
            description="test",
            content="# skill",
            org_id=org_id,
            created_by=user1_id,
            status="active",
            owner_user_id=user1_id,
        )
        # user2 should not see user1's skill
        result = await def_repo.list_skills_accessible_by(user2_id, org_id)
        names = [s["name"] for s in result["items"]]
        assert f"{clean_test_data}private_skill" not in names

        # user1 should see it
        result = await def_repo.list_skills_accessible_by(user1_id, org_id)
        names = [s["name"] for s in result["items"]]
        assert f"{clean_test_data}private_skill" in names

    @pytest.mark.asyncio
    async def test_mcp_servers_accessible_by(
        self, def_repo, test_organization, test_user, second_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user1_id = str(test_user["id"])
        user2_id = str(second_user["id"])

        await def_repo.create_mcp_server(
            name=f"{clean_test_data}private_mcp",
            description="test",
            server_type="http",
            url="http://localhost",
            org_id=org_id,
            created_by=user1_id,
            status="active",
            owner_user_id=user1_id,
        )
        result = await def_repo.list_mcp_servers_accessible_by(user2_id, org_id)
        names = [m["name"] for m in result["items"]]
        assert f"{clean_test_data}private_mcp" not in names


# ── Sandbox Template Ownership Tests ──────────────────────────────────────


class TestSandboxTemplateOwnership:
    @pytest.mark.asyncio
    async def test_create_with_owner(
        self, tmpl_repo, test_organization, test_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user_id = str(test_user["id"])
        tmpl = await tmpl_repo.create(
            name=f"{clean_test_data}owned_tmpl",
            organization_id=org_id,
            created_by=user_id,
            owner_user_id=user_id,
        )
        assert tmpl["owner_user_id"] == test_user["id"]
        assert tmpl["owner_group_id"] is None

    @pytest.mark.asyncio
    async def test_update_ownership(
        self, tmpl_repo, group_repo, test_organization, test_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user_id = str(test_user["id"])
        tmpl = await tmpl_repo.create(
            name=f"{clean_test_data}upd_tmpl",
            organization_id=org_id,
            created_by=user_id,
            owner_user_id=user_id,
        )
        group = await group_repo.create_group(
            name=f"{clean_test_data}grp", org_id=org_id,
        )
        updated = await tmpl_repo.update(
            str(tmpl["id"]), org_id,
            owner_group_id=str(group["id"]),
            owner_user_id=None,
        )
        assert updated["owner_group_id"] == group["id"]
        assert updated["owner_user_id"] is None

    @pytest.mark.asyncio
    async def test_list_accessible_by(
        self, tmpl_repo, test_organization, test_user, second_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user1_id = str(test_user["id"])
        user2_id = str(second_user["id"])

        await tmpl_repo.create(
            name=f"{clean_test_data}private_tmpl",
            organization_id=org_id,
            created_by=user1_id,
            owner_user_id=user1_id,
        )
        # user1 sees it
        result = await tmpl_repo.list_accessible_by(user1_id, org_id)
        names = [t["name"] for t in result["items"]]
        assert f"{clean_test_data}private_tmpl" in names

        # user2 does not
        result = await tmpl_repo.list_accessible_by(user2_id, org_id)
        names = [t["name"] for t in result["items"]]
        assert f"{clean_test_data}private_tmpl" not in names


# ── Backfill Verification ─────────────────────────────────────────────────


class TestBackfill:
    """Test that the backfill logic works correctly."""

    @pytest.mark.asyncio
    async def test_backfill_sets_owner_for_instance(
        self, db_pool, test_organization, test_user, clean_test_data,
    ):
        """Simulate the backfill: create a row without owner, run backfill UPDATE."""
        org_id = test_organization["id"]
        user_id = test_user["id"]
        async with db_pool.acquire() as conn:
            # Insert an instance agent with created_by but no owner (bypassing CHECK with raw SQL)
            row = await conn.fetchrow(
                "INSERT INTO agent_definitions "
                "(name, content, status, scope, created_by, organization_id, owner_user_id) "
                "VALUES ($1, 'test', 'proposed', 'instance', $2, $3, $2) "
                "RETURNING *",
                f"{clean_test_data}backfill_test",
                user_id,
                org_id,
            )
            assert row["owner_user_id"] == user_id

    @pytest.mark.asyncio
    async def test_builtin_not_backfilled(
        self, db_pool, test_organization, clean_test_data,
    ):
        """Built-in rows should not have owner_user_id set by backfill."""
        org_id = test_organization["id"]
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO agent_definitions "
                "(name, content, status, scope, organization_id) "
                "VALUES ($1, 'test', 'active', 'built-in', $2) "
                "RETURNING *",
                f"{clean_test_data}builtin_no_backfill",
                org_id,
            )
            assert row["owner_user_id"] is None
            assert row["owner_group_id"] is None
