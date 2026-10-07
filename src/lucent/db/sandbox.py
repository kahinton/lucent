"""Database repository for sandbox lifecycle tracking."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from lucent.db.pool import AuthorizedDatabasePool, scoped_acquire
from lucent.db.sandbox_template import SANDBOX_AUTH_POLICY, get_authorized_sandboxes_pool

logger = logging.getLogger(__name__)

__all__ = ["SANDBOX_AUTH_POLICY", "SandboxRepository", "get_authorized_sandboxes_pool"]


def _sanitize_runtime_config(config: dict | None) -> dict:
    """Return a database-safe copy of runtime sandbox configuration."""
    sanitized = dict(config or {})
    sanitized.pop("git_credentials", None)
    env_vars = sanitized.get("env_vars")
    if isinstance(env_vars, dict):
        sanitized["env_vars"] = {key: "***" for key in env_vars}
    return sanitized


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
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO sandboxes
                    (id, name, status, image, repo_url, branch, config,
                     container_id, task_id, request_id, organization_id, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12)
                RETURNING *
                """,
                UUID(id),
                name,
                status,
                image,
                repo_url,
                branch,
                _sanitize_runtime_config(config),
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
        organization_id: str | None = None,
    ) -> dict | None:
        """Update sandbox status and optional metadata.

        organization_id=None is the system-infra branch: lifecycle
        bookkeeping (create ready/failed, stop, destroy) runs ahead of any
        user context — the row ID came from the provisioning path itself,
        never from user input.
        """
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

        async with scoped_acquire(
            organization_id=organization_id, role="system" if not organization_id else None
        ) as conn:
            query = f"UPDATE sandboxes SET {', '.join(sets)} WHERE id = $1"
            if organization_id:
                # Fresh slot: optional fields already consume $3, $4, ...
                params.append(UUID(organization_id))
                query += f" AND organization_id = ${len(params)}"
            row = await conn.fetchrow(
                query + " RETURNING *",
                *params,
            )
            return dict(row) if row else None

    async def get(self, sandbox_id: str, organization_id: str | None = None) -> dict | None:
        """Get a sandbox by ID.

        User-facing callers pass organization_id so another org's row is
        simply absent. None is the system-infra branch (SandboxManager
        lifecycle bookkeeping) — it reaches every row and must stay
        reserved for callers with no user behind them.
        """
        async with scoped_acquire(
            organization_id=organization_id, role="system" if not organization_id else None
        ) as conn:
            query = "SELECT * FROM sandboxes WHERE id = $1"
            params: list[object] = [UUID(sandbox_id)]
            if organization_id:
                query += " AND organization_id = $2"
                params.append(UUID(organization_id))
            row = await conn.fetchrow(query, *params)
            return dict(row) if row else None

    async def find_reusable_for_request(
        self,
        *,
        request_id: str,
        organization_id: str,
        reuse_key: str,
        before_sequence_order: int,
    ) -> dict | None:
        """Find the latest live request sandbox created by an earlier task.

        Requiring an earlier sequence level prevents two parallel tasks in the
        same batch from accidentally sharing a mutable workspace.
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(
                """SELECT * FROM sandboxes
                   WHERE request_id = $1
                     AND organization_id = $2
                     AND status IN ('ready', 'running')
                     AND config->>'reuse_key' = $3
                     AND COALESCE((config->>'reuse_sequence_order')::int, -1) < $4
                   ORDER BY created_at DESC
                   LIMIT 1""",
                UUID(request_id),
                UUID(organization_id),
                reuse_key,
                before_sequence_order,
            )
        return dict(row) if row else None

    async def list_for_credential_migration(self, organization_id: str) -> list[dict]:
        """List sandbox rows that may contain plaintext runtime credentials."""
        async with scoped_acquire(organization_id=organization_id) as conn:
            rows = await conn.fetch(
                """
                SELECT id, organization_id, created_by, config
                FROM sandboxes
                WHERE organization_id = $1
                """,
                organization_id,
            )
        return [dict(row) for row in rows]

    async def redact_runtime_configs(self, organization_id: str) -> int:
        """Replace plaintext runtime env vars with redacted placeholders."""
        async with scoped_acquire(organization_id=organization_id) as conn:
            result = await conn.execute(
                """
                WITH normalized_configs AS (
                    SELECT
                        id,
                        CASE
                            WHEN jsonb_typeof(config) = 'string'
                            THEN (config #>> '{}')::jsonb
                            ELSE config
                        END AS config
                    FROM sandboxes
                    WHERE organization_id = $1
                      AND (
                          jsonb_typeof(config) = 'object'
                          OR (
                              jsonb_typeof(config) = 'string'
                              AND config #>> '{}' LIKE '{%'
                          )
                      )
                )
                UPDATE sandboxes AS sandbox
                SET config = CASE
                        WHEN jsonb_typeof(normalized.config->'env_vars') = 'object' THEN jsonb_set(
                            normalized.config - 'git_credentials',
                            '{env_vars}',
                            COALESCE(
                                (
                                    SELECT jsonb_object_agg(key, to_jsonb('***'::text))
                                    FROM jsonb_each(normalized.config->'env_vars') AS entry(key, value)
                                ),
                                '{}'::jsonb
                            ),
                            true
                        )
                        ELSE normalized.config - 'git_credentials'
                    END,
                    updated_at = NOW()
                FROM normalized_configs AS normalized
                WHERE sandbox.id = normalized.id
                  AND jsonb_typeof(normalized.config) = 'object'
                  AND (
                      normalized.config ? 'git_credentials'
                      OR (
                          jsonb_typeof(normalized.config->'env_vars') = 'object'
                          AND EXISTS (
                              SELECT 1
                              FROM jsonb_each_text(normalized.config->'env_vars') AS entry(key, value)
                              WHERE value <> '***'
                          )
                      )
                  )
                """,
                organization_id,
            )
        return int(result.rsplit(" ", 1)[-1])

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

        async with scoped_acquire(organization_id=organization_id) as conn:
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
        """List non-destroyed sandboxes (legacy org-wide read).

        The usage path — a member's own sandboxes — uses the clearance-driven
        ``list_sandboxes_accessible_by`` below; admin/owner listings keep
        using org-scoped ``list_all``/``list_active``.
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
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

    # ── Clearance-driven usage reads ──────────────────────────────────────
    #
    # Sandbox instances are "only yours" (Kyle, 2026-10-01): members see and
    # control only sandboxes created for them; admins/owners manage all via
    # the legacy org-scoped listings above. Ownership clearances are filed by
    # the 123 owner trigger (created_by); explicit grants live in
    # auth_clearances. A plain pool is fail-closed.

    async def _authorized_fetchrow(self, query: str, *params: object):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *params)

    async def _authorized_fetch(self, query: str, *params: object):
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *params)

    async def list_sandboxes_accessible_by(
        self,
        user_id: str,
        organization_id: str,
        *,
        exclude_destroyed: bool = True,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        """List sandboxes cleared for *use* by the pool's principal.

        Clearance-driven and default-deny on an authorized pool. The org
        predicate stays in the caller's SQL (defense in depth); the
        destroyed filter stays the caller's lifecycle filter.
        """
        if not isinstance(self.pool, AuthorizedDatabasePool):
            raise TypeError(
                "Sandbox usage reads require an AuthorizedDatabasePool "
                "built for the sandboxes policy (usage is clearance-driven "
                "and default-deny)"
            )
        params: list[Any] = [UUID(organization_id)]
        status_clause = ""
        if exclude_destroyed:
            status_clause = f" AND sb.status != ${len(params) + 1}"
            params.append("destroyed")
        count_query = (
            f"SELECT COUNT(*) AS total FROM sandboxes sb "
            f"WHERE sb.organization_id = $1{status_clause}"
        )
        query = (
            f"SELECT sb.* FROM sandboxes sb "
            f"WHERE sb.organization_id = $1{status_clause} "
            f"ORDER BY sb.created_at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
        params_with_page = [*params, limit, offset]

        count_row = await self._authorized_fetchrow(count_query, *params)
        total_count = count_row["total"] if count_row else 0
        rows = await self._authorized_fetch(query, *params_with_page)
        return {
            "items": [dict(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def get_usable_sandbox(self, sandbox_id: str) -> dict | None:
        """By-ID *usage* probe: the sandbox must be cleared for this principal."""
        if not isinstance(self.pool, AuthorizedDatabasePool):
            raise TypeError("Sandbox usage reads require an AuthorizedDatabasePool")
        row = await self._authorized_fetchrow(
            "SELECT * FROM sandboxes WHERE id = $1",
            UUID(sandbox_id),
        )
        return dict(row) if row else None
