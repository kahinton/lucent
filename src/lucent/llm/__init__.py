"""LLM engine abstraction layer.

Allows Lucent to use different LLM backends (GitHub Copilot SDK, LangChain)
via a common interface. The backend is selected via LUCENT_LLM_ENGINE env var.

Usage:
    from lucent.llm import get_engine

    engine = get_engine()
    result = await engine.run_session(
        model="your-enabled-model-id",
        system_message="You are a helpful assistant.",
        prompt="Hello!",
    )
"""

from lucent.llm.decision import (
    DecisionError,
    DecisionQuestion,
    DecisionRequest,
    DecisionResponse,
    decide,
)
from lucent.llm.engine import LLMEngine, ModelNotAvailableError, SessionEvent, SessionEventType
from lucent.llm.engine_registry import (
    EngineCapabilities,
    EngineKind,
    EngineSpec,
    UnknownEngineError,
    engine_availability,
    engine_kind,
    engine_names,
    engine_spec,
)
from lucent.llm.engine_registry import (
    get_engine as get_registered_engine,
)
from lucent.llm.factory import get_engine, get_engine_for_model, get_engine_name

__all__ = [
    "LLMEngine",
    "ModelNotAvailableError",
    "SessionEvent",
    "SessionEventType",
    "EngineCapabilities",
    "EngineKind",
    "EngineSpec",
    "UnknownEngineError",
    "engine_availability",
    "engine_kind",
    "engine_names",
    "engine_spec",
    "get_registered_engine",
    "get_engine",
    "get_engine_for_model",
    "get_engine_name",
    "DecisionError",
    "DecisionRequest",
    "DecisionQuestion",
    "DecisionResponse",
    "decide",
]
