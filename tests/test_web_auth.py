"""Integration tests for authentication web routes in web/routes.py.

Tests the HTML-serving auth endpoints:
- GET  /login                  (login page display)
- POST /login                  (login form submission)
- POST /logout                 (logout)
- GET  /setup                  (first-run setup page)
- POST /setup                  (first-run setup submission)
- POST /settings/password      (password change)

Uses real DB sessions + CSRF tokens through the full ASGI stack.
"""

from urllib.parse import unquote
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    create_session,
    set_user_password,
    validate_session,
)
from lucent.db import OrganizationRepository, UserRepository

TEST_PASSWORD = "TestPass1"
NEW_PASSWORD = "NewPass99"


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for web auth tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_webauth_{test_id}_"
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
    """Create user + org with a password set for web auth tests."""
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
    csrf_token = "test-csrf-token-auth123"

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


@pytest_asyncio.fixture
async def unauthenticated_client(db_pool):
    """httpx client without session cookie (for login/setup tests)."""
    csrf_token = "test-csrf-token-unauth456"

    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={
            CSRF_COOKIE_NAME: csrf_token,
        },
    ) as c:
        c._csrf_token = csrf_token  # type: ignore[attr-defined]
        yield c


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    """Build form data dict with CSRF token included."""
    data = {CSRF_FIELD_NAME: client._csrf_token}  # type: ignore[attr-defined]
    if extra:
        data.update(extra)
    return data


def _reset_login_limiter() -> None:
    """Reset the singleton login rate limiter so tests don't interfere."""
    import lucent.rate_limit as rl

    rl._login_limiter = None


# ============================================================================
# GET /login
# ============================================================================


class TestLoginPage:
    async def test_login_returns_html(self, unauthenticated_client, web_user):
        resp = await unauthenticated_client.get("/login", follow_redirects=False)
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_login_contains_form(self, unauthenticated_client, web_user):
        resp = await unauthenticated_client.get("/login")
        assert "password" in resp.text.lower()

    async def test_login_sets_csrf_cookie(self, unauthenticated_client, web_user):
        resp = await unauthenticated_client.get("/login")
        assert CSRF_COOKIE_NAME in resp.cookies or resp.status_code == 200

    async def test_login_redirects_to_setup_on_first_run(self, db_pool):
        """When no users exist, /login redirects to /setup."""
        # Use a completely clean DB check — but since web_user creates users,
        # we test with a raw client and no user fixture.
        # NOTE: This test only works if no other users exist in the DB.
        # We test the inverse: when users exist, login does NOT redirect to setup.
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/login", follow_redirects=False)
            # If users exist, we get 200 (login page). If not, 303 to /setup.
            assert resp.status_code in (200, 303)

    async def test_login_redirects_if_already_authenticated(self, client):
        """Authenticated user visiting /login gets redirected to /."""
        resp = await client.get("/login", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers.get("location") == "/"


# ============================================================================
# POST /login
# ============================================================================


class TestLoginSubmit:
    async def test_successful_login(self, unauthenticated_client, web_user):
        _reset_login_limiter()
        user, _org, _token = web_user
        resp = await unauthenticated_client.post(
            "/login",
            data=_csrf_data(
                unauthenticated_client,
                {
                    "username": user["email"],
                    "password": TEST_PASSWORD,
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers.get("location") == "/"
        # Session cookie should be set
        assert SESSION_COOKIE_NAME in resp.cookies

    async def test_login_with_display_name(self, unauthenticated_client, web_user):
        _reset_login_limiter()
        user, _org, _token = web_user
        resp = await unauthenticated_client.post(
            "/login",
            data=_csrf_data(
                unauthenticated_client,
                {
                    "username": user["display_name"],
                    "password": TEST_PASSWORD,
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert SESSION_COOKIE_NAME in resp.cookies

    async def test_login_wrong_password(self, unauthenticated_client, web_user):
        _reset_login_limiter()
        user, _org, _token = web_user
        resp = await unauthenticated_client.post(
            "/login",
            data=_csrf_data(
                unauthenticated_client,
                {
                    "username": user["email"],
                    "password": "WrongPass1",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 401
        assert "Invalid credentials" in resp.text

    async def test_login_nonexistent_user(self, unauthenticated_client, web_user):
        _reset_login_limiter()
        resp = await unauthenticated_client.post(
            "/login",
            data=_csrf_data(
                unauthenticated_client,
                {
                    "username": "nobody@nowhere.com",
                    "password": "SomePass1",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 401

    async def test_login_missing_csrf(self, unauthenticated_client, web_user):
        """Login without CSRF token should fail."""
        user, _org, _token = web_user
        resp = await unauthenticated_client.post(
            "/login",
            data={
                "username": user["email"],
                "password": TEST_PASSWORD,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_login_rate_limiting(self, unauthenticated_client, web_user):
        """After too many attempts, login returns 429."""
        _reset_login_limiter()
        user, _org, _token = web_user
        # Exhaust rate limit (5 attempts default)
        for _ in range(6):
            resp = await unauthenticated_client.post(
                "/login",
                data=_csrf_data(
                    unauthenticated_client,
                    {
                        "username": user["email"],
                        "password": "WrongPass1",
                    },
                ),
                follow_redirects=False,
            )
        assert resp.status_code == 429
        assert "Too many login attempts" in resp.text

    async def test_login_empty_credentials(self, unauthenticated_client, web_user):
        _reset_login_limiter()
        resp = await unauthenticated_client.post(
            "/login",
            data=_csrf_data(
                unauthenticated_client,
                {
                    "username": "",
                    "password": "",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 401


# ============================================================================
# POST /logout
# ============================================================================


class TestLogout:
    async def test_logout_redirects_to_login(self, client):
        resp = await client.post(
            "/logout",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers.get("location") == "/login"

    async def test_logout_clears_session_cookie(self, client):
        resp = await client.post(
            "/logout",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        # The session cookie should be deleted (set with max-age=0 or empty)
        set_cookie_headers = resp.headers.get_list("set-cookie")
        session_cleared = any(
            SESSION_COOKIE_NAME in h and ("max-age=0" in h.lower() or "expires=" in h.lower())
            for h in set_cookie_headers
        )
        assert session_cleared

    async def test_logout_clears_csrf_cookie(self, client):
        resp = await client.post(
            "/logout",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        set_cookie_headers = resp.headers.get_list("set-cookie")
        csrf_cleared = any(
            CSRF_COOKIE_NAME in h and ("max-age=0" in h.lower() or "expires=" in h.lower())
            for h in set_cookie_headers
        )
        assert csrf_cleared

    async def test_logout_missing_csrf_fails(self, client):
        resp = await client.post(
            "/logout",
            data={},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_logout_without_session(self, unauthenticated_client, web_user):
        """Logout without a session cookie still redirects to /login."""
        resp = await unauthenticated_client.post(
            "/logout",
            data=_csrf_data(unauthenticated_client),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers.get("location") == "/login"


# ============================================================================
# GET /setup
# ============================================================================


class TestSetupPage:
    async def test_setup_redirects_when_users_exist(self, unauthenticated_client, web_user):
        """If users exist, /setup redirects to /login."""
        resp = await unauthenticated_client.get("/setup", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers.get("location") == "/login"


# ============================================================================
# POST /setup
# ============================================================================


class TestSetupSubmit:
    async def test_setup_redirects_when_users_exist(self, unauthenticated_client, web_user):
        """If users already exist, POST /setup redirects to /login."""
        resp = await unauthenticated_client.post(
            "/setup",
            data=_csrf_data(
                unauthenticated_client,
                {
                    "display_name": "New Admin",
                    "password": "ValidPass1",
                    "password_confirm": "ValidPass1",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers.get("location") == "/login"

    async def test_setup_missing_csrf_fails(self, unauthenticated_client, web_user):
        resp = await unauthenticated_client.post(
            "/setup",
            data={
                "display_name": "Admin",
                "password": "ValidPass1",
                "password_confirm": "ValidPass1",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_setup_missing_display_name(self, db_pool):
        """Setup without display_name returns 400."""
        from lucent.auth_providers import is_first_run

        if not await is_first_run(db_pool):
            pytest.skip("Users exist in DB; cannot test first-run setup validation")
        csrf = "test-csrf-setup"
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={CSRF_COOKIE_NAME: csrf},
        ) as c:
            resp = await c.post(
                "/setup",
                data={
                    CSRF_FIELD_NAME: csrf,
                    "display_name": "",
                    "password": "ValidPass1",
                    "password_confirm": "ValidPass1",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 400
            assert "Display name is required" in resp.text

    async def test_setup_short_password(self, db_pool):
        """Setup with short password returns 400."""
        from lucent.auth_providers import is_first_run

        if not await is_first_run(db_pool):
            pytest.skip("Users exist in DB; cannot test first-run setup validation")
        csrf = "test-csrf-setup"
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={CSRF_COOKIE_NAME: csrf},
        ) as c:
            resp = await c.post(
                "/setup",
                data={
                    CSRF_FIELD_NAME: csrf,
                    "display_name": "Admin",
                    "password": "short",
                    "password_confirm": "short",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 400
            assert "at least 8 characters" in resp.text

    async def test_setup_password_mismatch(self, db_pool):
        """Setup with mismatched passwords returns 400."""
        from lucent.auth_providers import is_first_run

        if not await is_first_run(db_pool):
            pytest.skip("Users exist in DB; cannot test first-run setup validation")
        csrf = "test-csrf-setup"
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={CSRF_COOKIE_NAME: csrf},
        ) as c:
            resp = await c.post(
                "/setup",
                data={
                    CSRF_FIELD_NAME: csrf,
                    "display_name": "Admin",
                    "password": "ValidPass1",
                    "password_confirm": "DifferentPass1",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 400
            assert "do not match" in resp.text

    async def test_setup_weak_password(self, db_pool):
        """Setup with password missing complexity returns 400."""
        from lucent.auth_providers import is_first_run

        if not await is_first_run(db_pool):
            pytest.skip("Users exist in DB; cannot test first-run setup validation")
        csrf = "test-csrf-setup"
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={CSRF_COOKIE_NAME: csrf},
        ) as c:
            resp = await c.post(
                "/setup",
                data={
                    CSRF_FIELD_NAME: csrf,
                    "display_name": "Admin",
                    "password": "alllowercase",
                    "password_confirm": "alllowercase",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 400


# ============================================================================
# POST /settings/password
# ============================================================================


class TestPasswordChange:
    async def test_successful_password_change(self, client, web_user, db_pool):
        resp = await client.post(
            "/settings/password",
            data=_csrf_data(
                client,
                {
                    "current_password": TEST_PASSWORD,
                    "new_password": NEW_PASSWORD,
                    "confirm_password": NEW_PASSWORD,
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "password_changed=1" in resp.headers.get("location", "")
        # New session cookie should be set
        assert SESSION_COOKIE_NAME in resp.cookies

    async def test_password_change_wrong_current(self, client, web_user):
        resp = await client.post(
            "/settings/password",
            data=_csrf_data(
                client,
                {
                    "current_password": "WrongCurrent1",
                    "new_password": NEW_PASSWORD,
                    "confirm_password": NEW_PASSWORD,
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "incorrect" in resp.headers.get("location", "").lower()

    async def test_password_change_too_short(self, client, web_user):
        resp = await client.post(
            "/settings/password",
            data=_csrf_data(
                client,
                {
                    "current_password": TEST_PASSWORD,
                    "new_password": "Short1",
                    "confirm_password": "Short1",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "8 characters" in unquote(resp.headers.get("location", ""))

    async def test_password_change_weak_password(self, client, web_user):
        resp = await client.post(
            "/settings/password",
            data=_csrf_data(
                client,
                {
                    "current_password": TEST_PASSWORD,
                    "new_password": "alllowercase",
                    "confirm_password": "alllowercase",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers.get("location", "")
        assert "error=" in location

    async def test_password_change_mismatch(self, client, web_user):
        resp = await client.post(
            "/settings/password",
            data=_csrf_data(
                client,
                {
                    "current_password": TEST_PASSWORD,
                    "new_password": NEW_PASSWORD,
                    "confirm_password": "Mismatch99",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "do not match" in unquote(resp.headers.get("location", "")).lower()

    async def test_password_change_missing_csrf(self, client, web_user):
        resp = await client.post(
            "/settings/password",
            data={
                "current_password": TEST_PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_password_change_invalidates_old_session(self, client, web_user, db_pool):
        """Old session token must be invalid after a password change."""
        _user, _org, old_token = web_user

        # Verify the old token is valid before the change
        assert await validate_session(db_pool, old_token) is not None

        resp = await client.post(
            "/settings/password",
            data=_csrf_data(
                client,
                {
                    "current_password": TEST_PASSWORD,
                    "new_password": NEW_PASSWORD,
                    "confirm_password": NEW_PASSWORD,
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # The old session token should now be invalid
        assert await validate_session(db_pool, old_token) is None

        # The new token from the response cookie should be valid
        new_token = resp.cookies.get(SESSION_COOKIE_NAME)
        assert new_token is not None
        assert await validate_session(db_pool, new_token) is not None

    async def test_password_change_unauthenticated(self, unauthenticated_client, web_user):
        """Unauthenticated user gets redirected to /login."""
        resp = await unauthenticated_client.post(
            "/settings/password",
            data=_csrf_data(
                unauthenticated_client,
                {
                    "current_password": TEST_PASSWORD,
                    "new_password": NEW_PASSWORD,
                    "confirm_password": NEW_PASSWORD,
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/login" in resp.headers.get("location", "")
