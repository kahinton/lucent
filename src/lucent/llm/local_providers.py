"""Registry of local (self-hosted / OSS) model providers.

A local provider is an OpenAI-/Anthropic-compatible endpoint we run ourselves —
Ollama today, with room for LM Studio, vLLM, a LiteLLM gateway, and so on. Rather
than hardcode "ollama" anywhere, a provider declares **which API surfaces it
exposes** (``flavors``); each coding engine consumes providers whose surface it
supports. Adding a provider is one spec, no engine changes.

Flavors:
  - ``openai``     — OpenAI-compatible chat/completions (``/v1/chat/completions``).
                     Consumed by the Copilot harness via BYOK.
  - ``anthropic``  — Anthropic Messages API (``/v1/messages``). Ollama >= 0.14
                     serves this, so the Claude Code harness can talk to it
                     directly via ``ANTHROPIC_BASE_URL``.
  - ``codex-oss``  — a provider id the Codex CLI recognises natively (its
                     ``--oss`` / ``model_provider`` reserved ids: ollama, lmstudio).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

FLAVORS = ("openai", "anthropic", "codex-oss")


class UnknownLocalProviderError(ValueError):
    """Raised when a local provider name is not in the registry."""


@dataclass(frozen=True, slots=True)
class LocalProviderSpec:
    """Descriptor for one local/OSS model provider."""

    name: str
    display_name: str
    default_base_url: str
    base_url_env: str | None = None
    flavors: frozenset[str] = frozenset()
    # The provider id the Codex CLI uses for this backend (its reserved
    # ``model_provider`` / ``oss_provider`` values). None when Codex can't use it.
    codex_provider_id: str | None = None
    notes: str = ""


# Order defines the order returned by local_provider_names() and shown in UIs.
_BUILTIN_PROVIDERS: tuple[LocalProviderSpec, ...] = (
    LocalProviderSpec(
        name="ollama",
        display_name="Ollama",
        default_base_url="http://localhost:11434",
        base_url_env="OLLAMA_HOST",
        flavors=frozenset({"openai", "anthropic", "codex-oss"}),
        codex_provider_id="ollama",
        notes="Local models; also reachable by Claude Code and Copilot.",
    ),
    LocalProviderSpec(
        name="lmstudio",
        display_name="LM Studio",
        default_base_url="http://localhost:1234",
        base_url_env="LMSTUDIO_HOST",
        flavors=frozenset({"openai", "codex-oss"}),
        codex_provider_id="lmstudio",
        notes="OpenAI-compatible local server.",
    ),
)

LOCAL_PROVIDERS: dict[str, LocalProviderSpec] = {p.name: p for p in _BUILTIN_PROVIDERS}


def local_provider_spec(name: str) -> LocalProviderSpec:
    """Return the spec for a local provider name, or raise."""
    spec = LOCAL_PROVIDERS.get((name or "").strip().lower())
    if spec is None:
        raise UnknownLocalProviderError(
            f"Unknown local provider: {name!r}. Known: {sorted(LOCAL_PROVIDERS)}."
        )
    return spec


def is_local_provider(name: str) -> bool:
    """Whether a name refers to a registered local provider."""
    return (name or "").strip().lower() in LOCAL_PROVIDERS


def local_provider_names() -> list[str]:
    """Registered local provider names, in registry order."""
    return list(LOCAL_PROVIDERS)


def local_provider_choices() -> tuple[str, ...]:
    """Names suitable for an enum/form field."""
    return tuple(LOCAL_PROVIDERS)


def provider_supports_flavor(spec: LocalProviderSpec, flavor: str) -> bool:
    """Whether a local provider exposes a given API surface."""
    return flavor in spec.flavors


def resolve_local_base_url(
    spec: LocalProviderSpec,
    *,
    configured_base_url: str | None = None,
) -> str:
    """Resolve a provider's base URL: explicit config → env → provider default."""
    if configured_base_url and configured_base_url.strip():
        return configured_base_url.strip().rstrip("/")
    if spec.base_url_env:
        env_value = os.environ.get(spec.base_url_env, "").strip()
        if env_value:
            return env_value.rstrip("/")
    return spec.default_base_url.rstrip("/")
