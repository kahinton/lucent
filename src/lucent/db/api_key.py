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

from lucent.logging import get_logger

logger = get_logger(__name__)


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
        existing = await self.get_by_name(user_id, name)
        if existing:
            raise ValueError(f"An API key named '{name}' already exists")

        # Generate a secure random key with prefix
        raw_key = secrets.token_urlsafe(32)
        plain_key = f"hs_{raw_key}"
        key_prefix = plain_key[:11]  # "hs_" + first 8 chars

        # Hash the key for storage
        key_hash = bcrypt.hashpw(plain_key.encode(), bcrypt.gensalt()).decode()

        query = """
            INSERT INTO api_keys (user_id, organization_id, name, key_prefix, key_hash, scopes, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, user_id, organization_id, name, key_prefix, scopes,
                      last_used_at, use_count, expires_at, is_active, created_at, updated_at
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                str(user_id),
                str(organization_id) if organization_id else None,
                name,
                key_prefix,
                key_hash,
                scopes or ["read", "write"],
                expires_at,
            )

        result = self._row_to_dict(row), plain_key
        logger.info("API key created: name=%s, user=%s, prefix=%s", name, user_id, key_prefix)
        return result

    async def get_by_name(self, user_id: UUID, name: str) -> dict[str, Any] | None:
        """Get an active API key by name for a user.

        Args:
            user_id: The user ID.
            name: The key name.

        Returns:
            The API key record, or None if not found.
        """
        query = """
            SELECT id, user_id, organization_id, name, key_prefix, scopes,
                   last_used_at, use_count, expires_at, is_active, created_at, updated_at
            FROM api_keys
            WHERE user_id = $1 AND name = $2 AND revoked_at IS NULL
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(user_id), name)

        if row is None:
            return None

        return self._row_to_dict(row)

    async def verify(self, plain_key: str) -> dict[str, Any] | None:
        """Verify an API key and return the associated record.

        Args:
            plain_key: The plain text API key to verify.

        Returns:
            The API key record with user info if valid, None otherwise.
        """
        if not plain_key.startswith("hs_"):
            return None

        key_prefix = plain_key[:11]

        # Find all active keys with this prefix (prefix collisions are possible)
        query = """
            SELECT ak.id, ak.user_id, ak.organization_id, ak.name, ak.key_prefix,
                   ak.key_hash, ak.scopes, ak.last_used_at, ak.use_count,
                   ak.expires_at, ak.is_active, ak.created_at, ak.updated_at,
                   u.email as user_email, u.display_name as user_display_name, u.role as user_role
            FROM api_keys ak
            JOIN users u ON ak.user_id = u.id
            WHERE ak.key_prefix = $1
              AND ak.is_active = true
              AND ak.revoked_at IS NULL
              AND u.is_active = true
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, key_prefix)

        if not rows:
            return None

        # Check each matching key's hash (handles prefix collisions)
        matched_row = None
        for row in rows:
            if bcrypt.checkpw(plain_key.encode(), row["key_hash"].encode()):
                matched_row = row
                break

        if matched_row is None:
            logger.warning("API key verification failed: hash mismatch for prefix=%s", key_prefix)
            return None

        # Check expiration
        if matched_row["expires_at"] and matched_row["expires_at"] < datetime.now(timezone.utc):
            logger.warning("API key verification failed: expired key prefix=%s", key_prefix)
            return None

        # Update last used timestamp and count
        update_query = """
            UPDATE api_keys
            SET last_used_at = NOW(), use_count = use_count + 1
            WHERE id = $1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(update_query, matched_row["id"])

        result = self._row_to_dict(matched_row)
        result["user_email"] = matched_row["user_email"]
        result["user_display_name"] = matched_row["user_display_name"]
        result["user_role"] = matched_row["user_role"]
        return result

    async def list_by_user(self, user_id: UUID) -> list[dict[str, Any]]:
        """List all API keys for a user.

        Args:
            user_id: The user ID.

        Returns:
            List of API key records (without hashes).
        """
        query = """
            SELECT id, user_id, organization_id, name, key_prefix, scopes,
                   last_used_at, use_count, expires_at, is_active, created_at, updated_at
            FROM api_keys
            WHERE user_id = $1 AND revoked_at IS NULL
            ORDER BY created_at DESC
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, str(user_id))

        return [self._row_to_dict(row) for row in rows]

    async def get_by_id(self, key_id: UUID, user_id: UUID) -> dict[str, Any] | None:
        """Get an API key by ID (must belong to user).

        Args:
            key_id: The API key ID.
            user_id: The user ID (for ownership check).

        Returns:
            The API key record, or None if not found.
        """
        query = """
            SELECT id, user_id, organization_id, name, key_prefix, scopes,
                   last_used_at, use_count, expires_at, is_active, created_at, updated_at
            FROM api_keys
            WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(key_id), str(user_id))

        if row is None:
            return None

        return self._row_to_dict(row)

    async def revoke(self, key_id: UUID, user_id: UUID) -> bool:
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
            WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL
            RETURNING id
        """

        async with self.pool.acquire() as conn:
            result = await conn.fetchrow(query, str(key_id), str(user_id))

        if result is not None:
            logger.info("API key revoked: id=%s, user=%s", key_id, user_id)
        return result is not None

    async def update_name(self, key_id: UUID, user_id: UUID, name: str) -> dict[str, Any] | None:
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
            WHERE id = $2 AND user_id = $3 AND revoked_at IS NULL
            RETURNING id, user_id, organization_id, name, key_prefix, scopes,
                      last_used_at, use_count, expires_at, is_active, created_at, updated_at
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, name, str(key_id), str(user_id))

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
            result["organization_id"] = row["organization_id"] if isinstance(row["organization_id"], UUID) else UUID(row["organization_id"])
        else:
            result["organization_id"] = None

        return result
