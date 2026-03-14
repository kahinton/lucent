"""LLM engine abstraction layer.

Allows Lucent to use different LLM backends (GitHub Copilot SDK, LangChain)
via a common interface. The backend is selected via LUCENT_LLM_ENGINE env var.

Usage:
    from lucent.llm import get_engine

    engine = get_engine()
    result = await engine.run_session(
        model="claude-opus-4.6",
        system_message="You are a helpful assistant.",
        prompt="Hello!",
    )
"""

from lucent.llm.engine import LLMEngine, SessionEvent, SessionEventType
from lucent.llm.factory import get_engine, get_engine_name

__all__ = [
    "LLMEngine",
    "SessionEvent",
    "SessionEventType",
    "get_engine",
    "get_engine_name",
]
