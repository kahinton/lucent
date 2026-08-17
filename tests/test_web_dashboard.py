"""Integration tests for the operational overview on the chat home page."""

import re
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.auth_providers import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, create_session
from lucent.db import MemoryRepository, OrganizationRepository, UserRepository


@pytest_asyncio.fixture
async def web_prefix(db_pool, delete_test_organizations):
    test_id = str(uuid4())[:8]
    prefix = f"test_webhome_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await delete_test_organizations(conn, [f"{prefix}%"])


@pytest_asyncio.fixture
async def web_user(db_pool, web_prefix):
    org = await OrganizationRepository(db_pool).create(name=f"{web_prefix}org")
    user = await UserRepository(db_pool).create(
        external_id=f"{web_prefix}user",
        provider="basic",
        organization_id=org["id"],
        email=f"{web_prefix}user@test.com",
        display_name=f"{web_prefix}User",
    )
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def client(web_user):
    _user, _org, session_token = web_user
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={
            SESSION_COOKIE_NAME: session_token,
            CSRF_COOKIE_NAME: "test-csrf-token-home123",
        },
    ) as test_client:
        yield test_client


class TestChatHome:
    @pytest.mark.asyncio
    async def test_root_redirects_to_chat(self, client):
        response = await client.get("/", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/chat"

    @pytest.mark.asyncio
    async def test_root_requires_authentication(self, db_pool):
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    @pytest.mark.asyncio
    async def test_chat_preserves_controls_and_shows_empty_overview(self, client):
        response = await client.get("/chat")

        assert response.status_code == 200
        assert 'id="chat-input"' in response.text
        assert 'id="agent-select"' in response.text
        assert 'id="model-select"' in response.text
        assert 'id="session-menu-btn"' in response.text
        assert "Active goals" in response.text
        assert "No active goals." in response.text
        assert "Lucent offline" in response.text
        assert re.search(
            r'title="Show requests awaiting approval"[^>]*>.*?<span>0</span>',
            response.text,
            re.S,
        )
        assert 'data-overview-trigger="goals"' in response.text
        assert 'id="chat-overview-panel"' in response.text
        assert 'aria-label="Dashboard"' not in response.text

    @pytest.mark.asyncio
    async def test_chat_shows_scoped_approvals_active_goal_and_online_status(
        self, client, db_pool, web_user, web_prefix, monkeypatch
    ):
        user, org, _token = web_user
        other_user = await UserRepository(db_pool).create(
            external_id=f"{web_prefix}other",
            provider="basic",
            organization_id=org["id"],
            email=f"{web_prefix}other@test.com",
            display_name=f"{web_prefix}Other",
        )
        goal = await MemoryRepository(db_pool).create(
            username=f"{web_prefix}User",
            type="goal",
            content="Ship the compact chat home",
            metadata={
                "status": "active",
                "milestones": [
                    {"description": "Build overview", "status": "completed"},
                    {"description": "Validate overview", "status": "active"},
                ],
            },
            user_id=user["id"],
            organization_id=org["id"],
        )

        monkeypatch.setenv("LUCENT_AUTO_APPROVE", "false")
        from lucent.db.requests import RequestRepository

        request_repo = RequestRepository(db_pool)
        await request_repo.create_request(
            title="Viewer approval",
            description="**Review** the proposed implementation before work starts.",
            org_id=str(org["id"]),
            source="cognitive",
            created_by=str(user["id"]),
            goal_id=str(goal["id"]),
            goal_milestone_index=2,
        )
        await request_repo.create_request(
            title="Other approval",
            org_id=str(org["id"]),
            source="cognitive",
            created_by=str(other_user["id"]),
        )
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO daemon_instances
                   (instance_id, organization_id, hostname, pid, roles, status,
                    started_at, last_seen_at, metadata, created_at, updated_at)
                   VALUES ($1, $2::uuid, 'test-host', 1234, '{}', 'active',
                           NOW(), NOW(), '{}', NOW(), NOW())""",
                f"{web_prefix}daemon",
                org["id"],
            )

        response = await client.get("/chat")

        assert response.status_code == 200
        assert "Ship the compact chat home" in response.text
        assert "Viewer approval" in response.text
        assert "**Review** the proposed implementation" in response.text
        assert "data-markdown-source" in response.text
        assert "Lucent online" in response.text
        assert re.search(
            r'title="Show requests awaiting approval"[^>]*>.*?<span>1</span>',
            response.text,
            re.S,
        )
        assert "Other approval" not in response.text
