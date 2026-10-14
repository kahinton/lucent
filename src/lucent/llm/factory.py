"""Engine factory — resolves and returns LLM engines via the engine registry.

Routing:
  - ``get_engine()``: the configured default engine (``LUCENT_LLM_ENGINE`` /
    ``models.llm_engine``).
  - ``get_engine_for_model(model_id)``: per-model routing — explicit
    ``models.engine`` override, then the runtime registry override, then the
    provider's forced engine, else the configured default.

Engine identity, capabilities, and construction live in
``lucent.llm.engine_registry``; this module is a thin resolver over it so the
engine set is data-driven rather than hardcoded.
"""

from __future__ import annotations

from lucent.llm import engine_registry
from lucent.llm.engine import LLMEngine


def get_engine_name() -> str:
    """Get the configured default engine name."""
    # Imported lazily so ``lucent.settings`` can import the engine registry (for
    # its choice list) without a circular import through this module.
    from lucent.settings import llm_engine_name

    return llm_engine_name()


def _implemented_engine(engine_name: str | None) -> LLMEngine | None:
    """Return the engine for a name when it exists and is implemented, else None.

    Unknown or declared-but-unimplemented engine names are ignored (routing
    falls through), matching the previous behavior where unrecognized overrides
    were normalized away rather than failing a request.
    """
    if not engine_name:
        return None
    try:
        spec = engine_registry.engine_spec(engine_name)
    except engine_registry.UnknownEngineError:
        return None
    if not spec.implemented:
        return None
    return engine_registry.get_engine(engine_name)


def get_engine() -> LLMEngine:
    """Get the default engine based on LUCENT_LLM_ENGINE / models.llm_engine."""
    return engine_registry.get_engine(get_engine_name())


def get_engine_for_model(model_id: str) -> LLMEngine:
    """Route to the correct engine based on explicit override or provider."""
    try:
        from lucent.llm.langchain_engine import _resolve_model, get_registered_engine
        from lucent.model_registry import get_model

        model_info = get_model(model_id)

        # Decision models (System One: choice/noul/score via /v1/systemone)
        # are not chat models. Fail loudly rather than handing them to a chat
        # engine — discovery marks their engine "langchain" for the runtime
        # registry, but that must never route them into a chat turn, where
        # Ollama would reject the request with an opaque 400.
        if model_info and model_info.model_class == "decision":
            raise ValueError(
                f"Model '{model_id}' is a decision model, not a chat model. "
                "Use lucent.llm.decision.decide() for decision-model work."
            )

        engine = _implemented_engine(model_info.engine if model_info else None)
        if engine is not None:
            return engine

        engine = _implemented_engine(get_registered_engine(model_id))
        if engine is not None:
            return engine

        provider = model_info.provider if model_info else _resolve_model(model_id)[0]
        forced = engine_registry.engine_default_for_provider(provider)
        if forced:
            return engine_registry.get_engine(forced)
    except ImportError:
        pass

    return get_engine()


def reset_engine() -> None:
    """Reset the engine singletons (for testing or config reload)."""
    engine_registry.reset_engines()
