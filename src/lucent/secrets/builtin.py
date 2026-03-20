"""Built-in secret provider using PostgreSQL + Fernet encryption.

Encrypts secret values at rest using a key derived from LUCENT_SECRET_KEY
via PBKDF2. Raises a clear error if the env var is not set.
"""

from __future__ import annotations

import base64
import hashlib
import os
from uuid import UUID

import asyncpg
from cryptography.fernet import Fernet, InvalidToken

from lucent.secrets.base import SecretProvider, SecretScope


class SecretKeyError(Exception):
    """Raised when LUCENT_SECRET_KEY is missing or invalid."""


def _derive_fernet_key(secret_key: str) -> bytes:
    """Derive a 32-byte Fernet key from an arbitrary string using PBKDF2."""
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        secret_key.encode("utf-8"),
        b"lucent-secrets-v1",
        iterations=480_000,
        dklen=32,
    )
    return base64.urlsafe_b64encode(dk)


def _get_fernet(secret_key: str | None = None) -> Fernet:
    """Create a Fernet instance from LUCENT_SECRET_KEY or the provided key."""
    raw = secret_key or os.environ.get("LUCENT_SECRET_KEY")
    if not raw:
        raise SecretKeyError(
            "LUCENT_SECRET_KEY environment variable is not set. "
            "Secret storage requires an encryption key. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    return Fernet(_derive_fernet_key(raw))


class BuiltinSecretProvider(SecretProvider):
    """PostgreSQL-backed secret provider with Fernet encryption at rest."""

    def __init__(self, pool: asyncpg.Pool, secret_key: str | None = None) -> None:
        self._pool = pool
        self._fernet = _get_fernet(secret_key)

    def _encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def _decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken:
            raise SecretKeyError("Decryption failed — wrong key or corrupted data")

    def _scope_filter(self, scope: SecretScope) -> tuple[str, list]:
        """Build WHERE clause + params for scope-based lookups."""
        conditions = ["organization_id = $1"]
        params: list = [UUID(scope.organization_id)]
        idx = 2
        if scope.owner_user_id:
            conditions.append(f"owner_user_id = ${idx}")
            params.append(UUID(scope.owner_user_id))
            idx += 1
        if scope.owner_group_id:
            conditions.append(f"owner_group_id = ${idx}")
            params.append(UUID(scope.owner_group_id))
        return " AND ".join(conditions), params

    async def get(self, key: str, scope: SecretScope) -> str | None:
        where, params = self._scope_filter(scope)
        query = f"SELECT encrypted_value FROM secrets WHERE key = ${len(params) + 1} AND {where}"
        params.append(key)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        if row is None:
            return None
        return self._decrypt(row["encrypted_value"])

    async def set(self, key: str, value: str, scope: SecretScope) -> None:
        encrypted = self._encrypt(value)
        owner_user = UUID(scope.owner_user_id) if scope.owner_user_id else None
        owner_group = UUID(scope.owner_group_id) if scope.owner_group_id else None
        org_id = UUID(scope.organization_id)

        # Upsert: try update first, then insert
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                where, params = self._scope_filter(scope)
                update_q = (
                    f"UPDATE secrets SET encrypted_value = ${len(params) + 1}, "
                    f"updated_at = NOW() "
                    f"WHERE key = ${len(params) + 2} AND {where}"
                )
                params.extend([encrypted, key])
                result = await conn.execute(update_q, *params)
                if result == "UPDATE 0":
                    await conn.execute(
                        "INSERT INTO secrets (key, encrypted_value, owner_user_id, "
                        "owner_group_id, organization_id) VALUES ($1, $2, $3, $4, $5)",
                        key,
                        encrypted,
                        owner_user,
                        owner_group,
                        org_id,
                    )

    async def delete(self, key: str, scope: SecretScope) -> bool:
        where, params = self._scope_filter(scope)
        query = f"DELETE FROM secrets WHERE key = ${len(params) + 1} AND {where}"
        params.append(key)
        async with self._pool.acquire() as conn:
            result = await conn.execute(query, *params)
        return result != "DELETE 0"

    async def list_keys(self, scope: SecretScope) -> list[str]:
        where, params = self._scope_filter(scope)
        query = f"SELECT key FROM secrets WHERE {where} ORDER BY key"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [r["key"] for r in rows]

    async def get_secret_id(self, key: str, scope: SecretScope) -> str | None:
        """Get the UUID of a secret by key and scope (for ACL checks)."""
        where, params = self._scope_filter(scope)
        query = f"SELECT id FROM secrets WHERE key = ${len(params) + 1} AND {where}"
        params.append(key)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        return str(row["id"]) if row else None
