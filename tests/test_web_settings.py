"""Integration tests for settings web routes in web/routes.py.

Tests:
- GET  /settings                           (settings page)
- POST /settings/api-keys                  (create API key)
- POST /settings/api-keys/{key_id}/revoke  (revoke API key)
"""

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent import settings as runtime_settings
from lucent.api.app import create_app
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    create_session,
    set_user_password,
)
from lucent.db import (
    ApiKeyRepository,
    OrganizationRepository,
    RuntimeSettingsRepository,
    UserRepository,
)

TEST_PASSWORD = "TestPass1"


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    test_id = str(uuid4())[:8]
    prefix = f"test_webset_{test_id}_"
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
    _user, _org, session_token = web_user
    csrf_token = "test-csrf-token-set123"
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: session_token, CSRF_COOKIE_NAME: csrf_token},
    ) as c:
        c._csrf_token = csrf_token
        yield c


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    data = {CSRF_FIELD_NAME: client._csrf_token}
    if extra:
        data.update(extra)
    return data


async def _promote_web_user(db_pool, web_user, role: str = "admin") -> None:
    user, _org, _token = web_user
    user_repo = UserRepository(db_pool)
    await user_repo.update_role(user["id"], role)


# ---------------------------------------------------------------------------
# GET /settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_page_returns_200(client):
    resp = await client.get("/settings", follow_redirects=True)
    assert resp.status_code == 200
    assert "Settings" in resp.text


@pytest.mark.asyncio
async def test_settings_unauthenticated_redirects(db_pool):
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/settings", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert resp.headers.get("location", "") == "/settings/account"


# ---------------------------------------------------------------------------
# POST /settings/api-keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_api_key(client):
    resp = await client.post(
        "/settings/api-keys",
        data=_csrf_data(client, {"name": "integration-test-key"}),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "/settings" in location
    assert "new_key=" in location


@pytest.mark.asyncio
async def test_create_api_key_without_csrf_fails(client):
    resp = await client.post(
        "/settings/api-keys",
        data={"name": "no-csrf-key"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /settings/api-keys/{key_id}/revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_api_key(client, db_pool, web_user):
    user, org, _token = web_user
    api_key_repo = ApiKeyRepository(db_pool)
    key_record, _plain_key = await api_key_repo.create(
        user_id=user["id"],
        organization_id=org["id"],
        name="revoke-me",
    )

    resp = await client.post(
        f"/settings/api-keys/{key_record['id']}/revoke",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/settings" in resp.headers["location"]


@pytest.mark.asyncio
async def test_revoke_nonexistent_key_returns_404(client):
    fake_id = str(uuid4())
    resp = await client.post(
        f"/settings/api-keys/{fake_id}/revoke",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /settings/runtime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_settings_page_requires_admin(client):
    resp = await client.get("/settings/runtime", follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_runtime_settings_page_shows_env_fallback(
    client,
    db_pool,
    web_user,
    monkeypatch,
):
    await _promote_web_user(db_pool, web_user)
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ALPHA", "0.2")
    runtime_settings.clear_runtime_setting_cache()

    resp = await client.get("/settings/runtime")

    assert resp.status_code == 200
    assert "Runtime Settings" in resp.text
    assert "From env" in resp.text
    assert "LUCENT_SEARCH_VITALITY_BOOST_ALPHA" in resp.text
    assert "Default model" in resp.text
    assert "Chat model" in resp.text
    assert "GitHub token" in resp.text
    assert "Locked" in resp.text


@pytest.mark.asyncio
async def test_runtime_setting_update_persists_db_value(
    client,
    db_pool,
    web_user,
    monkeypatch,
):
    user, org, _token = web_user
    await _promote_web_user(db_pool, web_user)
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ALPHA", "0.2")
    runtime_settings.clear_runtime_setting_cache()

    resp = await client.post(
        "/settings/runtime/memory.search_vitality_boost_alpha",
        data=_csrf_data(client, {"value": "0.27"}),
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/settings/runtime?success=")

    repo = RuntimeSettingsRepository(db_pool)
    row = await repo.get_setting(org["id"], "memory.search_vitality_boost_alpha")
    assert row is not None
    assert row["value"] == 0.27
    assert row["updated_by"] == user["id"]
    assert runtime_settings.search_vitality_boost_alpha(
        organization_id=org["id"],
    ) == 0.27


@pytest.mark.asyncio
async def test_runtime_default_model_drives_model_registry(
    client,
    db_pool,
    web_user,
):
    from lucent.auth import set_current_user
    from lucent.model_registry import ModelInfo, get_default_model_id

    user, org, _token = web_user
    await _promote_web_user(db_pool, web_user)
    runtime_settings.clear_runtime_setting_cache()

    resp = await client.post(
        "/settings/runtime/models.default_model",
        data=_csrf_data(client, {"value": "test-default-model"}),
        follow_redirects=False,
    )

    assert resp.status_code == 303
    set_current_user({"id": user["id"], "organization_id": org["id"]})
    try:
        selected = get_default_model_id(
            models=[
                ModelInfo(
                    id="fallback-model",
                    provider="test",
                    name="Fallback",
                    category="general",
                ),
                ModelInfo(
                    id="test-default-model",
                    provider="test",
                    name="DB Default",
                    category="general",
                ),
            ]
        )
    finally:
        set_current_user(None)
    assert selected == "test-default-model"


@pytest.mark.asyncio
async def test_runtime_setting_reset_uses_env_fallback(
    client,
    db_pool,
    web_user,
    monkeypatch,
):
    _user, org, _token = web_user
    await _promote_web_user(db_pool, web_user)
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ALPHA", "0.31")
    runtime_settings.clear_runtime_setting_cache()

    await client.post(
        "/settings/runtime/memory.search_vitality_boost_alpha",
        data=_csrf_data(client, {"value": "0.27"}),
        follow_redirects=False,
    )
    assert runtime_settings.search_vitality_boost_alpha(
        organization_id=org["id"],
    ) == 0.27

    resp = await client.post(
        "/settings/runtime/memory.search_vitality_boost_alpha/reset",
        data=_csrf_data(client),
        follow_redirects=False,
    )

    assert resp.status_code == 303
    repo = RuntimeSettingsRepository(db_pool)
    row = await repo.get_setting(org["id"], "memory.search_vitality_boost_alpha")
    assert row is None
    assert runtime_settings.search_vitality_boost_alpha(
        organization_id=org["id"],
    ) == 0.31


@pytest.mark.asyncio
async def test_runtime_setting_update_json_response(
    client,
    db_pool,
    web_user,
):
    _user, org, _token = web_user
    await _promote_web_user(db_pool, web_user)
    runtime_settings.clear_runtime_setting_cache()

    resp = await client.post(
        "/settings/runtime/models.chat_model",
        data=_csrf_data(client, {"value": "gpt-4.1"}),
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        follow_redirects=False,
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["message"] == "Chat model updated."
    assert payload["setting"]["key"] == "models.chat_model"
    assert payload["setting"]["source"] == "database"
    assert payload["setting"]["source_label"] == "Saved in DB"
    assert payload["setting"]["display_value"] == "gpt-4.1"
    assert runtime_settings.chat_model_id(organization_id=org["id"]) == "gpt-4.1"


@pytest.mark.asyncio
async def test_runtime_setting_reset_json_response(
    client,
    db_pool,
    web_user,
    monkeypatch,
):
    _user, org, _token = web_user
    await _promote_web_user(db_pool, web_user)
    monkeypatch.setenv("LUCENT_CHAT_MODEL", "env-chat-model")
    runtime_settings.clear_runtime_setting_cache()

    await client.post(
        "/settings/runtime/models.chat_model",
        data=_csrf_data(client, {"value": "db-chat-model"}),
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        follow_redirects=False,
    )

    resp = await client.post(
        "/settings/runtime/models.chat_model/reset",
        data=_csrf_data(client),
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        follow_redirects=False,
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["message"] == "Chat model reset to fallback."
    assert payload["setting"]["source"] == "environment"
    assert payload["setting"]["source_label"] == "From env"
    assert payload["setting"]["display_value"] == "env-chat-model"
    assert payload["setting"]["form_value"] == "env-chat-model"
    repo = RuntimeSettingsRepository(db_pool)
    row = await repo.get_setting(org["id"], "models.chat_model")
    assert row is None


@pytest.mark.asyncio
async def test_runtime_setting_validation_rejects_out_of_range_value(
    client,
    db_pool,
    web_user,
):
    _user, org, _token = web_user
    await _promote_web_user(db_pool, web_user)
    runtime_settings.clear_runtime_setting_cache()

    resp = await client.post(
        "/settings/runtime/memory.search_vitality_boost_log_top_n",
        data=_csrf_data(client, {"value": "0"}),
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    repo = RuntimeSettingsRepository(db_pool)
    row = await repo.get_setting(org["id"], "memory.search_vitality_boost_log_top_n")
    assert row is None


def test_runtime_daemon_git_flags_registered(monkeypatch):
    runtime_settings.clear_runtime_setting_cache()
    monkeypatch.setenv("LUCENT_ALLOW_GIT_COMMIT", "true")
    monkeypatch.setenv("LUCENT_ALLOW_GIT_PUSH", "false")

    try:
        assert runtime_settings.daemon_git_commit_allowed() is True
        assert runtime_settings.daemon_git_push_allowed() is False
        assert runtime_settings.get_runtime_setting_definition(
            "daemon.allow_git_commit"
        ) is not None
        assert runtime_settings.get_runtime_setting_definition(
            "daemon.allow_git_push"
        ) is not None
    finally:
        runtime_settings.clear_runtime_setting_cache()


def test_runtime_daemon_git_flags_default_to_allowed(monkeypatch):
    runtime_settings.clear_runtime_setting_cache()
    monkeypatch.delenv("LUCENT_ALLOW_GIT_COMMIT", raising=False)
    monkeypatch.delenv("LUCENT_ALLOW_GIT_PUSH", raising=False)

    try:
        assert runtime_settings.daemon_git_commit_allowed() is True
        assert runtime_settings.daemon_git_push_allowed() is True
    finally:
        runtime_settings.clear_runtime_setting_cache()
