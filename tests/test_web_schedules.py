"""Integration tests for schedule web routes in web/routes.py.

Tests the HTML-serving endpoints:
- GET  /schedules              (list with filtering)
- GET  /schedules/{id}         (detail page)
- POST /schedules/{id}/toggle  (enable/disable)
- POST /schedules/{id}/delete  (remove schedule)
- POST /schedules/{id}/edit    (update fields)

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
from lucent.db.schedules import ScheduleRepository

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for web schedule tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_websched_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        # Clean schedules by org
        await conn.execute(
            "DELETE FROM schedules WHERE organization_id IN "
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
    )
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def client(db_pool, web_user):
    """httpx client with session + CSRF cookies pre-set."""
    _user, _org, session_token = web_user
    csrf_token = "test-csrf-token-abc123"

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
        # Stash CSRF token for POST helpers
        c._csrf_token = csrf_token  # type: ignore[attr-defined]
        yield c


@pytest_asyncio.fixture
async def schedule(db_pool, web_user):
    """Create a test schedule and return it."""
    _user, org, _token = web_user
    repo = ScheduleRepository(db_pool)
    return await repo.create_schedule(
        title="Web Test Schedule",
        org_id=str(org["id"]),
        schedule_type="interval",
        interval_seconds=3600,
        description="Hourly web test",
        agent_type="code",
    )


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    """Build form data dict with CSRF token included."""
    data = {CSRF_FIELD_NAME: client._csrf_token}  # type: ignore[attr-defined]
    if extra:
        data.update(extra)
    return data


# ============================================================================
# GET /schedules — list
# ============================================================================


class TestSchedulesList:
    async def test_list_returns_html(self, client, schedule):
        resp = await client.get("/schedules")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_list_contains_schedule_title(self, client, schedule):
        resp = await client.get("/schedules")
        assert "Web Test Schedule" in resp.text

    async def test_list_filter_by_status(self, client, schedule):
        resp = await client.get("/schedules", params={"status": "active"})
        assert resp.status_code == 200

    async def test_list_filter_by_enabled(self, client, schedule):
        resp = await client.get("/schedules", params={"enabled": "true"})
        assert resp.status_code == 200

    async def test_list_unauthenticated_redirects(self, db_pool):
        """No session cookie → redirect to login."""
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/schedules", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")


# ============================================================================
# GET /schedules/{id} — detail
# ============================================================================


class TestScheduleDetail:
    async def test_detail_returns_html(self, client, schedule):
        resp = await client.get(f"/schedules/{schedule['id']}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_detail_contains_title(self, client, schedule):
        resp = await client.get(f"/schedules/{schedule['id']}")
        assert "Web Test Schedule" in resp.text

    async def test_detail_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.get(f"/schedules/{fake_id}")
        assert resp.status_code == 404


# ============================================================================
# POST /schedules/{id}/toggle
# ============================================================================


class TestScheduleToggle:
    async def test_toggle_disables(self, client, schedule):
        """Toggle an enabled schedule → disabled, redirects."""
        resp = await client.post(
            f"/schedules/{schedule['id']}/toggle",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_toggle_changes_state(self, client, schedule, db_pool, web_user):
        """After toggling, enabled state flips."""
        _user, org, _token = web_user
        repo = ScheduleRepository(db_pool)

        before = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert before is not None
        was_enabled = before["enabled"]

        await client.post(
            f"/schedules/{schedule['id']}/toggle",
            data=_csrf_data(client),
            follow_redirects=False,
        )

        after = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert after is not None
        assert after["enabled"] is not was_enabled

    async def test_toggle_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.post(
            f"/schedules/{fake_id}/toggle",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    async def test_toggle_no_csrf_fails(self, client, schedule):
        """Missing CSRF token → 403."""
        resp = await client.post(
            f"/schedules/{schedule['id']}/toggle",
            follow_redirects=False,
        )
        assert resp.status_code == 403


# ============================================================================
# POST /schedules/{id}/delete
# ============================================================================


class TestScheduleDelete:
    async def test_delete_redirects(self, client, schedule):
        resp = await client.post(
            f"/schedules/{schedule['id']}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/schedules" in resp.headers.get("location", "")

    async def test_delete_removes_from_db(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        repo = ScheduleRepository(db_pool)

        await client.post(
            f"/schedules/{schedule['id']}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )

        result = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert result is None

    async def test_delete_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.post(
            f"/schedules/{fake_id}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    async def test_delete_no_csrf_fails(self, client, schedule):
        resp = await client.post(
            f"/schedules/{schedule['id']}/delete",
            follow_redirects=False,
        )
        assert resp.status_code == 403


# ============================================================================
# POST /schedules/{id}/edit
# ============================================================================


class TestScheduleEdit:
    async def test_edit_title(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        resp = await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client, {"title": "Updated Title"}),
            follow_redirects=False,
        )
        assert resp.status_code == 303

        repo = ScheduleRepository(db_pool)
        updated = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert updated is not None
        assert updated["title"] == "Updated Title"

    async def test_edit_description(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client, {"description": "New description"}),
            follow_redirects=False,
        )
        repo = ScheduleRepository(db_pool)
        updated = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert updated is not None
        assert updated["description"] == "New description"

    async def test_edit_agent_type(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client, {"agent_type": "research"}),
            follow_redirects=False,
        )
        repo = ScheduleRepository(db_pool)
        updated = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert updated is not None
        assert updated["agent_type"] == "research"

    async def test_edit_prompt(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client, {"prompt": "Do weekly review"}),
            follow_redirects=False,
        )
        repo = ScheduleRepository(db_pool)
        updated = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert updated is not None
        assert updated["prompt"] == "Do weekly review"

    async def test_edit_interval_seconds(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client, {"interval_seconds": "7200"}),
            follow_redirects=False,
        )
        repo = ScheduleRepository(db_pool)
        updated = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert updated is not None
        assert updated["interval_seconds"] == 7200

    async def test_edit_invalid_interval_ignored(self, client, schedule, db_pool, web_user):
        """Non-numeric interval_seconds is silently ignored."""
        _user, org, _token = web_user
        await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client, {"interval_seconds": "not-a-number"}),
            follow_redirects=False,
        )
        repo = ScheduleRepository(db_pool)
        updated = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert updated is not None
        assert updated["interval_seconds"] == 3600  # unchanged

    async def test_edit_no_changes(self, client, schedule):
        """Submitting form with no actual changes still redirects OK."""
        resp = await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_edit_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.post(
            f"/schedules/{fake_id}/edit",
            data=_csrf_data(client, {"title": "Ghost"}),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    async def test_edit_no_csrf_fails(self, client, schedule):
        resp = await client.post(
            f"/schedules/{schedule['id']}/edit",
            follow_redirects=False,
        )
        assert resp.status_code == 403
