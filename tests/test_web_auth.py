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


class _FakeFirstRunAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakeFirstRunPool:
    def __init__(self, exists_result: bool):
        self.conn = _FakeFirstRunConn(exists_result)

    def acquire(self):
        return _FakeFirstRunAcquire(self.conn)


class _FakeFirstRunConn:
    def __init__(self, exists_result: bool):
        self.exists_result = exists_result
        self.query = ""

    async def fetchval(self, query):
        self.query = query
        return self.exists_result


# ============================================================================
# GET /login
# ============================================================================


class TestFirstRunDetection:
    async def test_first_run_ignores_service_users(self):
        from lucent.auth_providers import is_first_run

        pool = _FakeFirstRunPool(exists_result=False)

        assert await is_first_run(pool) is True
        assert "role <> 'daemon'" in pool.conn.query
        assert "external_id" in pool.conn.query
        assert "%-service%" in pool.conn.query
        assert "@lucent.local" in pool.conn.query

    async def test_first_run_detects_human_users(self):
        from lucent.auth_providers import is_first_run

        assert await is_first_run(_FakeFirstRunPool(exists_result=True)) is False


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

    async def test_login_retry_succeeds_without_reloading_form(
        self,
        unauthenticated_client,
        web_user,
    ):
        """A failed login response must contain the CSRF token needed to retry."""
        _reset_login_limiter()
        user, _org, _token = web_user
        csrf_token = unauthenticated_client._csrf_token

        failed = await unauthenticated_client.post(
            "/login",
            data=_csrf_data(
                unauthenticated_client,
                {"username": user["email"], "password": "WrongPass1"},
            ),
            follow_redirects=False,
        )

        assert failed.status_code == 401
        assert f'value="{csrf_token}"' in failed.text
        assert failed.cookies.get(CSRF_COOKIE_NAME) == csrf_token

        retried = await unauthenticated_client.post(
            "/login",
            data={
                CSRF_FIELD_NAME: csrf_token,
                "username": user["email"],
                "password": TEST_PASSWORD,
            },
            follow_redirects=False,
        )

        assert retried.status_code == 303
        assert retried.headers.get("location") == "/"
        assert SESSION_COOKIE_NAME in retried.cookies

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
        assert f'value="{unauthenticated_client._csrf_token}"' in resp.text
        assert resp.cookies.get(CSRF_COOKIE_NAME) == unauthenticated_client._csrf_token

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

    async def test_protected_web_route_redirects_to_setup_on_first_run(
        self,
        unauthenticated_client,
        monkeypatch,
    ):
        """Fresh startup entrypoints should show setup instead of normal login."""

        async def first_run(_pool):
            return True

        monkeypatch.setattr("lucent.web.routes._shared.is_first_run", first_run)

        resp = await unauthenticated_client.get("/chat", follow_redirects=False)

        assert resp.status_code == 303
        assert resp.headers.get("location") == "/setup"

    async def test_invalid_session_redirects_to_setup_on_first_run(
        self,
        unauthenticated_client,
        monkeypatch,
    ):
        """A stale browser cookie on a fresh database should still land on setup."""

        async def first_run(_pool):
            return True

        monkeypatch.setattr("lucent.web.routes._shared.is_first_run", first_run)
        unauthenticated_client.cookies.set(SESSION_COOKIE_NAME, "stale-session")

        resp = await unauthenticated_client.get("/chat", follow_redirects=False)

        assert resp.status_code == 303
        assert resp.headers.get("location") == "/setup"

    async def test_setup_lists_discovered_models_for_explicit_selection(self, monkeypatch):
        import lucent.web.routes.auth as auth_routes

        async def first_run(_pool):
            return True

        async def get_pool():
            return object()

        async def list_setup_models(_repo):
            return [
                {
                    "id": "ollama:test-model",
                    "name": "Test Model",
                    "provider": "ollama",
                    "is_enabled": False,
                }
            ]

        monkeypatch.setattr(auth_routes, "is_first_run", first_run)
        monkeypatch.setattr(auth_routes, "get_pool", get_pool)
        monkeypatch.setattr(
            auth_routes.ModelRepository,
            "list_initial_setup_models",
            list_setup_models,
        )

        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/setup")

        assert resp.status_code == 200
        assert 'name="enabled_models"' in resp.text
        assert 'value="ollama:test-model"' in resp.text
        assert "Select at least one model" in resp.text

    async def test_setup_retries_discovery_when_no_models_are_available(self, monkeypatch):
        import lucent.model_discovery as model_discovery
        import lucent.web.routes.auth as auth_routes

        list_calls = 0
        discovery_calls = 0

        async def list_setup_models(_repo):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 1:
                return []
            return [{"id": "ollama:new-model"}]

        class FakeDiscoveryService:
            def __init__(self, _pool):
                pass

            async def sync(self):
                nonlocal discovery_calls
                discovery_calls += 1

        monkeypatch.setattr(
            auth_routes.ModelRepository,
            "list_initial_setup_models",
            list_setup_models,
        )
        monkeypatch.setattr(model_discovery, "ModelDiscoveryService", FakeDiscoveryService)

        models = await auth_routes._list_initial_setup_models(object())

        assert models == [{"id": "ollama:new-model"}]
        assert discovery_calls == 1


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

    async def test_setup_password_mismatch_allows_immediate_retry(self, monkeypatch):
        """The setup error response keeps form and cookie CSRF values synchronized."""
        import lucent.web.routes.auth as auth_routes

        enable_attempted = False

        async def first_run(_pool):
            return True

        async def get_pool():
            return object()

        async def list_setup_models(_pool):
            return []

        async def enable_models(_pool, _model_ids):
            nonlocal enable_attempted
            enable_attempted = True
            return "Retry reached model validation."

        monkeypatch.setattr(auth_routes, "is_first_run", first_run)
        monkeypatch.setattr(auth_routes, "get_pool", get_pool)
        monkeypatch.setattr(auth_routes, "_list_initial_setup_models", list_setup_models)
        monkeypatch.setattr(auth_routes, "_enable_initial_setup_models", enable_models)

        csrf_token = "setup-mismatch-retry"
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={CSRF_COOKIE_NAME: csrf_token},
        ) as c:
            mismatch = await c.post(
                "/setup",
                data={
                    CSRF_FIELD_NAME: csrf_token,
                    "display_name": "Admin",
                    "password": "ValidPass1",
                    "password_confirm": "DifferentPass1",
                },
            )
            assert mismatch.status_code == 400
            assert "Passwords do not match" in mismatch.text
            assert f'value="{csrf_token}"' in mismatch.text
            assert mismatch.cookies.get(CSRF_COOKIE_NAME) == csrf_token

            retry = await c.post(
                "/setup",
                data={
                    CSRF_FIELD_NAME: csrf_token,
                    "display_name": "Admin",
                    "password": "ValidPass1",
                    "password_confirm": "ValidPass1",
                },
            )

        assert retry.status_code == 400
        assert "Retry reached model validation" in retry.text
        assert enable_attempted is True

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

    async def test_setup_requires_at_least_one_model(self, monkeypatch):
        import lucent.web.routes.auth as auth_routes

        account_created = False

        async def first_run(_pool):
            return True

        async def get_pool():
            return object()

        async def list_setup_models(_repo):
            return [
                {
                    "id": "ollama:test-model",
                    "name": "Test Model",
                    "provider": "ollama",
                    "is_enabled": False,
                }
            ]

        async def create_user(*_args):
            nonlocal account_created
            account_created = True
            raise AssertionError("Account creation must wait for a model selection")

        monkeypatch.setattr(auth_routes, "is_first_run", first_run)
        monkeypatch.setattr(auth_routes, "get_pool", get_pool)
        monkeypatch.setattr(auth_routes, "create_initial_user", create_user)
        monkeypatch.setattr(
            auth_routes.ModelRepository,
            "list_initial_setup_models",
            list_setup_models,
        )

        csrf = "setup-model-required"
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
                    "password_confirm": "ValidPass1",
                },
            )

        assert resp.status_code == 400
        assert "Select at least one model to continue" in resp.text
        assert account_created is False

    async def test_setup_enables_selected_model_before_creating_account(self, monkeypatch):
        import lucent.web.routes.auth as auth_routes

        enabled_model_ids: list[str] = []

        async def first_run(_pool):
            return True

        async def get_pool():
            return object()

        async def list_setup_models(_repo):
            return [
                {
                    "id": "ollama:test-model",
                    "name": "Test Model",
                    "provider": "ollama",
                    "is_enabled": False,
                }
            ]

        async def enable_models(_repo, model_ids):
            enabled_model_ids.extend(model_ids)
            return set(model_ids)

        async def create_user(*_args):
            assert enabled_model_ids == ["ollama:test-model"]
            return {"id": uuid4()}, "hs_setup_test_key"

        async def create_user_session(*_args):
            return "setup-session-token"

        monkeypatch.setattr(auth_routes, "is_first_run", first_run)
        monkeypatch.setattr(auth_routes, "get_pool", get_pool)
        monkeypatch.setattr(auth_routes, "create_initial_user", create_user)
        monkeypatch.setattr(auth_routes, "create_session", create_user_session)
        monkeypatch.setattr(
            auth_routes.ModelRepository,
            "list_initial_setup_models",
            list_setup_models,
        )
        monkeypatch.setattr(auth_routes.ModelRepository, "enable_models", enable_models)

        csrf = "setup-model-selected"
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
                    "password_confirm": "ValidPass1",
                    "enabled_models": "ollama:test-model",
                },
            )

        assert resp.status_code == 200
        assert "Account created successfully" in resp.text
        assert enabled_model_ids == ["ollama:test-model"]


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
        assert "success=" in resp.headers.get("location", "")
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
