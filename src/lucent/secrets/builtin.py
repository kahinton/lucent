"""Built-in secret provider using PostgreSQL + Fernet encryption.

Encrypts secret values at rest using a key derived from LUCENT_SECRET_KEY
via PBKDF2. Raises a clear error if the env var is not set.
"""

from __future__ import annotations

import base64
import hashlib
import os
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

    def __init__(self, pool, secret_key: str | None = None) -> None:
        self._pool = pool
        self._fernet = _get_fernet(secret_key)
        from lucent.db.secrets import SecretRepository

        self._repository = SecretRepository(pool)

    def _encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def _decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken:
            raise SecretKeyError("Decryption failed — wrong key or corrupted data")

    async def get(self, key: str, scope: SecretScope) -> str | None:
        encrypted_value = await self._repository.get_encrypted_value(key, scope)
        if encrypted_value is None:
            return None
        return self._decrypt(encrypted_value)

    async def set(self, key: str, value: str, scope: SecretScope) -> None:
        encrypted = self._encrypt(value)
        await self._repository.upsert_encrypted_value(key, encrypted, scope)

    async def delete(self, key: str, scope: SecretScope) -> bool:
        return await self._repository.delete(key, scope)

    async def list_keys(self, scope: SecretScope) -> list[str]:
        return await self._repository.list_keys(scope)

    async def get_secret_id(self, key: str, scope: SecretScope) -> str | None:
        """Get the UUID of a secret by key and scope (for ACL checks)."""
        return await self._repository.get_id(key, scope)
