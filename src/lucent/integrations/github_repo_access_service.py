"""GitHub repository access checks with DB-backed caching."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from lucent.integrations.encryption import (
    BackendCredentialEncryptor,
    CredentialEncryptor,
    EncryptionError,
)
from lucent.logging import get_logger

logger = get_logger("integrations.github_repo_access")


class GitHubRepoAccessService:
    """Checks whether a user can access a GitHub repository."""

    POSITIVE_TTL = timedelta(minutes=15)
    NEGATIVE_TTL = timedelta(minutes=5)

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        encryptor: CredentialEncryptor | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.pool = pool
        self._encryptor = encryptor
        self.timeout_seconds = timeout_seconds

    async def check_access(self, user_id: UUID, repo_full_name: str) -> bool:
        """Return whether the user can access ``owner/repo`` on GitHub."""
        normalized_repo = repo_full_name.strip().lower()
        if not self._is_valid_repo_name(normalized_repo):
            return False

        cached = await self._get_cached(user_id=user_id, repo_full_name=normalized_repo)
        now = datetime.now(UTC)
        if cached and cached["expires_at"] > now:
            return bool(cached["has_access"])

        token = await self._get_user_github_token(user_id)
        has_access = False
        if token:
            has_access = await self._check_github_repo(token=token, repo_full_name=normalized_repo)

        ttl = self.POSITIVE_TTL if has_access else self.NEGATIVE_TTL
        await self._upsert_cache(
            user_id=user_id,
            repo_full_name=normalized_repo,
            has_access=has_access,
            checked_at=now,
            expires_at=now + ttl,
        )
        return has_access

    async def _get_cached(self, *, user_id: UUID, repo_full_name: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
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

    async def _upsert_cache(
        self,
        *,
        user_id: UUID,
        repo_full_name: str,
        has_access: bool,
        checked_at: datetime,
        expires_at: datetime,
    ) -> None:
        async with self.pool.acquire() as conn:
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

    async def _get_user_github_token(self, user_id: UUID) -> str | None:
        async with self.pool.acquire() as conn:
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
        if not row:
            return None

        encryptor = self._get_encryptor()
        if encryptor is None:
            return None

        try:
            secret_payload = encryptor.decrypt(row["encrypted_secret_payload"])
        except Exception:
            logger.warning(
                "Failed to decrypt GitHub credential for user %s",
                user_id,
                exc_info=True,
            )
            return None

        token = secret_payload.get("access_token")
        return str(token) if token else None

    def _get_encryptor(self) -> CredentialEncryptor | None:
        if self._encryptor is not None:
            return self._encryptor
        try:
            self._encryptor = BackendCredentialEncryptor()
        except EncryptionError:
            logger.warning("Credential encryptor unavailable for GitHub repo ACL checks")
            return None
        return self._encryptor

    async def _check_github_repo(self, *, token: str, repo_full_name: str) -> bool:
        url = f"https://api.github.com/repos/{repo_full_name}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.get(url, headers=headers)
        except Exception:
            logger.warning("GitHub repo access check failed for %s", repo_full_name, exc_info=True)
            return False

        if resp.status_code == 200:
            return True
        if resp.status_code in {401, 403, 404}:
            return False

        logger.warning(
            "Unexpected GitHub response status for %s: %s",
            repo_full_name,
            resp.status_code,
        )
        return False

    @staticmethod
    def _is_valid_repo_name(repo_full_name: str) -> bool:
        if "/" not in repo_full_name:
            return False
        owner, repo = repo_full_name.split("/", 1)
        return bool(owner and repo and "/" not in repo)
