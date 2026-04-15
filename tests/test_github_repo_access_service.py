"""Tests for GitHubRepoAccessService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from lucent.integrations.github_repo_access_service import GitHubRepoAccessService


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
async def test_check_access_without_credential_returns_false_and_caches() -> None:
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    access = await service.check_access(user_id, "owner/repo")

    assert access is False
    conn.execute.assert_awaited_once()
    assert conn.execute.await_args.args[3] is False


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
async def test_check_access_invalid_repo_name_short_circuits() -> None:
    user_id = uuid4()
    conn = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    access = await service.check_access(user_id, "invalid-repo-name")

    assert access is False
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_access_no_token_blocks_repo_tagged_memory() -> None:
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])  # cache miss, no credential
    conn.execute = AsyncMock()
    pool = _pool_with_conn(conn)
    service = GitHubRepoAccessService(pool, encryptor=SimpleNamespace(decrypt=lambda _: {}))

    access = await service.check_access(user_id, "owner/private-repo")

    assert access is False
    conn.execute.assert_awaited_once()


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
