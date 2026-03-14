"""Engine factory — selects and instantiates the configured LLM engine.

Uses LUCENT_LLM_ENGINE env var to choose the backend:
  - "copilot" (default): GitHub Copilot SDK
  - "langchain": LangChain with direct provider APIs
"""

from __future__ import annotations

import os

from lucent.llm.engine import LLMEngine

_engine: LLMEngine | None = None


def get_engine_name() -> str:
    """Get the configured engine name."""
    return os.environ.get("LUCENT_LLM_ENGINE", "copilot").lower()


def get_engine() -> LLMEngine:
    """Get or create the singleton LLM engine based on configuration.

    The engine is selected by the LUCENT_LLM_ENGINE env var.
    This is a lazy singleton — the engine is created on first call.
    """
    global _engine
    if _engine is not None:
        return _engine

    engine_name = get_engine_name()

    if engine_name == "copilot":
        from lucent.llm.copilot_engine import CopilotEngine

        github_token = os.environ.get("GITHUB_TOKEN", "")
        _engine = CopilotEngine(
            github_token=github_token or None,
        )

    elif engine_name == "langchain":
        from lucent.llm.langchain_engine import LangChainEngine

        _engine = LangChainEngine()

    else:
        raise ValueError(
            f"Unknown LLM engine: {engine_name!r}. "
            "Expected 'copilot' or 'langchain'. "
            "Set LUCENT_LLM_ENGINE env var."
        )

    return _engine


def reset_engine() -> None:
    """Reset the engine singleton (for testing)."""
    global _engine
    _engine = None
