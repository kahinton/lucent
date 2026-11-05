"""API key repository for Lucent.

Handles API key CRUD operations including creation, verification, and revocation.
"""

import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg
import bcrypt
from asyncpg import Pool

from lucent.db.pool import (
    preauth_guc_preamble,
    runner_guc_preamble,
    scoped_acquire,
    scoped_acquire_on,
    tenant_guc_scrub,
)
from lucent.logging import get_logger

logger = get_logger(__name__)


# All accepted API key prefixes. ``hs_`` is the interactive/service key family;
# ``lr_`` is the remote-agent family (Agent Control Protocol). The prefix is
# cosmetic for hashing but load-bearing for routing: it lets ``deps`` apply the
# remote-agent endpoint allowlist without a DB round trip on the scope, and it
# makes a leaked key obvious in logs and audit trails.
API_KEY_PREFIXES = ("hs_", "lr_")


# Least-privilege scope for an external control plane that provisions tenants.
# A key carrying only this scope may reach the organization administration
# surface (``POST /api/organizations/provision``, its list/read/rename and
# per-tenant settings, setting-locks, credential read, and model grants) and
# nothing else — the allowlist in ``lucent.api.deps``. The MCP middleware and
# the web login provider refuse it everywhere else. Mint one with:
#
#     python -m lucent.hyperadmin mint-key --email <operator> --scope admin-orgs
ADMIN_ORGS_SCOPE = "admin-orgs"


def _affected(status: str | None) -> int:
    """Extract the row count from an asyncpg command tag (e.g. ``DELETE 42``)."""
    if not status:
        return 0
    _, _, count = str(status).partition(" ")
    try:
        return int(count)
    except (TypeError, ValueError):
        return 0


class ApiKeyRepository:
    """Repository for API key CRUD operations."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def create(
        self,
        user_id: UUID,
        organization_id: UUID | None,
        name: str,
        scopes: list[str] | None = None,
        expires_at: datetime | None = None,
        memory_scope_user_id: UUID | None = None,
        memory_scope: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Create a new API key.

        Args:
            user_id: The user who owns this key.
            organization_id: The organization the key belongs to.
            name: User-provided name for the key.
            scopes: Permission scopes (default: ['read', 'write']).
            expires_at: Optional expiration datetime.

        Returns:
            Tuple of (api_key record, plain_text_key).
            The plain text key is only returned once at creation time.

        Raises:
            ValueError: If a key with this name already exists for this user.
        """
        # Check for existing active key with this name
        existing = await self.get_by_name(user_id, name, organization_id=organization_id)
        if existing:
            raise ValueError(f"An API key named '{name}' already exists")

        # Generate a secure random key with prefix
        raw_key = secrets.token_urlsafe(32)
        plain_key = f"hs_{raw_key}"
        key_prefix = plain_key[:11]  # "hs_" + first 8 chars

        # Hash the key for storage
        key_hash = bcrypt.hashpw(plain_key.encode(), bcrypt.gensalt()).decode()

        query = """
            INSERT INTO api_keys (user_id, organization_id, name,
                key_prefix, key_hash, scopes, expires_at,
                memory_scope_user_id, memory_scope)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id, user_id, organization_id, name, key_prefix, scopes,
                      last_used_at, use_count, expires_at, is_active, created_at, updated_at,
                      memory_scope_user_id, memory_scope
        """

        async with scoped_acquire_on(
            self.pool, organization_id=organization_id, user_id=user_id
        ) as conn:
            row = await conn.fetchrow(
                query,
                str(user_id),
                str(organization_id) if organization_id else None,
                name,
                key_prefix,
                key_hash,
                scopes or ["read", "write"],
                expires_at,
                str(memory_scope_user_id) if memory_scope_user_id else None,
                memory_scope,
            )

        result = self._row_to_dict(row), plain_key
        logger.info("API key created: name=%s, user=%s, prefix=%s", name, user_id, key_prefix)
        return result

    async def get_by_name(
        self,
        user_id: UUID,
        name: str,
        organization_id: UUID | None,
    ) -> dict[str, Any] | None:
        """Get an active API key by name for a user.

        Args:
            user_id: The user ID.
            name: The key name.

        Returns:
            The API key record, or None if not found.
        """
        query = """
            SELECT id, user_id, organization_id, name, key_prefix, scopes,
                   last_used_at, use_count, expires_at, is_active, created_at, updated_at,
                   memory_scope_user_id, memory_scope
            FROM api_keys
            WHERE user_id = $1 AND name = $2 AND organization_id = $3 AND revoked_at IS NULL
        """

        # verify() runs pre-auth (no tenant context exists yet), so it uses a
        # raw acquire; these lookup helpers run post-auth with the
        # caller's org threaded in.
        async with scoped_acquire_on(
            self.pool, organization_id=organization_id, user_id=user_id
        ) as conn:
            row = await conn.fetchrow(query, str(user_id), name, str(organization_id))

        if row is None:
            return None

        return self._row_to_dict(row)

    async def get_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        async with scoped_acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id, user_id, organization_id, name, key_prefix,
                          key_hash, scopes, last_used_at, use_count, expires_at,
                          is_active, created_at, updated_at,
                          memory_scope_user_id, memory_scope
                   FROM api_keys
                   WHERE key_hash = $1
                     AND revoked_at IS NULL
                     AND is_active = true""",
                key_hash,
            )
        return self._row_to_dict(row) if row else None

    async def verify(self, plain_key: str) -> dict[str, Any] | None:
        """Verify an API key and return the associated record.

        Args:
            plain_key: The plain text API key to verify.

        Returns:
            The API key record with user info if valid, None otherwise.
        """
        if not plain_key.startswith(API_KEY_PREFIXES):
            return None

        key_prefix = plain_key[:11]

        # Find all active keys with this prefix (prefix collisions are possible)
        query = """
            SELECT ak.id, ak.user_id, ak.organization_id, ak.name, ak.key_prefix,
                   ak.key_hash, ak.scopes, ak.last_used_at, ak.use_count,
                   ak.expires_at, ak.is_active, ak.created_at, ak.updated_at,
                   ak.memory_scope_user_id, ak.memory_scope,
                   u.email as user_email, u.display_name as user_display_name, u.role as user_role
            FROM api_keys ak
            JOIN users u ON ak.user_id = u.id
            WHERE ak.key_prefix = $1
              AND ak.is_active = true
              AND ak.revoked_at IS NULL
              AND u.is_active = true
              AND ak.organization_id = u.organization_id
        """

        async with self.pool.acquire() as conn:
            await conn.execute(preauth_guc_preamble())
            try:
                rows = await conn.fetch(query, key_prefix)
            finally:
                await conn.execute(tenant_guc_scrub())

        if not rows:
            return None

        # Check each matching key's hash (handles prefix collisions)
        matched_row = None
        for row in rows:
            if bcrypt.checkpw(plain_key.encode(), row["key_hash"].encode()):
                matched_row = row
                break

        if matched_row is None:
            logger.warning("API key verification failed: hash mismatch for key_id=<redacted>")
            return None

        # Check expiration
        if matched_row["expires_at"] and matched_row["expires_at"] < datetime.now(timezone.utc):
            logger.warning(
                "API key verification failed: expired key_id=%s", matched_row["id"]
            )
            return None

        # Update last used timestamp and count
        update_query = """
            UPDATE api_keys
            SET last_used_at = NOW(), use_count = use_count + 1
            WHERE id = $1
        """
        organization_id = matched_row["organization_id"]
        if organization_id:
            async with scoped_acquire_on(
                self.pool,
                organization_id=organization_id,
                user_id=matched_row["user_id"],
                role=matched_row["user_role"],
            ) as conn:
                await conn.execute(update_query, matched_row["id"])

        result = self._row_to_dict(matched_row)
        result["user_email"] = matched_row["user_email"]
        result["user_display_name"] = matched_row["user_display_name"]
        result["user_role"] = matched_row["user_role"]
        result["memory_scope_user_id"] = matched_row["memory_scope_user_id"]
        result["memory_scope"] = matched_row["memory_scope"]
        return result

    async def list_by_user(
        self,
        user_id: UUID,
        limit: int = 25,
        offset: int = 0,
        *,
        organization_id: UUID | None = None,
    ) -> dict:
        """List all API keys for a user.

        Args:
            user_id: The user ID.
            limit: Maximum number of results.
            offset: Pagination offset.

        Returns:
            Dict with items list and pagination info.
        """
        count_query = """
            SELECT COUNT(*) AS total FROM api_keys
            WHERE user_id = $1 AND organization_id = $2 AND revoked_at IS NULL
        """
        query = """
            SELECT id, user_id, organization_id, name, key_prefix, scopes,
                   last_used_at, use_count, expires_at, is_active, created_at, updated_at,
                   memory_scope_user_id, memory_scope
            FROM api_keys
            WHERE user_id = $1 AND organization_id = $2 AND revoked_at IS NULL
            ORDER BY created_at DESC
            LIMIT $3 OFFSET $4
        """

        async with scoped_acquire_on(
            self.pool, organization_id=organization_id, user_id=user_id
        ) as conn:
            count_row = await conn.fetchrow(count_query, str(user_id), str(organization_id))
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, str(user_id), str(organization_id), limit, offset)

        return {
            "items": [self._row_to_dict(row) for row in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def get_by_id(
        self, key_id: UUID, user_id: UUID, organization_id: UUID | None = None
    ) -> dict[str, Any] | None:
        """Get an API key by ID (must belong to user).

        Args:
            key_id: The API key ID.
            user_id: The user ID (for ownership check).

        Returns:
            The API key record, or None if not found.
        """
        query = """
            SELECT id, user_id, organization_id, name, key_prefix, scopes,
                   last_used_at, use_count, expires_at, is_active, created_at, updated_at,
                   memory_scope_user_id, memory_scope
            FROM api_keys
            WHERE id = $1 AND user_id = $2 AND organization_id = $3 AND revoked_at IS NULL
        """

        async with scoped_acquire_on(
            self.pool, organization_id=organization_id, user_id=user_id
        ) as conn:
            row = await conn.fetchrow(query, str(key_id), str(user_id), str(organization_id))

        if row is None:
            return None

        return self._row_to_dict(row)

    async def purge_expired(
        self,
        *,
        older_than_days: int = 0,
        organization_id: UUID | str | None = None,
        dry_run: bool = False,
        batch_size: int = 1000,
    ) -> dict[str, Any]:
        """Delete keys whose ``expires_at`` has already passed.

        Nothing else in the system reaps these. Expiry is enforced at
        verification time (``verify`` refuses a lapsed key), but the row stays
        ``is_active = true`` forever, so any count filtered on
        ``is_active AND revoked_at IS NULL`` grows without bound. On this
        instance that produced 22,435 permanently-dead rows being reported as
        "active API keys".

        Only *expired* keys are eligible — a key with ``expires_at IS NULL``
        never expires and is never touched, and neither is a key that is still
        live.

        Deletion is batched. ``api_keys`` carries an ``AFTER DELETE`` trigger
        per row that deletes the key's ``auth_clearances`` entry, so a single
        statement over tens of thousands of rows runs long enough to hit
        asyncpg's command timeout and roll back the whole thing.

        Args:
            older_than_days: Keep expired keys for this long past expiry, so a
                recent lapse is still visible when debugging. 0 deletes all.
            organization_id: Restrict to one tenant (system-wide when omitted).
            dry_run: Report what would be deleted without deleting.
            batch_size: Rows per statement.

        Returns:
            ``{"deleted": n, "candidates": n, "older_than_days": d}``.
        """
        where = [
            "expires_at IS NOT NULL",
            "expires_at < NOW() - make_interval(days => $1)",
        ]
        params: list[Any] = [older_than_days]
        if organization_id:
            params.append(str(organization_id))
            where.append(f"organization_id = ${len(params)}::uuid")
        where_sql = " AND ".join(where)

        async with scoped_acquire_on(
            self.pool, organization_id=organization_id, role="system"
        ) as conn:
            candidates = await conn.fetchval(
                f"SELECT COUNT(*) FROM api_keys WHERE {where_sql}", *params
            )
            candidates = int(candidates or 0)
            if dry_run or candidates == 0:
                return {
                    "deleted": 0,
                    "candidates": candidates,
                    "older_than_days": older_than_days,
                }

            deleted = 0
            while True:
                # ``ctid`` keeps each batch a single pass; re-running the
                # predicate each time is what shrinks the result set.
                status = await conn.execute(
                    f"""
                    DELETE FROM api_keys
                    WHERE ctid IN (
                        SELECT ctid FROM api_keys
                        WHERE {where_sql}
                        LIMIT {int(batch_size)}
                    )
                    """,
                    *params,
                )
                count = _affected(status)
                deleted += count
                if count < batch_size:
                    break

        return {
            "deleted": deleted,
            "candidates": candidates,
            "older_than_days": older_than_days,
        }

    async def revoke_any(
        self, key_id: UUID, *, organization_id: UUID | str | None
    ) -> bool:
        """Revoke an internally-managed key within its organization."""
        if organization_id is None:
            raise ValueError("Organization context is required to revoke an API key")
        query = """
            UPDATE api_keys
            SET revoked_at = COALESCE(revoked_at, $2::timestamptz),
                is_active = false,
                updated_at = $2::timestamptz
            WHERE id = $1 AND organization_id = $3::uuid AND revoked_at IS NULL
        """
        async with scoped_acquire_on(
            self.pool, organization_id=organization_id, role="daemon"
        ) as conn:
            result = await conn.execute(
                query, str(key_id), datetime.now(timezone.utc), str(organization_id)
            )
        return result in ("UPDATE 1",)

    async def revoke(
        self,
        key_id: UUID,
        user_id: UUID,
        *,
        organization_id: UUID | None = None,
    ) -> bool:
        """Revoke an API key.

        Args:
            key_id: The API key ID.
            user_id: The user ID (for ownership check).

        Returns:
            True if revoked, False if not found.
        """
        query = """
            UPDATE api_keys
            SET is_active = false, revoked_at = NOW()
            WHERE id = $1 AND user_id = $2 AND organization_id = $3 AND revoked_at IS NULL
            RETURNING id
        """

        async with scoped_acquire_on(
            self.pool, organization_id=organization_id, user_id=user_id
        ) as conn:
            result = await conn.fetchrow(query, str(key_id), str(user_id), str(organization_id))

        if result is not None:
            logger.info("API key revoked: id=%s, user=%s", key_id, user_id)
        return result is not None

    async def revoke_by_id(self, key_id: UUID) -> None:
        """Revoke a sandbox-owned API key by its ID."""
        async with self.pool.acquire() as conn:
            await conn.execute(runner_guc_preamble())
            try:
                await conn.execute(
                    """
                    UPDATE api_keys
                    SET is_active = false, revoked_at = NOW()
                    WHERE id = $1 AND revoked_at IS NULL
                    """,
                    key_id,
                )
            finally:
                await conn.execute(tenant_guc_scrub())


    async def update_name(
        self,
        key_id: UUID,
        user_id: UUID,
        name: str,
        *,
        organization_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        """Update an API key's name.

        Args:
            key_id: The API key ID.
            user_id: The user ID (for ownership check).
            name: The new name.

        Returns:
            The updated API key record, or None if not found.
        """
        query = """
            UPDATE api_keys
            SET name = $1, updated_at = NOW()
            WHERE id = $2 AND user_id = $3 AND organization_id = $4 AND revoked_at IS NULL
            RETURNING id, user_id, organization_id, name, key_prefix, scopes,
                      last_used_at, use_count, expires_at, is_active, created_at, updated_at,
                      memory_scope_user_id, memory_scope
        """

        async with scoped_acquire_on(
            self.pool, organization_id=organization_id, user_id=user_id
        ) as conn:
            row = await conn.fetchrow(query, name, str(key_id), str(user_id), str(organization_id))

        if row is None:
            return None

        return self._row_to_dict(row)

    def _row_to_dict(self, row: asyncpg.Record) -> dict[str, Any]:
        """Convert a database row to a dictionary."""
        result = {
            "id": row["id"],
            "user_id": row["user_id"] if isinstance(row["user_id"], UUID) else UUID(row["user_id"]),
            "name": row["name"],
            "key_prefix": row["key_prefix"],
            "scopes": row["scopes"],
            "last_used_at": row["last_used_at"],
            "use_count": row["use_count"],
            "expires_at": row["expires_at"],
            "is_active": row["is_active"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

        if "organization_id" in row.keys() and row["organization_id"]:
            result["organization_id"] = (
                row["organization_id"]
                if isinstance(row["organization_id"], UUID)
                else UUID(row["organization_id"])
            )
        else:
            result["organization_id"] = None

        if "memory_scope_user_id" in row.keys() and row["memory_scope_user_id"]:
            result["memory_scope_user_id"] = (
                row["memory_scope_user_id"]
                if isinstance(row["memory_scope_user_id"], UUID)
                else UUID(row["memory_scope_user_id"])
            )
        else:
            result["memory_scope_user_id"] = None

        result["memory_scope"] = row.get("memory_scope")

        return result
