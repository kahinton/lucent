"""Zero-touch tenant-isolation startup chain.

Runs before the server pool is created and makes a fresh ``docker compose
up`` from a database at migration 113 healthy with zero manual steps:

1. **Bootstrap migration run** — connect with the bootstrap superuser from
   ``DATABASE_URL`` and apply pending migrations up to (and including)
   ``114_tenant_roles.sql``. Migration 114 creates the ``lucent_app`` /
   ``lucent_diagnostics`` / ``lucent_backup`` roles and reassigns
   schema-object ownership to ``lucent_app``. Migrations 115/116 must NOT be
   applied from this session: 116 FORCE-binds row level security and the
   runner gate (``db/pool.py::_ensure_rls_wave_preconditions``) refuses a
   superuser/BYPASSRLS runner session by design — FORCE RLS cannot bind a
   superuser, so pre-cutover application would give only the appearance of
   enforcement. The wave is applied post-cutover through the lucent_app
   pool by the normal ``init_db()`` migration run.

2. **Credential provisioning** — load or create the ``lucent_app`` login
   password via the configured secret provider (OpenBao KV/Transit), stored
   as a system-managed secret in the ``__lucent_system__`` organization.
   This replaces the previous out-of-band compose-secret step: the app
   provisions its own credential at boot, so the operator never sets a
   database password by hand. (Environments with no reachable OpenBao are
   refused with an explicit error rather than silently degraded — the
   chain's contract is OpenBao provisioning, never a manual fallback.)

3. **Role cutover** — set the password on the ``lucent_app`` role and
   return the post-cutover connection URL. The server then creates its pool
   from this URL (``lucent_app`` is non-superuser and non-BYPASSRLS, so RLS
   policies bind it) and applies the remaining migrations (115/116).

4. **Boot tenant context** (wired in ``api/app.py``) — startup queries that
   run after cutover but before any request context exists bind the
   ``app.role='system'`` session GUC (the permissive branch every
   migration-116 policy carries) via ``set_tenant_scope``, so boot-time
   reads cannot be fail-closed-empty under RLS. The scope is cleared before
   background tasks are spawned so ContextVar inheritance cannot leak boot
   context into request/daemon task scope.

Idempotence: every step is a no-op when its precondition is already met
(migrations applied, secret present, password already matches). Re-runs on
every boot are safe and cheap.
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
from asyncpg import Connection

from lucent.logging import get_logger

logger = get_logger("startup.tenant_chain")

# Migration that completes the tenant-isolation wave. The bootstrap run
# stops before the wave; the wave is applied post-cutover through the
# lucent_app pool (see run_migrations_for_bootstrap's stop list in pool.py).
RLS_WAVE_MIGRATION = "116_rls_policies.sql"

# Migration that creates the lucent_app role; credential provisioning
# requires it to exist.
ROLES_MIGRATION = "114_tenant_roles.sql"

# System-managed secret keys (stored in the __lucent_system__ org scope,
# alongside lucent.system.signing_secret).
DB_APP_ROLE_SECRET_KEY = "lucent.system.db_app_role_password"

APP_ROLE_NAME = "lucent_app"
SYSTEM_SECRET_ORG_NAME = "__lucent_system__"

# Postgres error codes treated as transient lock contention on ALTER ROLE.
_LOCK_ERROR_CODES = frozenset({"40P01", "55P03"})


class TenantChainError(RuntimeError):
    """Raised when the zero-touch startup chain cannot complete."""


def mask_database_url(url: str | None) -> str:
    """Return a log-safe copy of a database URL (password masked)."""
    if not url:
        return "<unset>"
    try:
        parts = urlsplit(url)
        if parts.password is None:
            return url
        host_part = parts.hostname or ""
        if parts.port:
            host_part = f"{host_part}:{parts.port}"
        userinfo = (parts.username or "") + ":***@"
        return urlunsplit(
            (parts.scheme, userinfo + host_part, parts.path, parts.query, parts.fragment)
        )
    except ValueError:
        return "<unparseable database url>"


def _bootstrap_role_name(url: str) -> str:
    """Extract the username from a postgres URL (default ``lucent``)."""
    try:
        user = urlsplit(url).username
    except ValueError:
        return "lucent"
    return user or "lucent"


def _app_url(url: str, password: str) -> str:
    """Build the lucent_app connection URL from the bootstrap URL.

    Keeps the bootstrap URL's host/port/database/query, replaces the user
    with ``lucent_app`` and sets the provisioned password. The database
    name is always taken from the bootstrap URL — provisioning never
    redirects the server pool to another database. The password is
    percent-encoded (RFC 3986): it is embedded in the URL's userinfo
    component, and characters like ``/``, ``@`` or ``:`` would otherwise
    truncate or split the userinfo.
    """
    parts = urlsplit(url)
    host_part = parts.hostname or ""
    if parts.port:
        host_part = f"{host_part}:{parts.port}"
    encoded_password = quote(password, safe="")
    return urlunsplit(
        (
            parts.scheme,
            f"{APP_ROLE_NAME}:{encoded_password}@{host_part}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


async def _connect(url: str) -> Connection:
    """Open a one-shot administrative connection (caller closes)."""
    return await asyncpg.connect(url)


class _StaticPool:
    """Pool-shaped adapter exposing one existing connection.

    Repository methods call ``pool.acquire()`` as an async context manager.
    During boot-time provisioning there is no server pool yet, so this
    adapter hands out the bootstrap connection directly. The connection is
    the bootstrap superuser session, so the exempt-table org lookup needs
    no tenant GUC binding (and pre-116 nothing is RLS-bound anyway).
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return _Ctx()


# ---------------------------------------------------------------------------
# Step 1: bootstrap (superuser) migration run up to the RLS wave
# ---------------------------------------------------------------------------


async def _apply_migrations_until_rls_wave(url: str) -> bool:
    """Apply pending migrations on the bootstrap connection, stopping at 115.

    Returns True when migration 114 (role creation) is confirmed applied,
    i.e. the lucent_app role exists for the provisioning step. Delegates to
    the production migration runner in "stop before the RLS wave" mode; the
    wave itself (115/116) is applied later through the lucent_app pool by
    ``init_db()``.
    """
    from lucent.db.pool import run_migrations_for_bootstrap

    await run_migrations_for_bootstrap(url)
    return await _roles_migration_applied(url)


async def _roles_migration_applied(url: str) -> bool:
    """Check whether the lucent_app role exists (migration 114 applied)."""
    conn = await _connect(url)
    try:
        return bool(
            await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)",
                APP_ROLE_NAME,
            )
        )
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Step 2: credential provisioning via the configured secret provider
# ---------------------------------------------------------------------------


async def _boot_secret_provider(static_pool: _StaticPool):
    """Instantiate a secret provider for boot-time credential storage.

    The runtime provider is registered later in lifespan (it needs the
    server pool). The chain must read/write its secret *before* the pool
    exists, so this mirrors the provider selection from
    :mod:`lucent.secrets.registry` without touching the registry singleton:
    explicit ``LUCENT_SECRET_PROVIDER`` selects that provider; ``auto``
    probes OpenBao and falls back to builtin exactly like
    ``initialize_secret_provider`` does. The transit provider performs SQL
    against the ``secrets`` table, so it receives the bootstrap-connection
    adapter (superuser: unaffected by RLS binding).
    """
    from lucent.secrets.registry import (
        _get_vault_token,
        detect_provider,
        get_selected_provider_name,
    )
    from lucent.secrets.transit import TransitSecretProvider
    from lucent.secrets.vault import VaultSecretProvider

    selected = get_selected_provider_name()
    if selected == "auto":
        # detect_provider probes Vault/OpenBao reachability with its own
        # short-lived HTTP client; its pool parameter is unused.
        selected = await detect_provider(None)
        logger.info("Boot credential storage auto-detected provider: %s", selected)

    if selected == "vault":
        return VaultSecretProvider(
            vault_addr=os.environ["VAULT_ADDR"],
            vault_token=_get_vault_token(),
        )
    if selected == "transit":
        return TransitSecretProvider(
            static_pool,  # type: ignore[arg-type]
            vault_addr=os.environ["VAULT_ADDR"],
            vault_token=_get_vault_token(),
        )
    raise TenantChainError(
        "No OpenBao/Vault secret provider is available for boot-time "
        "credential provisioning (selected provider: "
        f"{selected or 'none'}). The zero-touch startup chain stores the "
        f"{APP_ROLE_NAME} database credential in secret storage; configure "
        "VAULT_ADDR/VAULT_TOKEN (OpenBao) or set LUCENT_SECRET_PROVIDER "
        "explicitly. Manual credential steps are not part of this contract."
    )


async def _load_or_create_app_password(provider, static_pool: _StaticPool) -> str:
    """Load the stored lucent_app password, or create and store a new one.

    The ``__lucent_system__`` organization is created on demand (idempotent)
    so the secret path is stable across boots from the very first boot. The
    lookup runs on the bootstrap superuser connection; ``organizations`` is
    on migration 116's global-exempt list, so superuser visibility matches
    the post-cutover system-role behavior.
    """
    from lucent.db.organization import OrganizationRepository
    from lucent.secrets.base import SecretScope

    org, _created = await OrganizationRepository(static_pool).get_or_create(
        SYSTEM_SECRET_ORG_NAME
    )
    scope = SecretScope(organization_id=str(org["id"]), system_managed=True)

    value = await provider.get(DB_APP_ROLE_SECRET_KEY, scope)
    if value:
        logger.info("Loaded %s credential from secret storage", APP_ROLE_NAME)
        return value

    import secrets as _secrets

    value = _secrets.token_urlsafe(32)
    await provider.set(DB_APP_ROLE_SECRET_KEY, value, scope)
    logger.info("Created %s credential in secret storage", APP_ROLE_NAME)
    return value


# ---------------------------------------------------------------------------
# Step 3: set the password on the role (cutover)
# ---------------------------------------------------------------------------


async def _set_role_password(url: str, role: str, password: str) -> None:
    """Set the login password for ``role`` on the bootstrap connection.

    ALTER ROLE is a utility statement: Postgres resolves bind parameters
    only for SELECT/INSERT/UPDATE/DELETE, so a ``PASSWORD $1`` placeholder
    reaches the executor as an unresolved ParamRef and fails (defGetString
    path). The password is therefore inlined as a quoted literal with
    single-quote doubling (standard_conforming_strings on — the default —
    makes backslash doubling unnecessary and undesirable). The role name is
    an identifier and is quoted with double-quote doubling.
    """
    safe_role = role.replace('"', '""')
    literal = "'" + password.replace("'", "''") + "'"
    conn = await _connect(url)
    try:
        await conn.execute(
            f'ALTER ROLE "{safe_role}" WITH LOGIN PASSWORD {literal}'
        )
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Step 4: cutover verification
# ---------------------------------------------------------------------------


async def _verify_app_role(url: str) -> None:
    """Prove the provisioned credential can actually connect as lucent_app."""
    try:
        conn = await asyncpg.connect(url)
    except Exception as exc:
        raise TenantChainError(
            "Post-cutover verification failed: could not connect as "
            f"{APP_ROLE_NAME} ({type(exc).__name__}). The role exists "
            "(migration 114 applied) but the provisioned credential was "
            "rejected; boot cannot continue safely."
        ) from exc
    await conn.close()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_tenant_cutover_chain(
    database_url: str | None = None,
    *,
    max_wait_seconds: float = 30.0,
) -> str:
    """Run the zero-touch cutover chain; return the post-cutover database URL.

    Steps (all idempotent):
      1. Apply pending migrations through the bootstrap superuser up to
         (and including) migration 114 — never the RLS wave.
      2. Load or create the ``lucent_app`` credential in OpenBao-backed
         secret storage as a system-managed secret.
      3. Set the password on ``lucent_app`` and return the cutover URL.

    Skips entirely (returns the cutover URL) when the chain is already
    complete on a previous boot: a lucent_app connection with the stored
    credential succeeds.
    """
    bootstrap = database_url or os.environ.get("DATABASE_URL", "")
    if not bootstrap:
        raise TenantChainError("DATABASE_URL is required for the startup chain")
    logger.info(
        "Tenant cutover chain starting (bootstrap role: %s, url: %s)",
        _bootstrap_role_name(bootstrap),
        mask_database_url(bootstrap),
    )

    roles_applied = await _apply_migrations_until_rls_wave(bootstrap)
    if not roles_applied:
        raise TenantChainError(
            "Migrations through 114 could not be applied on the bootstrap "
            f"connection; the {APP_ROLE_NAME} role does not exist. Inspect "
            "the server logs for the migration runner error."
        )

    conn = await _connect(bootstrap)
    try:
        static_pool = _StaticPool(conn)
        provider = await _boot_secret_provider(static_pool)
        password = await _load_or_create_app_password(provider, static_pool)
    except TenantChainError:
        raise
    except Exception as exc:
        raise TenantChainError(
            f"Credential provisioning failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        await conn.close()

    cutover_url = _app_url(bootstrap, password)

    # Fast path: chain already complete on a previous boot.
    try:
        await _verify_app_role(cutover_url)
        logger.info(
            "Tenant cutover chain already complete; connecting as %s",
            APP_ROLE_NAME,
        )
        return cutover_url
    except TenantChainError:
        pass

    # Cutover: set the password, then verify. A concurrent writer (the same
    # chain running in another process) may hold the role lock briefly;
    # retry within the bounded window.
    deadline = asyncio.get_running_loop().time() + max_wait_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            await _set_role_password(bootstrap, APP_ROLE_NAME, password)
            break
        except asyncpg.PostgresError as exc:
            if getattr(exc, "sqlstate", None) in _LOCK_ERROR_CODES and (
                asyncio.get_running_loop().time() < deadline
            ):
                logger.warning(
                    "%s ALTER ROLE is lock-contended (attempt %d): %s",
                    APP_ROLE_NAME,
                    attempt,
                    exc,
                )
                await asyncio.sleep(1.0)
                continue
            raise TenantChainError(
                f"Could not set the {APP_ROLE_NAME} password: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    await _verify_app_role(cutover_url)
    logger.info(
        "Tenant cutover complete: server pool will connect as %s", APP_ROLE_NAME
    )
    return cutover_url