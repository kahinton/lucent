"""Provider registry and startup selection for secret storage backends."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import httpx

from lucent.secrets.aws import AWSSecretProvider
from lucent.secrets.azure import AzureSecretProvider
from lucent.secrets.base import SecretProvider
from lucent.secrets.builtin import BuiltinSecretProvider
from lucent.secrets.transit import TransitSecretProvider
from lucent.secrets.vault import VaultSecretProvider

logger = logging.getLogger(__name__)

_SUPPORTED_PROVIDERS = {"builtin", "vault", "transit", "aws", "azure"}


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
    """Read selected provider from environment.

    Returns ``"auto"`` when ``LUCENT_SECRET_PROVIDER`` is unset or explicitly
    set to ``"auto"``.  ``"auto"`` triggers runtime detection in
    :func:`initialize_secret_provider`.
    """
    raw = os.environ.get("LUCENT_SECRET_PROVIDER", "").strip().lower()
    if not raw or raw == "auto":
        return "auto"
    if raw not in _SUPPORTED_PROVIDERS:
        raise ValueError(
            "Invalid LUCENT_SECRET_PROVIDER. "
            f"Expected one of {sorted(_SUPPORTED_PROVIDERS)} or 'auto', got '{raw}'."
        )
    return raw


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
        _validate_vault_addr()
        return
    if provider_name == "transit":
        missing = [k for k in ("VAULT_ADDR", "VAULT_TOKEN") if not os.environ.get(k)]
        if missing:
            raise ValueError(
                f"LUCENT_SECRET_PROVIDER=transit requires env vars: {', '.join(missing)}"
            )
        _validate_vault_addr()
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


def _validate_vault_addr() -> None:
    """Ensure VAULT_ADDR is a valid URL."""
    addr = os.environ.get("VAULT_ADDR", "")
    parsed = urlparse(addr)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(
            f"VAULT_ADDR must be a valid http(s) URL, got '{addr}'."
        )


async def detect_provider(pool) -> str:
    """Auto-detect the best available secret provider.

    Checks OpenBao/Vault availability, falls back to builtin.
    Returns provider name: ``"transit"``, ``"vault"``, or ``"builtin"``.
    """
    vault_addr = os.environ.get("VAULT_ADDR", "").rstrip("/")
    vault_token = os.environ.get("VAULT_TOKEN", "")
    if not vault_addr or not vault_token:
        logger.info("Auto-detect: VAULT_ADDR or VAULT_TOKEN not set, using builtin")
        return "builtin"

    headers = {"X-Vault-Token": vault_token}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            # Check Vault/OpenBao health
            resp = await client.get(f"{vault_addr}/v1/sys/health", headers=headers)
            if resp.status_code != 200:
                logger.info(
                    "Auto-detect: Vault/OpenBao not healthy (status %d), using builtin",
                    resp.status_code,
                )
                return "builtin"

            # Vault is healthy — check for Transit engine
            transit_resp = await client.get(
                f"{vault_addr}/v1/transit/keys/lucent-secrets", headers=headers
            )
            if transit_resp.status_code == 200:
                logger.info("Auto-detect: Transit engine available, using transit")
                return "transit"

            logger.info("Auto-detect: Vault healthy but no Transit key, using vault")
            return "vault"
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
        logger.info("Auto-detect: Vault/OpenBao unreachable (%s), using builtin", type(exc).__name__)
        return "builtin"


async def initialize_secret_provider(pool) -> SecretProvider:
    """Instantiate/register provider selected by LUCENT_SECRET_PROVIDER.

    When the env var is unset or set to ``"auto"``, auto-detection probes
    Vault/OpenBao availability and selects the best provider.
    """
    provider_name = get_selected_provider_name()

    if provider_name == "auto":
        provider_name = await detect_provider(pool)
        logger.info("Auto-detected secret provider: %s", provider_name)

    if SecretRegistry.is_registered(provider_name):
        SecretRegistry.set_default(provider_name)
        return SecretRegistry.get(provider_name)
    validate_provider_env(provider_name)
    if provider_name == "builtin":
        provider: SecretProvider = BuiltinSecretProvider(pool)
    elif provider_name == "vault":
        provider = VaultSecretProvider()
    elif provider_name == "transit":
        provider = TransitSecretProvider(
            pool,
            vault_addr=os.environ["VAULT_ADDR"],
            vault_token=os.environ["VAULT_TOKEN"],
        )
    elif provider_name == "aws":
        provider = AWSSecretProvider()
    else:
        provider = AzureSecretProvider()
    SecretRegistry.register(provider_name, provider)
    SecretRegistry.set_default(provider_name)
    return provider
