"""Provider registry and startup selection for secret storage backends."""

from __future__ import annotations

import os

from lucent.secrets.aws import AWSSecretProvider
from lucent.secrets.azure import AzureSecretProvider
from lucent.secrets.base import SecretProvider
from lucent.secrets.builtin import BuiltinSecretProvider
from lucent.secrets.vault import VaultSecretProvider

_SUPPORTED_PROVIDERS = {"builtin", "vault", "aws", "azure"}


class SecretRegistry:
    """Registry of named secret providers.

    Register providers at startup and retrieve them by name.
    The default provider name is 'builtin'.
    """

    _providers: dict[str, SecretProvider] = {}
    _default_name: str = "builtin"

    @classmethod
    def register(cls, name: str, provider: SecretProvider) -> None:
        """Register a secret provider under the given name."""
        cls._providers[name] = provider

    @classmethod
    def get(cls, name: str | None = None) -> SecretProvider:
        """Get a registered provider by name. Raises KeyError if not found."""
        name = name or cls._default_name
        if name not in cls._providers:
            raise KeyError(
                f"Secret provider '{name}' is not registered. "
                f"Available: {list(cls._providers.keys())}"
            )
        return cls._providers[name]

    @classmethod
    def is_registered(cls, name: str = "builtin") -> bool:
        """Check if a provider is registered."""
        return name in cls._providers

    @classmethod
    def reset(cls) -> None:
        """Clear all registered providers (for testing)."""
        cls._providers = {}
        cls._default_name = "builtin"

    @classmethod
    def set_default(cls, name: str) -> None:
        """Set the default provider name used by get()."""
        cls._default_name = name


def get_selected_provider_name() -> str:
    """Read selected provider from environment."""
    name = os.environ.get("LUCENT_SECRET_PROVIDER", "builtin").strip().lower()
    if name not in _SUPPORTED_PROVIDERS:
        raise ValueError(
            "Invalid LUCENT_SECRET_PROVIDER. "
            f"Expected one of {sorted(_SUPPORTED_PROVIDERS)}, got '{name}'."
        )
    return name


def validate_provider_env(provider_name: str) -> None:
    """Validate required env vars for selected provider."""
    if provider_name == "builtin":
        if not os.environ.get("LUCENT_SECRET_KEY"):
            raise ValueError(
                "LUCENT_SECRET_PROVIDER=builtin requires LUCENT_SECRET_KEY."
            )
        return
    if provider_name == "vault":
        missing = [k for k in ("VAULT_ADDR", "VAULT_TOKEN") if not os.environ.get(k)]
        if missing:
            raise ValueError(
                f"LUCENT_SECRET_PROVIDER=vault requires env vars: {', '.join(missing)}"
            )
        return
    if provider_name == "aws":
        if not (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")):
            raise ValueError(
                "LUCENT_SECRET_PROVIDER=aws requires AWS_REGION or AWS_DEFAULT_REGION."
            )
        if not (
            (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))
            or os.environ.get("AWS_PROFILE")
            or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
        ):
            raise ValueError(
                "LUCENT_SECRET_PROVIDER=aws requires AWS credentials "
                "(AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_PROFILE, or AWS_WEB_IDENTITY_TOKEN_FILE)."
            )
        return
    if provider_name == "azure":
        missing = [k for k in ("AZURE_KEY_VAULT_URL",) if not os.environ.get(k)]
        if missing:
            raise ValueError(
                f"LUCENT_SECRET_PROVIDER=azure requires env vars: {', '.join(missing)}"
            )
        if not (
            (
                os.environ.get("AZURE_TENANT_ID")
                and os.environ.get("AZURE_CLIENT_ID")
                and os.environ.get("AZURE_CLIENT_SECRET")
            )
            or os.environ.get("AZURE_CLIENT_CERTIFICATE_PATH")
            or os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
        ):
            raise ValueError(
                "LUCENT_SECRET_PROVIDER=azure requires AZURE client credentials."
            )


def initialize_secret_provider(pool) -> SecretProvider:
    """Instantiate/register provider selected by LUCENT_SECRET_PROVIDER."""
    provider_name = get_selected_provider_name()
    if SecretRegistry.is_registered(provider_name):
        SecretRegistry.set_default(provider_name)
        return SecretRegistry.get(provider_name)
    validate_provider_env(provider_name)
    if provider_name == "builtin":
        provider: SecretProvider = BuiltinSecretProvider(pool)
    elif provider_name == "vault":
        provider = VaultSecretProvider()
    elif provider_name == "aws":
        provider = AWSSecretProvider()
    else:
        provider = AzureSecretProvider()
    SecretRegistry.register(provider_name, provider)
    SecretRegistry.set_default(provider_name)
    return provider
