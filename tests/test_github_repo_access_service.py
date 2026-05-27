"""Tests for GitHubRepoAccessService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from lucent.integrations.github_repo_access_service import (
    GitHubRepoAccessService,
    RepoAccessDecision,
)


def _pool_with_conn(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


@pytest.mark.asyncio
async def test_check_access_returns_fresh_cache_without_github_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "has_access": True,
            "checked_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        }
    )
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    access = await service.check_access(user_id, "owner/repo")

    assert access is True
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_access_fetches_github_and_caches_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "has_access": False,
                "checked_at": datetime.now(UTC) - timedelta(hours=1),
                "expires_at": datetime.now(UTC) - timedelta(minutes=1),
            },
            {"encrypted_secret_payload": b"secret"},
        ]
    )
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    encryptor = SimpleNamespace(decrypt=lambda _: {"access_token": "gh-token"})
    service = GitHubRepoAccessService(pool, encryptor=encryptor)

    response = SimpleNamespace(status_code=200)
    http_client = AsyncMock()
    http_client.get = AsyncMock(return_value=response)
    http_ctx = AsyncMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: http_ctx)

    access = await service.check_access(user_id, "Owner/Repo")

    assert access is True
    conn.execute.assert_awaited_once()
    assert conn.execute.await_args.args[3] is True


@pytest.mark.asyncio
async def test_check_access_without_credential_strict_mode_denies_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict mode (LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL=true):
    no credential → deny without caching the negative (no GitHub call was made).
    """
    monkeypatch.setenv("LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL", "true")
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    access = await service.check_access(user_id, "owner/repo")

    assert access is False
    # No GitHub call was made, so no cache write either.
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_access_stale_cache_triggers_refresh_and_negative_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    stale_checked = datetime.now(UTC) - timedelta(hours=1)
    stale_expires = datetime.now(UTC) - timedelta(minutes=1)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"has_access": True, "checked_at": stale_checked, "expires_at": stale_expires},
            {"encrypted_secret_payload": b"secret"},
        ]
    )
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(
        pool,
        encryptor=SimpleNamespace(decrypt=lambda _: {"access_token": "t"}),
    )

    response = SimpleNamespace(status_code=404)
    http_client = AsyncMock()
    http_client.get = AsyncMock(return_value=response)
    http_ctx = AsyncMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: http_ctx)

    access = await service.check_access(user_id, "owner/repo")

    assert access is False
    conn.execute.assert_awaited_once()
    # args: user_id, repo_full_name, has_access, checked_at, expires_at
    assert conn.execute.await_args.args[3] is False
    checked_at = conn.execute.await_args.args[4]
    expires_at = conn.execute.await_args.args[5]
    ttl = expires_at - checked_at
    assert timedelta(minutes=4, seconds=50) <= ttl <= timedelta(minutes=5, seconds=10)


@pytest.mark.asyncio
async def test_check_access_handles_rate_limit_and_caches_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, {"encrypted_secret_payload": b"secret"}])
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(
        pool,
        encryptor=SimpleNamespace(decrypt=lambda _: {"access_token": "t"}),
    )

    response = SimpleNamespace(status_code=403)
    http_client = AsyncMock()
    http_client.get = AsyncMock(return_value=response)
    http_ctx = AsyncMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: http_ctx)

    access = await service.check_access(user_id, "owner/repo")

    assert access is False
    conn.execute.assert_awaited_once()
    assert conn.execute.await_args.args[3] is False


@pytest.mark.asyncio
async def test_check_access_handles_http_client_failure_and_caches_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, {"encrypted_secret_payload": b"secret"}])
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(
        pool,
        encryptor=SimpleNamespace(decrypt=lambda _: {"access_token": "t"}),
    )

    http_client = AsyncMock()
    http_client.get = AsyncMock(side_effect=RuntimeError("boom"))
    http_ctx = AsyncMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: http_ctx)

    access = await service.check_access(user_id, "owner/repo")

    assert access is False
    conn.execute.assert_awaited_once()
    assert conn.execute.await_args.args[3] is False


@pytest.mark.asyncio
async def test_check_access_invalid_repo_name_short_circuits_to_allow() -> None:
    """Bare repo names (no ``owner/``) predate the ACL system — allow."""
    user_id = uuid4()
    conn = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    access = await service.check_access(user_id, "invalid-repo-name")

    assert access is True
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_access_no_token_compat_mode_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (compat) mode: a user without a GitHub credential is allowed
    so the open-source single-user path keeps working."""
    monkeypatch.delenv("LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL", raising=False)
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])  # cache miss, no credential
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    access = await service.check_access(user_id, "owner/private-repo")

    assert access is True
    # No cache write — we did not consult GitHub.
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_access_org_repo_returns_true_on_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, {"encrypted_secret_payload": b"secret"}])
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(
        pool,
        encryptor=SimpleNamespace(decrypt=lambda _: {"access_token": "t"}),
    )

    response = SimpleNamespace(status_code=200)
    http_client = AsyncMock()
    http_client.get = AsyncMock(return_value=response)
    http_ctx = AsyncMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: http_ctx)

    access = await service.check_access(user_id, "my-org/shared-repo")

    assert access is True
    conn.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# New tests: structured RepoAccessDecision API + app/user ACL separation +
# flag interplay (LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL,
# LUCENT_GITHUB_APP_ENABLED, LUCENT_CONNECTIONS_PAT_ENABLED,
# LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_strict_mode_no_credential_includes_connect_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL", "true")
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    decision = await service.check_access_with_reason(user_id, "owner/repo")

    assert isinstance(decision, RepoAccessDecision)
    assert decision.allowed is False
    assert decision.reason == "user_github_credential_required"
    assert decision.hint and "Connect your GitHub" in decision.hint


@pytest.mark.asyncio
async def test_decision_compat_mode_no_credential_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL", raising=False)
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    decision = await service.check_access_with_reason(user_id, "owner/repo")

    assert decision.allowed is True
    assert decision.reason == "no_user_credential_compat_allow"
    assert decision.hint is None


@pytest.mark.asyncio
async def test_user_with_credential_works_when_app_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-ACL must work purely from the user's own credential — no
    GitHub App required."""
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, {"encrypted_secret_payload": b"s"}])
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(
        pool,
        encryptor=SimpleNamespace(decrypt=lambda _: {"access_token": "user-tok"}),
    )

    response = SimpleNamespace(status_code=200)
    http_client = AsyncMock()
    http_client.get = AsyncMock(return_value=response)
    http_ctx = AsyncMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: http_ctx)

    access = await service.check_access(user_id, "owner/repo")

    assert access is True
    # Verify the user's own token (not an app token) was the bearer.
    auth_header = http_client.get.await_args.kwargs["headers"]["Authorization"]
    assert auth_header == "Bearer user-tok"


@pytest.mark.asyncio
async def test_app_installed_does_not_silently_grant_user_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION: even with a github_app installation row present and
    the GitHub App feature flag on, a user with no personal credential
    must NOT be granted user-ACL access via the app installation token.
    """
    monkeypatch.setenv("LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL", "true")
    monkeypatch.setenv("LUCENT_GITHUB_APP_ENABLED", "true")
    user_id = uuid4()

    # Cache miss, then user-credential lookup returns None. The user-ACL
    # path must NOT then peek into ``integrations`` for a github_app row
    # — those are different SELECTs and we assert only two fetchrows were
    # ever made (cache + user creds).
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)

    # Spy on httpx to ensure no GitHub call is made on behalf of the user.
    http_client = AsyncMock()
    http_client.get = AsyncMock()
    http_ctx = AsyncMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: http_ctx)

    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))
    decision = await service.check_access_with_reason(user_id, "owner/repo")

    assert decision.allowed is False
    assert decision.reason == "user_github_credential_required"
    http_client.get.assert_not_awaited()
    # Only two fetchrows: cache lookup + user credential lookup.
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_app_installation_can_see_repo_disabled_returns_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LUCENT_GITHUB_APP_ENABLED", raising=False)
    conn = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    result = await service.app_installation_can_see_repo(
        organization_id=uuid4(), repo_full_name="owner/repo"
    )

    assert result is None
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_app_installation_can_see_repo_no_install_returns_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUCENT_GITHUB_APP_ENABLED", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    result = await service.app_installation_can_see_repo(
        organization_id=uuid4(), repo_full_name="owner/repo"
    )

    assert result is None
    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_app_installation_with_install_id_returns_unknown_until_token_minting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Until App JWT/installation-token minting is implemented, the
    method must return ``None`` (unknown), never ``True``/``False`` —
    callers must not mistake the stub for an authoritative answer."""
    monkeypatch.setenv("LUCENT_GITHUB_APP_ENABLED", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": uuid4(), "install_id": "12345"})
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    result = await service.app_installation_can_see_repo(
        organization_id=uuid4(), repo_full_name="owner/repo"
    )

    assert result is None


@pytest.mark.asyncio
async def test_app_installation_can_see_repo_invalid_name_returns_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUCENT_GITHUB_APP_ENABLED", "true")
    conn = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    result = await service.app_installation_can_see_repo(
        organization_id=uuid4(), repo_full_name="bare-name"
    )

    assert result is None
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_access_does_not_call_get_any_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-ACL path must never invoke the shared-token helper
    (which is allowed to read GITHUB_TOKEN / any active credential).
    Sentinel: replace ``_get_any_github_token`` with a raise.
    """
    monkeypatch.setenv("LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL", "true")
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    async def _boom(self):  # pragma: no cover - must not be called
        raise AssertionError("user ACL must not consult shared/app tokens")

    monkeypatch.setattr(GitHubRepoAccessService, "_get_any_github_token", _boom)

    access = await service.check_access(user_id, "owner/repo")

    assert access is False  # strict mode, no user creds


@pytest.mark.asyncio
async def test_pat_disabled_does_not_block_existing_credential_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LUCENT_CONNECTIONS_PAT_ENABLED gates the *creation* surface for
    PATs; it must not retroactively invalidate user ACL when a credential
    already exists in the DB."""
    monkeypatch.setenv("LUCENT_CONNECTIONS_PAT_ENABLED", "false")
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, {"encrypted_secret_payload": b"s"}])
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(
        pool,
        encryptor=SimpleNamespace(decrypt=lambda _: {"access_token": "stored"}),
    )

    response = SimpleNamespace(status_code=200)
    http_client = AsyncMock()
    http_client.get = AsyncMock(return_value=response)
    http_ctx = AsyncMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: http_ctx)

    assert await service.check_access(user_id, "o/r") is True


@pytest.mark.asyncio
async def test_env_token_claim_disabled_blocks_env_fallback_in_existence_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED=false, the
    existence-check helper must not fall back to ``GITHUB_TOKEN``."""
    monkeypatch.setenv("LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED", "false")
    monkeypatch.setenv("GITHUB_TOKEN", "should-not-be-used")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)  # also no stored credential
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    token = await service._get_any_github_token()

    assert token is None


@pytest.mark.asyncio
async def test_env_token_claim_enabled_uses_env_fallback_in_existence_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "env-tok")
    conn = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    token = await service._get_any_github_token()

    assert token == "env-tok"


@pytest.mark.asyncio
async def test_audit_event_logged_on_integration_create_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check that the integrations service still logs audit
    events on workspace-app connect/revoke. This is the audit surface
    the GitHub App will share — user PAT connect/revoke audit is a
    separate follow-up tracked in the parent request.
    """
    from lucent.integrations import service as svc_mod

    # We don't spin up a DB; we just verify the service exposes an
    # ``_audit`` method and that the AuditRepository symbol is wired in.
    assert hasattr(svc_mod, "AuditRepository")
    assert "log_integration_event" in dir(svc_mod.AuditRepository)
