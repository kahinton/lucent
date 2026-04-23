"""Global Lucent runtime settings and feature flags."""

from __future__ import annotations

import os


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def shadow_forget_enabled() -> bool:
    """Whether shadow forgetting sidecar reads/writes are enabled."""
    return _env_bool("LUCENT_SHADOW_FORGET_ENABLED", default=False)

