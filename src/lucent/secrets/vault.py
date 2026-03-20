"""HashiCorp Vault secret provider stub."""

from __future__ import annotations

from lucent.secrets.base import SecretProvider, SecretScope


class VaultSecretProvider(SecretProvider):
    """HashiCorp Vault integration. Requires VAULT_ADDR and VAULT_TOKEN env vars.

    TODO:
    - Read provider config from env and/or app config.
    - Implement KV v2 read/write/delete/list operations.
    - Enforce SecretScope mapping to Vault paths/policies.

    Expected environment configuration:
    - VAULT_ADDR: Vault API base URL (e.g. https://vault.example.com)
    - VAULT_TOKEN: Vault token with read/write access to the configured mount/path
    - Optional (future): VAULT_NAMESPACE, VAULT_MOUNT_PATH
    """

    async def get(self, key: str, scope: SecretScope) -> str | None:
        raise NotImplementedError(
            "VaultSecretProvider.get is not implemented. "
            "Set LUCENT_SECRET_PROVIDER=builtin for now, or implement Vault KV lookups "
            "with VAULT_ADDR and VAULT_TOKEN."
        )

    async def set(self, key: str, value: str, scope: SecretScope) -> None:
        raise NotImplementedError(
            "VaultSecretProvider.set is not implemented. "
            "Implement Vault KV writes before enabling LUCENT_SECRET_PROVIDER=vault."
        )

    async def delete(self, key: str, scope: SecretScope) -> bool:
        raise NotImplementedError(
            "VaultSecretProvider.delete is not implemented. "
            "Implement Vault KV delete before enabling LUCENT_SECRET_PROVIDER=vault."
        )

    async def list_keys(self, scope: SecretScope) -> list[str]:
        raise NotImplementedError(
            "VaultSecretProvider.list_keys is not implemented. "
            "Implement Vault KV metadata list before enabling LUCENT_SECRET_PROVIDER=vault."
        )
