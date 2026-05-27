"""Tests for built-in definition protection from daemon modification.

Built-in objects (agent definitions, skill definitions, MCP server configs with
scope='built-in', and schedules with is_system=true) should not be modifiable by
the daemon via the API.  Admin and owner roles must still be allowed.

The daemon should still be able to update non-built-in (instance-scoped) objects.
"""

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import DefinitionRepository, OrganizationRepository, UserRepository, get_pool
from lucent.db.audit import AuditRepository
from lucent.db.definitions import BuiltInProtectionError
from lucent.db.schedules import ScheduleRepository


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def bi_prefix(db_pool):
    """Unique prefix for test data; tears down at the end."""
    test_id = str(uuid4())[:8]
    prefix = f"test_bi_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        # Junction tables
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
        # Definitions
        await conn.execute(
            "DELETE FROM agent_definitions WHERE name LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM skill_definitions WHERE name LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM mcp_server_configs WHERE name LIKE $1", f"{prefix}%"
        )
        # Schedules
        await conn.execute(
            "DELETE FROM schedule_runs WHERE schedule_id IN "
            "(SELECT id FROM schedules WHERE title LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM schedules WHERE title LIKE $1", f"{prefix}%"
        )
        # Users / orgs
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
async def org_and_users(db_pool, bi_prefix):
    """Create an org with owner, admin, and daemon users."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{bi_prefix}org")
    user_repo = UserRepository(db_pool)

    owner = await user_repo.create(
        external_id=f"{bi_prefix}owner",
        provider="local",
        organization_id=org["id"],
        email=f"{bi_prefix}owner@test.com",
        display_name="Owner",
        role="owner",
    )
    admin = await user_repo.create(
        external_id=f"{bi_prefix}admin",
        provider="local",
        organization_id=org["id"],
        email=f"{bi_prefix}admin@test.com",
        display_name="Admin",
        role="admin",
    )
    daemon = await user_repo.create(
        external_id=f"{bi_prefix}daemon",
        provider="local",
        organization_id=org["id"],
        email=f"{bi_prefix}daemon@test.com",
        display_name="Daemon",
        role="daemon",
    )
    return {"org": org, "owner": owner, "admin": admin, "daemon": daemon}


def _make_client_for_user(user):
    """Return an async-context-manager that yields an httpx.AsyncClient
    authenticated as *user*."""

    class _Ctx:
        async def __aenter__(self):
            app = create_app()
            fake = CurrentUser(
                id=user["id"],
                organization_id=user["organization_id"],
                role=user.get("role", "member"),
                email=user.get("email"),
                display_name=user.get("display_name"),
                auth_method="api_key",
                api_key_scopes=["read", "write"],
            )

            async def override():
                return fake

            app.dependency_overrides[get_current_user] = override
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            self._client = httpx.AsyncClient(transport=transport, base_url="http://test")
            self._app = app
            return await self._client.__aenter__()

        async def __aexit__(self, *args):
            await self._client.__aexit__(*args)
            self._app.dependency_overrides.clear()

    return _Ctx()


# ── Helper: create built-in + instance definitions ──────────────────────


async def _create_agent(db_pool, org_id, user_id, name, scope="instance"):
    repo = DefinitionRepository(db_pool, audit_repo=AuditRepository(db_pool))
    agent = await repo.create_agent(
        name=name,
        description="test agent",
        content="# test",
        org_id=str(org_id),
        created_by=str(user_id),
        owner_user_id=str(user_id),
    )
    if scope == "built-in":
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_definitions SET scope = 'built-in' WHERE id = $1",
                agent["id"],
            )
    return agent


async def _create_skill(db_pool, org_id, user_id, name, scope="instance"):
    repo = DefinitionRepository(db_pool, audit_repo=AuditRepository(db_pool))
    skill = await repo.create_skill(
        name=name,
        description="test skill",
        content="# test",
        org_id=str(org_id),
        created_by=str(user_id),
        owner_user_id=str(user_id),
    )
    if scope == "built-in":
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE skill_definitions SET scope = 'built-in' WHERE id = $1",
                skill["id"],
            )
    return skill


async def _create_mcp_server(db_pool, org_id, user_id, name, scope="instance"):
    repo = DefinitionRepository(db_pool, audit_repo=AuditRepository(db_pool))
    server = await repo.create_mcp_server(
        name=name,
        description="test server",
        server_type="http",
        url="http://localhost:9999",
        org_id=str(org_id),
        created_by=str(user_id),
        owner_user_id=str(user_id),
    )
    if scope == "built-in":
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE mcp_server_configs SET scope = 'built-in' WHERE id = $1",
                server["id"],
            )
    return server


async def _create_schedule(db_pool, org_id, user_id, title, is_system=False):
    repo = ScheduleRepository(db_pool)
    sched = await repo.create_schedule(
        title=title,
        org_id=str(org_id),
        schedule_type="interval",
        interval_seconds=3600,
        description="test schedule",
        agent_type="code",
        created_by=str(user_id),
    )
    if is_system:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE schedules SET is_system = true WHERE id = $1",
                sched["id"],
            )
    return sched


# ============================================================================
# Repository-level tests (direct DB calls)
# ============================================================================


class TestAgentBuiltInProtection:
    """Daemon cannot update built-in agents; admin/owner can."""

    async def test_daemon_blocked_from_updating_builtin_agent(
        self, db_pool, bi_prefix, org_and_users
    ):
        agent = await _create_agent(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}builtin_agent",
            scope="built-in",
        )
        repo = DefinitionRepository(db_pool)
        with pytest.raises(BuiltInProtectionError):
            await repo.update_agent(
                str(agent["id"]),
                str(org_and_users["org"]["id"]),
                requester_role="daemon",
                name="hacked",
            )

    async def test_admin_can_update_builtin_agent(
        self, db_pool, bi_prefix, org_and_users
    ):
        agent = await _create_agent(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}builtin_agent_admin",
            scope="built-in",
        )
        repo = DefinitionRepository(db_pool)
        result = await repo.update_agent(
            str(agent["id"]),
            str(org_and_users["org"]["id"]),
            requester_role="admin",
            description="admin-updated",
        )
        assert result is not None
        assert result["description"] == "admin-updated"

    async def test_owner_can_update_builtin_agent(
        self, db_pool, bi_prefix, org_and_users
    ):
        agent = await _create_agent(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}builtin_agent_owner",
            scope="built-in",
        )
        repo = DefinitionRepository(db_pool)
        result = await repo.update_agent(
            str(agent["id"]),
            str(org_and_users["org"]["id"]),
            requester_role="owner",
            description="owner-updated",
        )
        assert result is not None
        assert result["description"] == "owner-updated"

    async def test_daemon_can_update_instance_agent(
        self, db_pool, bi_prefix, org_and_users
    ):
        agent = await _create_agent(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["daemon"]["id"],
            f"{bi_prefix}instance_agent",
            scope="instance",
        )
        repo = DefinitionRepository(db_pool)
        result = await repo.update_agent(
            str(agent["id"]),
            str(org_and_users["org"]["id"]),
            requester_role="daemon",
            description="daemon-updated",
        )
        assert result is not None
        assert result["description"] == "daemon-updated"


class TestSkillBuiltInProtection:
    """Daemon cannot update built-in skills; admin/owner can."""

    async def test_daemon_blocked_from_updating_builtin_skill(
        self, db_pool, bi_prefix, org_and_users
    ):
        skill = await _create_skill(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}builtin_skill",
            scope="built-in",
        )
        repo = DefinitionRepository(db_pool)
        with pytest.raises(BuiltInProtectionError):
            await repo.update_skill(
                str(skill["id"]),
                str(org_and_users["org"]["id"]),
                requester_role="daemon",
                content="hacked",
            )

    async def test_admin_can_update_builtin_skill(
        self, db_pool, bi_prefix, org_and_users
    ):
        skill = await _create_skill(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}builtin_skill_admin",
            scope="built-in",
        )
        repo = DefinitionRepository(db_pool)
        result = await repo.update_skill(
            str(skill["id"]),
            str(org_and_users["org"]["id"]),
            requester_role="admin",
            description="admin-updated",
        )
        assert result is not None
        assert result["description"] == "admin-updated"

    async def test_daemon_can_update_instance_skill(
        self, db_pool, bi_prefix, org_and_users
    ):
        skill = await _create_skill(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["daemon"]["id"],
            f"{bi_prefix}instance_skill",
            scope="instance",
        )
        repo = DefinitionRepository(db_pool)
        result = await repo.update_skill(
            str(skill["id"]),
            str(org_and_users["org"]["id"]),
            requester_role="daemon",
            description="daemon-updated",
        )
        assert result is not None
        assert result["description"] == "daemon-updated"


class TestMCPServerBuiltInProtection:
    """Daemon cannot update built-in MCP servers; admin/owner can."""

    async def test_daemon_blocked_from_updating_builtin_mcp_server(
        self, db_pool, bi_prefix, org_and_users
    ):
        server = await _create_mcp_server(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}builtin_mcp",
            scope="built-in",
        )
        repo = DefinitionRepository(db_pool)
        with pytest.raises(BuiltInProtectionError):
            await repo.update_mcp_server(
                str(server["id"]),
                str(org_and_users["org"]["id"]),
                requester_role="daemon",
                description="hacked",
            )

    async def test_owner_can_update_builtin_mcp_server(
        self, db_pool, bi_prefix, org_and_users
    ):
        server = await _create_mcp_server(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}builtin_mcp_owner",
            scope="built-in",
        )
        repo = DefinitionRepository(db_pool)
        result = await repo.update_mcp_server(
            str(server["id"]),
            str(org_and_users["org"]["id"]),
            requester_role="owner",
            description="owner-updated",
        )
        assert result is not None
        assert result["description"] == "owner-updated"

    async def test_daemon_can_update_instance_mcp_server(
        self, db_pool, bi_prefix, org_and_users
    ):
        server = await _create_mcp_server(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["daemon"]["id"],
            f"{bi_prefix}instance_mcp",
            scope="instance",
        )
        repo = DefinitionRepository(db_pool)
        result = await repo.update_mcp_server(
            str(server["id"]),
            str(org_and_users["org"]["id"]),
            requester_role="daemon",
            description="daemon-updated",
        )
        assert result is not None
        assert result["description"] == "daemon-updated"


class TestScheduleSystemProtection:
    """Daemon cannot modify system schedules; admin/owner can."""

    async def test_daemon_blocked_from_updating_system_schedule(
        self, db_pool, bi_prefix, org_and_users
    ):
        sched = await _create_schedule(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}system_sched",
            is_system=True,
        )
        repo = ScheduleRepository(db_pool)
        with pytest.raises(ValueError, match="Built-in system schedules"):
            await repo.update_schedule(
                str(sched["id"]),
                str(org_and_users["org"]["id"]),
                requester_role="daemon",
                description="hacked",
            )

    async def test_daemon_blocked_from_toggling_system_schedule(
        self, db_pool, bi_prefix, org_and_users
    ):
        sched = await _create_schedule(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}system_toggle",
            is_system=True,
        )
        repo = ScheduleRepository(db_pool)
        with pytest.raises(ValueError, match="Built-in system schedules"):
            await repo.toggle_schedule(
                str(sched["id"]),
                str(org_and_users["org"]["id"]),
                False,
                requester_role="daemon",
            )

    async def test_admin_can_update_system_schedule(
        self, db_pool, bi_prefix, org_and_users
    ):
        sched = await _create_schedule(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}system_admin",
            is_system=True,
        )
        repo = ScheduleRepository(db_pool)
        result = await repo.update_schedule(
            str(sched["id"]),
            str(org_and_users["org"]["id"]),
            requester_role="admin",
            description="admin-updated",
        )
        assert result is not None
        assert result["description"] == "admin-updated"

    async def test_owner_can_toggle_system_schedule(
        self, db_pool, bi_prefix, org_and_users
    ):
        sched = await _create_schedule(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}system_owner",
            is_system=True,
        )
        repo = ScheduleRepository(db_pool)
        result = await repo.toggle_schedule(
            str(sched["id"]),
            str(org_and_users["org"]["id"]),
            False,
            requester_role="owner",
        )
        assert result is not None
        assert result["enabled"] is False

    async def test_daemon_can_update_non_system_schedule(
        self, db_pool, bi_prefix, org_and_users
    ):
        sched = await _create_schedule(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["daemon"]["id"],
            f"{bi_prefix}user_sched",
            is_system=False,
        )
        repo = ScheduleRepository(db_pool)
        result = await repo.update_schedule(
            str(sched["id"]),
            str(org_and_users["org"]["id"]),
            requester_role="daemon",
            description="daemon-updated",
        )
        assert result is not None
        assert result["description"] == "daemon-updated"

    async def test_daemon_can_toggle_non_system_schedule(
        self, db_pool, bi_prefix, org_and_users
    ):
        sched = await _create_schedule(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["daemon"]["id"],
            f"{bi_prefix}user_toggle",
            is_system=False,
        )
        repo = ScheduleRepository(db_pool)
        result = await repo.toggle_schedule(
            str(sched["id"]),
            str(org_and_users["org"]["id"]),
            False,
            requester_role="daemon",
        )
        assert result is not None
        assert result["enabled"] is False


# ============================================================================
# API-level tests (HTTP endpoints)
# ============================================================================


class TestAgentAPIProtection:
    """PATCH /api/definitions/agents/{id} with daemon role on built-in."""

    async def test_daemon_gets_403_on_builtin_agent_api(
        self, db_pool, bi_prefix, org_and_users
    ):
        agent = await _create_agent(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["daemon"]["id"],
            f"{bi_prefix}api_builtin_agent",
            scope="built-in",
        )
        async with _make_client_for_user(org_and_users["daemon"]) as client:
            resp = await client.patch(
                f"/api/definitions/agents/{agent['id']}",
                json={
                    "name": f"{bi_prefix}api_builtin_agent",
                    "content": "# hacked",
                },
            )
        assert resp.status_code == 403
        assert "Built-in" in resp.json()["detail"]

    async def test_owner_can_patch_builtin_agent_api(
        self, db_pool, bi_prefix, org_and_users
    ):
        agent = await _create_agent(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}api_bi_agent_owner",
            scope="built-in",
        )
        async with _make_client_for_user(org_and_users["owner"]) as client:
            resp = await client.patch(
                f"/api/definitions/agents/{agent['id']}",
                json={
                    "name": f"{bi_prefix}api_bi_agent_owner",
                    "content": "# updated by owner",
                },
            )
        assert resp.status_code == 200

    async def test_daemon_can_patch_instance_agent_api(
        self, db_pool, bi_prefix, org_and_users
    ):
        agent = await _create_agent(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["daemon"]["id"],
            f"{bi_prefix}api_inst_agent",
            scope="instance",
        )
        async with _make_client_for_user(org_and_users["daemon"]) as client:
            resp = await client.patch(
                f"/api/definitions/agents/{agent['id']}",
                json={
                    "name": f"{bi_prefix}api_inst_agent",
                    "content": "# daemon update",
                },
            )
        assert resp.status_code == 200


class TestMCPServerAPIProtection:
    """PATCH /api/definitions/mcp-servers/{id} with daemon role on built-in."""

    async def test_daemon_gets_403_on_builtin_mcp_server_api(
        self, db_pool, bi_prefix, org_and_users
    ):
        server = await _create_mcp_server(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["daemon"]["id"],
            f"{bi_prefix}api_bi_mcp",
            scope="built-in",
        )
        async with _make_client_for_user(org_and_users["daemon"]) as client:
            resp = await client.patch(
                f"/api/definitions/mcp-servers/{server['id']}",
                json={"description": "hacked"},
            )
        assert resp.status_code == 403
        assert "Built-in" in resp.json()["detail"]


class TestScheduleAPIProtection:
    """PUT /api/schedules/{id} and POST toggle with daemon role on system schedule."""

    async def test_daemon_gets_403_on_system_schedule_update_api(
        self, db_pool, bi_prefix, org_and_users
    ):
        sched = await _create_schedule(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["daemon"]["id"],
            f"{bi_prefix}api_sys_sched",
            is_system=True,
        )
        async with _make_client_for_user(org_and_users["daemon"]) as client:
            resp = await client.put(
                f"/api/schedules/{sched['id']}",
                json={"description": "hacked"},
            )
        assert resp.status_code == 403
        assert "Built-in" in resp.json()["detail"]

    async def test_daemon_gets_403_on_system_schedule_toggle_api(
        self, db_pool, bi_prefix, org_and_users
    ):
        sched = await _create_schedule(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["daemon"]["id"],
            f"{bi_prefix}api_sys_toggle",
            is_system=True,
        )
        async with _make_client_for_user(org_and_users["daemon"]) as client:
            resp = await client.post(
                f"/api/schedules/{sched['id']}/toggle",
                json={"enabled": False},
            )
        assert resp.status_code == 403

    async def test_daemon_can_update_non_system_schedule_api(
        self, db_pool, bi_prefix, org_and_users
    ):
        sched = await _create_schedule(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["daemon"]["id"],
            f"{bi_prefix}api_user_sched",
            is_system=False,
        )
        async with _make_client_for_user(org_and_users["daemon"]) as client:
            resp = await client.put(
                f"/api/schedules/{sched['id']}",
                json={"description": "daemon update"},
            )
        assert resp.status_code == 200

    async def test_owner_can_update_system_schedule_api(
        self, db_pool, bi_prefix, org_and_users
    ):
        sched = await _create_schedule(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}api_sys_own",
            is_system=True,
        )
        async with _make_client_for_user(org_and_users["owner"]) as client:
            resp = await client.put(
                f"/api/schedules/{sched['id']}",
                json={"description": "owner update"},
            )
        assert resp.status_code == 200


# ============================================================================
# Error message tests
# ============================================================================


class TestProtectionErrorMessages:
    """Verify the error messages are informative and actionable."""

    async def test_builtin_error_message_mentions_on_disk(
        self, db_pool, bi_prefix, org_and_users
    ):
        agent = await _create_agent(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}msg_agent",
            scope="built-in",
        )
        repo = DefinitionRepository(db_pool)
        with pytest.raises(BuiltInProtectionError, match="on-disk source file"):
            await repo.update_agent(
                str(agent["id"]),
                str(org_and_users["org"]["id"]),
                requester_role="daemon",
                name="nope",
            )

    async def test_no_role_passes_through(
        self, db_pool, bi_prefix, org_and_users
    ):
        """When requester_role is None (legacy callers), the guard is not applied."""
        agent = await _create_agent(
            db_pool,
            org_and_users["org"]["id"],
            org_and_users["owner"]["id"],
            f"{bi_prefix}none_role_agent",
            scope="built-in",
        )
        repo = DefinitionRepository(db_pool)
        # Should not raise — None role defaults to "allow"
        result = await repo.update_agent(
            str(agent["id"]),
            str(org_and_users["org"]["id"]),
            requester_role=None,
            description="legacy-update",
        )
        assert result is not None
