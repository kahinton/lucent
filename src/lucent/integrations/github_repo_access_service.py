"""GitHub repository access checks with DB-backed caching.

Architectural rule enforced here:

* ``check_access`` (the user-ACL entry point) **must only** consult the
  user's own GitHub credential from ``enterprise_credentials``. It may
  not silently substitute a GitHub App installation token, an
  environment ``GITHUB_TOKEN``, or any other shared credential as proof
  that the *user* personally has access to a repository.
* App-installation visibility lives behind the separate
  :meth:`GitHubRepoAccessService.app_installation_can_see_repo` method,
  is gated by ``LUCENT_GITHUB_APP_ENABLED``, and is intended for
  webhook / app code paths only — never as a fallback for user ACL.

The compatibility flag
``LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL`` (see
``connection_flags.require_user_github_for_repo_acl``) controls the
behavior for the *no-credential* user case:

* ``False`` (default): preserve the open-source single-user path —
  users without a connected GitHub credential are allowed access to
  repo-tagged memories. This keeps the simple local setup working.
* ``True``: deny access with a structured reason and surface a
  "connect GitHub" hint to callers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class RepoAccessDecision:
    """Structured user-ACL decision for a repo-tagged memory.

    ``allowed`` is the boolean answer used for ACL enforcement.
    ``reason`` is a stable machine-readable code suitable for logs,
    metrics, and conveying *why* to the caller. ``hint`` is an optional
    user-facing remediation hint (e.g. "connect GitHub").
    """

    allowed: bool
    reason: str
    hint: str | None = None


class GitHubRepoAccessService:
    """Checks whether a user can access a GitHub repository."""

    POSITIVE_TTL = timedelta(minutes=15)
    NEGATIVE_TTL = timedelta(minutes=5)
    EXISTENCE_CACHE_TTL = timedelta(hours=1)

    # Module-level cache for repo existence — shared across requests
    _repo_exists_cache: dict[str, tuple[bool | None, datetime]] = {}

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
        """Return whether the user can access ``owner/repo`` on GitHub.

        Boolean shim over :meth:`check_access_with_reason` for the
        many existing call sites that only need a yes/no answer.
        """
        decision = await self.check_access_with_reason(user_id, repo_full_name)
        return decision.allowed

    async def check_access_with_reason(
        self, user_id: UUID, repo_full_name: str
    ) -> RepoAccessDecision:
        """Resolve user repo ACL with a structured reason.

        See module docstring for the architectural rule. This method
        consults *only* the user's own GitHub credential; it never
        falls back to a GitHub App installation token, the
        environment ``GITHUB_TOKEN``, or any other shared credential.
        """
        from lucent.integrations.connection_flags import (
            require_user_github_for_repo_acl,
        )

        normalized_repo = repo_full_name.strip().lower()
        if not self._is_valid_repo_name(normalized_repo):
            # Invalid repo format (e.g. bare name without owner/) — allow.
            # These memories predate the repo ACL system; blocking them
            # would silently strip access to legacy data.
            logger.debug(
                "Repo name '%s' is not in owner/repo format — allowing access",
                repo_full_name,
            )
            return RepoAccessDecision(allowed=True, reason="invalid_repo_format")

        cached = await self._get_cached(user_id=user_id, repo_full_name=normalized_repo)
        now = datetime.now(UTC)
        if cached and cached["expires_at"] > now:
            allowed = bool(cached["has_access"])
            return RepoAccessDecision(
                allowed=allowed,
                reason="cache_hit_allow" if allowed else "cache_hit_deny",
            )

        token = await self._get_user_github_token(user_id)
        if not token:
            # No *user* GitHub credential. We do NOT fall back to an app
            # installation token here — that would silently grant the
            # user access without proving they personally can see the
            # repo. Behavior is governed by the strict-mode flag.
            if require_user_github_for_repo_acl():
                logger.info(
                    "Denying repo access for user %s on %s: no user "
                    "GitHub credential and strict mode is on",
                    user_id,
                    normalized_repo,
                )
                return RepoAccessDecision(
                    allowed=False,
                    reason="user_github_credential_required",
                    hint=(
                        "Connect your GitHub account in Settings → "
                        "Connections to access repo-scoped memories."
                    ),
                )
            # Compatibility mode (default): single-user / open-source
            # deployments without a GitHub credential keep working.
            return RepoAccessDecision(
                allowed=True,
                reason="no_user_credential_compat_allow",
            )

        has_access = await self._check_github_repo(
            token=token, repo_full_name=normalized_repo
        )

        ttl = self.POSITIVE_TTL if has_access else self.NEGATIVE_TTL
        await self._upsert_cache(
            user_id=user_id,
            repo_full_name=normalized_repo,
            has_access=has_access,
            checked_at=now,
            expires_at=now + ttl,
        )
        return RepoAccessDecision(
            allowed=has_access,
            reason="github_api_allow" if has_access else "github_api_deny",
        )

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

    async def check_repo_exists(self, repo_full_name: str) -> bool | None:
        """Check if a GitHub repo exists using any available token.

        Returns True if repo exists, False if it does not (404),
        or None if we can't determine (no token available).
        Uses an in-memory cache with 1-hour TTL for performance.
        """
        normalized = repo_full_name.strip().lower()
        if not self._is_valid_repo_name(normalized):
            return None

        # Check in-memory cache first
        now = datetime.now(UTC)
        cached = self._repo_exists_cache.get(normalized)
        if cached:
            result, expires = cached
            if expires > now:
                return result

        # Try to find any active GitHub token in the system
        token = await self._get_any_github_token()
        if not token:
            return None

        url = f"https://api.github.com/repos/{normalized}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        result = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                result = True
            elif resp.status_code == 404:
                result = False
        except Exception:
            pass

        # Cache the result
        if result is not None:
            self._repo_exists_cache[normalized] = (result, now + self.EXISTENCE_CACHE_TTL)

        return result

    async def _get_any_github_token(self) -> str | None:
        """Get any active GitHub token from the system for existence checks.

        **Not** for user ACL. This is used by :meth:`check_repo_exists`
        to answer "does this repo exist on GitHub at all?" using
        whatever shared credential the deployment has lying around. It
        must never be plumbed into :meth:`check_access` — see the module
        docstring.
        """
        from lucent.integrations.connection_flags import env_token_claim_enabled

        # Try env var first (cheapest), but only if the deployment allows
        # surfacing environment-token claims. Enterprise deployments that
        # disable env-token claim should not silently fall back to it for
        # repo-existence either.
        if env_token_claim_enabled():
            env_token = os.environ.get("GITHUB_TOKEN", "")
            if env_token:
                return env_token
        # Try any stored credential (existence check only — caller is
        # responsible for never treating this as a user-ACL signal).
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT encrypted_secret_payload FROM enterprise_credentials
                   WHERE integration_type = 'github' AND status = 'active'
                   ORDER BY updated_at DESC LIMIT 1""",
            )
        if not row:
            return None
        encryptor = self._get_encryptor()
        if not encryptor:
            return None
        try:
            payload = encryptor.decrypt(row["encrypted_secret_payload"])
            return str(payload.get("access_token", "")) or None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # App-installation visibility (separate from user ACL — do NOT use as
    # a substitute for user GitHub credential checks).
    # ------------------------------------------------------------------

    async def app_installation_can_see_repo(
        self,
        *,
        organization_id: UUID,
        repo_full_name: str,
    ) -> bool | None:
        """Whether *any* active GitHub App installation owned by the org can
        see ``owner/repo``.

        ⚠️  **NOT a user ACL signal.** This method is intentionally
        separate from :meth:`check_access` / :meth:`check_access_with_reason`
        and must never feed into per-user repo permission decisions.
        Callers are limited to webhook / app code paths that need to
        answer "is the GitHub App installed on this repo?" — for
        example to decide whether to render a webhook-driven feature or
        to short-circuit work the App can't do.

        The method is gated by ``LUCENT_GITHUB_APP_ENABLED`` and returns
        ``None`` (i.e. "unknown") when:

        * The GitHub App feature flag is off, OR
        * The repo name is malformed, OR
        * No active ``github_app`` row exists in ``integrations`` for
          the org, OR
        * App installation token minting is not yet implemented for
          this deployment (current state — see follow-up).

        Returning ``None`` rather than ``False`` is deliberate: callers
        must not interpret "unknown" as an authoritative deny.
        """
        from lucent.integrations.connection_flags import github_app_enabled

        if not github_app_enabled():
            return None

        normalized = repo_full_name.strip().lower()
        if not self._is_valid_repo_name(normalized):
            return None

        async with self.pool.acquire() as conn:
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
        if not row:
            return None

        # Token minting from the App private key is intentionally out of
        # scope for this task — see the GitHub App webhook follow-up.
        # Until that lands, returning ``None`` here means the caller
        # treats app-install visibility as "unknown" and does not
        # mistake it for an authoritative deny — and crucially does not
        # mistake it for a user-ACL grant either.
        logger.debug(
            "app_installation_can_see_repo: github_app row found for org "
            "%s install_id=%s repo=%s but token minting not implemented; "
            "returning unknown",
            organization_id,
            row["install_id"],
            normalized,
        )
        return None

