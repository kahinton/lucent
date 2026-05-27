"""AWS Secrets Manager secret provider — PLANNED, NOT YET IMPLEMENTED.

This module defines the interface for a future AWS Secrets Manager
integration.  All methods raise ``NotImplementedError`` with guidance
on which provider to use instead.

Status: PLANNED
Available alternatives: builtin, vault, transit
Tracking: OWASP Finding 15
"""

from __future__ import annotations

from lucent.secrets.base import SecretProvider, SecretScope


class AWSSecretProvider(SecretProvider):
    """AWS Secrets Manager integration — **not yet implemented**.

    This provider is planned for a future release.  Selecting
    ``LUCENT_SECRET_PROVIDER=aws`` will fail at startup with a clear
    error message directing you to use ``builtin``, ``vault``, or
    ``transit`` instead.

    When implemented, it will require:
    - ``AWS_REGION`` (or ``AWS_DEFAULT_REGION``)
    - AWS credentials (IAM role, access key, or web identity)
    """

    async def get(self, key: str, scope: SecretScope) -> str | None:
        raise NotImplementedError(
            "AWS Secrets Manager provider is not yet implemented. "
            "Use LUCENT_SECRET_PROVIDER=builtin, vault, or transit."
        )

    async def set(self, key: str, value: str, scope: SecretScope) -> None:
        raise NotImplementedError(
            "AWS Secrets Manager provider is not yet implemented. "
            "Use LUCENT_SECRET_PROVIDER=builtin, vault, or transit."
        )

    async def delete(self, key: str, scope: SecretScope) -> bool:
        raise NotImplementedError(
            "AWS Secrets Manager provider is not yet implemented. "
            "Use LUCENT_SECRET_PROVIDER=builtin, vault, or transit."
        )

    async def list_keys(self, scope: SecretScope) -> list[str]:
        raise NotImplementedError(
            "AWS Secrets Manager provider is not yet implemented. "
            "Use LUCENT_SECRET_PROVIDER=builtin, vault, or transit."
        )
