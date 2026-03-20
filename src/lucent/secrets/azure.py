"""Azure Key Vault secret provider stub."""

from __future__ import annotations

from lucent.secrets.base import SecretProvider, SecretScope


class AzureSecretProvider(SecretProvider):
    """Azure Key Vault integration. Requires AZURE_* env vars.

    TODO:
    - Initialize azure-keyvault-secrets client with credential chain.
    - Map SecretScope to Azure secret naming/path strategy.
    - Implement get/set/delete/list via Azure Key Vault APIs.

    Expected environment configuration:
    - AZURE_TENANT_ID
    - AZURE_CLIENT_ID
    - AZURE_CLIENT_SECRET
    - AZURE_KEY_VAULT_URL
    """

    async def get(self, key: str, scope: SecretScope) -> str | None:
        raise NotImplementedError(
            "AzureSecretProvider.get is not implemented. "
            "Set LUCENT_SECRET_PROVIDER=builtin for now, or implement Azure Key Vault reads."
        )

    async def set(self, key: str, value: str, scope: SecretScope) -> None:
        raise NotImplementedError(
            "AzureSecretProvider.set is not implemented. "
            "Implement Azure Key Vault writes before enabling LUCENT_SECRET_PROVIDER=azure."
        )

    async def delete(self, key: str, scope: SecretScope) -> bool:
        raise NotImplementedError(
            "AzureSecretProvider.delete is not implemented. "
            "Implement Azure Key Vault delete before enabling LUCENT_SECRET_PROVIDER=azure."
        )

    async def list_keys(self, scope: SecretScope) -> list[str]:
        raise NotImplementedError(
            "AzureSecretProvider.list_keys is not implemented. "
            "Implement Azure Key Vault list before enabling LUCENT_SECRET_PROVIDER=azure."
        )
