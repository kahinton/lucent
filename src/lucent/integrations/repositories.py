"""Database repositories for the integrations subsystem.

IntegrationRepo, UserLinkRepo, PairingChallengeRepo — all org-scoped.
Follows the asyncpg repository pattern from src/lucent/db/.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from lucent.integrations.models import (
    IntegrationStatus,
    UserLinkStatus,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid lifecycle transitions
# ---------------------------------------------------------------------------

_INTEGRATION_TRANSITIONS: dict[str, set[str]] = {
    "active": {"disabled", "revoked", "deleted"},
    "disabled": {"active", "revoked", "deleted"},
    "revoked": {"deleted"},
}

_USER_LINK_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"active", "revoked"},
    "active": {"revoked", "superseded", "orphaned", "disabled"},
    "orphaned": {"active"},
    "disabled": {"active"},
}


class IntegrationRepo:
    """CRUD + lifecycle operations for the ``integrations`` table.

    Every query is scoped to ``organization_id`` for tenant isolation.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # -- Create ---------------------------------------------------------------

    async def create(
        self,
        *,
        organization_id: str,
        type: str,
        encrypted_config: bytes,
        created_by: str,
        external_workspace_id: str | None = None,
        allowed_channels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Insert a new integration (status defaults to ``active``)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO integrations
                    (organization_id, type, encrypted_config, created_by,
                     external_workspace_id, allowed_channels)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
                """,
                UUID(organization_id),
                type,
                encrypted_config,
                UUID(created_by),
                external_workspace_id,
                json.dumps(allowed_channels or []),
            )
        logger.info(
            "Integration created: id=%s org=%s type=%s",
            row["id"], organization_id, type,
        )
        return self._row_to_dict(row)

    # -- Read -----------------------------------------------------------------

    async def get_by_id(
        self, integration_id: str, organization_id: str,
    ) -> dict[str, Any] | None:
        """Get a single integration by ID (org-scoped)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM integrations WHERE id = $1 AND organization_id = $2",
                UUID(integration_id),
                UUID(organization_id),
            )
        return self._row_to_dict(row) if row else None

    async def get_active_by_type(
        self,
        organization_id: str,
        type: str,
        external_workspace_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find the active integration for an org+type+workspace tuple."""
        if external_workspace_id is not None:
            query = """
                SELECT * FROM integrations
                WHERE organization_id = $1
                  AND type = $2
                  AND external_workspace_id = $3
                  AND status = 'active'
            """
            params: list[Any] = [
                UUID(organization_id), type, external_workspace_id,
            ]
        else:
            query = """
                SELECT * FROM integrations
                WHERE organization_id = $1
                  AND type = $2
                  AND external_workspace_id IS NULL
                  AND status = 'active'
            """
            params = [UUID(organization_id), type]

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        return self._row_to_dict(row) if row else None

    async def list_by_org(
        self,
        organization_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List integrations for an org, optionally filtered by status."""
        conditions = ["organization_id = $1"]
        params: list[Any] = [UUID(organization_id)]
        idx = 2

        if status is not None:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        params.append(limit)
        where = " AND ".join(conditions)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM integrations WHERE {where}"
                f" ORDER BY created_at DESC LIMIT ${idx}",
                *params,
            )
        return [self._row_to_dict(r) for r in rows]

    # -- Update ---------------------------------------------------------------

    async def update(
        self,
        integration_id: str,
        organization_id: str,
        *,
        updated_by: str,
        allowed_channels: list[str] | None = None,
        encrypted_config: bytes | None = None,
    ) -> dict[str, Any] | None:
        """Update mutable fields (config, allowed_channels).

        Bumps ``config_version`` when config changes.
        """
        sets = ["updated_by = $3", "updated_at = NOW()"]
        params: list[Any] = [
            UUID(integration_id), UUID(organization_id), UUID(updated_by),
        ]
        idx = 4

        if allowed_channels is not None:
            sets.append(f"allowed_channels = ${idx}")
            params.append(json.dumps(allowed_channels))
            idx += 1

        if encrypted_config is not None:
            sets.append(f"encrypted_config = ${idx}")
            params.append(encrypted_config)
            idx += 1
            sets.append("config_version = config_version + 1")

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE integrations SET {', '.join(sets)}"
                f" WHERE id = $1 AND organization_id = $2 RETURNING *",
                *params,
            )
        return self._row_to_dict(row) if row else None

    # -- Lifecycle transitions ------------------------------------------------

    async def activate(
        self, integration_id: str, organization_id: str, *, updated_by: str,
    ) -> dict[str, Any] | None:
        """Transition ``disabled → active``."""
        return await self._transition(
            integration_id, organization_id,
            to_status=IntegrationStatus.ACTIVE,
            updated_by=updated_by,
            extra_sets=["disabled_at = NULL"],
        )

    async def disable(
        self, integration_id: str, organization_id: str, *, updated_by: str,
    ) -> dict[str, Any] | None:
        """Transition ``active → disabled``."""
        return await self._transition(
            integration_id, organization_id,
            to_status=IntegrationStatus.DISABLED,
            updated_by=updated_by,
            extra_sets=["disabled_at = NOW()"],
        )

    async def revoke(
        self,
        integration_id: str,
        organization_id: str,
        *,
        updated_by: str,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Transition ``active|disabled → revoked``."""
        extra_sets = ["revoked_at = NOW()"]
        extra_params: list[Any] = []
        if reason is not None:
            extra_sets.append("revoke_reason = ${next}")
            extra_params.append(reason)

        return await self._transition(
            integration_id, organization_id,
            to_status=IntegrationStatus.REVOKED,
            updated_by=updated_by,
            extra_sets=extra_sets,
            extra_params=extra_params,
        )

    async def soft_delete(
        self, integration_id: str, organization_id: str, *, updated_by: str,
    ) -> dict[str, Any] | None:
        """Transition ``active|disabled|revoked → deleted``."""
        return await self._transition(
            integration_id, organization_id,
            to_status=IntegrationStatus.DELETED,
            updated_by=updated_by,
        )

    # -- Internal helpers -----------------------------------------------------

    async def _transition(
        self,
        integration_id: str,
        organization_id: str,
        *,
        to_status: IntegrationStatus,
        updated_by: str,
        extra_sets: list[str] | None = None,
        extra_params: list[Any] | None = None,
    ) -> dict[str, Any] | None:
        """Execute a validated lifecycle transition."""
        valid_from = {
            k for k, v in _INTEGRATION_TRANSITIONS.items() if to_status.value in v
        }
        if not valid_from:
            raise ValueError(f"No valid source states for target '{to_status.value}'")

        placeholders = ["status = $3", "updated_by = $4", "updated_at = NOW()"]
        params: list[Any] = [
            UUID(integration_id),
            UUID(organization_id),
            to_status.value,
            UUID(updated_by),
        ]
        idx = 5

        for s in extra_sets or []:
            if "${next}" in s:
                s = s.replace("${next}", f"${idx}")
                params.append((extra_params or []).pop(0))
                idx += 1
            placeholders.append(s)
        # Consume any remaining extra_params that weren't referenced via ${next}
        for p in extra_params or []:
            params.append(p)

        from_clause = ", ".join(f"'{s}'" for s in valid_from)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE integrations SET {', '.join(placeholders)}"
                f" WHERE id = $1 AND organization_id = $2"
                f" AND status IN ({from_clause})"
                f" RETURNING *",
                *params,
            )

        if row:
            logger.info(
                "Integration %s transitioned to %s (org=%s)",
                integration_id, to_status.value, organization_id,
            )
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
        result = dict(row)
        if isinstance(result.get("allowed_channels"), str):
            result["allowed_channels"] = json.loads(result["allowed_channels"])
        return result


# ---------------------------------------------------------------------------
# UserLinkRepo
# ---------------------------------------------------------------------------


class UserLinkRepo:
    """CRUD + lifecycle operations for the ``user_links`` table.

    Every query is scoped to ``organization_id`` for tenant isolation.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # -- Create ---------------------------------------------------------------

    async def create(
        self,
        *,
        organization_id: str,
        integration_id: str,
        user_id: str,
        provider: str,
        external_user_id: str,
        verification_method: str,
        external_workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Insert a new user link (status defaults to ``pending``)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO user_links
                    (organization_id, integration_id, user_id, provider,
                     external_user_id, external_workspace_id, verification_method)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                UUID(organization_id),
                UUID(integration_id),
                UUID(user_id),
                provider,
                external_user_id,
                external_workspace_id,
                verification_method,
            )
        logger.info(
            "User link created: id=%s user=%s provider=%s",
            row["id"], user_id, provider,
        )
        return dict(row)

    # -- Read -----------------------------------------------------------------

    async def get_by_id(
        self, link_id: str, organization_id: str,
    ) -> dict[str, Any] | None:
        """Get a single user link by ID (org-scoped)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM user_links WHERE id = $1 AND organization_id = $2",
                UUID(link_id),
                UUID(organization_id),
            )
        return dict(row) if row else None

    async def list_by_org(
        self,
        organization_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List all user links for an org."""
        conditions = ["organization_id = $1"]
        params: list[Any] = [UUID(organization_id)]
        idx = 2

        if status is not None:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        params.append(limit)
        where = " AND ".join(conditions)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM user_links WHERE {where}"
                f" ORDER BY created_at DESC LIMIT ${idx}",
                *params,
            )
        return [dict(r) for r in rows]

    async def list_by_user(
        self, user_id: str, organization_id: str, *, status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List links for a specific user (org-scoped)."""
        conditions = ["user_id = $1", "organization_id = $2"]
        params: list[Any] = [UUID(user_id), UUID(organization_id)]
        idx = 3

        if status is not None:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        where = " AND ".join(conditions)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM user_links WHERE {where} ORDER BY created_at DESC",
                *params,
            )
        return [dict(r) for r in rows]

    async def list_by_integration(
        self, integration_id: str, organization_id: str, *, status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List links for a specific integration (org-scoped)."""
        conditions = ["integration_id = $1", "organization_id = $2"]
        params: list[Any] = [UUID(integration_id), UUID(organization_id)]
        idx = 3

        if status is not None:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        where = " AND ".join(conditions)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM user_links WHERE {where} ORDER BY created_at DESC",
                *params,
            )
        return [dict(r) for r in rows]

    async def resolve_identity(
        self,
        provider: str,
        external_user_id: str,
        external_workspace_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find the active link for an external identity tuple.

        Uses the partial unique index ``idx_user_links_active_identity``.
        """
        if external_workspace_id is not None:
            query = """
                SELECT * FROM user_links
                WHERE provider = $1
                  AND external_user_id = $2
                  AND external_workspace_id = $3
                  AND status = 'active'
            """
            params: list[Any] = [provider, external_user_id, external_workspace_id]
        else:
            query = """
                SELECT * FROM user_links
                WHERE provider = $1
                  AND external_user_id = $2
                  AND external_workspace_id IS NULL
                  AND status = 'active'
            """
            params = [provider, external_user_id]

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        return dict(row) if row else None

    # -- Lifecycle transitions ------------------------------------------------

    async def activate(
        self, link_id: str, organization_id: str,
    ) -> dict[str, Any] | None:
        """Transition ``pending|orphaned|disabled → active``."""
        return await self._transition(
            link_id, organization_id,
            to_status=UserLinkStatus.ACTIVE,
            extra_sets=["linked_at = COALESCE(linked_at, NOW())"],
        )

    async def revoke(
        self,
        link_id: str,
        organization_id: str,
        *,
        revoked_by: str | None = None,
    ) -> dict[str, Any] | None:
        """Transition ``pending|active → revoked``."""
        extra_sets = ["revoked_at = NOW()"]
        extra_params: list[Any] = []
        if revoked_by is not None:
            extra_sets.append("revoked_by = ${next}")
            extra_params.append(UUID(revoked_by))

        return await self._transition(
            link_id, organization_id,
            to_status=UserLinkStatus.REVOKED,
            extra_sets=extra_sets,
            extra_params=extra_params,
        )

    async def supersede(
        self,
        link_id: str,
        organization_id: str,
        *,
        superseded_by: str,
    ) -> dict[str, Any] | None:
        """Transition ``active → superseded``, linking to the replacement."""
        return await self._transition(
            link_id, organization_id,
            to_status=UserLinkStatus.SUPERSEDED,
            extra_sets=["superseded_by = ${next}"],
            extra_params=[UUID(superseded_by)],
        )

    async def orphan(
        self, link_id: str, organization_id: str,
    ) -> dict[str, Any] | None:
        """Transition ``active → orphaned`` (integration disabled/revoked)."""
        return await self._transition(
            link_id, organization_id,
            to_status=UserLinkStatus.ORPHANED,
        )

    async def disable(
        self, link_id: str, organization_id: str,
    ) -> dict[str, Any] | None:
        """Transition ``active → disabled``."""
        return await self._transition(
            link_id, organization_id,
            to_status=UserLinkStatus.DISABLED,
        )

    async def bulk_orphan_by_integration(
        self, integration_id: str, organization_id: str,
    ) -> int:
        """Orphan all active links for an integration (e.g. when it's revoked).

        Returns the number of rows affected.
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE user_links
                SET status = 'orphaned', updated_at = NOW()
                WHERE integration_id = $1
                  AND organization_id = $2
                  AND status = 'active'
                """,
                UUID(integration_id),
                UUID(organization_id),
            )
        count = int(result.split()[-1])
        if count:
            logger.info(
                "Bulk orphaned %d user links for integration %s",
                count, integration_id,
            )
        return count

    # -- Internal helpers -----------------------------------------------------

    async def _transition(
        self,
        link_id: str,
        organization_id: str,
        *,
        to_status: UserLinkStatus,
        extra_sets: list[str] | None = None,
        extra_params: list[Any] | None = None,
    ) -> dict[str, Any] | None:
        """Execute a validated lifecycle transition."""
        valid_from = {
            k for k, v in _USER_LINK_TRANSITIONS.items() if to_status.value in v
        }
        if not valid_from:
            raise ValueError(f"No valid source states for target '{to_status.value}'")

        placeholders = ["status = $3", "updated_at = NOW()"]
        params: list[Any] = [
            UUID(link_id), UUID(organization_id), to_status.value,
        ]
        idx = 4

        extra_params = list(extra_params or [])
        for s in extra_sets or []:
            if "${next}" in s:
                s = s.replace("${next}", f"${idx}")
                params.append(extra_params.pop(0))
                idx += 1
            placeholders.append(s)

        from_clause = ", ".join(f"'{s}'" for s in valid_from)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE user_links SET {', '.join(placeholders)}"
                f" WHERE id = $1 AND organization_id = $2"
                f" AND status IN ({from_clause})"
                f" RETURNING *",
                *params,
            )

        if row:
            logger.info(
                "User link %s transitioned to %s (org=%s)",
                link_id, to_status.value, organization_id,
            )
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# PairingChallengeRepo
# ---------------------------------------------------------------------------


class PairingChallengeRepo:
    """CRUD + lifecycle operations for the ``pairing_challenges`` table.

    Challenges are scoped through their integration's org membership.
    The ``user_id`` FK ensures the challenge belongs to a valid Lucent user.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # -- Create ---------------------------------------------------------------

    async def create(
        self,
        *,
        integration_id: str,
        user_id: str,
        code_hash: str,
        expires_at: datetime,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        """Insert a new pairing challenge."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO pairing_challenges
                    (integration_id, user_id, code_hash, expires_at, max_attempts)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
                """,
                UUID(integration_id),
                UUID(user_id),
                code_hash,
                expires_at,
                max_attempts,
            )
        logger.info(
            "Pairing challenge created: id=%s user=%s integration=%s",
            row["id"], user_id, integration_id,
        )
        return dict(row)

    # -- Read -----------------------------------------------------------------

    async def get_by_id(self, challenge_id: str) -> dict[str, Any] | None:
        """Get a single pairing challenge by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM pairing_challenges WHERE id = $1",
                UUID(challenge_id),
            )
        return dict(row) if row else None

    async def get_pending_for_user(
        self, user_id: str, integration_id: str,
    ) -> list[dict[str, Any]]:
        """Get all pending, non-expired challenges for a user+integration."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM pairing_challenges
                WHERE user_id = $1
                  AND integration_id = $2
                  AND status = 'pending'
                  AND expires_at > NOW()
                ORDER BY created_at DESC
                """,
                UUID(user_id),
                UUID(integration_id),
            )
        return [dict(r) for r in rows]

    async def count_recent_by_user(
        self, user_id: str, since: datetime,
    ) -> int:
        """Count challenges created since a given time (for rate limiting)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS cnt FROM pairing_challenges
                WHERE user_id = $1 AND created_at >= $2
                """,
                UUID(user_id),
                since,
            )
        return row["cnt"] if row else 0

    # -- Lifecycle operations -------------------------------------------------

    async def increment_attempts(self, challenge_id: str) -> dict[str, Any] | None:
        """Bump ``attempt_count``; transition to ``exhausted`` if limit hit.

        Returns the updated row, or None if the challenge is not pending.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE pairing_challenges
                SET attempt_count = attempt_count + 1,
                    status = CASE
                        WHEN attempt_count + 1 >= max_attempts THEN 'exhausted'
                        ELSE status
                    END
                WHERE id = $1 AND status = 'pending'
                RETURNING *
                """,
                UUID(challenge_id),
            )
        return dict(row) if row else None

    async def redeem(
        self, challenge_id: str, *, claimed_by_external_id: str,
    ) -> dict[str, Any] | None:
        """Transition ``pending → used`` and record the claiming external user.

        Only succeeds if the challenge is still pending and not expired.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE pairing_challenges
                SET status = 'used',
                    claimed_by_external_id = $2
                WHERE id = $1
                  AND status = 'pending'
                  AND expires_at > NOW()
                  AND attempt_count < max_attempts
                RETURNING *
                """,
                UUID(challenge_id),
                claimed_by_external_id,
            )
        if row:
            logger.info(
                "Pairing challenge %s redeemed by %s",
                challenge_id, claimed_by_external_id,
            )
        return dict(row) if row else None

    async def expire_stale(self) -> int:
        """Bulk-expire pending challenges past their TTL.

        Returns the number of rows affected.
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE pairing_challenges
                SET status = 'expired'
                WHERE status = 'pending' AND expires_at <= NOW()
                """
            )
        count = int(result.split()[-1])
        if count:
            logger.info("Expired %d stale pairing challenges", count)
        return count
