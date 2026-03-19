"""Database connection pool management for Lucent.

This module handles PostgreSQL connection pooling, initialization,
and optional OpenTelemetry instrumentation for query tracing.
"""

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

    Uses a `_migrations` table to record applied migrations and skip
    previously-run files. This prevents re-executing non-idempotent
    statements on every startup.
    """
    migrations_dir = Path(__file__).parent / "migrations"

    if not migrations_dir.exists():
        return

    # Get all SQL files sorted by name
    migration_files = sorted(migrations_dir.glob("*.sql"))

    async with pool.acquire() as conn:
        # Create tracking table if it doesn't exist
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                name TEXT PRIMARY KEY,
                applied_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)

        # Get already-applied migrations
        applied = set()
        rows = await conn.fetch("SELECT name FROM _migrations")
        for row in rows:
            applied.add(row["name"])

        # Apply new migrations (each in its own transaction for atomicity)
        for migration_file in migration_files:
            if migration_file.name in applied:
                continue

            sql = migration_file.read_text()
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO _migrations (name) VALUES ($1)",
                    migration_file.name,
                )
                logger.info("Applied migration: %s", migration_file.name)


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
