"""Engine factory — selects and instantiates LLM engines.

Supports multi-engine routing: cloud providers use the Copilot SDK engine,
local/custom providers (Ollama, etc.) use the LangChain engine.

  - get_engine(): Returns the default engine (based on LUCENT_LLM_ENGINE)
  - get_engine_for_model(model_id): Routes to the correct engine per-model
"""

from __future__ import annotations

import os

from lucent.llm.engine import LLMEngine

_engine: LLMEngine | None = None
_langchain_engine: LLMEngine | None = None
_copilot_engine: LLMEngine | None = None

# Providers that require the LangChain engine (not supported by Copilot SDK)
_LANGCHAIN_PROVIDERS = {"ollama"}


def get_engine_name() -> str:
    """Get the configured default engine name."""
    return os.environ.get("LUCENT_LLM_ENGINE", "copilot").lower()


def _get_copilot_engine() -> LLMEngine:
    """Get or create the Copilot SDK engine singleton."""
    global _copilot_engine
    if _copilot_engine is None:
        from lucent.llm.copilot_engine import CopilotEngine

        github_token = os.environ.get("GITHUB_TOKEN", "")
        _copilot_engine = CopilotEngine(github_token=github_token or None)
    return _copilot_engine


def _get_langchain_engine() -> LLMEngine:
    """Get or create the LangChain engine singleton."""
    global _langchain_engine
    if _langchain_engine is None:
        from lucent.llm.langchain_engine import LangChainEngine

        _langchain_engine = LangChainEngine()
    return _langchain_engine


def get_engine() -> LLMEngine:
    """Get the default engine based on LUCENT_LLM_ENGINE env var."""
    global _engine
    if _engine is not None:
        return _engine

    engine_name = get_engine_name()

    if engine_name == "copilot":
        _engine = _get_copilot_engine()
    elif engine_name == "langchain":
        _engine = _get_langchain_engine()
    else:
        raise ValueError(
            f"Unknown LLM engine: {engine_name!r}. "
            "Expected 'copilot' or 'langchain'. "
            "Set LUCENT_LLM_ENGINE env var."
        )

    return _engine


def get_engine_for_model(model_id: str) -> LLMEngine:
    """Route to the correct engine based on the model's provider.

    Cloud models (Anthropic, OpenAI, Google) use the default engine.
    Local/custom models (Ollama) use the LangChain engine.
    """
    try:
        from lucent.llm.langchain_engine import _resolve_model

        provider, _ = _resolve_model(model_id)
        if provider in _LANGCHAIN_PROVIDERS:
            return _get_langchain_engine()
    except ImportError:
        pass

    return get_engine()


def reset_engine() -> None:
    """Reset the engine singleton (for testing)."""
    global _engine
    _engine = None
