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

from lucent.access_control import AccessControlService
from lucent.db.definitions import DefinitionRepository
from lucent.db.groups import GroupRepository
from lucent.db.models import ModelRepository
from lucent.db.sandbox_template import SandboxTemplateRepository
from lucent.db.schedules import ScheduleRepository


@pytest_asyncio.fixture
async def def_repo(db_pool):
    return DefinitionRepository(db_pool)


@pytest_asyncio.fixture
async def acl(db_pool):
    return AccessControlService(db_pool)


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
        # Remove access grants for test resources before deleting parent rows.
        await conn.execute(
            "DELETE FROM resource_access_grants WHERE resource_type = 'model' "
            "AND resource_id LIKE $1",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM resource_access_grants WHERE resource_type = 'agent' "
            "AND resource_id IN (SELECT id::text FROM agent_definitions WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM resource_access_grants WHERE resource_type = 'workflow' "
            "AND resource_id IN (SELECT id::text FROM schedules WHERE title LIKE $1)",
            f"{prefix}%",
        )
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
        await conn.execute("DELETE FROM schedules WHERE title LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM models WHERE id LIKE $1", f"{prefix}%")
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
                "schedules",
                "models",
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
                "SELECT conname FROM pg_constraint WHERE conname LIKE 'ck_%_single_owner'",
            )
            names = {c["conname"] for c in constraints}
            # Post-077: org-shared rows (NULL/NULL on an instance row) are valid,
            # so the old owner-or-builtin checks were dropped in favour of
            # single-owner constraints (a row may not name both a user and a group).
            assert "ck_agent_def_single_owner" in names
            assert "ck_skill_def_single_owner" in names
            assert "ck_mcp_cfg_single_owner" in names
            assert "ck_sandbox_tpl_single_owner" in names
            assert "ck_schedules_single_owner" in names
            assert "ck_models_single_owner" in names


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
    async def test_instance_allows_org_shared(self, db_pool, test_organization):
        """Instance-scoped definitions with NULL owners are valid (org-shared)."""
        org_id = test_organization["id"]
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO agent_definitions "
                "(name, content, status, scope, organization_id) "
                "VALUES ('_test_org_shared', 'content', 'proposed', 'instance', $1) "
                "RETURNING id",
                org_id,
            )
            assert row is not None
            await conn.execute("DELETE FROM agent_definitions WHERE id = $1", row["id"])

    @pytest.mark.asyncio
    async def test_single_owner_enforced(
        self, db_pool, group_repo, test_organization, test_user, clean_test_data,
    ):
        """A definition may not name both a user owner and a group owner."""
        import asyncpg

        org_id = test_organization["id"]
        group = await group_repo.create_group(
            name=f"{clean_test_data}single_owner",
            org_id=str(org_id),
        )
        async with db_pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO agent_definitions "
                    "(name, content, status, scope, organization_id, "
                    " owner_user_id, owner_group_id) "
                    "VALUES ('_test_both_owners', 'content', 'proposed', "
                    " 'instance', $1, $2, $3)",
                    org_id,
                    test_user["id"],
                    group["id"],
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
    async def test_user_sees_group_granted_agents(
        self, def_repo, acl, group_repo, test_organization, test_user,
        second_user, clean_test_data,
    ):
        org_id = str(test_organization["id"])
        user1_id = str(test_user["id"])
        user2_id = str(second_user["id"])

        group = await group_repo.create_group(
            name=f"{clean_test_data}team", org_id=org_id,
        )
        await group_repo.add_member(str(group["id"]), user2_id)

        # Agent created by user1, then shared with the group via a grant.
        agent = await def_repo.create_agent(
            name=f"{clean_test_data}group_agent",
            description="test",
            content="# agent",
            org_id=org_id,
            created_by=user1_id,
            status="active",
            owner_user_id=user1_id,
        )
        # Before the grant, user2 cannot see it.
        before = await def_repo.list_agents_accessible_by(user2_id, org_id)
        assert f"{clean_test_data}group_agent" not in [a["name"] for a in before["items"]]

        await acl.grant_access(
            resource_type="agent",
            resource_id=str(agent["id"]),
            org_id=org_id,
            principal_type="group",
            principal_id=str(group["id"]),
            granted_by=user1_id,
        )

        # user2 is a group member with a group grant — should now see the agent.
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


# ── Workflow (schedules) + Model Ownership ────────────────────────────────


class TestWorkflowOwnership:
    """list_schedules filters by ownership; owners see their own workflows."""

    @pytest.mark.asyncio
    async def test_list_schedules_respects_ownership(
        self, db_pool, test_organization, test_user, second_user, clean_test_data,
    ):
        repo = ScheduleRepository(db_pool)
        org_id = str(test_organization["id"])
        user1_id = str(test_user["id"])
        user2_id = str(second_user["id"])

        await repo.create_schedule(
            title=f"{clean_test_data}wf_user1",
            org_id=org_id,
            created_by=user1_id,
            prompt="x",
        )

        # Owner sees their workflow.
        owned = await repo.list_schedules(
            org_id, requester_user_id=user1_id, requester_role="member",
        )
        assert f"{clean_test_data}wf_user1" in [s["title"] for s in owned["items"]]

        # A different member does not.
        other = await repo.list_schedules(
            org_id, requester_user_id=user2_id, requester_role="member",
        )
        assert f"{clean_test_data}wf_user1" not in [s["title"] for s in other["items"]]

        # Admin override sees it.
        as_admin = await repo.list_schedules(
            org_id, requester_user_id=user2_id, requester_role="admin",
        )
        assert f"{clean_test_data}wf_user1" in [s["title"] for s in as_admin["items"]]

    @pytest.mark.asyncio
    async def test_get_schedule_blocks_non_owner(
        self, db_pool, test_organization, test_user, second_user, clean_test_data,
    ):
        repo = ScheduleRepository(db_pool)
        org_id = str(test_organization["id"])
        user1_id = str(test_user["id"])
        user2_id = str(second_user["id"])

        sched = await repo.create_schedule(
            title=f"{clean_test_data}wf_detail",
            org_id=org_id,
            created_by=user1_id,
            prompt="x",
        )
        sid = str(sched["id"])

        assert await repo.get_schedule(
            sid, org_id, requester_user_id=user1_id, requester_role="member",
        ) is not None
        assert await repo.get_schedule(
            sid, org_id, requester_user_id=user2_id, requester_role="member",
        ) is None
        # No requester → unscoped (daemon/system path) still resolves.
        assert await repo.get_schedule(sid, org_id) is not None


class TestWorkflowOwnershipReassignment:
    """update_schedule can move a workflow between personal, group, and org-shared."""

    @pytest.mark.asyncio
    async def test_reassign_owner_user_to_org_shared_and_back(
        self, db_pool, acl, test_organization, test_user, clean_test_data,
    ):
        repo = ScheduleRepository(db_pool)
        org_id = str(test_organization["id"])
        user1_id = str(test_user["id"])

        sched = await repo.create_schedule(
            title=f"{clean_test_data}wf_reassign",
            org_id=org_id,
            created_by=user1_id,
            prompt="x",
        )
        sid = str(sched["id"])
        assert str(sched["owner_user_id"]) == user1_id

        # Personal -> org-shared (both owners NULL).
        updated = await repo.update_schedule(
            sid, org_id, owner_user_id=None, owner_group_id=None,
        )
        assert updated["owner_user_id"] is None
        assert updated["owner_group_id"] is None
        # Under the grant model, org-shared visibility comes from an org grant,
        # not from NULL owners. Grant one, then a plain member can see it.
        await acl.grant_access(
            resource_type="workflow",
            resource_id=sid,
            org_id=org_id,
            principal_type="org",
            principal_id=None,
            granted_by=user1_id,
        )
        assert await repo.get_schedule(
            sid, org_id, requester_user_id=user1_id, requester_role="member",
        ) is not None

        # Org-shared -> personal again.
        reverted = await repo.update_schedule(
            sid, org_id, owner_user_id=user1_id, owner_group_id=None,
        )
        assert str(reverted["owner_user_id"]) == user1_id

    @pytest.mark.asyncio
    async def test_reassign_to_group(
        self, db_pool, test_organization, test_user, group_repo, clean_test_data,
    ):
        repo = ScheduleRepository(db_pool)
        org_id = str(test_organization["id"])
        user1_id = str(test_user["id"])

        group = await group_repo.create_group(
            org_id=org_id, name=f"{clean_test_data}wf_group", created_by=user1_id,
        )
        group_id = str(group["id"])

        sched = await repo.create_schedule(
            title=f"{clean_test_data}wf_group_reassign",
            org_id=org_id,
            created_by=user1_id,
            prompt="x",
        )
        sid = str(sched["id"])

        updated = await repo.update_schedule(
            sid, org_id, owner_user_id=None, owner_group_id=group_id,
        )
        assert updated["owner_user_id"] is None
        assert str(updated["owner_group_id"]) == group_id


class TestModelOwnership:
    """list_models filters by grants; an org grant makes a model visible to all."""

    @pytest.mark.asyncio
    async def test_list_models_respects_ownership(
        self, db_pool, acl, test_organization, test_user, second_user, clean_test_data,
    ):
        repo = ModelRepository(db_pool)
        org_id = str(test_organization["id"])
        user1_id = str(test_user["id"])
        user2_id = str(second_user["id"])

        # Org-shared model: created, then granted to the whole org.
        shared_id = f"{clean_test_data}shared_model"
        await repo.create_model(
            model_id=shared_id, provider="test", name="Shared", org_id=org_id,
        )
        await acl.grant_access(
            resource_type="model",
            resource_id=shared_id,
            org_id=org_id,
            principal_type="org",
            principal_id=None,
            granted_by=user1_id,
        )
        # Personal model owned by user1, with no org grant.
        personal_id = f"{clean_test_data}personal_model"
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO models (id, provider, name, organization_id, "
                " scope, owner_user_id) "
                "VALUES ($1, 'test', 'Personal', $2::uuid, 'instance', $3::uuid)",
                personal_id, org_id, user1_id,
            )

        def ids(result):
            return [m["id"] for m in result["items"]]

        as_user1 = await repo.list_models(
            org_id=org_id, requester_user_id=user1_id, requester_role="member",
            limit=500,
        )
        assert shared_id in ids(as_user1)
        assert personal_id in ids(as_user1)

        as_user2 = await repo.list_models(
            org_id=org_id, requester_user_id=user2_id, requester_role="member",
            limit=500,
        )
        # Org grant => everyone sees the shared model; personal stays private.
        assert shared_id in ids(as_user2)
        assert personal_id not in ids(as_user2)

        as_admin = await repo.list_models(
            org_id=org_id, requester_user_id=user2_id, requester_role="admin",
            limit=500,
        )
        assert personal_id in ids(as_admin)

    @pytest.mark.asyncio
    async def test_user_grant_makes_model_visible(
        self, db_pool, acl, test_organization, test_user, second_user, clean_test_data,
    ):
        repo = ModelRepository(db_pool)
        org_id = str(test_organization["id"])
        user1_id = str(test_user["id"])
        user2_id = str(second_user["id"])

        model_id = f"{clean_test_data}targeted_model"
        await repo.create_model(
            model_id=model_id, provider="test", name="Targeted", org_id=org_id,
        )

        def ids(result):
            return [m["id"] for m in result["items"]]

        # No grant yet: neither member sees it (default-deny).
        before = await repo.list_models(
            org_id=org_id, requester_user_id=user2_id, requester_role="member",
            limit=500,
        )
        assert model_id not in ids(before)

        # Grant to user2 only.
        await acl.grant_access(
            resource_type="model",
            resource_id=model_id,
            org_id=org_id,
            principal_type="user",
            principal_id=user2_id,
            granted_by=user1_id,
        )

        as_user2 = await repo.list_models(
            org_id=org_id, requester_user_id=user2_id, requester_role="member",
            limit=500,
        )
        assert model_id in ids(as_user2)

        # user1 (no grant, not owner) still cannot see it.
        as_user1 = await repo.list_models(
            org_id=org_id, requester_user_id=user1_id, requester_role="member",
            limit=500,
        )
        assert model_id not in ids(as_user1)

