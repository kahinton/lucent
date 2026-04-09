"""Pluggable secret storage for Lucent.

Provides:
- SecretProvider ABC — interface for secret backends
- SecretScope — ownership scope for secrets
- SecretRegistry — provider registry
- BuiltinSecretProvider — Postgres + Fernet encryption backend
"""

from lucent.secrets.base import SecretProvider, SecretScope
from lucent.secrets.registry import (
    SecretRegistry,
    get_selected_provider_name,
    initialize_secret_provider,
    validate_provider_env,
)
from lucent.secrets.utils import SECRET_REF_PREFIX, resolve_env_vars

__all__ = [
    "SecretProvider",
    "SecretScope",
    "SecretRegistry",
    "SECRET_REF_PREFIX",
    "resolve_env_vars",
    "get_selected_provider_name",
    "validate_provider_env",
    "initialize_secret_provider",
]
