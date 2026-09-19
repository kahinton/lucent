"""Database connection pool management for Lucent.

This module handles PostgreSQL connection pooling, initialization,
and optional OpenTelemetry instrumentation for query tracing.

It also owns the tenant session-context contract (RLS wave, migrations
114/115/116): every scoped session carries three session-local GUCs:

    app.user_id : uuid | ''   (empty for the daemon/system role branches)
    app.org_id  : uuid | ''
    app.role    : 'member' | 'admin' | 'owner' | 'daemon' | 'system'

The RLS policies created by migration 116 read these GUCs. A session with
no context set sees zero rows on every RLS-bound table (fail-closed deny):
``current_setting('app.org_id', true)`` returns empty, so every policy
predicate is false. Context is set per-acquire via ``scoped_acquire()``
(session-local, scrubbed on release — transaction-local values would
vanish before the caller's first statement, a live-verified asyncpg
behavior); the pool ``reset`` hook clears any residue when a connection
is returned.
"""

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
import re
from typing import Any, AsyncIterator
from uuid import UUID

import asyncpg
from asyncpg import Connection, Pool

from lucent.logging import get_logger

logger = get_logger(__name__)

# Global connection pool
_pool: Pool | None = None
_asyncpg_instrumented: bool = False

# ---------------------------------------------------------------------------
# Tenant session context (RLS wave contract -- see module docstring).
# ---------------------------------------------------------------------------
RLS_WAVE_MIGRATION = "116_rls_policies.sql"
TENANT_GUCS = ("app.user_id", "app.org_id", "app.role", "app.auth_context")
_tenant_scope: ContextVar[dict[str, str] | None] = ContextVar(
    "tenant_scope", default=None
)


class TenantScopeError(RuntimeError):
    """Raised when a database operation would run without tenant context."""


def current_tenant_scope() -> dict[str, str] | None:
    """Return the current task's tenant scope, if any."""
    return _tenant_scope.get()


def set_tenant_scope(
    user_id: UUID | str | None = None,
    organization_id: UUID | str | None = None,
    role: str = "member",
) -> None:
    """Bind tenant context to the current asyncio task.

    ``scoped_acquire()`` reads this scope for every acquire that does not
    receive explicit parameters. Task-scoped by ContextVar: concurrent
    requests never share a scope. user_id/organization_id may be empty for
    the daemon/system role branches.
    """
    _tenant_scope.set(
        {
            "user_id": str(user_id) if user_id else "",
            "organization_id": str(organization_id) if organization_id else "",
            "role": role,
        }
    )


def clear_tenant_scope() -> None:
    """Reset the current task's tenant scope."""
    _tenant_scope.set(None)


def _runner_guc_preamble() -> str:
    """SQL preamble that binds app.role='system' to a session.

    Session-local set_config (is_local=false): the values live for the life
    of the pooled connection, so migrations and the enumerated system-infra
    paths execute under the permissive app.role='system' branch of every RLS
    policy (migration 116). Cleanup is the pool reset hook's job
    (_reset_connection), which scrubs the GUCs when the connection returns.
    """
    return (
        "SELECT set_config('app.user_id', '', false), "
        "set_config('app.org_id', '', false), "
        "set_config('app.role', 'system', false);"
    )


def _tenant_guc_scrub() -> str:
    """SQL that clears tenant context from a pooled connection.

    Sets every GUC to empty at session level: RLS policy predicates evaluate
    false, so any later raw acquire on this connection is fail-closed denied
    rather than inheriting a stale scope (or a bypass role).
    """
    return (
        "SELECT set_config('app.user_id', '', false), "
        "set_config('app.org_id', '', false), "
        "set_config('app.role', '', false), "
        "set_config('app.auth_context', '', false);"
    )


def runner_guc_preamble() -> str:
    """Public alias for the migration-runner / system-infra SQL preamble."""
    return _runner_guc_preamble()


def _preauth_guc_preamble() -> str:
    """Bind the narrow, SELECT-only pre-authentication branch.

    Bearer-key verification must read ``api_keys`` before Lucent can infer
    the key's organization. A distinct context keeps that bootstrap lookup
    separate from general system-infra access.
    """
    return (
        "SELECT set_config('app.user_id', '', false), "
        "set_config('app.org_id', '', false), "
        "set_config('app.role', 'system', false), "
        "set_config('app.auth_context', 'preauth', false);"
    )


def preauth_guc_preamble() -> str:
    """Public alias for the pre-authentication SQL preamble."""
    return _preauth_guc_preamble()


def tenant_guc_scrub() -> str:
    """Public alias: SQL that clears tenant context from a pooled connection."""
    return _tenant_guc_scrub()


def _instrument_asyncpg() -> None:
    """Apply OTEL auto-instrumentation to asyncpg when telemetry is enabled.

    Patches asyncpg globally so all connections (including pool connections)
    produce trace spans with:
      - db.system = "postgresql"
      - db.statement (the SQL query)
      - db.operation (SELECT, INSERT, UPDATE, DELETE)
      - Parent span from the current OTEL context (e.g. HTTP request span)

    Must be called after init_telemetry() and before pool creation.
    No-op when OTEL is disabled or packages are not installed.
    """
    global _asyncpg_instrumented
    if _asyncpg_instrumented:
        return

    from lucent.telemetry import is_enabled

    if not is_enabled():
        return

    try:
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor

        AsyncPGInstrumentor().instrument()
        _asyncpg_instrumented = True
        logger.info("OTEL: asyncpg instrumentation enabled")
    except Exception as e:
        logger.warning("OTEL: Failed to instrument asyncpg: %s", e)


def _uninstrument_asyncpg() -> None:
    """Remove OTEL instrumentation from asyncpg."""
    global _asyncpg_instrumented
    if not _asyncpg_instrumented:
        return

    try:
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor

        AsyncPGInstrumentor().uninstrument()
        _asyncpg_instrumented = False
        logger.info("OTEL: asyncpg instrumentation removed")
    except Exception as e:
        logger.warning("OTEL: Failed to uninstrument asyncpg: %s", e)


async def init_db(
    database_url: str | None = None, *, run_migrations: bool = True
) -> Pool:
    """Initialize the database connection pool and run migrations.

    Args:
        database_url: PostgreSQL connection URL. If not provided, uses DATABASE_URL env var.
        run_migrations: Whether to apply schema migrations. Defaults to True so the
            server applies migrations on startup. The daemon passes False — it connects
            with the least-privilege ``lucent_daemon`` role that intentionally lacks DDL
            (CREATE on schema public), so attempting to run migrations would fail with
            "permission denied for schema public". Migrations are the server's job.

    Returns:
        The initialized connection pool.

    Tenant cutover (startup chain): the URL handed to this function may be
    replaced before the pool is created. The zero-touch startup chain
    (lucent.startup.tenant_chain) runs migrations through the bootstrap
    superuser up to the RLS wave, provisions the ``lucent_app`` credential
    in OpenBao-backed secret storage, and hands the post-cutover URL to the
    server. This function re-checks the cutover just before the pool is
    created: if the current URL still connects as a superuser/BYPASSRLS
    role, the chain runs here and the pool is created from the returned
    ``lucent_app`` URL, so migrations 115/116 (FORCE RLS) bind the pool
    session instead of bypassing it.
    """
    global _pool

    if _pool is not None:
        return _pool

    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL environment variable is required")

    # Tenant cutover: when this server process is about to create its pool on
    # a superuser/BYPASSRLS connection, run the zero-touch chain first and
    # build the pool from the returned lucent_app URL. Idempotent: the chain
    # is a fast verify-then-return when cutover already happened (and is a
    # no-op for any non-superuser URL, including the daemon's restricted
    # role, which intentionally skips this path via run_migrations=False).
    if run_migrations and await _needs_tenant_cutover(url):
        from lucent.startup.tenant_chain import run_tenant_cutover_chain

        url = await run_tenant_cutover_chain(url)

    # Instrument asyncpg before pool creation so all connections are traced
    _instrument_asyncpg()

    # Create the connection pool
    _pool = await asyncpg.create_pool(
        url,
        min_size=2,
        max_size=10,
        command_timeout=60,
        init=_init_connection,
        reset=_reset_connection,
    )

    logger.info("Database connection pool created (min=2, max=10)")

    # Run migrations (server only — the daemon's restricted role cannot do DDL)
    if run_migrations:
        await _run_migrations(_pool)

    return _pool


async def _needs_tenant_cutover(url: str) -> bool:
    """Return True when ``url`` connects as a superuser or BYPASSRLS role.

    Used by init_db to decide whether the zero-touch tenant cutover chain
    must run before the pool is created. Connection failures return False —
    init_db's normal error path (create_pool raising) is the right place to
    surface an unreachable database, and the chain's bootstrap run will
    retry on the next boot.
    """
    try:
        conn = await asyncpg.connect(url)
    except Exception:
        return False
    try:
        row = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles "
            "WHERE rolname = current_user"
        )
    except Exception:
        return False
    finally:
        await conn.close()
    return bool(row and (row["rolsuper"] or row["rolbypassrls"]))


async def _init_connection(conn: Connection) -> None:
    """Initialize each connection with custom type codecs."""
    # Register UUID codec
    await conn.set_type_codec(
        "uuid",
        encoder=str,
        decoder=lambda x: UUID(x) if x else None,
        schema="pg_catalog",
    )
    # Register JSON codec for JSONB
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def _ensure_rls_wave_preconditions(conn: Connection, name: str) -> None:
    """Fail closed if the RLS-wave migration applies before cutover.

    Migration 116 FORCE-binds row level security on every tenant table. FORCE
    also binds the table owner, but a **superuser or BYPASSRLS** session
    bypasses every policy — so applying 116 while the server pool still
    connects as the bootstrap superuser would give only the appearance of
    enforcement. The runner refuses to apply it until the startup chain has
    cut the pool over to ``lucent_app`` (created, granted, and handed
    ownership by migration 114, credential provisioned at boot via OpenBao).
    """
    if name != RLS_WAVE_MIGRATION:
        return

    row = await conn.fetchrow(
        "SELECT rolsuper, rolbypassrls FROM pg_roles "
        "WHERE rolname = current_user"
    )
    if row and (row["rolsuper"] or row["rolbypassrls"]):
        raise RuntimeError(
            f"Migration {name} aborted: the runner session is superuser or "
            "BYPASSRLS, so FORCE ROW LEVEL SECURITY would not bind it. Cut "
            "the server pool over to the lucent_app role in compose "
            "(DATABASE_URL), restart the lucent service, and let this "
            "migration apply after cutover. Migration 114 has already "
            "created the role and its grants."
        )


async def _reset_connection(conn: Connection) -> None:
    """Pool reset hook: clear tenant-context residue on connection return.

    scoped_acquire re-sets the GUCs at every acquire; this hook guarantees a
    returned connection never carries a stale scope (or a bypass role) into
    the next acquire, including after exceptions inside the caller's body.
    """
    try:
        await conn.execute(_tenant_guc_scrub())
    except Exception:  # pragma: no cover - pool reset must never raise
        logger.debug("tenant GUC scrub on connection return failed", exc_info=True)


async def _run_migrations(pool: Pool, *, stop_before: str | None = None) -> None:
    """Run SQL migration files in order, tracking which have been applied.

    Uses a ``schema_migrations`` table to record applied migrations with
    SHA-256 checksums.  Skips previously-run files and warns when a file's
    content has changed since it was applied.

    Args:
        stop_before: Optional migration filename at which the run stops
            (file excluded). The bootstrap startup chain passes
            ``115_provision_orphan_tables.sql`` so the superuser session
            applies everything through 114 (role creation + ownership
            reassignment) without touching the RLS wave: the
            ``_ensure_rls_wave_preconditions`` gate already refuses to apply
            116 from a superuser session, and stopping cleanly keeps the
            gate as the only enforcement of that invariant.
    """
    migrations_dir = Path(__file__).parent / "migrations"

    if not migrations_dir.exists():
        return

    # Get all forward SQL files sorted by name (exclude rollback files)
    migration_files = _discover_forward_migration_files(migrations_dir)

    async with pool.acquire() as conn:  # rls: system-infra — audited no-scope site
        # Bind app.role='system' (session-local) for migration statements:
        # the RLS policies from migration 116 carry a permissive branch for
        # app.role='system', so DDL and data migrations keep working after
        # the RLS wave. Session-local (not transaction-local): asyncpg runs
        # each top-level statement in its own implicit transaction, which
        # would commit txn-local values away before the next statement
        # (live-verified). Cleanup is the pool reset hook's job.
        await conn.execute(_runner_guc_preamble())

        # Bootstrap tracking table (handles legacy _migrations upgrade)
        await _bootstrap_schema_migrations(conn, migration_files)

        # Get already-applied migrations with checksums
        applied: dict[str, str | None] = {}
        rows = await conn.fetch("SELECT name, checksum FROM schema_migrations")
        for row in rows:
            applied[row["name"]] = row["checksum"]

        applied_count = 0
        skipped_count = 0

        for migration_file in migration_files:
            # Bootstrap-run stop boundary: the startup chain applies the
            # pre-wave migrations only (through 114); 115/116 are applied
            # post-cutover through the lucent_app pool.
            if stop_before is not None and migration_file.name == stop_before:
                logger.info(
                    "Bootstrap migration run stopping before %s (RLS wave is "
                    "applied post-cutover through the app role)",
                    migration_file.name,
                )
                break

            await _ensure_rls_wave_preconditions(conn, migration_file.name)

            if migration_file.name in applied:
                # Verify checksum to detect post-application drift. The
                # orphan-provision migration stores its file checksum with an
                # "orphan-created:" prefix (apply-time marker read by its
                # rollback file), so the drift check compares the suffix.
                current_checksum = _file_checksum(migration_file)
                recorded = applied[migration_file.name]
                recorded_cmp = recorded
                if recorded and recorded.startswith("orphan-created:"):
                    recorded_cmp = recorded.split(":", 1)[1]
                if recorded_cmp and current_checksum != recorded_cmp:
                    logger.warning(
                        "Migration %s modified after application "
                        "(recorded: %s, current: %s)",
                        migration_file.name,
                        (recorded_cmp or "")[:12],
                        current_checksum[:12],
                    )
                skipped_count += 1
                continue

            sql = migration_file.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            # 115 records whether it created any of the three orphan tables
            # or found them pre-existing; its .down.sql reads this marker to
            # decide whether rollback may drop them (fresh replay) or must
            # no-op (live DB). 114 runs before 115 and reassigns ownership,
            # so on the live DB the orphans are already lucent_app-owned
            # (pre != 0, no marker); on a fresh replay they did not exist
            # (pre == 0) and 115 creates them — but pre is re-read before
            # apply here only to distinguish the two cases accurately.
            if migration_file.name == "115_provision_orphan_tables.sql":
                pre = await conn.fetchval(
                    "SELECT count(*) FROM pg_tables "
                    "WHERE schemaname='public' AND tablename IN "
                    "('memories', 'requests', 'resource_access_grants')"
                )
                if pre > 0:
                    # 114 already reassigned ownership; the tables existed
                    # before this wave. Record the un-prefixed checksum.
                    pass
                else:
                    checksum = "orphan-created:" + checksum

            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (name, checksum) "
                    "VALUES ($1, $2)",
                    migration_file.name,
                    checksum,
                )
                applied_count += 1
                logger.info("Applied migration: %s", migration_file.name)

        if applied_count > 0 or skipped_count > 0:
            logger.info(
                "Migrations complete: %d applied, %d skipped",
                applied_count,
                skipped_count,
            )


async def run_migrations_for_bootstrap(database_url: str | None = None) -> None:
    """Run migrations through the bootstrap superuser, stopping before the
    RLS wave (tenant startup chain, step 1).

    Creates a dedicated throwaway pool on the bootstrap URL (the
    superuser from ``DATABASE_URL``), applies every pending migration up to
    and including ``114_tenant_roles.sql``, and closes it. The module-level
    server pool is untouched: the server's real pool does not exist yet
    (this runs inside init_db, before create_pool) and bootstrap DDL must
    never be visible through ``get_pool()``. Migrations 115/116 are
    deliberately not applied here — the RLS wave must be applied
    post-cutover through the ``lucent_app`` pool (the
    ``_ensure_rls_wave_preconditions`` gate refuses a superuser runner
    session). The bootstrap role also cannot be disabled by this path: 116's
    FORCE binding would lock the break-glass superuser out of its own
    runner before the first cutover boot completes.
    """
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL environment variable is required")

    _instrument_asyncpg()
    bootstrap_pool = await asyncpg.create_pool(
        url,
        min_size=1,
        max_size=2,
        command_timeout=60,
        init=_init_connection,
        reset=_reset_connection,
    )
    try:
        await _run_migrations(bootstrap_pool, stop_before="115_provision_orphan_tables.sql")
    finally:
        try:
            await bootstrap_pool.close()
        except Exception:  # pragma: no cover - cleanup best-effort
            logger.debug("bootstrap pool close failed", exc_info=True)


async def _rollback_migrations(
    pool: Pool,
    *,
    steps: int = 1,
    target_name: str | None = None,
    allow_irreversible: bool = False,
) -> int:
    """Rollback previously-applied migrations in reverse order.

    Rollback files use paired ``.down.sql`` files next to each forward migration:

    - Forward migration: ``NNN_description.sql``
    - Rollback migration: ``NNN_description.down.sql``

    Optional metadata comments are supported in either file:

    - ``-- lucent: rollback=irreversible``
    - ``-- lucent: warning=...``
    """
    migrations_dir = Path(__file__).parent / "migrations"
    if not migrations_dir.exists():
        return 0

    migration_files = _discover_forward_migration_files(migrations_dir)
    migration_map = {path.name: path for path in migration_files}

    async with pool.acquire() as conn:  # rls: system-infra — audited no-scope site
        await _bootstrap_schema_migrations(conn, migration_files)

        rows = await conn.fetch(
            "SELECT name, checksum FROM schema_migrations ORDER BY name DESC"
        )
        if not rows:
            logger.info("Rollback requested, but no applied migrations were found")
            return 0

        applied: dict[str, str | None] = {
            row["name"]: row["checksum"] for row in rows
        }
        applied_names = [row["name"] for row in rows]

        if target_name:
            to_rollback = [name for name in applied_names if name > target_name]
        else:
            to_rollback = applied_names[: max(steps, 0)]

        if not to_rollback:
            logger.info("Rollback requested, but no migrations matched the request")
            return 0

        rolled_back = 0
        for migration_name in to_rollback:
            migration_file = migration_map.get(migration_name)
            if migration_file is None:
                logger.warning(
                    "Applied migration %s is missing from disk; cannot rollback",
                    migration_name,
                )
                if not allow_irreversible:
                    raise RuntimeError(
                        f"Migration {migration_name} cannot be rolled back: "
                        "forward file missing"
                    )
                continue

            current_checksum = _file_checksum(migration_file)
            recorded = applied.get(migration_name)
            if recorded and current_checksum != recorded:
                logger.warning(
                    "Rollback for %s using modified forward migration "
                    "(recorded: %s, current: %s)",
                    migration_name,
                    recorded[:12],
                    current_checksum[:12],
                )

            up_metadata = _parse_migration_metadata(migration_file)
            down_file = migration_file.with_name(f"{migration_file.stem}.down.sql")

            if not down_file.exists() or up_metadata.get("rollback") == "irreversible":
                reason = "marked irreversible" if up_metadata.get("rollback") == "irreversible" else "missing .down.sql file"
                logger.warning(
                    "Migration %s is irreversible (%s)", migration_name, reason
                )
                if not allow_irreversible:
                    raise RuntimeError(
                        f"Migration {migration_name} is irreversible ({reason})"
                    )
                continue

            down_metadata = _parse_migration_metadata(down_file)
            if down_metadata.get("rollback") == "irreversible":
                logger.warning(
                    "Migration %s rollback file marks migration as irreversible",
                    migration_name,
                )
                if not allow_irreversible:
                    raise RuntimeError(
                        f"Migration {migration_name} is irreversible "
                        "(rollback metadata)"
                    )
                continue

            warning = down_metadata.get("warning")
            if warning:
                logger.warning(
                    "Rollback warning for %s: %s", migration_name, warning
                )

            rollback_sql = down_file.read_text()
            if not rollback_sql.strip():
                logger.warning("Rollback file for %s is empty", migration_name)
                if not allow_irreversible:
                    raise RuntimeError(
                        f"Migration {migration_name} is irreversible "
                        "(empty rollback file)"
                    )
                continue

            async with conn.transaction():
                await conn.execute(rollback_sql)
                await conn.execute(
                    "DELETE FROM schema_migrations WHERE name = $1",
                    migration_name,
                )
            rolled_back += 1
            logger.info("Rolled back migration: %s", migration_name)

        logger.info("Rollback complete: %d rolled back", rolled_back)
        return rolled_back


async def _bootstrap_schema_migrations(
    conn: Connection,
    migration_files: list[Path],
) -> None:
    """Create the schema_migrations table and migrate from legacy _migrations.

    On first run against a database that used the old ``_migrations`` table,
    copies all records across and backfills checksums from current file
    content, then drops the legacy table.
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name        TEXT PRIMARY KEY,
            checksum    TEXT,
            applied_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # Check for legacy _migrations table
    legacy_exists = await conn.fetchval(
        "SELECT EXISTS ("
        "  SELECT FROM information_schema.tables"
        "  WHERE table_schema = 'public' AND table_name = '_migrations'"
        ")"
    )

    if not legacy_exists:
        return

    # Copy records that aren't already in schema_migrations
    migrated = await conn.fetch(
        "INSERT INTO schema_migrations (name, applied_at) "
        "SELECT name, applied_at FROM _migrations "
        "WHERE name NOT IN (SELECT name FROM schema_migrations) "
        "RETURNING name"
    )

    if migrated:
        # Backfill checksums from current file content
        file_map = {f.name: f for f in migration_files}
        for row in migrated:
            name = row["name"]
            if name in file_map:
                checksum = _file_checksum(file_map[name])
                await conn.execute(
                    "UPDATE schema_migrations SET checksum = $1 WHERE name = $2",
                    checksum,
                    name,
                )
        logger.info(
            "Migrated %d entries from legacy _migrations table", len(migrated)
        )

    await conn.execute("DROP TABLE _migrations")
    logger.info("Dropped legacy _migrations table")


def _file_checksum(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's UTF-8 content."""
    return hashlib.sha256(path.read_text().encode()).hexdigest()


def _discover_forward_migration_files(migrations_dir: Path) -> list[Path]:
    """Return sorted forward migration files, excluding rollback files."""
    return sorted(
        path
        for path in migrations_dir.glob("*.sql")
        if not path.name.endswith(".down.sql")
    )


def _parse_migration_metadata(path: Path) -> dict[str, str]:
    """Parse metadata comments from a migration file.

    Supports comment lines in the format:
    ``-- lucent: key=value``
    """
    metadata: dict[str, str] = {}
    pattern = re.compile(r"^\s*--\s*lucent:\s*([a-zA-Z0-9_-]+)\s*=\s*(.+?)\s*$")

    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("--"):
            break
        match = pattern.match(line)
        if match:
            key = match.group(1).strip().lower()
            value = match.group(2).strip()
            metadata[key] = value

    return metadata


@asynccontextmanager
async def scoped_acquire(
    user_id: UUID | str | None = None,
    organization_id: UUID | str | None = None,
    role: str | None = None,
) -> AsyncIterator[Connection]:
    """Acquire a connection bound to tenant context for its statements.

    Sets ``app.user_id`` / ``app.org_id`` / ``app.role`` **session-local**
    before yielding, and scrubs them on release. Verified against live
    asyncpg behavior (scratch-cluster probe, this wave): transaction-local
    ``set_config(..., true)`` set before yielding **vanishes before the
    caller's first statement**, because asyncpg runs each top-level
    ``execute()``/``fetch()`` in its own implicit transaction that commits
    immediately. Session-local values survive implicit transactions, apply
    to every statement in the block, and are cleared by the scrub below
    plus the pool reset hook (``_reset_connection``) as a backstop — so no
    tenant context ever leaks to the next acquire on the pooled connection.
    Explicit parameters win; with no explicit parameters the task's scope
    (``set_tenant_scope``) is used.

    Fail-closed: if the resolved scope carries no organization_id and the
    role is not a permissive branch (``daemon``/``system``), the acquire
    raises ``TenantScopeError`` instead of running unscoped.
    """
    scope = current_tenant_scope()
    resolved_user = str(user_id) if user_id else (scope or {}).get("user_id", "")
    resolved_org = (
        str(organization_id)
        if organization_id
        else (scope or {}).get("organization_id", "")
    )
    resolved_role = role or (scope or {}).get("role", "member")

    if not resolved_org and resolved_role not in ("daemon", "system"):
        raise TenantScopeError(
            "scoped_acquire() refused: no tenant context on this task. "
            "Set explicit user_id/organization_id, or set_tenant_scope(...) "
            "for the daemon/system role branches. RLS-bound tables return "
            "zero rows without context."
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.user_id', $1, false), "
            "set_config('app.org_id', $2, false), "
            "set_config('app.role', $3, false);",
            resolved_user,
            resolved_org,
            resolved_role or "member",
        )
        try:
            yield conn
        finally:
            await conn.execute(_tenant_guc_scrub())


@asynccontextmanager
async def scoped_acquire_on(
    pool: Pool,
    user_id: UUID | str | None = None,
    organization_id: UUID | str | None = None,
    role: str | None = None,
) -> AsyncIterator[Connection]:
    """Bind tenant context to a connection from an explicit pool.

    Same contract as :func:`scoped_acquire` (same guard, same session-local
    GUCs, same release scrub) but takes the pool as a parameter instead of
    reading the module singleton. For components that own or are handed a
    pool without registering it as the process pool — the secret providers
    at boot (the server pool does not exist yet) and any tool wrapper that
    carries its own pool.
    """
    scope = current_tenant_scope()
    resolved_user = str(user_id) if user_id else (scope or {}).get("user_id", "")
    resolved_org = (
        str(organization_id)
        if organization_id
        else (scope or {}).get("organization_id", "")
    )
    resolved_role = role or (scope or {}).get("role", "member")

    if not resolved_org and resolved_role not in ("daemon", "system"):
        raise TenantScopeError(
            "scoped_acquire_on() refused: no tenant context for this pool. "
            "Pass explicit user_id/organization_id, or a daemon/system role "
            "branch. RLS-bound tables return zero rows without context."
        )

    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.user_id', $1, false), "
            "set_config('app.org_id', $2, false), "
            "set_config('app.role', $3, false);",
            resolved_user,
            resolved_org,
            resolved_role or "member",
        )
        try:
            yield conn
        finally:
            await conn.execute(_tenant_guc_scrub())


async def get_pool() -> Pool:
    """Get the database connection pool.

    Returns:
        The active connection pool.

    Raises:
        RuntimeError: If the pool has not been initialized.
    """
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_db() first.")
    return _pool


async def close_db() -> None:
    """Close the database connection pool and remove instrumentation."""
    global _pool
    if _pool is not None:
        try:
            await _pool.close()
            logger.info("Database connection pool closed")
        except Exception:
            logger.exception("Error closing database pool")
        finally:
            _pool = None
    _uninstrument_asyncpg()
