"""Provider registry and startup selection for secret storage backends."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

from lucent.secrets.base import SecretProvider
from lucent.secrets.builtin import BuiltinSecretProvider
from lucent.secrets.transit import TransitSecretProvider
from lucent.secrets.vault import VaultSecretProvider

logger = logging.getLogger(__name__)

# Providers that are fully implemented and available for use.
_SUPPORTED_PROVIDERS = {"builtin", "vault", "transit"}

# Providers that are planned but not yet implemented.  Selecting one of
# these produces a clear error at startup rather than a confusing
# ``NotImplementedError`` at runtime.
_PLANNED_PROVIDERS = {"aws", "azure"}


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
    if raw in _PLANNED_PROVIDERS:
        raise ValueError(
            f"LUCENT_SECRET_PROVIDER='{raw}' is not yet implemented. "
            f"The '{raw}' provider is planned for a future release. "
            f"Available providers: {sorted(_SUPPORTED_PROVIDERS)}."
        )
    if raw not in _SUPPORTED_PROVIDERS:
        raise ValueError(
            "Invalid LUCENT_SECRET_PROVIDER. "
            f"Expected one of {sorted(_SUPPORTED_PROVIDERS)} or 'auto', got '{raw}'."
        )
    return raw


def validate_provider_env(provider_name: str) -> None:
    """Validate required env vars for selected provider.

    Only validates implemented providers (builtin, vault, transit).
    AWS and Azure are rejected at the ``get_selected_provider_name`` stage.
    """
    if provider_name == "builtin":
        if not os.environ.get("LUCENT_SECRET_KEY"):
            raise ValueError(
                "LUCENT_SECRET_PROVIDER=builtin requires LUCENT_SECRET_KEY."
            )
        return
    if provider_name == "vault":
        missing = ["VAULT_ADDR"] if not os.environ.get("VAULT_ADDR") else []
        if not _get_vault_token():
            missing.append("VAULT_TOKEN or VAULT_TOKEN_FILE")
        if missing:
            raise ValueError(
                f"LUCENT_SECRET_PROVIDER=vault requires env vars: {', '.join(missing)}"
            )
        _validate_vault_addr()
        return
    if provider_name == "transit":
        missing = ["VAULT_ADDR"] if not os.environ.get("VAULT_ADDR") else []
        if not _get_vault_token():
            missing.append("VAULT_TOKEN or VAULT_TOKEN_FILE")
        if missing:
            raise ValueError(
                f"LUCENT_SECRET_PROVIDER=transit requires env vars: {', '.join(missing)}"
            )
        _validate_vault_addr()
        return


def _validate_vault_addr() -> None:
    """Ensure VAULT_ADDR is a valid URL."""
    addr = os.environ.get("VAULT_ADDR", "")
    parsed = urlparse(addr)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(
            f"VAULT_ADDR must be a valid http(s) URL, got '{addr}'."
        )


def _get_vault_token() -> str:
    """Return Vault/OpenBao token from env or VAULT_TOKEN_FILE.

    Docker local development writes the root token to a shared file. Reading
    from that file avoids copying secret material into process environment.
    """
    token = os.environ.get("VAULT_TOKEN", "")
    if token:
        return token
    token_file = os.environ.get("VAULT_TOKEN_FILE", "")
    if not token_file:
        return ""
    try:
        return Path(token_file).read_text().strip()
    except OSError:
        logger.info("VAULT_TOKEN_FILE is set but could not be read")
        return ""


async def detect_provider(pool) -> str:
    """Auto-detect the best available secret provider.

    Checks OpenBao/Vault availability, falls back to builtin.
    Returns provider name: ``"transit"``, ``"vault"``, or ``"builtin"``.
    """
    vault_addr = os.environ.get("VAULT_ADDR", "").rstrip("/")
    vault_token = _get_vault_token()
    if not vault_addr or not vault_token:
        logger.info("Auto-detect: VAULT_ADDR or Vault token not set, using builtin")
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
        provider = VaultSecretProvider(
            vault_addr=os.environ["VAULT_ADDR"],
            vault_token=_get_vault_token(),
        )
    elif provider_name == "transit":
        provider = TransitSecretProvider(
            pool,
            vault_addr=os.environ["VAULT_ADDR"],
            vault_token=_get_vault_token(),
        )
    else:
        # Should not reach here — get_selected_provider_name rejects
        # unknown and planned providers before we get to this point.
        raise ValueError(f"Unknown secret provider: {provider_name}")
    SecretRegistry.register(provider_name, provider)
    SecretRegistry.set_default(provider_name)
    return provider
