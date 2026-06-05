"""Web tests for Settings → Connections page and mutation endpoints.

Covers:
 * Two-section layout visibility per feature flag and per role
   (admin vs. regular user).
 * CSRF rejection on every JSON mutation endpoint.
 * Backend feature-flag rejection (PAT, env-token claim) regardless of
   what the UI does.
 * Regression: PAT save response NEVER returns the token, and the
   token is encrypted at rest in ``enterprise_credentials``.
"""

from __future__ import annotations

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
from lucent.db import OrganizationRepository, UserRepository

TEST_PASSWORD = "TestPass1"
CSRF_VALUE = "test-csrf-token-conn123"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    test_id = str(uuid4())[:8]
    prefix = f"test_webconn_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM enterprise_credentials WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%"
        )


async def _make_user(db_pool, prefix: str, role: str = "member"):
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{prefix}{role}",
        provider="basic",
        organization_id=org["id"],
        email=f"{prefix}{role}@test.com",
        display_name=f"{prefix}{role}",
    )
    if role != "member":
        await user_repo.update_role(user["id"], role)
    await set_user_password(db_pool, user["id"], TEST_PASSWORD)
    token = await create_session(db_pool, user["id"])
    return user, org, token


def _client(session_token: str) -> httpx.AsyncClient:
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: session_token, CSRF_COOKIE_NAME: CSRF_VALUE},
    )


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test owns its own flag state."""
    for var in (
        "LUCENT_CONNECTIONS_PAT_ENABLED",
        "LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED",
        "LUCENT_CONNECTIONS_OAUTH_ENABLED",
        "LUCENT_WORKSPACE_INTEGRATIONS_ENABLED",
        "LUCENT_GITHUB_APP_ENABLED",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    for prov in ("github", "slack", "jira"):
        monkeypatch.delenv(f"LUCENT_OAUTH_{prov.upper()}_CLIENT_ID", raising=False)
        monkeypatch.delenv(f"LUCENT_OAUTH_{prov.upper()}_CLIENT_SECRET", raising=False)


# ===========================================================================
# Smoke + render
# ===========================================================================


@pytest.mark.asyncio
async def test_connections_page_renders_smoke(db_pool, web_prefix):
    """Page renders 200 with both sections present (smoke check via test client)."""
    _user, _org, token = await _make_user(db_pool, web_prefix, role="admin")
    async with _client(token) as c:
        resp = await c.get("/settings/connections", follow_redirects=False)
    assert resp.status_code == 200, resp.text[:500]
    body = resp.text
    assert 'data-section="workspace-connections"' in body
    assert 'data-section="your-connected-accounts"' in body
    assert "Workspace connections" in body
    assert "Your connected accounts" in body


# ===========================================================================
# Section / flag visibility
# ===========================================================================


@pytest.mark.asyncio
async def test_workspace_section_hidden_when_flag_off(
    db_pool, web_prefix, monkeypatch
):
    monkeypatch.setenv("LUCENT_WORKSPACE_INTEGRATIONS_ENABLED", "false")
    _user, _org, token = await _make_user(db_pool, web_prefix, role="admin")
    async with _client(token) as c:
        resp = await c.get("/settings/connections")
    assert resp.status_code == 200
    assert 'data-section="workspace-connections"' not in resp.text
    # Personal section still renders
    assert 'data-section="your-connected-accounts"' in resp.text


@pytest.mark.asyncio
async def test_workspace_section_visible_to_member_without_mutation_controls(
    db_pool, web_prefix
):
    """Non-admin sees the workspace section but NO disable/revoke buttons."""
    _user, _org, token = await _make_user(db_pool, web_prefix, role="member")
    async with _client(token) as c:
        resp = await c.get("/settings/connections")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-section="workspace-connections"' in body
    assert "View only" in body
    assert "data-workspace-disable=" not in body
    assert "data-workspace-revoke=" not in body


@pytest.mark.asyncio
async def test_pat_form_hidden_when_flag_off(db_pool, web_prefix, monkeypatch):
    monkeypatch.setenv("LUCENT_CONNECTIONS_PAT_ENABLED", "false")
    _user, _org, token = await _make_user(db_pool, web_prefix, role="admin")
    async with _client(token) as c:
        resp = await c.get("/settings/connections")
    assert resp.status_code == 200
    assert 'data-testid="pat-form-github"' not in resp.text
    assert 'data-testid="pat-form-slack"' not in resp.text


@pytest.mark.asyncio
async def test_pat_form_visible_when_flag_on_default(db_pool, web_prefix):
    _user, _org, token = await _make_user(db_pool, web_prefix, role="admin")
    async with _client(token) as c:
        resp = await c.get("/settings/connections")
    assert resp.status_code == 200
    assert 'data-testid="pat-form-github"' in resp.text


@pytest.mark.asyncio
async def test_env_claim_hidden_when_flag_off(db_pool, web_prefix, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_for_render")
    monkeypatch.setenv("LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED", "false")
    _user, _org, token = await _make_user(db_pool, web_prefix, role="admin")
    async with _client(token) as c:
        resp = await c.get("/settings/connections")
    assert resp.status_code == 200
    assert 'data-env-claim="github"' not in resp.text


@pytest.mark.asyncio
async def test_env_claim_visible_when_token_present_and_flag_on(
    db_pool, web_prefix, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_for_render_xxxx")
    _user, _org, token = await _make_user(db_pool, web_prefix, role="admin")
    async with _client(token) as c:
        resp = await c.get("/settings/connections")
    assert resp.status_code == 200
    assert 'data-env-claim="github"' in resp.text


@pytest.mark.asyncio
async def test_oauth_button_hidden_when_provider_not_configured(db_pool, web_prefix):
    """OAuth button should NOT appear unless a client ID is configured."""
    _user, _org, token = await _make_user(db_pool, web_prefix, role="admin")
    async with _client(token) as c:
        resp = await c.get("/settings/connections")
    assert resp.status_code == 200
    assert 'data-oauth-connect="github"' not in resp.text
    assert 'data-oauth-connect="slack"' not in resp.text
    assert 'data-oauth-connect="jira"' not in resp.text


@pytest.mark.asyncio
async def test_oauth_button_visible_when_provider_configured(
    db_pool, web_prefix, monkeypatch
):
    monkeypatch.setenv("LUCENT_OAUTH_GITHUB_CLIENT_ID", "test-client-id")
    _user, _org, token = await _make_user(db_pool, web_prefix, role="admin")
    async with _client(token) as c:
        resp = await c.get("/settings/connections")
    assert resp.status_code == 200
    assert 'data-oauth-connect="github"' in resp.text


@pytest.mark.asyncio
async def test_oauth_button_hidden_when_oauth_flag_off_even_if_configured(
    db_pool, web_prefix, monkeypatch
):
    monkeypatch.setenv("LUCENT_OAUTH_GITHUB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("LUCENT_CONNECTIONS_OAUTH_ENABLED", "false")
    _user, _org, token = await _make_user(db_pool, web_prefix, role="admin")
    async with _client(token) as c:
        resp = await c.get("/settings/connections")
    assert resp.status_code == 200
    assert 'data-oauth-connect="github"' not in resp.text


# ===========================================================================
# CSRF rejection on JSON mutation endpoints
# ===========================================================================


@pytest.mark.asyncio
async def test_pat_save_rejects_missing_csrf(db_pool, web_prefix):
    _user, _org, token = await _make_user(db_pool, web_prefix)
    async with _client(token) as c:
        resp = await c.post(
            "/settings/connections/pat",
            json={"provider": "github", "token": "ghp_xyz"},
        )
    assert resp.status_code == 403
    assert "CSRF" in resp.text


@pytest.mark.asyncio
async def test_pat_save_rejects_invalid_csrf(db_pool, web_prefix):
    _user, _org, token = await _make_user(db_pool, web_prefix)
    async with _client(token) as c:
        resp = await c.post(
            "/settings/connections/pat",
            json={"provider": "github", "token": "ghp_xyz"},
            headers={"X-CSRF-Token": "wrong-token"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_env_claim_rejects_missing_csrf(db_pool, web_prefix):
    _user, _org, token = await _make_user(db_pool, web_prefix)
    async with _client(token) as c:
        resp = await c.post(
            "/settings/connections/env/claim",
            json={"provider": "github"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_oauth_start_rejects_missing_csrf(db_pool, web_prefix):
    _user, _org, token = await _make_user(db_pool, web_prefix)
    async with _client(token) as c:
        resp = await c.post(
            "/settings/connections/oauth/start",
            json={"provider": "github"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_revoke_rejects_missing_csrf(db_pool, web_prefix):
    _user, _org, token = await _make_user(db_pool, web_prefix)
    fake_id = str(uuid4())
    async with _client(token) as c:
        resp = await c.post(
            f"/settings/connections/{fake_id}/revoke", follow_redirects=False
        )
    assert resp.status_code == 403


# ===========================================================================
# Feature-flag backend hard-rejection
# ===========================================================================


@pytest.mark.asyncio
async def test_pat_save_hard_rejects_when_flag_off(
    db_pool, web_prefix, monkeypatch
):
    """Backend MUST reject PAT save when flag is off, regardless of UI state."""
    monkeypatch.setenv("LUCENT_CONNECTIONS_PAT_ENABLED", "false")
    _user, _org, token = await _make_user(db_pool, web_prefix)
    async with _client(token) as c:
        resp = await c.post(
            "/settings/connections/pat",
            json={"provider": "github", "token": "ghp_should_not_save"},
            headers={"X-CSRF-Token": CSRF_VALUE},
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "feature_disabled"
    assert body["feature"] == "LUCENT_CONNECTIONS_PAT_ENABLED"

    # Confirm nothing was written.
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM enterprise_credentials WHERE organization_id = $1",
            _org["id"],
        )
    assert count == 0


@pytest.mark.asyncio
async def test_env_claim_hard_rejects_when_flag_off(
    db_pool, web_prefix, monkeypatch
):
    monkeypatch.setenv("LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED", "false")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_present_but_disabled")
    _user, _org, token = await _make_user(db_pool, web_prefix)
    async with _client(token) as c:
        resp = await c.post(
            "/settings/connections/env/claim",
            json={"provider": "github"},
            headers={"X-CSRF-Token": CSRF_VALUE},
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "feature_disabled"
    assert body["feature"] == "LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED"


# ===========================================================================
# PAT confidentiality regression — never returned, always encrypted at rest
# ===========================================================================


@pytest.mark.asyncio
async def test_pat_response_never_returns_token_and_token_is_encrypted_at_rest(
    db_pool, web_prefix
):
    """Regression: PAT save responses must not echo the token, and the
    token bytes must NOT appear in plaintext in ``enterprise_credentials``."""
    secret = "ghp_super_secret_value_abc12345"
    _user, _org, token = await _make_user(db_pool, web_prefix)

    async with _client(token) as c:
        resp = await c.post(
            "/settings/connections/pat",
            json={"provider": "github", "token": secret, "display_name": "MyPAT"},
            headers={"X-CSRF-Token": CSRF_VALUE},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "saved"
    # Token must not appear ANYWHERE in the response payload.
    assert secret not in resp.text

    # Confirm encryption at rest: the encrypted_secret_payload column
    # must be non-null and must NOT contain the secret as a substring.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT encrypted_secret_payload FROM enterprise_credentials "
            "WHERE id = $1",
            body["id"],
        )
    assert row is not None
    encrypted = bytes(row["encrypted_secret_payload"])
    assert len(encrypted) > 0
    assert secret.encode("utf-8") not in encrypted
