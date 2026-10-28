"""Unit tests for the standalone ops console.

The console is a pure HTTP client with no database access, so these tests use
``httpx.MockTransport`` to stand in for the Lucent API. That keeps them fast and,
more importantly, lets each test state exactly what the server returns — which is
what the console's behaviour is defined by.
"""

from __future__ import annotations

import httpx
import pytest

from ops_console.client import DEFAULT_TIMEOUT, LucentAPIError, LucentClient
from ops_console.config import ConfigError, load_config
from ops_console.session import (
    issue_session,
    new_csrf_token,
    session_is_valid,
)

BASE = "http://lucent.test"


def _client(handler) -> LucentClient:
    return LucentClient(
        BASE,
        "hs_test_key",
        transport=httpx.MockTransport(handler),
    )


class TestConfig:
    def test_missing_api_key_fails_loudly(self):
        with pytest.raises(ConfigError) as exc:
            load_config({"LUCENT_OPS_CONSOLE_TOKEN": "pass"})
        assert "LUCENT_OPS_API_KEY" in str(exc.value)

    def test_missing_console_token_fails_loudly(self):
        with pytest.raises(ConfigError) as exc:
            load_config({"LUCENT_OPS_API_KEY": "hs_x"})
        assert "LUCENT_OPS_CONSOLE_TOKEN" in str(exc.value)

    def test_defaults_bind_to_loopback(self):
        config = load_config(
            {"LUCENT_OPS_API_KEY": "hs_x", "LUCENT_OPS_CONSOLE_TOKEN": "pass"}
        )
        assert config.host == "127.0.0.1"
        assert config.port == 8767
        assert config.secure_cookies is False

    def test_env_overrides(self):
        config = load_config(
            {
                "LUCENT_OPS_API_KEY": "hs_x",
                "LUCENT_OPS_CONSOLE_TOKEN": "pass",
                "LUCENT_OPS_BASE_URL": "http://lucent:8766/",
                "LUCENT_OPS_PORT": "9999",
                "LUCENT_OPS_SECURE_COOKIES": "true",
            }
        )
        assert config.api_root == "http://lucent:8766"
        assert config.port == 9999
        assert config.secure_cookies is True

    def test_signing_secret_is_generated_when_absent(self):
        config = load_config(
            {"LUCENT_OPS_API_KEY": "hs_x", "LUCENT_OPS_CONSOLE_TOKEN": "pass"}
        )
        assert config.signing_secret


class TestSession:
    def test_round_trip(self):
        token = issue_session("secret", hours=1)
        assert session_is_valid("secret", token)

    def test_wrong_secret_rejected(self):
        token = issue_session("secret", hours=1)
        assert not session_is_valid("other-secret", token)

    def test_expired_session_rejected(self):
        assert not session_is_valid("secret", "1.deadbeef")

    def test_garbage_rejected(self):
        assert not session_is_valid("secret", None)
        assert not session_is_valid("secret", "")
        assert not session_is_valid("secret", "no-dot")

    def test_csrf_tokens_are_unique_and_long(self):
        first, second = new_csrf_token(), new_csrf_token()
        assert first != second
        assert len(first) > 16


class TestLucentClient:
    async def test_health_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/health"
            return httpx.Response(200, json={"status": "healthy"})

        assert (await _client(handler).health())["status"] == "healthy"

    async def test_failure_raises_with_server_detail(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"detail": "Requires hyperadmin role or higher"})

        with pytest.raises(LucentAPIError) as exc:
            await _client(handler).list_organizations()
        assert exc.value.status_code == 403
        assert exc.value.is_auth_error
        assert "hyperadmin" in exc.value.detail

    async def test_validation_error_list_is_flattened(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                422,
                json={
                    "detail": [
                        {"loc": ["body", "name"], "msg": "field required"},
                        {"loc": ["body", "first_admin", "role"], "msg": "invalid"},
                    ]
                },
            )

        with pytest.raises(LucentAPIError) as exc:
            await _client(handler).provision_organization(
                name="x", admin_email="a@b.test"
            )
        assert "name: field required" in exc.value.detail
        assert "first_admin.role: invalid" in exc.value.detail

    async def test_unreachable_server_is_a_client_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with pytest.raises(LucentAPIError) as exc:
            await _client(handler).health()
        assert exc.value.status_code == 0
        assert "Cannot reach Lucent" in exc.value.detail

    async def test_bearer_header_is_sent(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"id": "u1", "role": "hyperadmin"})

        await _client(handler).me()
        assert seen["auth"] == "Bearer hs_test_key"

    async def test_provision_sends_first_admin(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "organization": {"id": "o1", "name": "Acme"},
                    "user": {"id": "u1"},
                    "api_key": "hs_new",
                },
            )

        result = await _client(handler).provision_organization(
            name="Acme", admin_email="owner@acme.test", admin_display_name="Owner"
        )
        assert captured["body"]["first_admin"]["email"] == "owner@acme.test"
        assert captured["body"]["name"] == "Acme"
        assert result["api_key"] == "hs_new"

    async def test_provision_omits_first_admin_when_no_identity(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={})

        await _client(handler).provision_organization(name="Shell", admin_email=None)
        assert "first_admin" not in captured["body"]

    async def test_delete_sends_confirmation_query(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"success": True, "message": "deleted"})

        await _client(handler).delete_organization("org-1", "Acme Inc")
        assert "confirm_name=Acme" in captured["url"]

    async def test_reads_retry_once(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("transient")
            return httpx.Response(200, json={"organizations": [], "total_count": 0})

        payload = await _client(handler).list_organizations()
        assert payload["total_count"] == 0
        assert calls["n"] == 2

    async def test_writes_do_not_retry(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("down")

        with pytest.raises(LucentAPIError):
            await _client(handler).provision_organization(
                name="X", admin_email="a@b.test"
            )
        assert calls["n"] == 1, "a retried create could duplicate a tenant"

    async def test_setting_reset_sends_reset_flag(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["body"] = json.loads(request.content)
            captured["path"] = request.url.path
            return httpx.Response(200, json={"key": "features.sandboxes_enabled"})

        await _client(handler).reset_setting("org-1", "features.sandboxes_enabled")
        assert captured["body"]["reset"] is True
        assert captured["path"].endswith("/settings/features.sandboxes_enabled")

    async def test_lock_setting_uses_the_dedicated_collection(self):
        """``setting-locks`` exists because ``settings/{key:path}`` is greedy.

        The server declares ``PUT /settings/{setting_key:path}``; a trailing
        ``/lock`` segment would be swallowed whole as part of the key, so the
        console must target the separate collection.
        """
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["method"] = request.method
            captured["body"] = json.loads(request.content)
            captured["path"] = request.url.path
            return httpx.Response(200, json={"key": "features.sandboxes_enabled"})

        await _client(handler).lock_setting(
            "org-1", "features.sandboxes_enabled", "Not in your plan."
        )
        assert captured["method"] == "PUT"
        assert captured["body"] == {"reason": "Not in your plan."}
        assert captured["path"].endswith(
            "/setting-locks/features.sandboxes_enabled"
        )

    async def test_unlock_setting_deletes_the_lock(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            return httpx.Response(200, json={"key": "features.sandboxes_enabled"})

        await _client(handler).unlock_setting("org-1", "features.sandboxes_enabled")
        assert captured["method"] == "DELETE"
        assert "/setting-locks/" in captured["path"]

    async def test_list_setting_locks_retries(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("transient")
            return httpx.Response(200, json=[])

        assert await _client(handler).list_setting_locks("org-1") == []
        assert calls["n"] == 2

    async def test_apply_lock_set_sends_the_target_state(self):
        """The bulk call declares the target state, so the body must be whole.

        A delta would put reconciliation in the caller and let two callers
        disagree about what "the tier" is.
        """
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"organization_id": "org-1", "locked": []})

        locks = [
            {"key": "features.sandboxes_enabled", "reason": "Starter", "value": False},
        ]
        await _client(handler).apply_lock_set("org-1", locks)
        assert captured["method"] == "PUT"
        assert captured["path"].endswith("/api/organizations/org-1/setting-locks")
        assert captured["body"]["locks"] == locks
        # Converging by default is what makes a retried webhook a no-op.
        assert captured["body"]["release_unlisted"] is True
        assert captured["body"]["dry_run"] is False

    async def test_apply_lock_set_passes_dry_run_and_keep_flags(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"organization_id": "org-1"})

        await _client(handler).apply_lock_set(
            "org-1", [], release_unlisted=False, dry_run=True
        )
        assert captured["body"]["release_unlisted"] is False
        assert captured["body"]["dry_run"] is True

    async def test_release_lock_set_names_exact_keys(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"organization_id": "org-1"})

        await _client(handler).release_lock_set("org-1", ["features.sandboxes_enabled"])
        assert captured["method"] == "POST"
        assert captured["path"].endswith("/setting-locks/release")
        assert captured["body"] == {"keys": ["features.sandboxes_enabled"]}

    async def test_monitor_setting_locks_forwards_the_key_filter(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200, json={"total": 0, "locks": [], "by_setting": {}, "by_organization": {}}
            )

        await _client(handler).monitor_setting_locks(key="features.sandboxes_enabled")
        assert captured["path"].endswith("/api/organizations/instance/setting-locks")
        assert captured["params"] == {"key": "features.sandboxes_enabled"}

    async def test_timeout_is_bounded(self):
        client = LucentClient(BASE, "hs_x")
        assert client._timeout == DEFAULT_TIMEOUT
        assert DEFAULT_TIMEOUT > 0


class TestConsoleApp:
    """Routing/auth behaviour of the app itself (no server required)."""

    @pytest.fixture
    def app(self):
        from ops_console.app import create_app

        config = load_config(
            {
                "LUCENT_OPS_API_KEY": "hs_x",
                "LUCENT_OPS_CONSOLE_TOKEN": "passphrase",
                "LUCENT_OPS_BASE_URL": BASE,
            }
        )
        return create_app(config)

    async def test_root_redirects_to_login(self, app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://console") as c:
            resp = await c.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    async def test_login_page_renders(self, app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://console") as c:
            resp = await c.get("/login")
        assert resp.status_code == 200
        assert "Operator sign-in" in resp.text
        # The API key must never appear in a rendered page.
        assert "hs_x" not in resp.text

    async def test_bad_passphrase_rejected(self, app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://console") as c:
            page = await c.get("/login")
            token = _csrf_from(page.text)
            resp = await c.post(
                "/login",
                data={"token": "wrong", "csrf_token": token},
                headers={"cookie": f"lucent_ops_csrf={token}"},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert "error=Incorrect" in resp.headers["location"]

    async def test_good_passphrase_sets_session(self, app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://console") as c:
            page = await c.get("/login")
            token = _csrf_from(page.text)
            resp = await c.post(
                "/login",
                data={"token": "passphrase", "csrf_token": token},
                headers={"cookie": f"lucent_ops_csrf={token}"},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert "lucent_ops_session" in resp.cookies

    async def test_login_requires_csrf(self, app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://console") as c:
            resp = await c.post(
                "/login",
                data={"token": "passphrase"},
                headers={"cookie": "lucent_ops_csrf=expected"},
                follow_redirects=False,
            )
        assert resp.status_code == 403

    async def test_state_change_requires_csrf_after_login(self, app):
        from ops_console.session import issue_session

        session = issue_session(app.state.config.signing_secret, hours=1)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://console") as c:
            resp = await c.post(
                "/organizations/org-1/suspend",
                data={"csrf_token": "wrong"},
                headers={
                    "cookie": f"lucent_ops_session={session}; lucent_ops_csrf=right"
                },
                follow_redirects=False,
            )
        assert resp.status_code == 403


def _csrf_from(html: str) -> str:
    """Pull the CSRF token out of the login page's hidden input."""
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]
