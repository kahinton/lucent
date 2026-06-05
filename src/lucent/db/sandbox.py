"""Database repository for sandbox lifecycle tracking."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


class SandboxRepository:
    """Persistent storage for sandbox records."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(
        self,
        *,
        id: str,
        name: str,
        status: str = "creating",
        image: str = "python:3.12-slim",
        repo_url: str | None = None,
        branch: str | None = None,
        config: dict | None = None,
        container_id: str | None = None,
        task_id: str | None = None,
        request_id: str | None = None,
        organization_id: str | None = None,
        created_by: str | None = None,
    ) -> dict:
        """Insert a new sandbox record."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO sandboxes
                    (id, name, status, image, repo_url, branch, config,
                     container_id, task_id, request_id, organization_id, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                RETURNING *
                """,
                UUID(id),
                name,
                status,
                image,
                repo_url,
                branch,
                json.dumps(config or {}),
                container_id,
                UUID(task_id) if task_id else None,
                UUID(request_id) if request_id else None,
                UUID(organization_id) if organization_id else None,
                UUID(created_by) if created_by else None,
            )
            return dict(row)

    async def update_status(
        self,
        sandbox_id: str,
        status: str,
        *,
        container_id: str | None = None,
        error: str | None = None,
        ready_at: datetime | None = None,
        stopped_at: datetime | None = None,
        destroyed_at: datetime | None = None,
    ) -> dict | None:
        """Update sandbox status and optional metadata."""
        sets = ["status = $2", "updated_at = NOW()"]
        params: list[Any] = [UUID(sandbox_id), status]
        idx = 3

        if container_id is not None:
            sets.append(f"container_id = ${idx}")
            params.append(container_id)
            idx += 1
        if error is not None:
            sets.append(f"error = ${idx}")
            params.append(error)
            idx += 1
        if ready_at is not None:
            sets.append(f"ready_at = ${idx}")
            params.append(ready_at)
            idx += 1
        if stopped_at is not None:
            sets.append(f"stopped_at = ${idx}")
            params.append(stopped_at)
            idx += 1
        if destroyed_at is not None:
            sets.append(f"destroyed_at = ${idx}")
            params.append(destroyed_at)
            idx += 1

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE sandboxes SET {', '.join(sets)} WHERE id = $1 RETURNING *",
                *params,
            )
            return dict(row) if row else None

    async def get(self, sandbox_id: str) -> dict | None:
        """Get a sandbox by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM sandboxes WHERE id = $1", UUID(sandbox_id))
            return dict(row) if row else None

    async def list_all(
        self,
        organization_id: str | None = None,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        """List sandboxes, optionally filtered."""
        conditions = []
        params: list[Any] = []
        idx = 1

        if organization_id:
            conditions.append(f"organization_id = ${idx}")
            params.append(UUID(organization_id))
            idx += 1
        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) AS total FROM sandboxes {where}",
                *params,
            )
            total_count = count_row["total"] if count_row else 0
            params.extend([limit, offset])
            rows = await conn.fetch(
                f"SELECT * FROM sandboxes {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
                *params,
            )
            return {
                "items": [dict(r) for r in rows],
                "total_count": total_count,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(rows) < total_count,
            }

    async def list_active(self, organization_id: str | None = None, limit: int = 25, offset: int = 0) -> dict:
        """List non-destroyed sandboxes."""
        async with self.pool.acquire() as conn:
            if organization_id:
                count_row = await conn.fetchrow(
                    "SELECT COUNT(*) AS total FROM sandboxes WHERE status != 'destroyed' AND organization_id = $1",
                    UUID(organization_id),
                )
                total_count = count_row["total"] if count_row else 0
                rows = await conn.fetch(
                    """SELECT * FROM sandboxes
                       WHERE status != 'destroyed' AND organization_id = $1
                       ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
                    UUID(organization_id),
                    limit,
                    offset,
                )
            else:
                count_row = await conn.fetchrow(
                    "SELECT COUNT(*) AS total FROM sandboxes WHERE status != 'destroyed'"
                )
                total_count = count_row["total"] if count_row else 0
                rows = await conn.fetch(
                    """SELECT * FROM sandboxes WHERE status != 'destroyed'
                       ORDER BY created_at DESC LIMIT $1 OFFSET $2""",
                    limit,
                    offset,
                )
        return {
            "items": [dict(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }
