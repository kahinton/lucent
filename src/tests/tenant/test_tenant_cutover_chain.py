"""Unit tests for the zero-touch tenant cutover startup chain.

Covers :mod:`lucent.startup.tenant_chain` (the boot chain that makes a
fresh ``docker compose up`` from a database at migration 113 healthy with
zero manual steps) plus the pool-side gates it depends on:

- The cutover ``ALTER ROLE`` must inline the password as a quoted literal:
  Postgres resolves bind parameters only for SELECT/INSERT/UPDATE/DELETE,
  so ``PASSWORD $1`` would reach the executor as an unresolved ParamRef
  and abort the boot chain (utility statements cannot take parameters).
- Idempotence contracts: already-cut-over boots verify and return without
  touching the role; a failed role creation aborts before cutover.
- Lock-contention retry on ALTER ROLE (55P03/40P01) is bounded, not fatal.
- The zero-touch contract: the chain refuses builtin (non-OpenBao) secret
  storage rather than silently degrading credential provisioning.
- Pool-side fail-closed guard and the superuser/BYPASSRLS cutover gate.

All tests are fake-based — no database, no network, no containers.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import asyncpg
import pytest

from lucent.db import pool as db_pool
from lucent.startup import tenant_chain as tc


# ═════════════════════════════════════════════════════════════════════
# URL helpers
# ═════════════════════════════════════════════════════════════════════


def test_mask_database_url_hides_password():
    masked = tc.mask_database_url(
        "postgresql://lucent:hunter2@postgres:5432/lucent"
    )
    assert "hunter2" not in masked
    assert "lucent:***@postgres:5432/lucent" in masked


def test_mask_database_url_handles_none_and_unparseable():
    assert tc.mask_database_url(None) == "<unset>"
    assert tc.mask_database_url("postgresql://[::1/lucent") == "<unparseable database url>"


def test_app_url_rewrites_role_and_keeps_target_database():
    url = tc._app_url(
        "postgresql://lucent:pw@postgres:5432/lucent?sslmode=disable", "s3cret"
    )
    assert url == "postgresql://lucent_app:s3cret@postgres:5432/lucent?sslmode=disable"
    # The bootstrap role and its password must not survive into the cutover URL.
    assert "lucent:pw@" not in url


def test_app_url_escapes_special_characters_in_password():
    url = tc._app_url("postgresql://lucent:pw@postgres:5432/lucent", "p@a/ss word")
    # The real consumer is asyncpg's DSN parser (libpq semantics): it must
    # recover the exact password from the percent-encoded userinfo.
    params = _asyncpg_password_from_dsn(url)
    assert params == "p@a/ss word"
    # And the pre-fix failure mode is gone: the raw special-char password
    # would have truncated the userinfo at the first '/'.
    assert "p%40a%2Fss%20word" in url


def _asyncpg_password_from_dsn(dsn: str) -> str:
    from asyncpg.connect_utils import _parse_connect_arguments

    base = dict(
        host=None, port=None, user=None, password=None, passfile=None,
        database=None, command_timeout=None, statement_cache_size=100,
        max_cached_statement_lifetime=300, max_cacheable_statement_size=1024 * 15,
        ssl=False, direct_tls=False, server_settings=None,
        target_session_attrs=None, krbsrvname=None, gsslib=None,
        service=None, servicefile=None,
    )
    _, params, _config = _parse_connect_arguments(dsn=dsn, **base)
    return params.password


def test_bootstrap_role_name_extraction():
    assert tc._bootstrap_role_name("postgresql://lucent:pw@db/lucent") == "lucent"
    assert tc._bootstrap_role_name("postgresql://db/lucent") == "lucent"


# ═════════════════════════════════════════════════════════════════════
# Step 3: ALTER ROLE cutover — the utility-statement parameter trap
# ═════════════════════════════════════════════════════════════════════


class _FakeConn:
    def __init__(self):
        self.executed: list[tuple[str, list | None]] = []

    async def execute(self, sql, *args):
        self.executed.append((sql, list(args) if args else None))
        return "ALTER ROLE"

    async def close(self):
        pass


async def test_set_role_password_inlines_password_no_bind_params(monkeypatch):
    """ALTER ROLE is a utility statement: bind parameters are unresolved at
    execute time (only SELECT/INSERT/UPDATE/DELETE take them), so the
    password must be inlined as a quoted literal."""
    fake_conn = _FakeConn()

    async def fake_connect(url):
        return fake_conn

    monkeypatch.setattr(tc.asyncpg, "connect", fake_connect)
    await tc._set_role_password(
        "postgresql://lucent:pw@db/lucent", "lucent_app", "s3cret"
    )
    sql, args = fake_conn.executed[0]
    assert args is None, "no bind parameters may be sent for utility statements"
    assert "$1" not in sql
    assert "PASSWORD 's3cret'" in sql
    assert 'ROLE "lucent_app"' in sql


async def test_set_role_password_quotes_doubled_password(monkeypatch):
    """A password containing a single quote must survive via doubling."""
    fake_conn = _FakeConn()

    async def fake_connect(url):
        return fake_conn

    monkeypatch.setattr(tc.asyncpg, "connect", fake_connect)
    await tc._set_role_password(
        "postgresql://lucent:pw@db/lucent", "lucent_app", "it's"
    )
    sql, _ = fake_conn.executed[0]
    assert "PASSWORD 'it''s'" in sql


async def test_set_role_password_quotes_doubled_role_name(monkeypatch):
    fake_conn = _FakeConn()

    async def fake_connect(url):
        return fake_conn

    monkeypatch.setattr(tc.asyncpg, "connect", fake_connect)
    await tc._set_role_password("postgresql://lucent:pw@db/lucent", 'ro"le', "pw")
    sql, _ = fake_conn.executed[0]
    assert 'ROLE "ro""le"' in sql


# ═════════════════════════════════════════════════════════════════════
# Orchestration: idempotence and failure ordering
# ═════════════════════════════════════════════════════════════════════


def _stub_provisioning(monkeypatch, password: str = "stored-pw") -> dict[str, int]:
    """Stub the provisioning seams so orchestration tests never open a
    connection or touch secret storage; returns a call counter."""
    calls: dict[str, int] = {"provision": 0}

    fake_conn = _FakeConn()

    async def fake_connect(url):
        return fake_conn

    async def fake_provider(static_pool):
        calls["provision"] += 1
        return object()

    async def fake_password(provider, static_pool):
        return password

    monkeypatch.setattr(tc, "_connect", fake_connect)
    monkeypatch.setattr(tc, "_boot_secret_provider", fake_provider)
    monkeypatch.setattr(tc, "_load_or_create_app_password", fake_password)
    return calls


async def test_chain_already_complete_short_circuits(monkeypatch):
    """When lucent_app already accepts the stored credential, no ALTER ROLE
    runs and the cutover URL is returned unchanged."""
    calls: dict[str, int] = {"migrate": 0, "verify": 0, "setpw": 0}

    async def fake_migrate(url):
        calls["migrate"] += 1
        return True

    async def fake_verify(url):
        calls["verify"] += 1
        # success on the fast-path check → chain already complete

    async def fake_setpw(url, role, password):
        calls["setpw"] += 1

    monkeypatch.setattr(tc, "_apply_migrations_until_rls_wave", fake_migrate)
    monkeypatch.setattr(tc, "_verify_app_role", fake_verify)
    monkeypatch.setattr(tc, "_set_role_password", fake_setpw)
    _stub_provisioning(monkeypatch, "stored-pw")

    url = await tc.run_tenant_cutover_chain(
        "postgresql://lucent:pw@postgres:5432/lucent"
    )
    assert url == "postgresql://lucent_app:stored-pw@postgres:5432/lucent"
    assert calls == {"migrate": 1, "verify": 1, "setpw": 0}


async def test_chain_cutover_sets_password_when_verify_fails(monkeypatch):
    """Fast-path verify failure (credential not yet set) → password is set
    and the cutover is verified a second time."""
    calls: dict[str, int] = {"verify": 0, "setpw": 0}
    setpw_passwords: list[str] = []

    async def fake_migrate(url):
        return True

    async def fake_verify(url):
        calls["verify"] += 1
        if calls["verify"] == 1:
            raise tc.TenantChainError("not yet cut over")

    async def fake_setpw(url, role, password):
        calls["setpw"] += 1
        setpw_passwords.append(password)

    monkeypatch.setattr(tc, "_apply_migrations_until_rls_wave", fake_migrate)
    monkeypatch.setattr(tc, "_verify_app_role", fake_verify)
    monkeypatch.setattr(tc, "_set_role_password", fake_setpw)
    _stub_provisioning(monkeypatch, "fresh-pw")

    url = await tc.run_tenant_cutover_chain(
        "postgresql://lucent:pw@postgres:5432/lucent"
    )
    assert calls["setpw"] == 1
    assert calls["verify"] == 2
    assert setpw_passwords == ["fresh-pw"]
    assert url == "postgresql://lucent_app:fresh-pw@postgres:5432/lucent"


async def test_chain_aborts_when_roles_migration_not_applied(monkeypatch):
    """The chain must never cut over to a role migration 114 did not create."""

    async def fake_migrate(url):
        return False

    async def fail_setpw(url, role, password):  # pragma: no cover
        raise AssertionError("cutover must not run without the role")

    monkeypatch.setattr(tc, "_apply_migrations_until_rls_wave", fake_migrate)
    monkeypatch.setattr(tc, "_set_role_password", fail_setpw)

    with pytest.raises(tc.TenantChainError, match="114"):
        await tc.run_tenant_cutover_chain(
            "postgresql://lucent:pw@postgres:5432/lucent"
        )


async def test_chain_retries_lock_contended_alter_role(monkeypatch):
    """55P03/40P01 on ALTER ROLE is transient contention: retry within the
    bounded window instead of failing the boot."""
    attempts: list[int] = []

    async def fake_migrate(url):
        return True

    async def fake_verify(url):
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 1:
            raise tc.TenantChainError("not yet cut over")

    verify_calls = 0

    async def flaky_setpw(url, role, password):
        attempts.append(1)
        if len(attempts) == 1:
            err = asyncpg.PostgresError("tuple concurrently updated")
            err.sqlstate = "55P03"
            raise err

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(tc, "_apply_migrations_until_rls_wave", fake_migrate)
    monkeypatch.setattr(tc, "_verify_app_role", fake_verify)
    monkeypatch.setattr(tc, "_set_role_password", flaky_setpw)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    _stub_provisioning(monkeypatch, "fresh-pw")
    await tc.run_tenant_cutover_chain(
        "postgresql://lucent:pw@postgres:5432/lucent"
    )
    assert len(attempts) == 2
    assert sleeps  # backoff happened between attempts


async def test_chain_requires_database_url():
    with pytest.raises(tc.TenantChainError, match="DATABASE_URL"):
        await tc.run_tenant_cutover_chain("")


# ═════════════════════════════════════════════════════════════════════
# Zero-touch contract: OpenBao-backed provisioning only
# ═════════════════════════════════════════════════════════════════════


async def test_boot_secret_provider_refuses_builtin(monkeypatch):
    """The chain's contract is OpenBao provisioning — a builtin (local,
    non-durable) selection must be refused loudly, never silently used."""
    monkeypatch.setenv("LUCENT_SECRET_PROVIDER", "builtin")
    with pytest.raises(tc.TenantChainError, match="OpenBao"):
        await tc._boot_secret_provider(tc._StaticPool(None))


async def test_boot_secret_provider_refuses_missing_vault_env(monkeypatch):
    monkeypatch.setenv("LUCENT_SECRET_PROVIDER", "vault")
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    with pytest.raises(Exception):  # env validation must fail loudly
        await tc._boot_secret_provider(tc._StaticPool(None))


# ═════════════════════════════════════════════════════════════════════
# Pool-side gates the chain depends on
# ═════════════════════════════════════════════════════════════════════


async def test_needs_tenant_cutover_true_for_superuser(monkeypatch):
    class _FakeConn:
        async def fetchrow(self, sql):
            return {"rolsuper": True, "rolbypassrls": False}

        async def close(self):
            pass

    async def fake_connect(url):
        return _FakeConn()

    monkeypatch.setattr(db_pool.asyncpg, "connect", fake_connect)
    assert await db_pool._needs_tenant_cutover("postgresql://x") is True


async def test_needs_tenant_cutover_false_for_scoped_role(monkeypatch):
    class _FakeConn:
        async def fetchrow(self, sql):
            return {"rolsuper": False, "rolbypassrls": False}

        async def close(self):
            pass

    async def fake_connect(url):
        return _FakeConn()

    monkeypatch.setattr(db_pool.asyncpg, "connect", fake_connect)
    assert await db_pool._needs_tenant_cutover("postgresql://x") is False


async def test_needs_tenant_cutover_false_when_db_unreachable(monkeypatch):
    async def boom(url):
        raise OSError("connection refused")

    monkeypatch.setattr(db_pool.asyncpg, "connect", boom)
    assert await db_pool._needs_tenant_cutover("postgresql://x") is False


async def test_scoped_acquire_fails_closed_without_context():
    """No scope, no permissive role → the acquire is refused, not run
    unscoped (this is the fail-closed boot contract)."""
    with pytest.raises(db_pool.TenantScopeError):
        async with db_pool.scoped_acquire():
            pass  # pragma: no cover — never yields


async def test_scoped_acquire_system_role_passes_guard_without_org():
    """The system branch is permissive on empty org (boot path) — it gets
    past the guard and then fails at pool init (RuntimeError), proving the
    guard itself did not refuse it."""
    with pytest.raises(RuntimeError, match="not initialized"):
        async with db_pool.scoped_acquire(role="system"):
            pass  # pragma: no cover


def test_runner_guc_preamble_binds_system_role():
    preamble = db_pool._runner_guc_preamble()
    assert "'app.role', 'system'" in preamble
    scrub = db_pool._tenant_guc_scrub()
    assert "'app.role', ''" in scrub
    assert "app.user_id" in scrub and "app.org_id" in scrub


def test_preauth_guc_preamble_binds_narrow_context():
    preamble = db_pool._preauth_guc_preamble()
    assert "'app.role', 'system'" in preamble
    assert "'app.auth_context', 'preauth'" in preamble
    scrub = db_pool._tenant_guc_scrub()
    assert "'app.auth_context', ''" in scrub


def test_rls_wave_migration_names_agree():
    """The chain and the pool runner must agree on where the wave starts
    (stop boundary) — a split-brain here would apply 115/116 pre-cutover."""
    assert tc.RLS_WAVE_MIGRATION == db_pool.RLS_WAVE_MIGRATION
    assert tc.ROLES_MIGRATION == "114_tenant_roles.sql"
