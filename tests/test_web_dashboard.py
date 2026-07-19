"""Integration tests for dashboard web routes in web/routes.py."""

import re
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    create_session,
    set_user_password,
)
from lucent.db import MemoryRepository, OrganizationRepository, UserRepository

TEST_PASSWORD = "TestPass1"


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for web dashboard tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_webdash_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        # Clean up requests/tasks before memories. Goal-linked requests use
        # goal_memory_id + goal_milestone_index, so deleting goal memories first
        # can violate the request check constraint while FK actions run.
        await conn.execute(
            "DELETE FROM tasks WHERE request_id IN "
            "(SELECT id FROM requests WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM request_memories WHERE request_id IN "
            "(SELECT id FROM requests WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM requests WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM daemon_instances WHERE organization_id IN "
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
    """Create user + org with a password set for web dashboard tests."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{web_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{web_prefix}user",
        provider="basic",
        organization_id=org["id"],
        email=f"{web_prefix}user@test.com",
        display_name=f"{web_prefix}User",
    )
    await set_user_password(db_pool, user["id"], TEST_PASSWORD)
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def client(db_pool, web_user):
    """httpx client with session + CSRF cookies pre-set."""
    _user, _org, session_token = web_user
    csrf_token = "test-csrf-token-dash123"

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


# ============================================================================
# GET / (dashboard)
# ============================================================================


class TestDashboard:
    @pytest.mark.asyncio
    async def test_dashboard_returns_200(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Goals we're pursuing together" in resp.text
        assert "Admin attention queue" not in resp.text
        assert "Processing events" not in resp.text
        assert "Daemon Status" not in resp.text
        assert 'aria-label="Lucent chat"' in resp.text
        assert 'src="/static/lucent_logo_64.png"' in resp.text
        assert "Memory</span>" not in resp.text
        assert "Agency</span>" not in resp.text
        assert "AI Teammate" not in resp.text

    @pytest.mark.asyncio
    async def test_dashboard_shows_active_goal_progress(
        self, client, db_pool, web_user, web_prefix
    ):
        user, org, _token = web_user
        repo = MemoryRepository(db_pool)
        goal = await repo.create(
            username=f"{web_prefix}User",
            type="goal",
            content="Dashboard collaborative goal",
            metadata={
                "status": "active",
                "milestones": [
                    {"description": "Define dashboard goal view", "status": "completed"},
                    {"description": "Validate data accuracy", "status": "active"},
                    {"description": "Ship useful overview", "status": "active"},
                ],
            },
            tags=["dashboard", "goal"],
            importance=7,
            user_id=user["id"],
            organization_id=org["id"],
        )

        from lucent.db.requests import RequestRepository

        req_repo = RequestRepository(db_pool)
        await req_repo.create_request(
            title="Move dashboard goal forward",
            org_id=str(org["id"]),
            source="user",
            goal_id=str(goal["id"]),
            goal_milestone_index=2,
        )

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Dashboard collaborative goal" in resp.text
        assert "1 of 3 milestones complete" in resp.text
        assert "Next: Validate data accuracy" in resp.text
        assert "Current: Move dashboard goal forward" in resp.text

    @pytest.mark.asyncio
    async def test_dashboard_current_work_excludes_old_failed_requests(
        self, client, db_pool, web_user
    ):
        _user, org, _token = web_user
        from lucent.db.requests import RequestRepository

        req_repo = RequestRepository(db_pool)
        await req_repo.create_request(
            title="Open current dashboard work",
            org_id=str(org["id"]),
            source="user",
        )
        failed_req = await req_repo.create_request(
            title="Old failed dashboard work",
            org_id=str(org["id"]),
            source="user",
        )
        await req_repo.update_request_status(
            str(failed_req["id"]), "failed", org_id=str(org["id"])
        )

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Current Work" in resp.text
        assert "Open current dashboard work" in resp.text
        assert "Old failed dashboard work" not in resp.text

    @pytest.mark.asyncio
    async def test_dashboard_contains_user_info(self, client, web_prefix):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert f"{web_prefix}User" in resp.text

    @pytest.mark.asyncio
    async def test_dashboard_unauthenticated_redirects(self, db_pool):
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_dashboard_with_memories(self, client, db_pool, web_user, web_prefix):
        user, org, _token = web_user
        repo = MemoryRepository(db_pool)
        await repo.create(
            username=f"{web_prefix}User",
            type="experience",
            content="Dashboard test memory content",
            tags=["test", "dashboard"],
            importance=5,
            user_id=user["id"],
            organization_id=org["id"],
        )
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "1" in resp.text

    @pytest.mark.asyncio
    async def test_dashboard_shows_daemon_status_panel(self, client, db_pool, web_user, web_prefix):
        user, org, _token = web_user
        user_repo = UserRepository(db_pool)
        await user_repo.update_role(user["id"], "owner")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO daemon_instances
                   (instance_id, organization_id, hostname, pid, roles, status,
                    started_at, last_seen_at, metadata, created_at, updated_at)
                   VALUES ($1, $2::uuid, $3, $4, $5::text[], 'active',
                           NOW(), NOW(), $6::jsonb, NOW(), NOW())""",
                "daemon-test-instance",
                org["id"],
                "test-host",
                12345,
                ["dispatcher", "scheduler"],
                '{"cycle_count": 42}',
            )

        repo = MemoryRepository(db_pool)
        await repo.create(
            username=f"{web_prefix}User",
            type="procedural",
            content="## Daemon State\n\n### Cycle focus\n- Processing review queue",
            tags=["daemon", "daemon-state"],
            importance=5,
            user_id=user["id"],
            organization_id=org["id"],
        )

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Daemon Status" in resp.text
        assert "Online" in resp.text
        assert "Cycle count:</span> 42" in resp.text
        assert "Processing review queue" in resp.text

    @pytest.mark.asyncio
    async def test_dashboard_sidebar_shows_approval_badge(
        self, client, db_pool, web_user, web_prefix, monkeypatch
    ):
        user, org, _token = web_user
        other_user = await UserRepository(db_pool).create(
            external_id=f"{web_prefix}other_user",
            provider="basic",
            organization_id=org["id"],
            email=f"{web_prefix}other@test.com",
            display_name=f"{web_prefix}Other User",
        )

        # Each member has one pending request; the sidebar must count only the viewer's.
        monkeypatch.setenv("LUCENT_AUTO_APPROVE", "false")
        from lucent.db.requests import RequestRepository
        req_repo = RequestRepository(db_pool)
        await req_repo.create_request(
            title="Needs approval test",
            org_id=str(org["id"]),
            source="cognitive",
            created_by=str(user["id"]),
        )
        await req_repo.create_request(
            title="Other user's pending request",
            org_id=str(org["id"]),
            source="cognitive",
            created_by=str(other_user["id"]),
        )

        resp = await client.get("/")
        assert resp.status_code == 200
        badge = re.search(
            r'href="/activity"(?:(?!</a>).)*title="Requests awaiting approval"[^>]*>'
            r"\s*(\d+)\s*</span>",
            resp.text,
            re.S,
        )
        assert badge and badge.group(1) == "1"

    @pytest.mark.asyncio
    async def test_dashboard_owner_sees_admin_operations(
        self, client, db_pool, web_user, monkeypatch
    ):
        user, org, _token = web_user

        user_repo = UserRepository(db_pool)
        await user_repo.update_role(user["id"], "owner")

        # Create a request that needs owner/admin attention.
        monkeypatch.setenv("LUCENT_AUTO_APPROVE", "false")
        from lucent.db.requests import RequestRepository

        req_repo = RequestRepository(db_pool)
        await req_repo.create_request(
            title="Owner dashboard approval",
            org_id=str(org["id"]),
            source="cognitive",
        )
        cancelled_req = await req_repo.create_request(
            title="Cancelled dashboard approval",
            org_id=str(org["id"]),
            source="cognitive",
        )
        await req_repo.update_request_status(
            str(cancelled_req["id"]), "cancelled", org_id=str(org["id"])
        )

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Admin attention queue" in resp.text
        assert "Org operations" in resp.text
        assert "Definition proposals" in resp.text
        assert "Owner dashboard approval" in resp.text
        assert "Cancelled dashboard approval" not in resp.text
        assert re.search(r"Work approvals</p>\s*<p[^>]*>\s*1\s*</p>", resp.text)

    @pytest.mark.asyncio
    async def test_dashboard_filters_repo_tagged_memories_when_acl_denied(
        self, client, db_pool, web_user, web_prefix, monkeypatch
    ):
        user, org, _token = web_user
        repo = MemoryRepository(db_pool)
        await repo.create(
            username=f"{web_prefix}User",
            type="technical",
            content=f"{web_prefix}Hidden repo dashboard memory",
            tags=["dashboard", "acl"],
            metadata={"repo": "org/private-repo"},
            user_id=user["id"],
            organization_id=org["id"],
        )
        await repo.create(
            username=f"{web_prefix}User",
            type="experience",
            content=f"{web_prefix}Visible dashboard memory",
            tags=["dashboard", "acl"],
            user_id=user["id"],
            organization_id=org["id"],
        )

        async def _deny_access(self, user_id, repo_full_name):  # pragma: no cover - signature shim
            return False

        monkeypatch.setattr(
            "lucent.integrations.github_repo_access_service.GitHubRepoAccessService.check_access",
            _deny_access,
        )

        resp = await client.get("/")
        assert resp.status_code == 200
        assert f"{web_prefix}Visible dashboard memory" in resp.text
        assert f"{web_prefix}Hidden repo dashboard memory" not in resp.text
