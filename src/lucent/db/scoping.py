"""
Database scoping enforcement for Lucent.

This module provides a fail-closed scoping mechanism. Any database connection
obtained through this module must be explicitly bound to an organization and user.
If a query is attempted on a ScopedConnection without these being set, it raises
a ScopingError.
"""

from uuid import UUID
from typing import Any, AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg
from asyncpg import Connection, Pool

class ScopingError(Exception):
    """Raised when a database operation is attempted without a valid tenant/user scope."""
    pass

class ScopedConnection:
    """
    A proxy for asyncpg.Connection that enforces fail-closed scoping.
    Any call to fetch, fetchrow, fetchval, or execute will raise ScopingError
    if org_id or user_id are not explicitly provided.
    """
    def __init__(self, conn: Connection, org_id: UUID | None = None, user_id: UUID | None = None):
        self._conn = conn
        self._org_id = org_id
        self._user_id = user_id

    def _verify_scope(self):
        if self._org_id is None or self._user_id is None:
            raise ScopingError(
                f"Database operation attempted without complete scope. "
                f"org_id: {self._org_id}, user_id: {self._user_id}"
            )

    async def fetch(self, query: str, *args: Any):
        self._verify_scope()
        return await self._conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any):
        self._verify_scope()
        return await self._conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any):
        self._verify_scope()
        return await self._conn.fetchval(query, *args)

    async def execute(self, query: str, *args: Any):
        self._verify_scope()
        return await self._conn.execute(query, *args)

    async def executemany(self, query: str, args: list):
        self._verify_scope()
        return await self._conn.executemany(query, args)

    async def transaction(self):
        return await self._conn.transaction()

    def __getattr__(self, name):
        return getattr(self._conn, name)

@asynccontextmanager
async def scoped_connection(pool: Pool, org_id: UUID | None, user_id: UUID | None) -> AsyncGenerator[ScopedConnection, None]:
    """
    Acquire a connection from the pool and wrap it in a ScopedConnection.
    The connection is automatically released back to the pool when the context exits.
    """
    async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
        yield ScopedConnection(conn, org_id, user_id)
