"""Database connection pool management for Lucent.

This module handles PostgreSQL connection pooling, initialization,
and optional OpenTelemetry instrumentation for query tracing.

It also owns the tenant session-context contract: every scoped session
carries three session-local GUCs:

    app.user_id : uuid | ''   (empty for the daemon/system role branches)
    app.org_id  : uuid | ''
    app.role    : 'member' | 'admin' | 'owner' | 'daemon' | 'system'

Context is set per-acquire via ``scoped_acquire()`` (session-local, scrubbed
on release — transaction-local values would vanish before the caller's first
statement, a live-verified asyncpg behavior); the pool ``reset`` hook clears
any residue when a connection is returned.
"""

import functools
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Final
from uuid import UUID

import asyncpg
from asyncpg import Connection, Pool

from lucent.db.scope_audit import (
    ScopeAuditedConnection,
    ScopeAuditedPool,
    audit_enabled,
    effective_scope,
)
from lucent.logging import get_logger

logger = get_logger(__name__)

# Global connection pool
_pool: Pool | None = None
_asyncpg_instrumented: bool = False

# ---------------------------------------------------------------------------
# Tenant session context (see module docstring).
# ---------------------------------------------------------------------------
TENANT_GUCS = ("app.user_id", "app.org_id", "app.role", "app.auth_context")
_tenant_scope: ContextVar[dict[str, str] | None] = ContextVar("tenant_scope", default=None)


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
    paths run with an explicit system context. Cleanup is the pool reset
    hook's job (_reset_connection), which scrubs the GUCs on connection return.
    """
    return (
        "SELECT set_config('app.user_id', '', false), "
        "set_config('app.org_id', '', false), "
        "set_config('app.role', 'system', false);"
    )


def _tenant_guc_scrub() -> str:
    """SQL that clears tenant context from a pooled connection.

    Sets every GUC to empty at session level, so any later raw acquire does
    not inherit a stale scope. Includes every GUC any acquire may set:
    the tenant trio, the authorized-pool clearance context, and the group
    list the clearance rewrite reads.
    """
    return (
        "SELECT set_config('app.user_id', '', false), "
        "set_config('app.org_id', '', false), "
        "set_config('app.role', '', false), "
        "set_config('app.auth_context', '', false), "
        "set_config('app.auth_group_ids', '', false);"
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


async def init_db(database_url: str | None = None, *, run_migrations: bool = True) -> Pool:
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

    """
    global _pool

    if _pool is not None:
        return _pool

    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL environment variable is required")

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

    if audit_enabled():
        _pool = ScopeAuditedPool(_pool)

    logger.info("Database connection pool created (min=2, max=10)")

    # Run migrations (server only — the daemon's restricted role cannot do DDL)
    if run_migrations:
        await _run_migrations(_pool)

    return _pool


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


async def _reset_connection(conn: Connection) -> None:
    """Pool reset hook: clear tenant-context residue on connection return.

    scoped_acquire re-sets the GUCs at every acquire; this hook guarantees a
    returned connection never carries a stale scope (or a bypass role) into
    the next acquire, including after exceptions inside the caller's body.
    """
    try:
        await conn.execute(_tenant_guc_scrub())
    except Exception:  # pragma: no cover - pool reset must never raise
        logger.debug("tenant context scrub on connection return failed", exc_info=True)


async def _run_migrations(pool: Pool) -> None:
    """Run SQL migration files in order, tracking which have been applied.

    Uses a ``schema_migrations`` table to record applied migrations with
    SHA-256 checksums.  Skips previously-run files and warns when a file's
    content has changed since it was applied.

    """
    migrations_dir = Path(__file__).parent / "migrations"

    if not migrations_dir.exists():
        return

    # Get all forward SQL files sorted by name (exclude rollback files)
    migration_files = _discover_forward_migration_files(migrations_dir)

    async with pool.acquire() as conn:
        # Bind app.role='system' (session-local) for migration statements.
        # Session-local (not transaction-local): asyncpg runs
        # each top-level statement in its own implicit transaction, which
        # would commit txn-local values away before the next statement
        # (live-verified). Cleanup is the pool reset hook's job.
        await conn.execute(_runner_guc_preamble())

        # Bootstrap tracking table (handles legacy _migrations upgrade).
        await _bootstrap_schema_migrations(conn, migration_files)

        # Get already-applied migrations with checksums
        applied: dict[str, str | None] = {}
        rows = await conn.fetch("SELECT name, checksum FROM schema_migrations")
        for row in rows:
            applied[row["name"]] = row["checksum"]

        applied_count = 0
        skipped_count = 0

        for migration_file in migration_files:
            if migration_file.name in applied:
                # Verify checksum to detect post-application drift.
                current_checksum = _file_checksum(migration_file)
                recorded = applied[migration_file.name]
                if recorded and current_checksum != recorded:
                    logger.warning(
                        "Migration %s modified after application (recorded: %s, current: %s)",
                        migration_file.name,
                        (recorded or "")[:12],
                        current_checksum[:12],
                    )
                skipped_count += 1
                continue

            sql = migration_file.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (name, checksum) VALUES ($1, $2)",
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

    async with pool.acquire() as conn:
        await _bootstrap_schema_migrations(conn, migration_files)

        rows = await conn.fetch("SELECT name, checksum FROM schema_migrations ORDER BY name DESC")
        if not rows:
            logger.info("Rollback requested, but no applied migrations were found")
            return 0

        applied: dict[str, str | None] = {row["name"]: row["checksum"] for row in rows}
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
                        f"Migration {migration_name} cannot be rolled back: forward file missing"
                    )
                continue

            current_checksum = _file_checksum(migration_file)
            recorded = applied.get(migration_name)
            if recorded and current_checksum != recorded:
                logger.warning(
                    "Rollback for %s using modified forward migration (recorded: %s, current: %s)",
                    migration_name,
                    recorded[:12],
                    current_checksum[:12],
                )

            up_metadata = _parse_migration_metadata(migration_file)
            down_file = migration_file.with_name(f"{migration_file.stem}.down.sql")

            if not down_file.exists() or up_metadata.get("rollback") == "irreversible":
                reason = (
                    "marked irreversible"
                    if up_metadata.get("rollback") == "irreversible"
                    else "missing .down.sql file"
                )
                logger.warning("Migration %s is irreversible (%s)", migration_name, reason)
                if not allow_irreversible:
                    raise RuntimeError(f"Migration {migration_name} is irreversible ({reason})")
                continue

            down_metadata = _parse_migration_metadata(down_file)
            if down_metadata.get("rollback") == "irreversible":
                logger.warning(
                    "Migration %s rollback file marks migration as irreversible",
                    migration_name,
                )
                if not allow_irreversible:
                    raise RuntimeError(
                        f"Migration {migration_name} is irreversible (rollback metadata)"
                    )
                continue

            warning = down_metadata.get("warning")
            if warning:
                logger.warning("Rollback warning for %s: %s", migration_name, warning)

            rollback_sql = down_file.read_text()
            if not rollback_sql.strip():
                logger.warning("Rollback file for %s is empty", migration_name)
                if not allow_irreversible:
                    raise RuntimeError(
                        f"Migration {migration_name} is irreversible (empty rollback file)"
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
        logger.info("Migrated %d entries from legacy _migrations table", len(migrated))

    await conn.execute("DROP TABLE _migrations")
    logger.info("Dropped legacy _migrations table")


def _file_checksum(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's UTF-8 content."""
    return hashlib.sha256(path.read_text().encode()).hexdigest()


def _discover_forward_migration_files(migrations_dir: Path) -> list[Path]:
    """Return sorted forward migration files, excluding rollback files."""
    return sorted(
        path for path in migrations_dir.glob("*.sql") if not path.name.endswith(".down.sql")
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
    before yielding. Verified against live asyncpg behavior (scratch-cluster
    probe, this wave): transaction-local ``set_config(..., true)`` set before
    yielding **vanishes before the caller's first statement**, because asyncpg
    runs each top-level ``execute()``/``fetch()`` in its own implicit
    transaction that commits immediately. Session-local values survive
    implicit transactions, apply to every statement in the block, and are
    cleared on the connection's return by the pool reset hook
    (``_reset_connection``) — this function only ever rides the process pool
    (:func:`get_pool`), so no separate release-side scrub round trip happens
    here (one round trip per block, halved 2026-10-06; tenant context never
    leaks to the next acquire). ``scoped_acquire_on`` keeps its own scrub
    because it accepts foreign pools without the reset hook.
    Explicit parameters win; with no explicit parameters the task's scope
    (``set_tenant_scope``) is used.

    Fail-closed: if the resolved scope carries no organization_id and the
    role is not a permissive branch (``daemon``/``system``), the acquire
    raises ``TenantScopeError`` instead of running unscoped.
    """
    scope = current_tenant_scope()
    resolved_user = str(user_id) if user_id else (scope or {}).get("user_id", "")
    resolved_org = (
        str(organization_id) if organization_id else (scope or {}).get("organization_id", "")
    )
    resolved_role = role or (scope or {}).get("role", "member")

    if not resolved_org and resolved_role not in ("daemon", "system"):
        raise TenantScopeError(
            "scoped_acquire() refused: no tenant context on this task. "
            "Set explicit user_id/organization_id, or set_tenant_scope(...) "
            "for the daemon/system role branches. Tenant-scoped tables return "
            "zero rows without context."
        )

    pool = await get_pool()
    audit_scope = effective_scope(
        scope,
        user_id=user_id,
        organization_id=organization_id,
        role=role,
    )
    async with pool.acquire() as conn:
        conn = ScopeAuditedConnection(conn, audit_scope)
        await conn.execute(
            "SELECT set_config('app.user_id', $1, false), "
            "set_config('app.org_id', $2, false), "
            "set_config('app.role', $3, false);",
            resolved_user,
            resolved_org,
            resolved_role or "member",
        )
        yield conn


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
        str(organization_id) if organization_id else (scope or {}).get("organization_id", "")
    )
    resolved_role = role or (scope or {}).get("role", "member")

    if not resolved_org and resolved_role not in ("daemon", "system"):
        raise TenantScopeError(
            "scoped_acquire_on() refused: no tenant context for this pool. "
            "Pass explicit user_id/organization_id, or a daemon/system role "
            "branch. Tenant-scoped tables return zero rows without context."
        )

    async with pool.acquire() as conn:
        audit_scope = effective_scope(
            scope,
            user_id=user_id,
            organization_id=organization_id,
            role=role,
        )
        conn = ScopeAuditedConnection(conn, audit_scope)
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


# ---------------------------------------------------------------------------
# Authorization-aware pool access
# ---------------------------------------------------------------------------

IDENTIFIER_PATTERN: Final[str] = r"[A-Za-z_][A-Za-z0-9_]*"
SQL_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "and",
        "as",
        "case",
        "cross",
        "else",
        "end",
        "except",
        "fetch",
        "for",
        "from",
        "full",
        "group",
        "having",
        "inner",
        "intersect",
        "into",
        "join",
        "lateral",
        "left",
        "limit",
        "natural",
        "offset",
        "on",
        "order",
        "outer",
        "returning",
        "right",
        "select",
        "set",
        "then",
        "true",
        "union",
        "using",
        "when",
        "where",
        "window",
        "with",
    }
)


def _table_reference_pattern(table: str) -> re.Pattern[str]:
    """Regex matching every SQL position that can hold a table reference.

    Covers FROM/JOIN position and comma-separated FROM lists
    (``FROM tasks a, tasks b``), plus a quoted-identifier form. The
    negative lookahead rejects qualified columns (``tasks.owner_id``) and
    prefix-named tables (``tasks_extra``), which are never table positions.
    """
    escaped = re.escape(table)
    return re.compile(
        r"(?:\b(?:FROM|JOIN)\s+|,\s*)"
        rf"(?:public\s*\.\s*)?(?:\"{escaped}\"|{escaped})(?![A-Za-z0-9_$.])",
        flags=re.IGNORECASE,
    )


def _count_table_references(query: str, table: str) -> int:
    """Count table-position references to ``table``: FROM/JOIN and comma lists.

    ``FROM tasks a, tasks b`` and ``JOIN tasks ...`` both count. A bare
    ``tasks`` after a comma in another position (``ORDER BY title, projects``)
    counts too — the SQL positions are ambiguous to a regex, and a missed
    reference would run unscoped; queries like that fail closed instead.

    Used to detect references the rewrite pattern would have to leave
    untouched (quoted identifiers cannot be re-scoped): any occurrence the
    rewrite does not consume makes the caller fail the whole query.
    """
    return len(_table_reference_pattern(table).findall(query))


@dataclass(frozen=True)
class AuthPrincipal:
    """The explicit database authorization context for one request."""

    user_id: UUID
    username: str
    organization_id: UUID
    group_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class AuthTablePolicy:
    """How one resource table is authorized by auth-ID clearances.

    ``direct_columns`` is useful for resources that also retain legacy
    ownership columns. An empty tuple means the resource must be granted
    through ``auth_clearances``.
    """

    table: str
    auth_id_column: str = "auth_id"
    direct_columns: tuple[str, ...] = ("user_id", "organization_id")

    def __post_init__(self) -> None:
        # Validate at construction: a policy with an unmappable direct column
        # must fail loudly where it is declared, not in the middle of a query.
        self.validate()

    def validate(self) -> None:
        if not self.table or not re.fullmatch(IDENTIFIER_PATTERN, self.table):
            raise ValueError("table must be a non-empty SQL identifier")
        if self.auth_id_column and not re.fullmatch(IDENTIFIER_PATTERN, self.auth_id_column):
            raise ValueError("auth_id_column must be a SQL identifier")
        for column in self.direct_columns:
            if not re.fullmatch(IDENTIFIER_PATTERN, column):
                raise ValueError(f"direct column is not a SQL identifier: {column}")
            if column not in DIRECT_COLUMN_GUCS:
                raise ValueError(
                    f"direct column has no principal session context mapping: {column}; "
                    f"supported columns are {', '.join(sorted(DIRECT_COLUMN_GUCS))}"
                )
        if len(set(self.direct_columns)) != len(self.direct_columns):
            raise ValueError("direct_columns must be unique")


DIRECT_COLUMN_GUCS: Final[dict[str, str]] = {
    # Only these resource columns map onto a principal context GUC. Anything
    # else in direct_columns would compile to `current_setting('app.<column>')`
    # — a GUC nothing ever sets, i.e. a silently always-false predicate.
    "user_id": "app.user_id",
    "organization_id": "app.org_id",
}


class AuthAccessRole(str, Enum):
    """Resource access role granted to one principal."""

    READ = "read"
    WRITE = "write"
    OWNER = "owner"


AUTH_ACCESS_ROLE_RANKS: Final[dict[AuthAccessRole, int]] = {
    AuthAccessRole.READ: 1,
    AuthAccessRole.WRITE: 2,
    AuthAccessRole.OWNER: 3,
}


@functools.lru_cache(maxsize=256)
def _authorized_table_sql(policy: AuthTablePolicy) -> str:
    """Compile the clearance-scoped SQL for one resource table.

    Cached: the output depends only on the (frozen, hashable) policy.
    """
    resource = "resource"
    authorization = "clearance"
    predicates = [f"{authorization}.authorized IS NOT NULL"]
    for column in policy.direct_columns:
        predicates.append(
            f"{resource}.{column} = current_setting('{DIRECT_COLUMN_GUCS[column]}', true)::uuid"
        )
    where_predicate = "(" + " OR ".join(predicates) + ")"
    return f"""
        SELECT {resource}.*
        FROM {policy.table} AS {resource}
        LEFT JOIN LATERAL (
            SELECT 1 AS authorized
            FROM (
                SELECT
                    current_setting('app.user_id', true)::uuid AS user_id,
                    current_setting('app.org_id', true)::uuid AS org_id,
                    COALESCE(
                        string_to_array(
                            NULLIF(current_setting('app.auth_group_ids', true), ''),
                            ','
                        )::uuid[],
                        ARRAY[]::uuid[]
                    ) AS group_ids
            ) AS principal
            JOIN auth_clearances AS {authorization}
              ON {authorization}.auth_id = {resource}.{policy.auth_id_column}
             AND CASE {authorization}.role
                   WHEN 'read' THEN {AUTH_ACCESS_ROLE_RANKS[AuthAccessRole.READ]}
                   WHEN 'write' THEN {AUTH_ACCESS_ROLE_RANKS[AuthAccessRole.WRITE]}
                   WHEN 'owner' THEN {AUTH_ACCESS_ROLE_RANKS[AuthAccessRole.OWNER]}
                   ELSE 0
                 END >= CASE current_setting('app.auth_context', true)
                   WHEN 'read' THEN {AUTH_ACCESS_ROLE_RANKS[AuthAccessRole.READ]}
                   WHEN 'write' THEN {AUTH_ACCESS_ROLE_RANKS[AuthAccessRole.WRITE]}
                   WHEN 'owner' THEN {AUTH_ACCESS_ROLE_RANKS[AuthAccessRole.OWNER]}
                   ELSE 0
                 END
             AND CASE {authorization}.principal_type
                   WHEN 'user' THEN {authorization}.principal_id = principal.user_id
                   WHEN 'group' THEN {authorization}.principal_id = ANY(principal.group_ids)
                   WHEN 'org' THEN {authorization}.principal_id = principal.org_id
                   ELSE FALSE
                 END
            LIMIT 1
        ) AS {authorization} ON TRUE
        WHERE {where_predicate}
    """


@dataclass(frozen=True)
class AuthorizedDatabasePool:
    """A pool whose configured resource queries are auth-ID scoped."""

    pool: Pool
    principal: AuthPrincipal
    table_policies: tuple[AuthTablePolicy, ...]
    required_role: AuthAccessRole | str = AuthAccessRole.READ

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(policy.table for policy in self.table_policies)

    def _authorized_table_sql(self, policy: AuthTablePolicy) -> str:
        return _authorized_table_sql(policy)

    def _prepare_query(self, query: str) -> str:
        return _cached_prepare_query(self.table_policies, query)

    def _reject_shadowed_tables(self, query: str) -> None:
        _reject_shadowed_tables(self.table_policies, query)

    def _reject_manual_auth_tables(self, query: str) -> None:
        _reject_manual_auth_tables(self.table_policies, query)

    def _reject_writes(self, query: str) -> None:
        _reject_writes(self.table_policies, query)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator["AuthorizedDatabaseConnection"]:
        """Acquire a connection carrying this principal's clearance context.

        One round trip: a single statement sets all four session-local GUCs.
        The release-side scrub is the pool reset hook's job
        (``_reset_connection``, which now also clears ``app.auth_group_ids``)
        — this pool only wraps the process pool, and dropping the in-block
        scrub halves the per-acquire round trips.
        """
        async with self.pool.acquire() as connection:
            await connection.execute(
                "SELECT set_config('app.user_id', $1, false), "
                "set_config('app.org_id', $2, false), "
                "set_config('app.auth_group_ids', $3, false), "
                "set_config('app.auth_context', $4, false);",
                str(self.principal.user_id),
                str(self.principal.organization_id),
                ",".join(str(group_id) for group_id in self.principal.group_ids),
                AuthAccessRole(self.required_role).value,
            )
            yield AuthorizedDatabaseConnection(connection, self)


@functools.lru_cache(maxsize=2048)
def _cached_prepare_query(
    table_policies: tuple[AuthTablePolicy, ...], query: str
) -> str:
    """Rewrite configured table references while preserving outer SQL.

    Fail-closed: every FROM/JOIN-position reference to a configured table
    must be rewritten. Quoted references (``FROM "projects"``, which the
    rewrite cannot safely re-scope) are detected by reference counting and
    rejected rather than silently left unscoped.

    Cached by (policies, query): the rewrite is deterministic and the safety
    checks below depend only on the same inputs, so a cache hit skips them
    without changing any outcome. lru_cache does not memoize exceptions, so
    rejected queries keep raising AuthorizedQueryError on every call.
    """
    _reject_shadowed_tables(table_policies, query)
    _reject_manual_auth_tables(table_policies, query)
    _reject_writes(table_policies, query)
    rewritten = query
    matched = False
    for policy in table_policies:
        total_references = _count_table_references(query, policy.table)
        authorized_sql = _authorized_table_sql(policy).strip()
        pattern = (
            r"(?P<lead>\b(?:FROM|JOIN)\s+|,\s*)"
            r"(?:public\s*\.\s*)?"
            rf"(?P<table>{re.escape(policy.table)})(?![A-Za-z0-9_$.])"
            rf"(?P<alias>\s+(?:AS\s+)?"
            rf"(?P<alias_name>(?!(?:{'|'.join(SQL_KEYWORDS)}))\b"
            rf"{IDENTIFIER_PATTERN}))?"
        )

        def replace(match: re.Match[str]) -> str:
            nonlocal matched
            matched = True
            alias = match.group("alias_name") or policy.table
            # Drop any schema qualifier: the replacement is a subquery,
            # which cannot carry a "public." prefix.
            return f"{match.group('lead')}({authorized_sql}) AS {alias}"

        rewritten, rewritten_count = re.subn(
            pattern, replace, rewritten, flags=re.IGNORECASE
        )
        if rewritten_count < total_references:
            raise AuthorizedQueryError(
                f"Authorized table {policy.table} must be referenced with "
                "an unquoted identifier; quoted references cannot be "
                "re-scoped safely"
            )
        if rewritten_count:
            matched = True
    if not matched:
        raise AuthorizedQueryError(
            "Authorized connection only supports configured tables: "
            + ", ".join(policy.table for policy in table_policies)
        )
    return rewritten


def _reject_shadowed_tables(table_policies: tuple[AuthTablePolicy, ...], query: str) -> None:
    for policy in table_policies:
        pattern = (
            rf"\b(?:WITH|,)\s+{re.escape(policy.table)}"
            rf"(?:\s*\([^)]*\))?\s+AS\s+(?:NOT\s+)?MATERIALIZED\s*\("
            rf"|\b(?:WITH|,)\s+{re.escape(policy.table)}"
            rf"(?:\s*\([^)]*\))?\s+AS\s*\("
        )
        if re.search(pattern, query, flags=re.IGNORECASE):
            raise AuthorizedQueryError(
                f"A CTE shadows authorized table {policy.table}; use a distinct CTE name"
            )


def _reject_manual_auth_tables(table_policies: tuple[AuthTablePolicy, ...], query: str) -> None:
    if re.search(r"\bauth_clearances\b|\bauth_ids\b", query, flags=re.IGNORECASE):
        raise AuthorizedQueryError(
            "Manual auth-table references are not allowed on authorized connections"
        )


def _reject_writes(table_policies: tuple[AuthTablePolicy, ...], query: str) -> None:
    if re.search(r"\bset_config\s*\(", query, flags=re.IGNORECASE):
        raise AuthorizedQueryError("Authorized connections cannot change principal context")
    if re.search(r"\b(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE)\b", query, flags=re.IGNORECASE):
        raise AuthorizedQueryError("Authorized database connections are read-only")


@dataclass
class AuthorizedDatabaseConnection:
    """Connection wrapper applying the pool's authorization contract."""

    _connection: Connection
    _pool: AuthorizedDatabasePool

    @property
    def principal(self) -> AuthPrincipal:
        return self._pool.principal

    async def fetch(self, query: str, *parameters: object) -> list[asyncpg.Record]:
        return await self._query("fetch", query, parameters)

    async def fetchrow(self, query: str, *parameters: object) -> asyncpg.Record | None:
        return await self._query("fetchrow", query, parameters)

    async def fetchval(self, query: str, *parameters: object) -> Any | None:
        return await self._query("fetchval", query, parameters)

    async def _query(self, method_name: str, query: str, parameters: tuple[object, ...]) -> Any:
        prepared_query = self._pool._prepare_query(query)
        return await getattr(self._connection, method_name)(prepared_query, *parameters)


class AuthorizedQueryError(RuntimeError):
    """Raised when a query is unsafe for automatic auth-ID authorization."""


def _normalize_table_policies(
    table_policies: str | AuthTablePolicy | tuple[AuthTablePolicy, ...],
) -> tuple[AuthTablePolicy, ...]:
    """Normalize string shorthand into validated, de-duplicated policies."""
    policies = (table_policies,) if isinstance(table_policies, AuthTablePolicy) else table_policies
    if isinstance(policies, str):
        policies = (AuthTablePolicy(table=policies),)
    normalized: list[AuthTablePolicy] = []
    seen_tables: set[str] = set()
    for policy in policies:
        if isinstance(policy, str):
            policy = AuthTablePolicy(table=policy)
        policy.validate()
        if policy.table.lower() in seen_tables:
            raise ValueError(f"Duplicate authorized table policy: {policy.table}")
        seen_tables.add(policy.table.lower())
        normalized.append(policy)
    if not normalized:
        raise ValueError("At least one table policy is required")
    return tuple(normalized)


# Per-principal group sets, cached per process for 5 seconds — the same TTL
# contract as the legacy AccessControlRepository cache. Keyed by (user_id,
# organization_id) because `db/groups.py` is the only `user_groups` writer and
# every membership mutation calls ``access_control.AccessControlService.
# invalidate_user_groups`` (which also drops entries here), a cache entry can
# only outlive a mutation that bypassed that invalidation.
_GROUP_CACHE_TTL: Final[timedelta] = timedelta(seconds=5)
_group_cache: Final[dict[tuple[str, str], tuple[datetime, tuple[UUID, ...]]]] = {}


def invalidate_user_groups(user_id: UUID | str) -> None:
    """Drop the cached group sets for one user (every organization)."""
    user_key = str(user_id)
    for cache_key in [key for key in _group_cache if key[0] == user_key]:
        _group_cache.pop(cache_key, None)


async def _resolve_group_ids(
    pool: Pool,
    user_id: UUID,
    organization_id: UUID,
) -> tuple[UUID, ...]:
    """Resolve the caller's groups within one organization (5s TTL cache)."""
    cache_key = (str(user_id), str(organization_id))
    now = datetime.now(UTC)
    cached = _group_cache.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    group_rows = await pool.fetch(
        "SELECT user_groups.group_id "
        "FROM user_groups JOIN groups ON groups.id = user_groups.group_id "
        "WHERE user_groups.user_id = $1 AND groups.organization_id = $2 "
        "ORDER BY user_groups.group_id",
        user_id,
        organization_id,
    )
    group_ids = tuple(UUID(str(group_row["group_id"])) for group_row in group_rows)
    _group_cache[cache_key] = (now + _GROUP_CACHE_TTL, group_ids)
    return group_ids


async def get_authorized_pool_for_user(
    pool: Pool,
    user: dict[str, Any],
    table_policies: str | AuthTablePolicy | tuple[AuthTablePolicy, ...],
    required_role: AuthAccessRole | str = AuthAccessRole.READ,
) -> AuthorizedDatabasePool:
    """Return an authorized pool for an already-authenticated user record."""
    required_role = AuthAccessRole(required_role)

    normalized = _normalize_table_policies(table_policies)

    try:
        user_id = UUID(str(user["id"]))
        organization_id = UUID(str(user["organization_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Authenticated user is missing a valid identity scope") from exc

    username = str(user.get("display_name") or user.get("email") or user_id)
    principal = AuthPrincipal(
        user_id=user_id,
        username=username,
        organization_id=organization_id,
        group_ids=await _resolve_group_ids(pool, user_id, organization_id),
    )
    return AuthorizedDatabasePool(pool, principal, tuple(normalized), required_role)


async def get_authorized_pool(
    username: str,
    table_policies: str | AuthTablePolicy | tuple[AuthTablePolicy, ...],
    required_role: AuthAccessRole | str = AuthAccessRole.READ,
) -> AuthorizedDatabasePool:
    """Return a pool whose queries are authorized for ``username``."""
    if not username.strip():
        raise ValueError("username is required for authorized pool access")
    required_role = AuthAccessRole(required_role)

    pool = await get_pool()
    policies = (table_policies,) if isinstance(table_policies, AuthTablePolicy) else table_policies
    if isinstance(policies, str):
        policies = (AuthTablePolicy(table=policies),)
    normalized = _normalize_table_policies(policies)

    rows = await pool.fetch(
        "SELECT id AS user_id, COALESCE(display_name, email, id::text) AS username, "
        "organization_id "
        "FROM users "
        "WHERE COALESCE(display_name, email) = $1 AND is_active IS TRUE "
        "ORDER BY organization_id",
        username,
    )
    if not rows or rows[0]["organization_id"] is None:
        raise ValueError(f"Unable to resolve active principal for username: {username}")
    organization_id = UUID(str(rows[0]["organization_id"]))
    if any(UUID(str(row["organization_id"])) != organization_id for row in rows):
        raise ValueError(f"Ambiguous principal across organizations for username: {username}")
    return await get_authorized_pool_for_user(
        pool,
        {
            "id": rows[0]["user_id"],
            "organization_id": rows[0]["organization_id"],
            "display_name": rows[0]["username"],
        },
        normalized,
        required_role,
    )


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
