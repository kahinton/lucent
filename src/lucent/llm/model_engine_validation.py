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
# (how the general engine reaches each provider), so it stays here.
LANGCHAIN_PROVIDER_PACKAGES = {
    "anthropic": "langchain_anthropic",
    "openai": "langchain_openai",
    "google": "langchain_google_genai",
    "google_genai": "langchain_google_genai",
    "ollama": "langchain_ollama",
}


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

    if normalized_engine == "langchain":
        if find_spec("langchain") is None:
            raise ValueError(
                "Engine 'langchain' requires the 'langchain' package. "
                "Install with: pip install langchain"
            )
        package = LANGCHAIN_PROVIDER_PACKAGES.get(normalized_provider)
        if package and find_spec(package) is None:
            raise ValueError(
                f"Engine 'langchain' for provider '{provider}' requires package '{package}'. "
                f"Install with: pip install {package.replace('_', '-')}"
            )

    return warnings
