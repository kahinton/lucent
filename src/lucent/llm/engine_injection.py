"""Translate a local provider into engine-specific run configuration.

Each coding engine consumes a local provider through the API surface it speaks
(see :mod:`lucent.llm.local_providers`). This module is the one place that knows
how to phrase that surface per engine, so engines stay free of provider naming
and new providers need no engine changes.

  - ``copilot``      → BYOK ``provider={"type": "openai", "base_url": <base>/v1}``
  - ``claude_code``  → subprocess env ``ANTHROPIC_BASE_URL`` (+ auth token)
  - ``codex``        → ``model_provider=<provider id>`` (+ env/base-url overrides)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lucent.llm.local_providers import (
    LocalProviderSpec,
    is_local_provider,
    local_provider_spec,
    provider_supports_flavor,
    resolve_local_base_url,
)

# The local-provider API surface each engine consumes.
ENGINE_LOCAL_FLAVOR: dict[str, str] = {
    "copilot": "openai",
    "claude_code": "anthropic",
    "codex": "codex-oss",
}

# Codex reserves these provider ids and refuses custom definitions for them.
_CODEX_RESERVED_PROVIDER_IDS = frozenset({"openai", "ollama", "lmstudio", "amazon-bedrock"})


class LocalInjectionError(ValueError):
    """Raised when a provider cannot be injected into an engine."""


@dataclass(frozen=True)
class LocalInjection:
    """Resolved local-model injection for one engine."""

    engine: str
    provider: str
    base_url: str
    model: str | None
    kwargs: dict[str, Any] = field(default_factory=dict)


def engine_local_flavor(engine: str) -> str | None:
    """The local-provider surface an engine consumes, if any."""
    return ENGINE_LOCAL_FLAVOR.get(engine)


def local_provider_for_backend(backend: dict[str, Any] | None) -> LocalProviderSpec | None:
    """Return the local provider a backend targets, or None.

    A backend targets a local provider when its ``provider`` names a registered
    local provider (e.g. ``ollama``). Native backends (provider ``anthropic`` /
    ``openai``) return None and use the engine's provider-native models.
    """
    if not backend:
        return None
    provider = str(backend.get("provider") or "").strip().lower()
    if not is_local_provider(provider):
        return None
    return local_provider_spec(provider)


def build_local_injection(
    engine: str,
    spec: LocalProviderSpec,
    *,
    base_url: str | None = None,
    model: str | None = None,
) -> LocalInjection:
    """Build the engine-specific injection for a local provider."""
    flavor = engine_local_flavor(engine)
    if flavor is None:
        raise LocalInjectionError(f"Engine {engine!r} does not consume local providers.")
    if not provider_supports_flavor(spec, flavor):
        raise LocalInjectionError(
            f"Local provider {spec.name!r} does not expose the {flavor!r} surface "
            f"required by engine {engine!r} (it exposes: {sorted(spec.flavors)})."
        )

    base = resolve_local_base_url(spec, configured_base_url=base_url)
    kwargs: dict[str, Any]

    if engine == "copilot":
        kwargs = {"provider": {"type": "openai", "base_url": f"{base}/v1"}}
    elif engine == "claude_code":
        # Claude Code reads ANTHROPIC_BASE_URL; the local server ignores the token
        # but Claude Code requires one. Blank any inherited API key so it can't
        # shadow the token.
        kwargs = {
            "env": {
                "ANTHROPIC_BASE_URL": base,
                "ANTHROPIC_AUTH_TOKEN": spec.name,
                "ANTHROPIC_API_KEY": "",
            }
        }
    elif engine == "codex":
        if not spec.codex_provider_id:
            raise LocalInjectionError(
                f"Local provider {spec.name!r} is not usable by Codex (no provider id)."
            )
        kwargs = {"model_provider": spec.codex_provider_id}
        overrides: dict[str, Any] = {}
        # Codex reserves its native provider ids and rejects custom definitions for
        # them, so we point those at their base URL via the conventional env var
        # instead of a model_providers block.
        if spec.codex_provider_id not in _CODEX_RESERVED_PROVIDER_IDS:
            overrides["model_providers"] = {spec.codex_provider_id: {"base_url": f"{base}/v1"}}
        if overrides:
            kwargs["config_overrides"] = overrides
        if spec.base_url_env and base:
            kwargs["env"] = {spec.base_url_env: base}
    else:  # pragma: no cover - guarded by engine_local_flavor
        raise LocalInjectionError(f"Engine {engine!r} does not consume local providers.")

    return LocalInjection(
        engine=engine, provider=spec.name, base_url=base, model=model, kwargs=kwargs
    )


def backend_local_injection(
    engine: str, backend: dict[str, Any] | None, *, model: str | None = None
) -> LocalInjection | None:
    """Local injection for a backend, or None when it is not a local provider."""
    spec = local_provider_for_backend(backend)
    if spec is None:
        return None
    configured_base_url = (backend.get("config") or {}).get("base_url")
    return build_local_injection(engine, spec, base_url=configured_base_url, model=model)


# ── Provider compatibility (for UI + validation) ───────────────────────────
# The provider(s) each engine can host natively; local providers are added by
# matching the engine's required flavor. Exposed so UIs only offer combinations
# we actually support.

_ENGINE_NATIVE_PROVIDERS: dict[str, tuple[str, ...]] = {
    "copilot": ("copilot",),
    "claude_code": ("anthropic",),
    "codex": ("openai",),
}

_NATIVE_PROVIDER_LABELS: dict[str, str] = {
    "copilot": "GitHub Copilot (hosted)",
    "anthropic": "Anthropic API",
    "openai": "OpenAI API",
}


def compatible_provider_options(engine: str) -> list[dict[str, str]]:
    """Providers an engine can actually use: its native provider(s) plus every
    local provider that exposes the API surface the engine consumes."""
    from lucent.llm.local_providers import LOCAL_PROVIDERS

    options: list[dict[str, str]] = []
    for name in _ENGINE_NATIVE_PROVIDERS.get(engine, ()):
        options.append(
            {
                "name": name,
                "label": _NATIVE_PROVIDER_LABELS.get(name, name.title()),
                "group": "native",
            }
        )
    flavor = engine_local_flavor(engine)
    if flavor is not None:
        for spec in LOCAL_PROVIDERS.values():
            if flavor in spec.flavors:
                options.append(
                    {"name": spec.name, "label": spec.display_name, "group": "local"}
                )
    return options


def provider_allowed_for_engine(engine: str, provider: str) -> bool:
    """Whether an engine+provider pairing is one we support."""
    provider = (provider or "").strip().lower()
    return any(option["name"] == provider for option in compatible_provider_options(engine))
