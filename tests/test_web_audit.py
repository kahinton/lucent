"""Integration tests for GET /audit web route in web/routes.py.

Tests:
- Page loads for authenticated user (team mode)
- Pagination params (page, action_type filter)
- Unauthenticated redirect to /login
- Content rendering (audit entries, empty state, template elements)
- Non-team-mode returns 404

Uses real DB sessions + CSRF tokens through the full ASGI stack.
"""

from unittest.mock import patch
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
from lucent.db import AuditRepository, MemoryRepository, OrganizationRepository, UserRepository

TEST_PASSWORD = "TestPass1"


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for web audit tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_webaud_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
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
    """Create user + org with a password set for web audit tests."""
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
    csrf_token = "test-csrf-token-aud123"

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
        yield c


@pytest_asyncio.fixture
async def audit_entries(db_pool, web_user, web_prefix):
    """Create a memory with audit log entries for testing."""
    user, org, _token = web_user
    mem_repo = MemoryRepository(db_pool)
    audit_repo = AuditRepository(db_pool)

    memory = await mem_repo.create(
        username=f"{web_prefix}User",
        type="experience",
        content="Audit test memory",
        tags=["audit-test"],
        importance=5,
        user_id=user["id"],
        organization_id=org["id"],
    )

    await audit_repo.log(
        memory_id=memory["id"],
        action_type="create",
        user_id=user["id"],
        organization_id=org["id"],
        new_values={"content": "Audit test memory"},
        version=memory["version"],
    )

    updated = await mem_repo.update(
        memory_id=memory["id"],
        content="Updated audit test memory",
    )

    await audit_repo.log(
        memory_id=memory["id"],
        action_type="update",
        user_id=user["id"],
        organization_id=org["id"],
        changed_fields=["content"],
        old_values={"content": "Audit test memory"},
        new_values={"content": "Updated audit test memory"},
        version=updated["version"],
    )

    return memory


# ============================================================================
# GET /audit — page loads for authenticated user
# ============================================================================


class TestAuditPageLoad:
    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_audit_returns_200(_mock_tm, self, client):
        resp = await client.get("/audit")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_audit_contains_page_header(_mock_tm, self, client):
        resp = await client.get("/audit")
        assert "Audit Log" in resp.text

    @pytest.mark.asyncio
    async def test_audit_not_team_mode_returns_404(self, client):
        """Without team mode, GET /audit returns 404."""
        resp = await client.get("/audit")
        assert resp.status_code == 404


# ============================================================================
# GET /audit — unauthenticated redirect
# ============================================================================


class TestAuditUnauthenticated:
    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_unauthenticated_redirects_to_login(_mock_tm, self, db_pool):
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/audit", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")


# ============================================================================
# GET /audit — pagination params
# ============================================================================


class TestAuditPagination:
    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_page_param_accepted(_mock_tm, self, client):
        resp = await client.get("/audit", params={"page": "2"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_action_type_filter(_mock_tm, self, client, audit_entries):
        resp = await client.get("/audit", params={"action_type": "create"})
        assert resp.status_code == 200
        # The filter dropdown should show 'create' selected
        assert "Create" in resp.text or "create" in resp.text

    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_action_type_filter_update(_mock_tm, self, client, audit_entries):
        resp = await client.get("/audit", params={"action_type": "update"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_page_and_action_type_combined(_mock_tm, self, client):
        resp = await client.get(
            "/audit", params={"page": "1", "action_type": "delete"},
        )
        assert resp.status_code == 200


# ============================================================================
# GET /audit — content rendering
# ============================================================================


class TestAuditContentRendering:
    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_empty_state_shown_when_no_entries(_mock_tm, self, client):
        """With no audit entries, page shows empty state message."""
        resp = await client.get("/audit")
        assert resp.status_code == 200
        assert "No audit entries" in resp.text or "audit log is empty" in resp.text

    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_entries_rendered(_mock_tm, self, client, audit_entries):
        """With audit entries present, they appear on the page."""
        resp = await client.get("/audit")
        assert resp.status_code == 200
        # Should contain the memory ID (truncated) as a link
        memory_id_str = str(audit_entries["id"])
        assert memory_id_str[:8] in resp.text or memory_id_str[:12] in resp.text

    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_action_types_dropdown_rendered(_mock_tm, self, client):
        """Filter dropdown contains expected action type options."""
        resp = await client.get("/audit")
        assert resp.status_code == 200
        assert "All Actions" in resp.text

    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_entry_count_shown(_mock_tm, self, client, audit_entries):
        """Total entry count appears in the page."""
        resp = await client.get("/audit")
        assert resp.status_code == 200
        assert "entries" in resp.text

    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_create_entry_rendered_with_icon(_mock_tm, self, client, audit_entries):
        """Create action entry shows on the page."""
        resp = await client.get("/audit")
        assert resp.status_code == 200
        # The template renders action_type capitalized
        assert "Create" in resp.text or "create" in resp.text

    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_update_entry_shows_changed_fields(
        _mock_tm, self, client, audit_entries,
    ):
        """Update entries show changed fields."""
        resp = await client.get("/audit")
        assert resp.status_code == 200
        assert "content" in resp.text.lower()

    @pytest.mark.asyncio
    @patch("lucent.web.routes.is_team_mode", return_value=True)
    async def test_memory_link_present(_mock_tm, self, client, audit_entries):
        """Each entry links to the memory detail page."""
        resp = await client.get("/audit")
        assert resp.status_code == 200
        assert f"/memories/{audit_entries['id']}" in resp.text
