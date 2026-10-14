"""Validation helpers for model engine overrides.

Engine identity is derived from :mod:`lucent.llm.engine_registry` so the set of
valid engines and the providers each supports live in one place.
"""

from __future__ import annotations

from importlib.util import find_spec

from lucent.llm.engine_registry import engine_names, engine_spec

# Only engines that exist in this build are selectable. Declared-but-unimplemented
# engines (e.g. claude_code, codex) are excluded until they ship.
ALLOWED_ENGINES = set(engine_names(implemented_only=True))

# Providers the Copilot harness can drive (from its registry spec).
COPILOT_SUPPORTED_PROVIDERS = set(engine_spec("copilot").provider_support or ())

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

    if normalized_engine == "copilot" and normalized_provider not in COPILOT_SUPPORTED_PROVIDERS:
        warnings.append(
            f"Provider '{provider}' may not be supported by Copilot SDK. Model was saved anyway."
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
