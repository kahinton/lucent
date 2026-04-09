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

    async def get_least_accessed(
        self,
        organization_id: UUID | None = None,
        user_id: UUID | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get the least frequently accessed memories.

        Includes memories with zero accesses, scoped to a user or organization.

        Args:
            organization_id: Optional filter by organization.
            user_id: Optional filter by user ownership.
            since: Optional datetime to count accesses after.
            limit: Maximum results to return.

        Returns:
            List of {memory_id, access_count, last_accessed} sorted ascending by access_count.
        """
        memory_conditions = ["m.deleted_at IS NULL"]
        memory_params: list[Any] = []
        access_conditions = []
        access_params: list[Any] = []
        param_idx = 1

        if user_id:
            memory_conditions.append(f"m.user_id = ${param_idx}")
            memory_params.append(str(user_id))
            param_idx += 1
            access_conditions.append(f"al.user_id = ${param_idx}")
            access_params.append(str(user_id))
            param_idx += 1

        elif organization_id:
            memory_conditions.append(f"m.organization_id = ${param_idx}")
            memory_params.append(str(organization_id))
            param_idx += 1
            access_conditions.append(f"al.organization_id = ${param_idx}")
            access_params.append(str(organization_id))
            param_idx += 1

        if since:
            access_conditions.append(f"al.accessed_at >= ${param_idx}")
            access_params.append(since)
            param_idx += 1

        access_on_clause = "al.memory_id = m.id"
        if access_conditions:
            access_on_clause += " AND " + " AND ".join(access_conditions)

        where_clause = " AND ".join(memory_conditions)
        query = f"""
            SELECT
                m.id AS memory_id,
                COUNT(al.id) AS access_count,
                MAX(al.accessed_at) AS last_accessed
            FROM memories m
            LEFT JOIN memory_access_log al
                ON {access_on_clause}
            WHERE {where_clause}
            GROUP BY m.id
            ORDER BY access_count ASC, last_accessed ASC NULLS FIRST, m.id ASC
            LIMIT ${param_idx}
        """

        params = [*memory_params, *access_params, limit]

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

    async def get_access_frequency(
        self,
        bucket: str = "day",
        organization_id: UUID | None = None,
        user_id: UUID | None = None,
        since: datetime | None = None,
        limit: int = 90,
    ) -> list[dict[str, Any]]:
        """Get access frequency over time.

        Args:
            bucket: Time bucket, one of 'hour', 'day', 'week'.
            organization_id: Optional filter by organization.
            user_id: Optional filter by user.
            since: Optional datetime lower bound.
            limit: Maximum number of buckets to return.

        Returns:
            List of {bucket_start, access_count} sorted by time descending.
        """
        bucket_map = {"hour": "hour", "day": "day", "week": "week"}
        bucket_unit = bucket_map.get(bucket, "day")

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
            SELECT
                DATE_TRUNC('{bucket_unit}', accessed_at) AS bucket_start,
                COUNT(*) AS access_count
            FROM memory_access_log
            WHERE {where_clause}
            GROUP BY bucket_start
            ORDER BY bucket_start DESC
            LIMIT ${param_idx}
        """

        params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [
            {
                "bucket_start": row["bucket_start"],
                "access_count": row["access_count"],
            }
            for row in rows
        ]

    async def get_access_counts(self, memory_ids: list[UUID]) -> dict[UUID, int]:
        """Get lifetime access counts for specific memories."""
        if not memory_ids:
            return {}

        placeholders = ", ".join(f"${i + 1}" for i in range(len(memory_ids)))
        query = f"""
            SELECT memory_id, COUNT(*) AS access_count
            FROM memory_access_log
            WHERE memory_id IN ({placeholders})
            GROUP BY memory_id
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *[str(mid) for mid in memory_ids])

        counts: dict[UUID, int] = {}
        for row in rows:
            memory_id = (
                row["memory_id"]
                if isinstance(row["memory_id"], UUID)
                else UUID(row["memory_id"])
            )
            counts[memory_id] = row["access_count"]
        return counts

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
