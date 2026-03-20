"""AWS Secrets Manager secret provider stub."""

from __future__ import annotations

from lucent.secrets.base import SecretProvider, SecretScope


class AWSSecretProvider(SecretProvider):
    """AWS Secrets Manager integration. Requires AWS credentials.

    TODO:
    - Initialize boto3/botocore client with region and credentials.
    - Map SecretScope to deterministic secret naming/path strategy.
    - Implement get/set/delete/list via Secrets Manager APIs.

    Expected environment configuration:
    - AWS_REGION (or AWS_DEFAULT_REGION)
    - AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (or IAM role)
    - Optional (future): AWS_SESSION_TOKEN, custom endpoint
    """

    async def get(self, key: str, scope: SecretScope) -> str | None:
        raise NotImplementedError(
            "AWSSecretProvider.get is not implemented. "
            "Set LUCENT_SECRET_PROVIDER=builtin for now, or implement AWS Secrets Manager reads."
        )

    async def set(self, key: str, value: str, scope: SecretScope) -> None:
        raise NotImplementedError(
            "AWSSecretProvider.set is not implemented. "
            "Implement AWS Secrets Manager writes before enabling LUCENT_SECRET_PROVIDER=aws."
        )

    async def delete(self, key: str, scope: SecretScope) -> bool:
        raise NotImplementedError(
            "AWSSecretProvider.delete is not implemented. "
            "Implement AWS Secrets Manager delete before enabling LUCENT_SECRET_PROVIDER=aws."
        )

    async def list_keys(self, scope: SecretScope) -> list[str]:
        raise NotImplementedError(
            "AWSSecretProvider.list_keys is not implemented. "
            "Implement AWS Secrets Manager list before enabling LUCENT_SECRET_PROVIDER=aws."
        )
