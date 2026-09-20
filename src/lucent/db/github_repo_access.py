"""Repository for GitHub repo access checks and their cache rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from asyncpg import Pool

from lucent.db.pool import (
    runner_guc_preamble,
    scoped_acquire_on,
    tenant_guc_scrub,
)


class GitHubRepoAccessRepository:
    """Database-backed state for GitHub repository ACL decisions."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def get_cached(
        self, *, user_id: UUID, repo_full_name: str
    ) -> dict[str, Any] | None:
        async with scoped_acquire_on(self.pool, user_id=user_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT has_access, checked_at, expires_at
                FROM github_repo_access_cache
                WHERE user_id = $1 AND repo_full_name = $2
                """,
                user_id,
                repo_full_name,
            )
        return dict(row) if row else None

    async def upsert_cache(
        self,
        *,
        user_id: UUID,
        repo_full_name: str,
        has_access: bool,
        checked_at: datetime,
        expires_at: datetime,
    ) -> None:
        async with scoped_acquire_on(self.pool, user_id=user_id) as conn:
            await conn.execute(
                """
                INSERT INTO github_repo_access_cache (
                    user_id, repo_full_name, has_access, checked_at, expires_at
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, repo_full_name)
                DO UPDATE SET
                    has_access = EXCLUDED.has_access,
                    checked_at = EXCLUDED.checked_at,
                    expires_at = EXCLUDED.expires_at
                """,
                user_id,
                repo_full_name,
                has_access,
                checked_at,
                expires_at,
            )

    async def resolve_user_org(self, user_id: UUID) -> UUID | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT organization_id FROM users WHERE id = $1", user_id
            )
        return row["organization_id"] if row else None

    async def get_user_credential_payload(self, *, user_id: UUID) -> bytes | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT organization_id FROM users WHERE id = $1", user_id
            )
        org_id = row["organization_id"] if row else None
        if not org_id:
            return None

        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT encrypted_secret_payload
                FROM enterprise_credentials
                WHERE integration_type = 'github'
                  AND scope_type = 'user'
                  AND owner_user_id = $1
                  AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                user_id,
            )
        return row["encrypted_secret_payload"] if row else None

    async def get_any_credential_payload(self) -> bytes | None:
        async with self.pool.acquire() as conn:
            await conn.execute(runner_guc_preamble())
            try:
                row = await conn.fetchrow(
                    """
                    SELECT encrypted_secret_payload
                    FROM enterprise_credentials
                    WHERE integration_type = 'github' AND status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                )
            finally:
                await conn.execute(tenant_guc_scrub())
        return row["encrypted_secret_payload"] if row else None

    async def get_active_app_installation(
        self, *, organization_id: UUID
    ) -> dict[str, Any] | None:
        async with scoped_acquire_on(
            self.pool, organization_id=organization_id
        ) as conn:
            row = await conn.fetchrow(
                """
                SELECT id, install_id
                  FROM integrations
                 WHERE organization_id = $1
                   AND type = 'github_app'
                   AND status = 'active'
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                organization_id,
            )
        return dict(row) if row else None

    async def list_accessible_repo_full_names(
        self, *, user_id: UUID, organization_id: str | None
    ) -> list[str]:
        async with scoped_acquire_on(
            self.pool,
            organization_id=organization_id, user_id=user_id
        ) as conn:
            rows = await conn.fetch(
                """
                SELECT repo_full_name
                FROM github_repo_access_cache
                WHERE user_id = $1
                  AND has_access = true
                  AND expires_at > NOW()
                """,
                user_id,
            )
        return [row["repo_full_name"] for row in rows]
