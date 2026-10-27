"""Validation helpers for model engine overrides.

Engine identity is derived from :mod:`lucent.llm.engine_registry` so the set of
valid engines and the providers each supports live in one place.
"""

from __future__ import annotations

from importlib.util import find_spec

from lucent.llm.engine_registry import (
    compatible_engine_names,
    engine_names,
    engine_supports_provider,
)

# Only engines implemented in this build are selectable.
ALLOWED_ENGINES = set(engine_names(implemented_only=True))

# Provider → LangChain integration package. This is genuinely LangChain-specific
# (how the general engine reaches each provider), so it stays here. Derived from
# the provider catalog so every integration Lucent can run is covered, not only
# the four the base install ships — keeping this list hardcoded is what made an
# installed ``langchain-groq`` unusable.
def _provider_package_map() -> dict[str, str]:
    from lucent.llm.langchain_providers import LANGCHAIN_PROVIDERS, normalize_provider

    packages = {spec.langchain_provider: spec.package for spec in LANGCHAIN_PROVIDERS.values()}
    for name, spec in LANGCHAIN_PROVIDERS.items():
        packages[name] = spec.package
        packages[normalize_provider(name)] = spec.package
    return packages


LANGCHAIN_PROVIDER_PACKAGES = _provider_package_map()


def normalize_engine(engine: str | None) -> str | None:
    """Normalize engine override value; None means auto-detect."""
    if engine is None:
        return None
    normalized = engine.strip().lower()
    if normalized in ("", "auto"):
        return None
    if normalized in ALLOWED_ENGINES:
        return normalized
    allowed = ", ".join(f"'{name}'" for name in sorted(ALLOWED_ENGINES))
    raise ValueError(f"Invalid engine value. Expected null or one of: {allowed}.")


def validate_engine_override(provider: str, engine: str | None) -> list[str]:
    """Validate provider/engine combination.

    Returns warnings (non-fatal) and raises ValueError on fatal validation failures.
    """
    warnings: list[str] = []
    normalized_engine = normalize_engine(engine)
    normalized_provider = (provider or "").strip().lower()

    if not compatible_engine_names(normalized_provider):
        # A provider Lucent knows about but whose integration package is missing
        # deserves a fixable message ("pip install langchain-groq"), not the
        # generic "no engine supports it" — that reads like a Lucent limitation.
        from lucent.llm.langchain_providers import (
            LANGCHAIN_PROVIDERS,
            normalize_provider,
            provider_package_installed,
        )

        catalog_spec = LANGCHAIN_PROVIDERS.get(normalize_provider(normalized_provider))
        if catalog_spec is not None and not provider_package_installed(catalog_spec.name):
            raise ValueError(
                f"Provider '{provider}' needs the '{catalog_spec.package}' package. "
                f"Install with: pip install {catalog_spec.package.replace('_', '-')}"
            )
        raise ValueError(
            f"No engine supports provider '{provider}' yet. Add an engine "
            "backend for it before creating models with this provider."
        )

    if normalized_engine and not engine_supports_provider(
        normalized_engine, normalized_provider
    ):
        compatible = compatible_engine_names(normalized_provider)
        raise ValueError(
            f"Engine '{normalized_engine}' cannot run provider '{provider}'. "
            f"Compatible engines: {', '.join(compatible) or 'none'}."
        )

    if normalized_engine == "langchain" or (
        not normalized_engine
        and "langchain" in compatible_engine_names(normalized_provider)
    ):
        if find_spec("langchain") is None:
            raise ValueError(
                "Engine 'langchain' requires the 'langchain' package. "
                "Install with: pip install langchain"
            )
        # Provider-name lookup falls back to the integration id so both
        # ``google`` and ``google_genai`` resolve to langchain-google-genai.
        from lucent.llm.langchain_providers import (
            LANGCHAIN_PROVIDERS,
            normalize_provider,
        )

        package = LANGCHAIN_PROVIDER_PACKAGES.get(normalized_provider)
        if package is None:
            spec = LANGCHAIN_PROVIDERS.get(normalize_provider(normalized_provider))
            package = spec.package if spec else None
        if package and find_spec(package) is None:
            raise ValueError(
                f"Engine 'langchain' for provider '{provider}' requires package '{package}'. "
                f"Install with: pip install {package.replace('_', '-')}"
            )

    return warnings
