"""Azure Key Vault secret provider — PLANNED, NOT YET IMPLEMENTED.

This module defines the interface for a future Azure Key Vault
integration.  All methods raise ``NotImplementedError`` with guidance
on which provider to use instead.

Status: PLANNED
Available alternatives: builtin, vault, transit
Tracking: OWASP Finding 15
"""

from __future__ import annotations

from lucent.secrets.base import SecretProvider, SecretScope


class AzureSecretProvider(SecretProvider):
    """Azure Key Vault integration — **not yet implemented**.

    This provider is planned for a future release.  Selecting
    ``LUCENT_SECRET_PROVIDER=azure`` will fail at startup with a clear
    error message directing you to use ``builtin``, ``vault``, or
    ``transit`` instead.

    When implemented, it will require:
    - ``AZURE_KEY_VAULT_URL``
    - Azure credentials (client secret, certificate, or federated token)
    """

    async def get(self, key: str, scope: SecretScope) -> str | None:
        raise NotImplementedError(
            "Azure Key Vault provider is not yet implemented. "
            "Use LUCENT_SECRET_PROVIDER=builtin, vault, or transit."
        )

    async def set(self, key: str, value: str, scope: SecretScope) -> None:
        raise NotImplementedError(
            "Azure Key Vault provider is not yet implemented. "
            "Use LUCENT_SECRET_PROVIDER=builtin, vault, or transit."
        )

    async def delete(self, key: str, scope: SecretScope) -> bool:
        raise NotImplementedError(
            "Azure Key Vault provider is not yet implemented. "
            "Use LUCENT_SECRET_PROVIDER=builtin, vault, or transit."
        )

    async def list_keys(self, scope: SecretScope) -> list[str]:
        raise NotImplementedError(
            "Azure Key Vault provider is not yet implemented. "
            "Use LUCENT_SECRET_PROVIDER=builtin, vault, or transit."
        )
