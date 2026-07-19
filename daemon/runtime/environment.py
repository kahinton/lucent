"""Host environment initialization and runtime secret resolution."""

from __future__ import annotations

import os
import re
from pathlib import Path


def auto_load_vault_env(daemon_file: Path) -> None:
    """Load local OpenBao connection metadata when running outside Compose."""
    if os.environ.get("VAULT_ADDR") and (
        os.environ.get("VAULT_TOKEN") or os.environ.get("VAULT_TOKEN_FILE")
    ):
        return
    shared_token = daemon_file.resolve().parent.parent / ".openbao" / "shared" / "vault-token"
    if not shared_token.exists():
        return
    os.environ.setdefault("VAULT_ADDR", "http://127.0.0.1:8200")
    os.environ.setdefault("VAULT_TOKEN_FILE", str(shared_token))


def resolve_env_vars(value: str) -> str:
    """Resolve environment placeholders while preserving unknown variables."""
    return re.sub(
        r"\$\{([^}]+)\}",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        value,
    )


async def resolve_runtime_value(value: str) -> str:
    """Resolve environment interpolation and optional secret references."""
    from lucent.secrets import SecretRegistry
    from lucent.secrets.utils import is_secret_reference, resolve_secret_reference

    resolved = resolve_env_vars(value)
    if not is_secret_reference(resolved):
        return resolved
    return await resolve_secret_reference(resolved, SecretRegistry.get())


async def get_secret_provider():
    """Return the configured secret provider, initializing it when possible."""
    from lucent.secrets import SecretRegistry, initialize_secret_provider

    if SecretRegistry.is_registered():
        return SecretRegistry.get()
    try:
        from lucent.db import get_pool

        return await initialize_secret_provider(await get_pool())
    except Exception:
        return None
