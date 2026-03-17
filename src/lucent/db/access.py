"""Access tracking repository for Lucent.

Handles memory access logging and analytics.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
from asyncpg import Pool


class AccessRepository:
    """Repository for memory access tracking."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def log_access(
        self,
        memory_id: UUID,
        access_type: str,
        user_id: UUID | None = None,
        organization_id: UUID | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Log a memory access and update last_accessed_at.

        Args:
            memory_id: The UUID of the accessed memory.
            access_type: Type of access ('view' or 'search_result').
            user_id: The user who accessed the memory.
            organization_id: The organization context.
            context: Additional context (search query, filters, etc.).
        """
        # Insert access log entry
        log_query = """
            INSERT INTO memory_access_log
                (memory_id, user_id, organization_id, access_type, context)
            VALUES ($1, $2, $3, $4, $5)
        """

        # Update last_accessed_at on the memory
        update_query = """
            UPDATE memories
            SET last_accessed_at = NOW()
            WHERE id = $1 AND deleted_at IS NULL
        """

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    log_query,
                    str(memory_id),
                    str(user_id) if user_id else None,
                    str(organization_id) if organization_id else None,
                    access_type,
                    context or {},
                )
                await conn.execute(update_query, str(memory_id))

    async def log_batch_access(
        self,
        memory_ids: list[UUID],
        access_type: str,
        user_id: UUID | None = None,
        organization_id: UUID | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Log access for multiple memories (e.g., search results).

        Args:
            memory_ids: List of memory UUIDs that were accessed.
            access_type: Type of access ('view' or 'search_result').
            user_id: The user who accessed the memories.
            organization_id: The organization context.
            context: Additional context (search query, filters, etc.).
        """
        if not memory_ids:
            return

        user_id_str = str(user_id) if user_id else None
        org_id_str = str(organization_id) if organization_id else None
        ctx = context or {}

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Batch insert access logs using executemany
                log_query = """
                    INSERT INTO memory_access_log
                        (memory_id, user_id, organization_id, access_type, context)
                    VALUES ($1, $2, $3, $4, $5)
                """

                await conn.executemany(
                    log_query,
                    [(str(mid), user_id_str, org_id_str, access_type, ctx) for mid in memory_ids],
                )

                # Batch update last_accessed_at
                placeholders = ", ".join(f"${i + 1}" for i in range(len(memory_ids)))
                update_query = f"""
                    UPDATE memories
                    SET last_accessed_at = NOW()
                    WHERE id IN ({placeholders}) AND deleted_at IS NULL
                """
                await conn.execute(update_query, *[str(mid) for mid in memory_ids])

    async def get_access_history(
        self,
        memory_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Get the access history for a specific memory.

        Args:
            memory_id: The UUID of the memory.
            limit: Maximum entries to return.
            offset: Pagination offset.

        Returns:
            Dict with access entries and pagination info.
        """
        query = """
            SELECT id, memory_id, user_id, organization_id, access_type,
                   accessed_at, context
            FROM memory_access_log
            WHERE memory_id = $1
            ORDER BY accessed_at DESC
            LIMIT $2 OFFSET $3
        """

        count_query = """
            SELECT COUNT(*) as total
            FROM memory_access_log
            WHERE memory_id = $1
        """

        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(count_query, str(memory_id))
            total_count = count_row["total"] if count_row else 0

            rows = await conn.fetch(query, str(memory_id), limit, offset)

        return {
            "entries": [self._row_to_dict(row) for row in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def get_search_history(
        self,
        memory_id: UUID,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get the search queries that returned this memory.

        Args:
            memory_id: The UUID of the memory.
            limit: Maximum entries to return.

        Returns:
            List of search access entries with query context.
        """
        query = """
            SELECT id, memory_id, user_id, organization_id, access_type,
                   accessed_at, context
            FROM memory_access_log
            WHERE memory_id = $1 AND access_type = 'search_result'
            ORDER BY accessed_at DESC
            LIMIT $2
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, str(memory_id), limit)

        return [self._row_to_dict(row) for row in rows]

    async def get_user_activity(
        self,
        user_id: UUID,
        limit: int = 100,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent memory access activity for a user.

        Args:
            user_id: The UUID of the user.
            limit: Maximum entries to return.
            since: Optional datetime to filter entries after.

        Returns:
            List of access entries.
        """
        conditions = ["user_id = $1"]
        params: list[Any] = [str(user_id)]
        param_idx = 2

        if since:
            conditions.append(f"accessed_at >= ${param_idx}")
            params.append(since)
            param_idx += 1

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT id, memory_id, user_id, organization_id, access_type,
                   accessed_at, context
            FROM memory_access_log
            WHERE {where_clause}
            ORDER BY accessed_at DESC
            LIMIT ${param_idx}
        """

        params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [self._row_to_dict(row) for row in rows]

    async def get_most_accessed(
        self,
        organization_id: UUID | None = None,
        user_id: UUID | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get the most frequently accessed memories.

        Args:
            organization_id: Optional filter by organization.
            user_id: Optional filter by user.
            since: Optional datetime to count accesses after.
            limit: Maximum results to return.

        Returns:
            List of {memory_id, access_count, last_accessed} sorted by access_count.
        """
        conditions = []
        params: list[Any] = []
        param_idx = 1

        if organization_id:
            conditions.append(f"organization_id = ${param_idx}")
            params.append(str(organization_id))
            param_idx += 1

        if user_id:
            conditions.append(f"user_id = ${param_idx}")
            params.append(str(user_id))
            param_idx += 1

        if since:
            conditions.append(f"accessed_at >= ${param_idx}")
            params.append(since)
            param_idx += 1

        where_clause = " AND ".join(conditions) if conditions else "TRUE"

        query = f"""
            SELECT memory_id, COUNT(*) as access_count, MAX(accessed_at) as last_accessed
            FROM memory_access_log
            WHERE {where_clause}
            GROUP BY memory_id
            ORDER BY access_count DESC, last_accessed DESC
            LIMIT ${param_idx}
        """

        params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [
            {
                "memory_id": row["memory_id"]
                if isinstance(row["memory_id"], UUID)
                else UUID(row["memory_id"]),
                "access_count": row["access_count"],
                "last_accessed": row["last_accessed"],
            }
            for row in rows
        ]

    def _row_to_dict(self, row: asyncpg.Record) -> dict[str, Any]:
        """Convert a database row to a dictionary."""
        user_id = None
        if row["user_id"]:
            user_id = row["user_id"] if isinstance(row["user_id"], UUID) else UUID(row["user_id"])

        org_id = None
        if row["organization_id"]:
            org_id = (
                row["organization_id"]
                if isinstance(row["organization_id"], UUID)
                else UUID(row["organization_id"])
            )

        memory_id = (
            row["memory_id"] if isinstance(row["memory_id"], UUID) else UUID(row["memory_id"])
        )

        return {
            "id": row["id"],
            "memory_id": memory_id,
            "user_id": user_id,
            "organization_id": org_id,
            "access_type": row["access_type"],
            "accessed_at": row["accessed_at"],
            "context": row["context"],
        }
