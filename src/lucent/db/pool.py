"""Database connection pool management for Lucent.

This module handles PostgreSQL connection pooling, initialization,
and optional OpenTelemetry instrumentation for query tracing.
"""

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import asyncpg
from asyncpg import Connection, Pool

from lucent.logging import get_logger

logger = get_logger(__name__)

# Global connection pool
_pool: Pool | None = None
_asyncpg_instrumented: bool = False


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


async def init_db(database_url: str | None = None) -> Pool:
    """Initialize the database connection pool and run migrations.

    Args:
        database_url: PostgreSQL connection URL. If not provided, uses DATABASE_URL env var.

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
    )

    logger.info("Database connection pool created (min=2, max=10)")

    # Run migrations
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


async def _run_migrations(pool: Pool) -> None:
    """Run SQL migration files in order, tracking which have been applied.

    Uses a ``schema_migrations`` table to record applied migrations with
    SHA-256 checksums.  Skips previously-run files and warns when a file's
    content has changed since it was applied.
    """
    migrations_dir = Path(__file__).parent / "migrations"

    if not migrations_dir.exists():
        return

    # Get all SQL files sorted by name
    migration_files = sorted(migrations_dir.glob("*.sql"))

    async with pool.acquire() as conn:
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
            if migration_file.name in applied:
                # Verify checksum to detect post-application drift
                current_checksum = _file_checksum(migration_file)
                recorded = applied[migration_file.name]
                if recorded and current_checksum != recorded:
                    logger.warning(
                        "Migration %s modified after application "
                        "(recorded: %s, current: %s)",
                        migration_file.name,
                        recorded[:12],
                        current_checksum[:12],
                    )
                skipped_count += 1
                continue

            sql = migration_file.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
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
