"""Tests for definition audit logging.

Covers:
- Agent lifecycle audit (create, update, approve, reject, delete)
- Skill lifecycle audit (create, update, approve, reject, delete)
- MCP server lifecycle audit (create, update, approve, reject, delete)
- Grant/revoke audit (grant_skill, revoke_skill, grant_mcp_server,
  revoke_mcp_server, update_mcp_tool_grants)
- Audit failure isolation (audit errors don't break business logic)
- No-audit backward compat (audit_repo=None)
- Context richness (old/new values in update audit entries)
"""

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest_asyncio

from lucent.db import AuditRepository, OrganizationRepository, UserRepository
from lucent.db.audit import (
    DEFINITION_APPROVE,
    DEFINITION_CREATE,
    DEFINITION_DELETE,
    DEFINITION_GRANT,
    DEFINITION_REJECT,
    DEFINITION_REVOKE,
    DEFINITION_UPDATE,
)
from lucent.db.definitions import DefinitionRepository

SENTINEL_ID = UUID("00000000-0000-0000-0000-000000000000")


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def da_prefix(db_pool):
    """Unique prefix and cleanup for definition-audit tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_defaudit_{test_id}_"
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
        # Clean audit entries produced by these tests (sentinel memory_id)
        await conn.execute(
            "DELETE FROM memory_audit_log WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%"
        )


@pytest_asyncio.fixture
async def da_org(db_pool, da_prefix):
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{da_prefix}org")


@pytest_asyncio.fixture
async def da_user(db_pool, da_org, da_prefix):
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{da_prefix}user",
        provider="local",
        organization_id=da_org["id"],
        email=f"{da_prefix}user@test.com",
        display_name=f"{da_prefix}User",
        role="admin",
    )


@pytest_asyncio.fixture
def audit_repo(db_pool):
    return AuditRepository(db_pool)


@pytest_asyncio.fixture
def def_repo(db_pool, audit_repo):
    """DefinitionRepository with audit wired in."""
    return DefinitionRepository(db_pool, audit_repo=audit_repo)


@pytest_asyncio.fixture
def def_repo_no_audit(db_pool):
    """DefinitionRepository without audit (backward compat)."""
    return DefinitionRepository(db_pool)


# ── Helpers ──────────────────────────────────────────────────────────────


async def _fetch_audit_entries(db_pool, org_id) -> list[dict]:
    """Fetch all definition audit entries for an org, newest first."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM memory_audit_log "
            "WHERE organization_id = $1 AND memory_id = $2 "
            "ORDER BY created_at DESC",
            str(org_id),
            str(SENTINEL_ID),
        )
    results = []
    for r in rows:
        d = dict(r)
        # Normalize UUIDs to strings for easy comparison
        for key in ("user_id", "organization_id", "memory_id", "id"):
            if isinstance(d.get(key), UUID):
                d[key] = str(d[key])
        results.append(d)
    return results


async def _create_test_agent(def_repo, org_id, user_id, name="AuditAgent"):
    return await def_repo.create_agent(
        name=name,
        description="desc",
        content="# Agent",
        org_id=str(org_id),
        created_by=str(user_id),
    )


async def _create_test_skill(def_repo, org_id, user_id, name="AuditSkill"):
    return await def_repo.create_skill(
        name=name,
        description="desc",
        content="# Skill",
        org_id=str(org_id),
        created_by=str(user_id),
    )


async def _create_test_mcp(def_repo, org_id, user_id, name="AuditMCP"):
    return await def_repo.create_mcp_server(
        name=name,
        description="desc",
        server_type="http",
        url="http://localhost:1234",
        org_id=str(org_id),
        created_by=str(user_id),
    )


# ============================================================================
# 1. Agent Lifecycle Audit
# ============================================================================


class TestAgentLifecycleAudit:
    """Verify create/update/approve/reject/delete produce correct audit entries."""

    async def test_create_agent_audit(self, db_pool, def_repo, da_org, da_user):
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        entries = await _fetch_audit_entries(db_pool, da_org["id"])

        assert len(entries) >= 1
        entry = entries[0]
        assert entry["action_type"] == DEFINITION_CREATE
        assert entry["context"]["definition_type"] == "agent"
        assert entry["context"]["definition_id"] == str(agent["id"])
        assert entry["user_id"] == str(da_user["id"])
        assert entry["organization_id"] == str(da_org["id"])

    async def test_update_agent_audit(self, db_pool, def_repo, da_org, da_user):
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        await def_repo.update_agent(
            str(agent["id"]), str(da_org["id"]),
            name="Updated", description="new desc",
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])

        update_entries = [e for e in entries if e["action_type"] == DEFINITION_UPDATE]
        assert len(update_entries) >= 1
        entry = update_entries[0]
        assert entry["context"]["definition_type"] == "agent"
        assert entry["context"]["definition_id"] == str(agent["id"])

    async def test_approve_agent_audit(self, db_pool, def_repo, da_org, da_user):
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        await def_repo.approve_agent(
            str(agent["id"]), str(da_org["id"]), str(da_user["id"]),
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])

        approve_entries = [e for e in entries if e["action_type"] == DEFINITION_APPROVE]
        assert len(approve_entries) == 1
        entry = approve_entries[0]
        assert entry["context"]["definition_type"] == "agent"
        assert entry["context"]["definition_id"] == str(agent["id"])
        assert entry["user_id"] == str(da_user["id"])

    async def test_reject_agent_audit(self, db_pool, def_repo, da_org, da_user):
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        await def_repo.reject_agent(
            str(agent["id"]), str(da_org["id"]), str(da_user["id"]),
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])

        reject_entries = [e for e in entries if e["action_type"] == DEFINITION_REJECT]
        assert len(reject_entries) == 1
        entry = reject_entries[0]
        assert entry["context"]["definition_type"] == "agent"
        assert entry["context"]["definition_id"] == str(agent["id"])
        assert entry["user_id"] == str(da_user["id"])

    async def test_delete_agent_audit(self, db_pool, def_repo, da_org, da_user):
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        agent_id = str(agent["id"])
        deleted = await def_repo.delete_agent(agent_id, str(da_org["id"]))
        assert deleted is True

        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        delete_entries = [e for e in entries if e["action_type"] == DEFINITION_DELETE]
        assert len(delete_entries) == 1
        entry = delete_entries[0]
        assert entry["context"]["definition_type"] == "agent"
        assert entry["context"]["definition_id"] == agent_id


# ============================================================================
# 2. Skill Lifecycle Audit
# ============================================================================


class TestSkillLifecycleAudit:
    """Verify create/update/approve/reject/delete produce correct audit entries."""

    async def test_create_skill_audit(self, db_pool, def_repo, da_org, da_user):
        skill = await _create_test_skill(def_repo, da_org["id"], da_user["id"])
        entries = await _fetch_audit_entries(db_pool, da_org["id"])

        create_entries = [e for e in entries if e["action_type"] == DEFINITION_CREATE]
        assert len(create_entries) >= 1
        entry = create_entries[0]
        assert entry["context"]["definition_type"] == "skill"
        assert entry["context"]["definition_id"] == str(skill["id"])
        assert entry["user_id"] == str(da_user["id"])
        assert entry["organization_id"] == str(da_org["id"])

    async def test_update_skill_audit(self, db_pool, def_repo, da_org, da_user):
        skill = await _create_test_skill(def_repo, da_org["id"], da_user["id"])
        await def_repo.update_skill(
            str(skill["id"]), str(da_org["id"]),
            name="UpdatedSkill", content="# new",
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])

        update_entries = [e for e in entries if e["action_type"] == DEFINITION_UPDATE]
        assert len(update_entries) >= 1
        entry = update_entries[0]
        assert entry["context"]["definition_type"] == "skill"
        assert entry["context"]["definition_id"] == str(skill["id"])

    async def test_approve_skill_audit(self, db_pool, def_repo, da_org, da_user):
        skill = await _create_test_skill(def_repo, da_org["id"], da_user["id"])
        await def_repo.approve_skill(
            str(skill["id"]), str(da_org["id"]), str(da_user["id"]),
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])

        approve_entries = [e for e in entries if e["action_type"] == DEFINITION_APPROVE]
        assert len(approve_entries) == 1
        entry = approve_entries[0]
        assert entry["context"]["definition_type"] == "skill"
        assert entry["user_id"] == str(da_user["id"])

    async def test_reject_skill_audit(self, db_pool, def_repo, da_org, da_user):
        skill = await _create_test_skill(def_repo, da_org["id"], da_user["id"])
        await def_repo.reject_skill(
            str(skill["id"]), str(da_org["id"]), str(da_user["id"]),
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])

        reject_entries = [e for e in entries if e["action_type"] == DEFINITION_REJECT]
        assert len(reject_entries) == 1
        assert reject_entries[0]["context"]["definition_type"] == "skill"

    async def test_delete_skill_audit(self, db_pool, def_repo, da_org, da_user):
        skill = await _create_test_skill(def_repo, da_org["id"], da_user["id"])
        skill_id = str(skill["id"])
        deleted = await def_repo.delete_skill(skill_id, str(da_org["id"]))
        assert deleted is True

        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        delete_entries = [e for e in entries if e["action_type"] == DEFINITION_DELETE]
        assert len(delete_entries) == 1
        assert delete_entries[0]["context"]["definition_type"] == "skill"
        assert delete_entries[0]["context"]["definition_id"] == skill_id


# ============================================================================
# 3. MCP Server Lifecycle Audit
# ============================================================================


class TestMCPServerLifecycleAudit:
    """Verify create/update/approve/reject/delete produce correct audit entries."""

    async def test_create_mcp_server_audit(self, db_pool, def_repo, da_org, da_user):
        mcp = await _create_test_mcp(def_repo, da_org["id"], da_user["id"])
        entries = await _fetch_audit_entries(db_pool, da_org["id"])

        create_entries = [e for e in entries if e["action_type"] == DEFINITION_CREATE]
        assert len(create_entries) >= 1
        entry = create_entries[0]
        assert entry["context"]["definition_type"] == "mcp_server"
        assert entry["context"]["definition_id"] == str(mcp["id"])
        assert entry["user_id"] == str(da_user["id"])
        assert entry["organization_id"] == str(da_org["id"])

    async def test_update_mcp_server_audit(self, db_pool, def_repo, da_org, da_user):
        mcp = await _create_test_mcp(def_repo, da_org["id"], da_user["id"])
        await def_repo.update_mcp_server(
            str(mcp["id"]), str(da_org["id"]),
            name="UpdatedMCP", url="http://new:5555",
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])

        update_entries = [e for e in entries if e["action_type"] == DEFINITION_UPDATE]
        assert len(update_entries) >= 1
        entry = update_entries[0]
        assert entry["context"]["definition_type"] == "mcp_server"
        assert entry["context"]["definition_id"] == str(mcp["id"])

    async def test_approve_mcp_server_audit(self, db_pool, def_repo, da_org, da_user):
        mcp = await _create_test_mcp(def_repo, da_org["id"], da_user["id"])
        await def_repo.approve_mcp_server(
            str(mcp["id"]), str(da_org["id"]), str(da_user["id"]),
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])

        approve_entries = [e for e in entries if e["action_type"] == DEFINITION_APPROVE]
        assert len(approve_entries) == 1
        entry = approve_entries[0]
        assert entry["context"]["definition_type"] == "mcp_server"
        assert entry["user_id"] == str(da_user["id"])

    async def test_reject_mcp_server_audit(self, db_pool, def_repo, da_org, da_user):
        mcp = await _create_test_mcp(def_repo, da_org["id"], da_user["id"])
        await def_repo.reject_mcp_server(
            str(mcp["id"]), str(da_org["id"]), str(da_user["id"]),
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])

        reject_entries = [e for e in entries if e["action_type"] == DEFINITION_REJECT]
        assert len(reject_entries) == 1
        assert reject_entries[0]["context"]["definition_type"] == "mcp_server"

    async def test_delete_mcp_server_audit(self, db_pool, def_repo, da_org, da_user):
        mcp = await _create_test_mcp(def_repo, da_org["id"], da_user["id"])
        mcp_id = str(mcp["id"])
        deleted = await def_repo.delete_mcp_server(mcp_id, str(da_org["id"]))
        assert deleted is True

        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        delete_entries = [e for e in entries if e["action_type"] == DEFINITION_DELETE]
        assert len(delete_entries) == 1
        assert delete_entries[0]["context"]["definition_type"] == "mcp_server"
        assert delete_entries[0]["context"]["definition_id"] == mcp_id


# ============================================================================
# 4. Grant / Revoke Audit
# ============================================================================


class TestGrantRevokeAudit:
    """Verify grant/revoke operations produce audit entries."""

    async def test_grant_skill_audit(self, db_pool, def_repo, da_org, da_user):
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        skill = await _create_test_skill(def_repo, da_org["id"], da_user["id"])

        ok = await def_repo.grant_skill(
            str(agent["id"]), str(skill["id"]),
            org_id=str(da_org["id"]), user_id=str(da_user["id"]),
        )
        assert ok is True

        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        grant_entries = [e for e in entries if e["action_type"] == DEFINITION_GRANT]
        assert len(grant_entries) >= 1
        entry = grant_entries[0]
        assert entry["context"]["definition_type"] == "skill"
        assert entry["context"]["definition_id"] == str(skill["id"])
        assert entry["context"]["agent_id"] == str(agent["id"])
        assert entry["user_id"] == str(da_user["id"])

    async def test_revoke_skill_audit(self, db_pool, def_repo, da_org, da_user):
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        skill = await _create_test_skill(def_repo, da_org["id"], da_user["id"])
        await def_repo.grant_skill(str(agent["id"]), str(skill["id"]))

        revoked = await def_repo.revoke_skill(
            str(agent["id"]), str(skill["id"]),
            org_id=str(da_org["id"]), user_id=str(da_user["id"]),
        )
        assert revoked is True

        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        revoke_entries = [e for e in entries if e["action_type"] == DEFINITION_REVOKE]
        assert len(revoke_entries) >= 1
        entry = revoke_entries[0]
        assert entry["context"]["definition_type"] == "skill"
        assert entry["context"]["definition_id"] == str(skill["id"])
        assert entry["context"]["agent_id"] == str(agent["id"])

    async def test_grant_mcp_server_audit(self, db_pool, def_repo, da_org, da_user):
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        mcp = await _create_test_mcp(def_repo, da_org["id"], da_user["id"])

        ok = await def_repo.grant_mcp_server(
            str(agent["id"]), str(mcp["id"]),
            org_id=str(da_org["id"]), user_id=str(da_user["id"]),
        )
        assert ok is True

        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        grant_entries = [e for e in entries if e["action_type"] == DEFINITION_GRANT]
        assert len(grant_entries) >= 1
        entry = grant_entries[0]
        assert entry["context"]["definition_type"] == "mcp_server"
        assert entry["context"]["definition_id"] == str(mcp["id"])
        assert entry["context"]["agent_id"] == str(agent["id"])

    async def test_revoke_mcp_server_audit(self, db_pool, def_repo, da_org, da_user):
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        mcp = await _create_test_mcp(def_repo, da_org["id"], da_user["id"])
        await def_repo.grant_mcp_server(str(agent["id"]), str(mcp["id"]))

        revoked = await def_repo.revoke_mcp_server(
            str(agent["id"]), str(mcp["id"]),
            org_id=str(da_org["id"]), user_id=str(da_user["id"]),
        )
        assert revoked is True

        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        revoke_entries = [e for e in entries if e["action_type"] == DEFINITION_REVOKE]
        assert len(revoke_entries) >= 1
        entry = revoke_entries[0]
        assert entry["context"]["definition_type"] == "mcp_server"
        assert entry["context"]["agent_id"] == str(agent["id"])

    async def test_update_mcp_tool_grants_audit(
        self, db_pool, def_repo, da_org, da_user,
    ):
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        mcp = await _create_test_mcp(def_repo, da_org["id"], da_user["id"])
        await def_repo.grant_mcp_server(str(agent["id"]), str(mcp["id"]))

        updated = await def_repo.update_mcp_tool_grants(
            str(agent["id"]),
            str(mcp["id"]),
            allowed_tools=["tool_a", "tool_b"],
            org_id=str(da_org["id"]),
            user_id=str(da_user["id"]),
        )
        assert updated is True

        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        update_entries = [e for e in entries if e["action_type"] == DEFINITION_UPDATE]
        assert len(update_entries) >= 1
        entry = update_entries[0]
        assert entry["context"]["definition_type"] == "mcp_server"
        assert entry["context"]["definition_id"] == str(mcp["id"])
        assert entry["context"]["agent_id"] == str(agent["id"])
        assert entry["context"]["allowed_tools"] == ["tool_a", "tool_b"]

    async def test_grant_skill_no_org_skips_audit(
        self, db_pool, def_repo, da_org, da_user,
    ):
        """Grant without org_id should still succeed but skip audit."""
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        skill = await _create_test_skill(def_repo, da_org["id"], da_user["id"])

        # Count entries before
        entries_before = await _fetch_audit_entries(db_pool, da_org["id"])
        grant_before = [e for e in entries_before if e["action_type"] == DEFINITION_GRANT]

        ok = await def_repo.grant_skill(str(agent["id"]), str(skill["id"]))
        assert ok is True

        entries_after = await _fetch_audit_entries(db_pool, da_org["id"])
        grant_after = [e for e in entries_after if e["action_type"] == DEFINITION_GRANT]
        # No new grant audit entries (creates are still there)
        assert len(grant_after) == len(grant_before)


# ============================================================================
# 5. Audit Failure Isolation
# ============================================================================


class TestAuditFailureIsolation:
    """Verify audit failures don't break the main business operation."""

    async def test_create_agent_succeeds_when_audit_raises(
        self, db_pool, da_org, da_user,
    ):
        audit = AuditRepository(db_pool)
        repo = DefinitionRepository(db_pool, audit_repo=audit)

        with patch.object(
            audit, "log_definition_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            agent = await repo.create_agent(
                name="SurvivesAuditFail",
                description="desc",
                content="# agent",
                org_id=str(da_org["id"]),
                created_by=str(da_user["id"]),
            )

        assert agent is not None
        assert agent["name"] == "SurvivesAuditFail"

    async def test_approve_agent_succeeds_when_audit_raises(
        self, db_pool, da_org, da_user,
    ):
        audit = AuditRepository(db_pool)
        repo = DefinitionRepository(db_pool, audit_repo=audit)

        # Create without patching so the agent exists
        agent = await repo.create_agent(
            name="ApproveAuditFail",
            description="desc",
            content="# agent",
            org_id=str(da_org["id"]),
            created_by=str(da_user["id"]),
        )

        with patch.object(
            audit, "log_definition_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            result = await repo.approve_agent(
                str(agent["id"]), str(da_org["id"]), str(da_user["id"]),
            )

        assert result is not None
        assert result["status"] == "active"

    async def test_delete_skill_succeeds_when_audit_raises(
        self, db_pool, da_org, da_user,
    ):
        audit = AuditRepository(db_pool)
        repo = DefinitionRepository(db_pool, audit_repo=audit)

        skill = await repo.create_skill(
            name="DeleteAuditFail",
            description="desc",
            content="# skill",
            org_id=str(da_org["id"]),
            created_by=str(da_user["id"]),
        )

        with patch.object(
            audit, "log_definition_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            deleted = await repo.delete_skill(str(skill["id"]), str(da_org["id"]))

        assert deleted is True

    async def test_grant_mcp_succeeds_when_audit_raises(
        self, db_pool, da_org, da_user,
    ):
        audit = AuditRepository(db_pool)
        repo = DefinitionRepository(db_pool, audit_repo=audit)

        agent = await repo.create_agent(
            name="GrantAuditFail",
            description="desc",
            content="# agent",
            org_id=str(da_org["id"]),
            created_by=str(da_user["id"]),
        )
        mcp = await repo.create_mcp_server(
            name="GrantAuditFailMCP",
            description="desc",
            server_type="http",
            url="http://localhost:1111",
            org_id=str(da_org["id"]),
            created_by=str(da_user["id"]),
        )

        with patch.object(
            audit, "log_definition_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            ok = await repo.grant_mcp_server(
                str(agent["id"]), str(mcp["id"]),
                org_id=str(da_org["id"]),
            )

        assert ok is True


# ============================================================================
# 6. No Audit When audit_repo is None (Backward Compat)
# ============================================================================


class TestNoAuditBackwardCompat:
    """DefinitionRepository works fine without audit_repo."""

    async def test_create_agent_no_audit(
        self, db_pool, def_repo_no_audit, da_org, da_user,
    ):
        agent = await def_repo_no_audit.create_agent(
            name="NoAuditAgent",
            description="desc",
            content="# agent",
            org_id=str(da_org["id"]),
            created_by=str(da_user["id"]),
        )
        assert agent is not None
        assert agent["name"] == "NoAuditAgent"

    async def test_update_skill_no_audit(
        self, db_pool, def_repo_no_audit, da_org, da_user,
    ):
        skill = await def_repo_no_audit.create_skill(
            name="NoAuditSkill",
            description="desc",
            content="# skill",
            org_id=str(da_org["id"]),
            created_by=str(da_user["id"]),
        )
        updated = await def_repo_no_audit.update_skill(
            str(skill["id"]), str(da_org["id"]),
            description="updated desc",
        )
        assert updated is not None
        assert updated["description"] == "updated desc"

    async def test_delete_mcp_no_audit(
        self, db_pool, def_repo_no_audit, da_org, da_user,
    ):
        mcp = await def_repo_no_audit.create_mcp_server(
            name="NoAuditMCP",
            description="desc",
            server_type="http",
            url="http://localhost:2222",
            org_id=str(da_org["id"]),
            created_by=str(da_user["id"]),
        )
        deleted = await def_repo_no_audit.delete_mcp_server(
            str(mcp["id"]), str(da_org["id"]),
        )
        assert deleted is True

    async def test_grant_revoke_no_audit(
        self, db_pool, def_repo_no_audit, da_org, da_user,
    ):
        agent = await def_repo_no_audit.create_agent(
            name="NoAuditGrantAgent",
            description="desc",
            content="# agent",
            org_id=str(da_org["id"]),
            created_by=str(da_user["id"]),
        )
        skill = await def_repo_no_audit.create_skill(
            name="NoAuditGrantSkill",
            description="desc",
            content="# skill",
            org_id=str(da_org["id"]),
            created_by=str(da_user["id"]),
        )
        ok = await def_repo_no_audit.grant_skill(
            str(agent["id"]), str(skill["id"]),
        )
        assert ok is True
        revoked = await def_repo_no_audit.revoke_skill(
            str(agent["id"]), str(skill["id"]),
        )
        assert revoked is True


# ============================================================================
# 7. Context Richness
# ============================================================================


class TestContextRichness:
    """Verify update operations include useful context (fields changed, etc)."""

    async def test_update_agent_context_has_updated_fields(
        self, db_pool, def_repo, da_org, da_user,
    ):
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        await def_repo.update_agent(
            str(agent["id"]), str(da_org["id"]),
            name="NewName", description="NewDesc",
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        update_entries = [e for e in entries if e["action_type"] == DEFINITION_UPDATE]
        assert len(update_entries) >= 1

        ctx = update_entries[0]["context"]
        assert "updated_fields" in ctx
        assert "name" in ctx["updated_fields"]
        assert "description" in ctx["updated_fields"]

    async def test_update_skill_context_has_updated_fields(
        self, db_pool, def_repo, da_org, da_user,
    ):
        skill = await _create_test_skill(def_repo, da_org["id"], da_user["id"])
        await def_repo.update_skill(
            str(skill["id"]), str(da_org["id"]),
            content="# Updated content",
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        update_entries = [e for e in entries if e["action_type"] == DEFINITION_UPDATE]
        assert len(update_entries) >= 1

        ctx = update_entries[0]["context"]
        assert "updated_fields" in ctx
        assert "content" in ctx["updated_fields"]
        # Only content was changed, not name
        assert "name" not in ctx["updated_fields"]

    async def test_update_mcp_server_context_has_updated_fields(
        self, db_pool, def_repo, da_org, da_user,
    ):
        mcp = await _create_test_mcp(def_repo, da_org["id"], da_user["id"])
        await def_repo.update_mcp_server(
            str(mcp["id"]), str(da_org["id"]),
            name="NewMCPName", url="http://new:9999",
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        update_entries = [e for e in entries if e["action_type"] == DEFINITION_UPDATE]
        assert len(update_entries) >= 1

        ctx = update_entries[0]["context"]
        assert "updated_fields" in ctx
        assert "name" in ctx["updated_fields"]
        assert "url" in ctx["updated_fields"]

    async def test_update_mcp_tool_grants_context_has_tools(
        self, db_pool, def_repo, da_org, da_user,
    ):
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        mcp = await _create_test_mcp(def_repo, da_org["id"], da_user["id"])
        await def_repo.grant_mcp_server(str(agent["id"]), str(mcp["id"]))

        await def_repo.update_mcp_tool_grants(
            str(agent["id"]),
            str(mcp["id"]),
            allowed_tools=["search", "create"],
            org_id=str(da_org["id"]),
            user_id=str(da_user["id"]),
        )

        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        update_entries = [e for e in entries if e["action_type"] == DEFINITION_UPDATE]
        tool_updates = [
            e for e in update_entries
            if "allowed_tools" in (e["context"] or {})
        ]
        assert len(tool_updates) >= 1
        assert tool_updates[0]["context"]["allowed_tools"] == ["search", "create"]

    async def test_grant_context_has_agent_id(
        self, db_pool, def_repo, da_org, da_user,
    ):
        """Grant audit context includes which agent was granted the resource."""
        agent = await _create_test_agent(def_repo, da_org["id"], da_user["id"])
        skill = await _create_test_skill(def_repo, da_org["id"], da_user["id"])

        await def_repo.grant_skill(
            str(agent["id"]), str(skill["id"]),
            org_id=str(da_org["id"]), user_id=str(da_user["id"]),
        )

        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        grant_entries = [e for e in entries if e["action_type"] == DEFINITION_GRANT]
        assert len(grant_entries) >= 1
        assert grant_entries[0]["context"]["agent_id"] == str(agent["id"])

    async def test_create_agent_notes_contain_name(
        self, db_pool, def_repo, da_org, da_user,
    ):
        """Audit notes for create should reference the definition name."""
        await _create_test_agent(
            def_repo, da_org["id"], da_user["id"], name="MySpecialAgent",
        )
        entries = await _fetch_audit_entries(db_pool, da_org["id"])
        create_entries = [e for e in entries if e["action_type"] == DEFINITION_CREATE]
        assert any("MySpecialAgent" in (e["notes"] or "") for e in create_entries)
