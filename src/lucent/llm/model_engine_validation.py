"""Validation helpers for model engine overrides."""

from __future__ import annotations

from importlib.util import find_spec

ALLOWED_ENGINES = {"copilot", "langchain"}
COPILOT_SUPPORTED_PROVIDERS = {"anthropic", "openai", "google", "ollama"}
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
    raise ValueError("Invalid engine value. Expected null, 'copilot', or 'langchain'.")


def validate_engine_override(provider: str, engine: str | None) -> list[str]:
    """Validate provider/engine combination.

    Returns warnings (non-fatal) and raises ValueError on fatal validation failures.
    """
    warnings: list[str] = []
    normalized_engine = normalize_engine(engine)
    normalized_provider = (provider or "").strip().lower()

    if normalized_engine == "copilot" and normalized_provider not in COPILOT_SUPPORTED_PROVIDERS:
        warnings.append(
            f"Provider '{provider}' may not be supported by Copilot SDK. "
            "Model was saved anyway."
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
