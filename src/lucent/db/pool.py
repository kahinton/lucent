"""Database connection pool management for Lucent.

This module handles PostgreSQL connection pooling and initialization.
"""

import json
import os
from pathlib import Path
from uuid import UUID

import asyncpg
from asyncpg import Pool, Connection

# Global connection pool
_pool: Pool | None = None


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
    
    # Create the connection pool
    _pool = await asyncpg.create_pool(
        url,
        min_size=2,
        max_size=10,
        command_timeout=60,
        init=_init_connection,
    )
    
    # Run migrations
    await _run_migrations(_pool)
    
    return _pool


async def _init_connection(conn: Connection) -> None:
    """Initialize each connection with custom type codecs."""
    # Register UUID codec
    await conn.set_type_codec(
        'uuid',
        encoder=str,
        decoder=lambda x: UUID(x) if x else None,
        schema='pg_catalog',
    )
    # Register JSON codec for JSONB
    await conn.set_type_codec(
        'jsonb',
        encoder=json.dumps,
        decoder=json.loads,
        schema='pg_catalog',
    )


async def _run_migrations(pool: Pool) -> None:
    """Run SQL migration files in order."""
    migrations_dir = Path(__file__).parent / "migrations"
    
    if not migrations_dir.exists():
        return
    
    # Get all SQL files sorted by name
    migration_files = sorted(migrations_dir.glob("*.sql"))
    
    async with pool.acquire() as conn:
        for migration_file in migration_files:
            sql = migration_file.read_text()
            await conn.execute(sql)


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
    """Close the database connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
