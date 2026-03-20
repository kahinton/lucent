"""HashiCorp Vault / OpenBao secret provider using the KV v2 HTTP API."""

from __future__ import annotations

import logging
import os

import httpx

from lucent.secrets.base import SecretProvider, SecretScope

logger = logging.getLogger(__name__)


class VaultSecretProvider(SecretProvider):
    """KV v2 secret provider for HashiCorp Vault and OpenBao.

    Environment configuration:
    - VAULT_ADDR: Vault/OpenBao API base URL (e.g. http://openbao:8200)
    - VAULT_TOKEN: Token with read/write access to the KV mount
    - VAULT_KV_MOUNT: KV v2 mount path (default: "secret")
    """

    def __init__(self) -> None:
        addr = os.environ.get("VAULT_ADDR")
        token = os.environ.get("VAULT_TOKEN")
        if not addr or not token:
            raise ValueError(
                "VaultSecretProvider requires VAULT_ADDR and VAULT_TOKEN "
                "environment variables to be set."
            )
        self._mount = os.environ.get("VAULT_KV_MOUNT", "secret")
        self._client = httpx.AsyncClient(
            base_url=addr.rstrip("/"),
            headers={"X-Vault-Token": token},
            timeout=10.0,
        )

    def _build_path(self, scope: SecretScope) -> str:
        """Build the Vault path prefix for a scope (without the key)."""
        if scope.owner_user_id:
            owner_type = "user"
            owner_id = scope.owner_user_id
        elif scope.owner_group_id:
            owner_type = "group"
            owner_id = scope.owner_group_id
        else:
            raise ValueError("SecretScope must have owner_user_id or owner_group_id")
        return f"lucent/{scope.organization_id}/{owner_type}/{owner_id}"

    async def get(self, key: str, scope: SecretScope) -> str | None:
        path = self._build_path(scope)
        url = f"/v1/{self._mount}/data/{path}/{key}"
        logger.info("Vault GET secret at %s/%s", path, key)
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Vault connection error reading secret '{key}': {type(exc).__name__}"
            ) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError(
                f"Vault returned {resp.status_code} reading secret '{key}'"
            )
        data = resp.json()
        logger.debug("Vault GET response for %s/%s (status=%d)", path, key, resp.status_code)
        return data["data"]["data"]["value"]

    async def set(self, key: str, value: str, scope: SecretScope) -> None:
        path = self._build_path(scope)
        url = f"/v1/{self._mount}/data/{path}/{key}"
        logger.info("Vault SET secret at %s/%s", path, key)
        try:
            resp = await self._client.post(url, json={"data": {"value": value}})
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Vault connection error writing secret '{key}': {type(exc).__name__}"
            ) from exc
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Vault returned {resp.status_code} writing secret '{key}'"
            )

    async def delete(self, key: str, scope: SecretScope) -> bool:
        path = self._build_path(scope)
        url = f"/v1/{self._mount}/metadata/{path}/{key}"
        logger.info("Vault DELETE secret at %s/%s", path, key)
        try:
            resp = await self._client.delete(url)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Vault connection error deleting secret '{key}': {type(exc).__name__}"
            ) from exc
        if resp.status_code == 204:
            return True
        if resp.status_code == 404:
            return False
        raise RuntimeError(
            f"Vault returned {resp.status_code} deleting secret '{key}'"
        )

    async def list_keys(self, scope: SecretScope) -> list[str]:
        path = self._build_path(scope)
        url = f"/v1/{self._mount}/metadata/{path}/"
        logger.info("Vault LIST keys at %s", path)
        try:
            resp = await self._client.request("LIST", url)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Vault connection error listing keys: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise RuntimeError(
                f"Vault returned {resp.status_code} listing keys"
            )
        data = resp.json()
        logger.debug("Vault LIST response for %s (status=%d)", path, resp.status_code)
        return data["data"]["keys"]

    async def health_check(self) -> bool:
        """Check if OpenBao/Vault is reachable and unsealed."""
        try:
            resp = await self._client.get("/v1/sys/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
