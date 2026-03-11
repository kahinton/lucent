"""Audit repository for Lucent.

Handles audit log operations for tracking memory changes.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
from asyncpg import Pool


class AuditRepository:
    """Repository for audit log operations."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def log(
        self,
        memory_id: UUID,
        action_type: str,
        user_id: UUID | None = None,
        organization_id: UUID | None = None,
        changed_fields: list[str] | None = None,
        old_values: dict[str, Any] | None = None,
        new_values: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        notes: str | None = None,
        version: int | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create an audit log entry.

        Args:
            memory_id: The UUID of the affected memory.
            action_type: Type of action (create, update, delete, etc.).
            user_id: The user who performed the action.
            organization_id: The organization context.
            changed_fields: List of field names that were modified.
            old_values: Previous values of changed fields.
            new_values: New values after the change.
            context: Additional context (IP, user agent, etc.).
            notes: Optional notes about the action.
            version: The version number this entry represents.
            snapshot: Full memory state at this version (for point-in-time restore).

        Returns:
            The created audit log entry.
        """
        query = """
            INSERT INTO memory_audit_log
                (memory_id, user_id, organization_id, action_type,
                 changed_fields, old_values, new_values, context, notes,
                 version, snapshot)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id, memory_id, user_id, organization_id, action_type,
                      created_at, changed_fields, old_values, new_values, context, notes,
                      version, snapshot
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                str(memory_id),
                str(user_id) if user_id else None,
                str(organization_id) if organization_id else None,
                action_type,
                changed_fields,
                old_values,
                new_values,
                context or {},
                notes,
                version,
                snapshot,
            )

        return self._row_to_dict(row)

    async def get_by_memory_id(
        self,
        memory_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Get all audit entries for a specific memory.

        Args:
            memory_id: The UUID of the memory.
            limit: Maximum entries to return.
            offset: Pagination offset.

        Returns:
            Dict with entries list and pagination info.
        """
        query = """
            SELECT id, memory_id, user_id, organization_id, action_type,
                   created_at, changed_fields, old_values, new_values, context, notes
            FROM memory_audit_log
            WHERE memory_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
        """

        count_query = """
            SELECT COUNT(*) as total
            FROM memory_audit_log
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

    # Columns allowed in _get_filtered_entries to prevent SQL injection
    _FILTERABLE_COLUMNS = frozenset({"user_id", "organization_id"})

    async def get_by_user_id(
        self,
        user_id: UUID,
        action_type: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Get all audit entries for actions by a specific user.

        Args:
            user_id: The UUID of the user.
            action_type: Optional filter by action type.
            since: Optional filter to entries after this time.
            limit: Maximum entries to return.
            offset: Pagination offset.

        Returns:
            Dict with entries list and pagination info.
        """
        return await self._get_filtered_entries(
            "user_id", user_id, action_type, since, limit, offset
        )

    async def get_by_organization_id(
        self,
        organization_id: UUID,
        action_type: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Get all audit entries for an organization.

        Args:
            organization_id: The UUID of the organization.
            action_type: Optional filter by action type.
            since: Optional filter to entries after this time.
            limit: Maximum entries to return.
            offset: Pagination offset.

        Returns:
            Dict with entries list and pagination info.
        """
        return await self._get_filtered_entries(
            "organization_id", organization_id, action_type, since, limit, offset
        )

    async def _get_filtered_entries(
        self,
        filter_column: str,
        filter_value: UUID,
        action_type: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Get paginated audit entries filtered by a primary column.

        Shared implementation for get_by_user_id and get_by_organization_id.

        Args:
            filter_column: Column to filter on (must be in _FILTERABLE_COLUMNS).
            filter_value: UUID value to match.
            action_type: Optional filter by action type.
            since: Optional filter to entries after this time.
            limit: Maximum entries to return.
            offset: Pagination offset.

        Returns:
            Dict with entries list and pagination info.
        """
        if filter_column not in self._FILTERABLE_COLUMNS:
            raise ValueError(f"Invalid filter column: {filter_column}")

        conditions = [f"{filter_column} = $1"]
        params: list[Any] = [str(filter_value)]
        param_idx = 2

        if action_type:
            conditions.append(f"action_type = ${param_idx}")
            params.append(action_type)
            param_idx += 1

        if since:
            conditions.append(f"created_at >= ${param_idx}")
            params.append(since)
            param_idx += 1

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT id, memory_id, user_id, organization_id, action_type,
                   created_at, changed_fields, old_values, new_values, context, notes
            FROM memory_audit_log
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ${param_idx} OFFSET ${param_idx + 1}
        """

        count_query = f"""
            SELECT COUNT(*) as total
            FROM memory_audit_log
            WHERE {where_clause}
        """

        params.extend([limit, offset])

        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(count_query, *params[:-2])
            total_count = count_row["total"] if count_row else 0

            rows = await conn.fetch(query, *params)

        return {
            "entries": [self._row_to_dict(row) for row in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def get_recent(
        self,
        organization_id: UUID | None = None,
        action_types: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get recent audit entries, optionally filtered.

        Useful for admin dashboards and monitoring.

        Args:
            organization_id: Optional filter by organization.
            action_types: Optional filter by action types.
            since: Optional datetime to get entries after.
            limit: Maximum entries to return.

        Returns:
            List of recent audit entries.
        """
        conditions = []
        params: list[Any] = []
        param_idx = 1

        if organization_id:
            conditions.append(f"organization_id = ${param_idx}")
            params.append(str(organization_id))
            param_idx += 1

        if action_types:
            placeholders = ", ".join(f"${i}" for i in range(param_idx, param_idx + len(action_types)))
            conditions.append(f"action_type IN ({placeholders})")
            params.extend(action_types)
            param_idx += len(action_types)

        if since:
            conditions.append(f"created_at >= ${param_idx}")
            params.append(since)
            param_idx += 1

        where_clause = " AND ".join(conditions) if conditions else "TRUE"

        query = f"""
            SELECT id, memory_id, user_id, organization_id, action_type,
                   created_at, changed_fields, old_values, new_values, context, notes
            FROM memory_audit_log
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ${param_idx}
        """

        params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [self._row_to_dict(row) for row in rows]

    async def get_versions(
        self,
        memory_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Get version history for a specific memory.

        Returns audit entries that have a version number, ordered by
        version descending (newest first).

        Args:
            memory_id: The UUID of the memory.
            limit: Maximum entries to return.
            offset: Pagination offset.

        Returns:
            Dict with versions list and pagination info.
        """
        query = """
            SELECT id, memory_id, user_id, organization_id, action_type,
                   created_at, changed_fields, old_values, new_values, context, notes,
                   version, snapshot
            FROM memory_audit_log
            WHERE memory_id = $1 AND version IS NOT NULL
            ORDER BY version DESC
            LIMIT $2 OFFSET $3
        """

        count_query = """
            SELECT COUNT(*) as total
            FROM memory_audit_log
            WHERE memory_id = $1 AND version IS NOT NULL
        """

        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(count_query, str(memory_id))
            total_count = count_row["total"] if count_row else 0

            rows = await conn.fetch(query, str(memory_id), limit, offset)

        return {
            "versions": [self._row_to_dict(row) for row in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def get_version_snapshot(
        self,
        memory_id: UUID,
        version: int,
    ) -> dict[str, Any] | None:
        """Get the snapshot for a specific version of a memory.

        Args:
            memory_id: The UUID of the memory.
            version: The version number to retrieve.

        Returns:
            The audit entry with snapshot, or None if not found.
        """
        query = """
            SELECT id, memory_id, user_id, organization_id, action_type,
                   created_at, changed_fields, old_values, new_values, context, notes,
                   version, snapshot
            FROM memory_audit_log
            WHERE memory_id = $1 AND version = $2
            LIMIT 1
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(memory_id), version)

        if row is None:
            return None

        return self._row_to_dict(row)

    def _row_to_dict(self, row: asyncpg.Record) -> dict[str, Any]:
        """Convert a database row to a dictionary."""
        # Handle UUIDs
        user_id = None
        if row["user_id"]:
            user_id = row["user_id"] if isinstance(row["user_id"], UUID) else UUID(row["user_id"])

        org_id = None
        if row["organization_id"]:
            org_id = row["organization_id"] if isinstance(row["organization_id"], UUID) else UUID(row["organization_id"])

        memory_id = row["memory_id"] if isinstance(row["memory_id"], UUID) else UUID(row["memory_id"])

        result = {
            "id": row["id"],
            "memory_id": memory_id,
            "user_id": user_id,
            "organization_id": org_id,
            "action_type": row["action_type"],
            "created_at": row["created_at"],
            "changed_fields": row["changed_fields"],
            "old_values": row["old_values"],
            "new_values": row["new_values"],
            "context": row["context"],
            "notes": row["notes"],
        }

        # Include version and snapshot if present in the row
        if "version" in row.keys():
            result["version"] = row["version"]
        if "snapshot" in row.keys():
            result["snapshot"] = row["snapshot"]

        return result
