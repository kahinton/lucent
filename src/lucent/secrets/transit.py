"""Transit secret provider using OpenBao/Vault Transit engine + PostgreSQL.

Delegates all cryptographic operations to OpenBao's Transit engine while
storing ciphertext in PostgreSQL. Lucent never sees the encryption key —
even if the database and Lucent container are both compromised, secrets
remain safe without access to OpenBao's transit key.
"""

from __future__ import annotations

import base64
import logging
from uuid import UUID

import asyncpg
import httpx

from lucent.secrets.base import SecretProvider, SecretScope

logger = logging.getLogger(__name__)


class TransitSecretProvider(SecretProvider):
    """Transit engine secret provider for OpenBao/Vault.

    Encrypts and decrypts secrets via the Transit engine HTTP API.
    Ciphertext is stored in the PostgreSQL ``secrets`` table (same schema
    as the builtin provider).  The encryption key never leaves OpenBao.

    Parameters
    ----------
    pool : asyncpg.Pool
        PostgreSQL connection pool.
    vault_addr : str
        OpenBao/Vault API base URL (e.g. ``http://openbao:8200``).
    vault_token : str
        Token with access to the Transit engine.
    transit_key : str
        Name of the Transit encryption key (default ``lucent-secrets``).
    transit_mount : str
        Mount path of the Transit engine (default ``transit``).
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        vault_addr: str,
        vault_token: str,
        transit_key: str = "lucent-secrets",
        transit_mount: str = "transit",
    ) -> None:
        self._pool = pool
        from lucent.db.secrets import SecretRepository

        self._repository = SecretRepository(pool)
        self._transit_key = transit_key
        self._transit_mount = transit_mount
        self._client = httpx.AsyncClient(
            base_url=vault_addr.rstrip("/"),
            headers={"X-Vault-Token": vault_token},
            timeout=10.0,
        )

    # ------------------------------------------------------------------
    # Transit encrypt / decrypt helpers
    # ------------------------------------------------------------------

    async def _encrypt(self, plaintext: str) -> str:
        """Encrypt *plaintext* via the Transit engine.

        Returns the ciphertext string (e.g. ``vault:v1:…``).
        """
        encoded = base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
        url = f"/v1/{self._transit_mount}/encrypt/{self._transit_key}"
        try:
            resp = await self._client.post(url, json={"plaintext": encoded})
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Transit encrypt request failed: {type(exc).__name__}"
            ) from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"Transit encrypt returned {resp.status_code}"
            )
        return resp.json()["data"]["ciphertext"]

    async def _decrypt(self, ciphertext: str) -> str:
        """Decrypt *ciphertext* via the Transit engine.

        Returns the original plaintext string.
        """
        url = f"/v1/{self._transit_mount}/decrypt/{self._transit_key}"
        try:
            resp = await self._client.post(url, json={"ciphertext": ciphertext})
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Transit decrypt request failed: {type(exc).__name__}"
            ) from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"Transit decrypt returned {resp.status_code}"
            )
        b64 = resp.json()["data"]["plaintext"]
        return base64.b64decode(b64).decode("utf-8")

    # ------------------------------------------------------------------
    # Scope helpers (same pattern as BuiltinSecretProvider)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # SecretProvider interface
    # ------------------------------------------------------------------

    async def get(self, key: str, scope: SecretScope) -> str | None:
        encrypted = await self._repository.get_encrypted_value(key, scope)
        if encrypted is None:
            return None
        return await self._decrypt(encrypted.decode("utf-8"))

    async def set(self, key: str, value: str, scope: SecretScope) -> None:
        ciphertext = await self._encrypt(value)
        await self._repository.upsert_encrypted_value(key, ciphertext.encode("utf-8"), scope)

    async def delete(self, key: str, scope: SecretScope) -> bool:
        return await self._repository.delete(key, scope)

    async def list_keys(self, scope: SecretScope) -> list[str]:
        return await self._repository.list_keys(scope)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Check OpenBao connectivity and Transit key availability."""
        try:
            # Check system health
            resp = await self._client.get("/v1/sys/health")
            if resp.status_code != 200:
                return False
            # Verify the transit key exists
            key_url = f"/v1/{self._transit_mount}/keys/{self._transit_key}"
            resp = await self._client.get(key_url)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
