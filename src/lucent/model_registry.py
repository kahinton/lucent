"""Model registry for Lucent.

Tracks available LLM models and their capabilities.
Used by the daemon, MCP tools, and API to validate model selections.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class NoModelsAvailableError(RuntimeError):
    """Raised when model selection is attempted but no enabled model exists.

    The system never invents a hardcoded default. If an administrator has not
    enabled at least one model (capable of the required features), callers must
    fail loudly rather than silently fall back to an arbitrary model that the
    provider may not even be configured for.
    """


@dataclass(frozen=True)
class ModelInfo:
    """Metadata for a single LLM model."""

    id: str  # model identifier string used by Copilot SDK / Lucent
    provider: str  # provider name (anthropic, openai, google)
    name: str  # human-readable display name
    category: str  # task category: general, fast, reasoning, agentic, visual
    api_model_id: str = ""  # provider API model ID (for direct API / LangChain)
    context_window: int = 0  # context window size in tokens (0 = unknown)
    supports_tools: bool = True  # whether the model supports tool/function calling
    supports_vision: bool = False  # whether the model supports image input
    notes: str = ""  # additional notes about the model
    tags: list[str] = field(default_factory=list)
    # Selectable provider reasoning/thinking levels reported by provider metadata.
    reasoning_efforts: list[str] = field(default_factory=list)
    engine: str | None = None  # engine override: None=auto, "copilot", "langchain"
    enabled: bool = True  # whether the model is available for use


@dataclass(frozen=True)
class ModelSelection:
    """Result of model selection for a task."""

    model_id: str
    reason: str
    source: str  # explicit, default, specialized, fallback
    default_model_id: str | None = None
    requested_category: str | None = None
    alternatives: list[str] = field(default_factory=list)


# ── Model Registry ────────────────────────────────────────────────────────
# Source: https://docs.github.com/en/copilot/reference/ai-models/model-comparison
# Last updated: 2026-04-17

MODELS: list[ModelInfo] = [
    # ── OpenAI ────────────────────────────────────────────────────────────
    ModelInfo(
        id="gpt-4.1",
        provider="openai",
        name="GPT-4.1",
        category="general",
        api_model_id="gpt-4.1",
        supports_vision=True,
        notes="General-purpose coding and writing. Fast, accurate code completions.",
        tags=["coding", "writing", "general"],
    ),
    ModelInfo(
        id="gpt-5-mini",
        provider="openai",
        name="GPT-5 mini",
        category="general",
        api_model_id="gpt-5-mini",
        supports_vision=True,
        notes="Reliable default for most coding and writing tasks.",
        tags=["coding", "writing", "general", "fast"],
    ),
    ModelInfo(
        id="gpt-5.1",
        provider="openai",
        name="GPT-5.1",
        category="reasoning",
        api_model_id="gpt-5.1",
        notes="Multi-step problem solving and architecture-level code analysis.",
        tags=["reasoning", "debugging", "architecture"],
    ),
    ModelInfo(
        id="gpt-5.1-codex",
        provider="openai",
        name="GPT-5.1-Codex",
        category="reasoning",
        api_model_id="gpt-5.1-codex",
        notes="Deep reasoning and debugging. Multi-step problem solving.",
        tags=["reasoning", "debugging", "code"],
    ),
    ModelInfo(
        id="gpt-5.1-codex-max",
        provider="openai",
        name="GPT-5.1 Codex Max",
        category="agentic",
        api_model_id="gpt-5.1-codex-max",
        notes="Agentic software development. High premium request cost.",
        tags=["agentic", "coding", "premium"],
    ),
    ModelInfo(
        id="gpt-5.1-codex-mini",
        provider="openai",
        name="GPT-5.1-Codex-Mini",
        category="reasoning",
        api_model_id="gpt-5.1-codex-mini",
        notes="Deep reasoning and debugging, smaller footprint.",
        tags=["reasoning", "debugging", "code"],
    ),
    ModelInfo(
        id="gpt-5.2",
        provider="openai",
        name="GPT-5.2",
        category="reasoning",
        api_model_id="gpt-5.2",
        notes="Deep reasoning and debugging.",
        tags=["reasoning", "debugging"],
    ),
    ModelInfo(
        id="gpt-5.2-codex",
        provider="openai",
        name="GPT-5.2-Codex",
        category="agentic",
        api_model_id="gpt-5.2-codex",
        notes="Agentic software development.",
        tags=["agentic", "coding"],
    ),
    ModelInfo(
        id="gpt-5.3-codex",
        provider="openai",
        name="GPT-5.3-Codex",
        category="agentic",
        api_model_id="gpt-5.3-codex",
        notes="Higher-quality code on complex engineering tasks.",
        tags=["agentic", "coding"],
    ),
    ModelInfo(
        id="gpt-5.4",
        provider="openai",
        name="GPT-5.4",
        category="reasoning",
        api_model_id="gpt-5.4",
        notes="Complex reasoning, code analysis, and technical decisions.",
        tags=["reasoning", "analysis"],
    ),
    # ── Anthropic ─────────────────────────────────────────────────────────
    ModelInfo(
        id="claude-haiku-4.5",
        provider="anthropic",
        name="Claude Haiku 4.5",
        category="fast",
        api_model_id="claude-haiku-4-5-20251001",
        notes="Fast, reliable answers to lightweight coding questions.",
        tags=["fast", "coding", "lightweight"],
    ),
    ModelInfo(
        id="claude-opus-4.5",
        provider="anthropic",
        name="Claude Opus 4.5",
        category="reasoning",
        api_model_id="claude-opus-4-5-20251101",
        notes="Complex problem-solving, sophisticated reasoning.",
        tags=["reasoning", "analysis", "premium"],
    ),
    ModelInfo(
        id="claude-opus-4.6",
        provider="anthropic",
        name="Claude Opus 4.6",
        category="reasoning",
        api_model_id="claude-opus-4-6-20260301",
        notes="Previous Opus generation. Kept available for comparison and pinned tasks.",
        tags=["reasoning", "analysis", "premium"],
    ),
    ModelInfo(
        id="claude-opus-4.7",
        provider="anthropic",
        name="Claude Opus 4.7",
        category="reasoning",
        api_model_id="claude-opus-4.7",
        context_window=200000,
        notes=(
            "Anthropic's latest frontier model. "
            "High-capability option for high-stakes reasoning and agentic work."
        ),
        tags=["reasoning", "frontier", "agentic", "analysis", "premium"],
    ),
    ModelInfo(
        id="claude-sonnet-4.0",
        provider="anthropic",
        name="Claude Sonnet 4.0",
        category="reasoning",
        api_model_id="claude-sonnet-4-20250514",
        supports_vision=True,
        notes="Performance and practicality, balanced for coding workflows.",
        tags=["reasoning", "coding", "balanced"],
    ),
    ModelInfo(
        id="claude-sonnet-4.5",
        provider="anthropic",
        name="Claude Sonnet 4.5",
        category="general",
        api_model_id="claude-sonnet-4-5-20250620",
        supports_vision=True,
        notes="General-purpose coding and agent tasks.",
        tags=["general", "coding", "agentic"],
    ),
    ModelInfo(
        id="claude-sonnet-4.6",
        provider="anthropic",
        name="Claude Sonnet 4.6",
        category="general",
        api_model_id="claude-sonnet-4-6-20260115",
        supports_vision=True,
        notes="Reliable completions and smarter reasoning under pressure.",
        tags=["general", "coding", "agentic", "reasoning"],
    ),
    ModelInfo(
        id="claude-opus-4.6-1m",
        provider="anthropic",
        name="Claude Opus 4.6 1M",
        category="reasoning",
        api_model_id="claude-opus-4-6-1m-20260301",
        context_window=1000000,
        notes="Opus 4.6 with 1 million token context window.",
        tags=["reasoning", "frontier", "large-context"],
    ),  # Untested in Copilot SDK — validate availability separately
    # ── Google ────────────────────────────────────────────────────────────
    ModelInfo(
        id="gemini-2.5-pro",
        provider="google",
        name="Gemini 2.5 Pro",
        category="reasoning",
        api_model_id="gemini-2.5-pro",
        notes="Complex code generation, debugging, and research workflows.",
        tags=["reasoning", "research", "coding"],
    ),
    ModelInfo(
        id="gemini-3-flash",
        provider="google",
        name="Gemini 3 Flash",
        category="fast",
        api_model_id="gemini-3-flash",
        notes="Fast, reliable answers to lightweight coding questions.",
        tags=["fast", "coding", "lightweight"],
    ),
    ModelInfo(
        id="gemini-3-pro",
        provider="google",
        name="Gemini 3 Pro",
        category="reasoning",
        api_model_id="gemini-3-pro",
        supports_vision=True,
        notes="Advanced reasoning across long contexts. Scientific and technical analysis.",
        tags=["reasoning", "research", "long-context"],
    ),
    ModelInfo(
        id="gemini-3.1-pro",
        provider="google",
        name="Gemini 3.1 Pro",
        category="reasoning",
        api_model_id="gemini-3.1-pro",
        notes="Effective edit-then-test loops with high tool precision.",
        tags=["reasoning", "agentic", "tools"],
        enabled=False,  # Copilot SDK returns "Model not available" (2026-04-18)
    ),
]

# Build lookup indexes
_MODEL_BY_ID: dict[str, ModelInfo] = {m.id: m for m in MODELS}
_MODELS_BY_CATEGORY: dict[str, list[ModelInfo]] = {}
_MODELS_BY_PROVIDER: dict[str, list[ModelInfo]] = {}
_HARDCODED_ENABLED_IDS: set[str] = set()
for _m in MODELS:
    _MODELS_BY_CATEGORY.setdefault(_m.category, []).append(_m)
    _MODELS_BY_PROVIDER.setdefault(_m.provider, []).append(_m)
    if _m.enabled:
        _HARDCODED_ENABLED_IDS.add(_m.id)

# DB-sourced model cache (populated by load_models_from_db)
_db_models: list[ModelInfo] | None = None
_db_model_by_id: dict[str, ModelInfo] = {}
_db_enabled_ids: set[str] = set()


def _sync_runtime_model_registry(models: list[ModelInfo]) -> None:
    """Sync DB-loaded models into the runtime LLM routing registry.

    The DB model registry is the source of truth for admin-managed custom
    models. Keep LangChain/Copilot routing state aligned whenever this cache is
    loaded or refreshed so chat, API, and daemon processes can resolve
    Ollama/custom models without a restart.
    """
    try:
        from lucent.llm.langchain_engine import clear_runtime_model_registry, register_model

        clear_runtime_model_registry()
        for model in models:
            if (
                model.provider == "ollama"
                or model.engine is not None
                or model.provider not in {"anthropic", "openai", "google"}
            ):
                register_model(
                    model.id,
                    model.provider,
                    model.api_model_id or model.id,
                    engine=model.engine,
                )
    except Exception:
        # Routing falls back to static maps/provider inference. Do not make
        # model listing unavailable just because optional runtime imports fail.
        return


async def load_models_from_db(pool) -> list[ModelInfo]:
    """Load models from the database, replacing the hardcoded registry.

    Call this at startup (after DB is available). Returns the loaded models.
    Falls back silently to hardcoded MODELS on any error.
    """
    global _db_models, _db_model_by_id, _db_enabled_ids, _MODEL_BY_ID
    try:
        from lucent.db.models import ModelRepository

        repo = ModelRepository(pool)
        rows = (await repo.list_models(limit=500))["items"]
        if not rows:
            return MODELS

        loaded = []
        enabled_ids = set()
        by_id = {}
        for r in rows:
            m = ModelInfo(
                id=r["id"],
                provider=r["provider"],
                name=r["name"],
                category=r["category"],
                api_model_id=r.get("api_model_id", ""),
                context_window=r.get("context_window", 0),
                supports_tools=r.get("supports_tools", True),
                supports_vision=r.get("supports_vision", False),
                notes=r.get("notes", ""),
                tags=list(r.get("tags") or []),
                reasoning_efforts=list(r.get("reasoning_efforts") or []),
                engine=r.get("engine"),
            )
            loaded.append(m)
            by_id[m.id] = m
            if r.get("is_enabled", True):
                enabled_ids.add(m.id)

        _db_models = loaded
        _db_model_by_id = by_id
        _db_enabled_ids = enabled_ids
        _MODEL_BY_ID = by_id
        _sync_runtime_model_registry(loaded)
        return loaded
    except Exception:
        return MODELS


def is_model_enabled(model_id: str) -> bool:
    """Check if a model is enabled.

    When DB models are loaded, checks the DB enabled set.
    Otherwise, checks the hardcoded model's ``enabled`` field.
    """
    if _db_models is not None:
        return model_id in _db_enabled_ids
    return model_id in _HARDCODED_ENABLED_IDS


# ── Public API ────────────────────────────────────────────────────────────


def get_model(model_id: str) -> ModelInfo | None:
    """Look up a model by its ID string."""
    return _MODEL_BY_ID.get(model_id)


def list_models(
    category: str | None = None,
    provider: str | None = None,
    include_disabled: bool = False,
) -> list[ModelInfo]:
    """List available models, optionally filtered by category or provider."""
    source = _db_models if _db_models is not None else MODELS
    models = source
    if not include_disabled:
        if _db_models is not None:
            models = [m for m in models if m.id in _db_enabled_ids]
        else:
            models = [m for m in models if m.enabled]
    if category:
        models = [m for m in models if m.category == category]
    if provider:
        models = [m for m in models if m.provider == provider]
    return models


def _available_models(models: list[ModelInfo] | None = None) -> list[ModelInfo]:
    """Return models that are available for selection."""
    if models is not None:
        return list(models)
    return list_models()


def _first_model_id(models: list[ModelInfo], *, category: str | None = None) -> str | None:
    for model in models:
        if category is None or model.category == category:
            return model.id
    return None


def get_default_model_id(
    models: list[ModelInfo] | None = None,
    *,
    preferred_model: str | None = None,
    require_tools: bool = False,
) -> str:
    """Return the default enabled model for general work.

    Defaults are intentionally selected from currently enabled models. Prefer a
    deployment/admin-selected default when it is enabled; otherwise prefer
    general-purpose models before specialized/premium categories. This keeps
    model choice opt-in and cost-conscious.
    """
    available = _available_models(models)
    if require_tools:
        available = [m for m in available if m.supports_tools]
    if not available:
        # No enabled model satisfies the request. Never invent a default — an
        # admin must explicitly enable a model. Fail loudly so the daemon crashes
        # at startup and chat/API surfaces a clear error instead of routing to an
        # arbitrary, possibly-unconfigured provider.
        raise NoModelsAvailableError(
            "No enabled models are available"
            + (" that support tool calling" if require_tools else "")
            + ". Enable at least one model in the model registry "
            "(Settings → Models) before running model-dependent work."
        )

    available_by_id = {m.id: m for m in available}
    from lucent.settings import default_model_id

    for candidate in (
        preferred_model,
        default_model_id(),
    ):
        if candidate and candidate in available_by_id:
            return candidate

    for category in ("general", "fast", "reasoning", "agentic", "research", "visual"):
        model_id = _first_model_id(available, category=category)
        if model_id:
            return model_id
    return available[0].id


def _infer_task_category(
    *,
    agent_type: str | None = None,
    title: str | None = None,
    description: str | None = None,
) -> tuple[str | None, str]:
    """Infer a specialized category only when task signals justify it."""
    agent = (agent_type or "").strip().lower()
    text = f"{title or ''} {description or ''}".lower()

    agentic_signals = (
        "multi-file",
        "repo-wide",
        "large refactor",
        "edit-test",
        "implementation across",
        "sustained",
        "agentic",
    )
    reasoning_signals = (
        "architecture",
        "security",
        "deep reasoning",
        "root cause",
        "complex",
        "trade-off",
        "strategy",
        "reflection",
        "synthesis",
        "long context",
        "cross-reference",
        "investigate",
    )
    high_risk_memory_signals = (
        "create_memory",
        "update_memory",
        "delete_memory",
        "soft-delete",
        "delete",
        "consolidat",
        "deduplicat",
        "merge",
        "migrat",
        "retire",
        "retirement",
        "transfer",
        "verify",
        "vitality",
        "forget",
        "curate",
    )

    if agent == "memory" and any(signal in text for signal in high_risk_memory_signals):
        return "reasoning", "memory task mutates or verifies durable state"

    if agent in {"fast", "memory"}:
        return "fast", "lightweight/memory work benefits from a faster cheaper model"

    if agent == "code" and any(signal in text for signal in agentic_signals):
        return "agentic", "code task has sustained multi-step execution signals"

    if agent in {"research", "planning", "reflection", "review", "request-review"}:
        if any(signal in text for signal in reasoning_signals):
            return "reasoning", "task has explicit complex reasoning signals"

    if any(signal in text for signal in reasoning_signals):
        return "reasoning", "task description has explicit complex reasoning signals"

    return None, "no clear reason to override the default model"


def select_model_for_task(
    *,
    agent_type: str | None = None,
    title: str | None = None,
    description: str | None = None,
    explicit_model: str | None = None,
    preferred_default: str | None = None,
    models: list[ModelInfo] | None = None,
    require_vision: bool = False,
    require_tools: bool = False,
) -> ModelSelection:
    """Choose an enabled model for a task using generic capabilities.

    Rule of thumb: use the default model unless task metadata contains a clear
    reason to choose a specialized category. This avoids hardcoding vendor/model
    IDs into planners while still allowing cheap fast models and high-capability
    models where they are justified.
    """
    all_available = _available_models(models)
    available = all_available
    if require_tools:
        available = [m for m in available if m.supports_tools]
    if require_vision:
        vision_models = [m for m in available if m.supports_vision]
        if vision_models:
            available = vision_models

    if explicit_model and any(m.id == explicit_model for m in available):
        default_id = get_default_model_id(
            available,
            preferred_model=preferred_default,
            require_tools=require_tools,
        )
        return ModelSelection(
            model_id=explicit_model,
            reason="explicit model was provided and is enabled",
            source="explicit",
            default_model_id=default_id,
            alternatives=[m.id for m in available if m.id != explicit_model][:5],
        )

    default_id = get_default_model_id(
        available if available else None,
        preferred_model=preferred_default,
        require_tools=require_tools,
    )
    if explicit_model and any(m.id == explicit_model for m in all_available):
        missing = []
        explicit_info = next(m for m in all_available if m.id == explicit_model)
        if require_tools and not explicit_info.supports_tools:
            missing.append("tool/function calling")
        if require_vision and not explicit_info.supports_vision:
            missing.append("vision")
        if missing:
            return ModelSelection(
                model_id=default_id,
                reason=(
                    f"Explicit model '{explicit_model}' lacks required "
                    f"capability: {', '.join(missing)}. Using default capable model."
                ),
                source="fallback",
                default_model_id=default_id,
                alternatives=[m.id for m in available if m.id != default_id][:5],
            )

    category, category_reason = _infer_task_category(
        agent_type=agent_type,
        title=title,
        description=description,
    )
    default_model = next((m for m in available if m.id == default_id), None)

    if not category or (default_model and default_model.category == category):
        return ModelSelection(
            model_id=default_id,
            reason=f"Using default model: {category_reason}.",
            source="default",
            default_model_id=default_id,
            requested_category=category,
            alternatives=[m.id for m in available if m.id != default_id][:5],
        )

    candidates = [m for m in available if m.category == category]
    if category == "reasoning" and not candidates:
        candidates = [m for m in available if "reasoning" in m.tags]
    if category == "agentic" and not candidates:
        candidates = [m for m in available if "agentic" in m.tags or "coding" in m.tags]
    if category == "fast" and not candidates:
        candidates = [m for m in available if "fast" in m.tags or "lightweight" in m.tags]

    if candidates:
        selected = candidates[0]
        return ModelSelection(
            model_id=selected.id,
            reason=f"Selected {category} model because {category_reason}.",
            source="specialized",
            default_model_id=default_id,
            requested_category=category,
            alternatives=[m.id for m in available if m.id != selected.id][:5],
        )

    return ModelSelection(
        model_id=default_id,
        reason=(
            f"Using default model because no enabled {category!r} model matched; "
            f"original signal: {category_reason}."
        ),
        source="fallback",
        default_model_id=default_id,
        requested_category=category,
        alternatives=[m.id for m in available if m.id != default_id][:5],
    )


def validate_model(model_id: str, *, require_tools: bool = False) -> str | None:
    """Validate a model selection. Returns an error message, or None if valid.

    Checks that the model ID exists in the registry (DB-loaded or hardcoded)
    and is enabled. In strict mode (default), unknown models are rejected.
    Set LUCENT_MODEL_VALIDATION=lenient to allow unrecognized model IDs.
    """
    from lucent.settings import model_validation_mode

    strict = model_validation_mode() != "lenient"

    # DB-loaded registry takes precedence when available
    if _db_models is not None:
        if model_id in _db_model_by_id:
            if model_id not in _db_enabled_ids:
                return f"Model '{model_id}' is disabled by admin. Choose an enabled model."
            if require_tools and not _db_model_by_id[model_id].supports_tools:
                return (
                    f"Model '{model_id}' does not support tool/function calling. "
                    "Daemon tasks require a tool-capable model."
                )
            return None  # Known and enabled
        # Model not found in DB registry
        if strict:
            available = sorted(_db_enabled_ids)
            return (
                f"Unknown model '{model_id}'. "
                f"Available models: {', '.join(available)}. "
                f"Use list_available_models to see all options."
            )
        return None  # Lenient: allow unknown

    # No DB loaded — check against hardcoded registry
    if model_id in _MODEL_BY_ID:
        if not _MODEL_BY_ID[model_id].enabled:
            return f"Model '{model_id}' is disabled. Choose an enabled model."
        if require_tools and not _MODEL_BY_ID[model_id].supports_tools:
            return (
                f"Model '{model_id}' does not support tool/function calling. "
                "Daemon tasks require a tool-capable model."
            )
        return None  # Known and enabled

    if strict:
        available = sorted(_HARDCODED_ENABLED_IDS)
        return (
            f"Unknown model '{model_id}'. "
            f"Available models: {', '.join(available)}. "
            f"Use list_available_models to see all options."
        )
    return None  # Lenient: allow unknown


def validate_reasoning_effort(
    model_id: str,
    reasoning_effort: str | None,
) -> str | None:
    """Validate a reasoning effort selection for a model.

    Returns an error message, or None if valid. Empty/None means use the
    provider default and is always accepted.
    """
    if not reasoning_effort:
        return None
    effort = reasoning_effort.strip().lower()

    model = get_model(model_id)
    if not model:
        # Let validate_model provide the unknown-model error elsewhere.
        return None
    allowed = list(model.reasoning_efforts or [])
    if not allowed:
        return f"Model '{model_id}' does not expose selectable reasoning effort levels."
    if effort not in allowed:
        return (
            f"Model '{model_id}' does not allow reasoning_effort '{effort}'. "
            f"Allowed values: {', '.join(allowed)}."
        )
    return None


def get_recommended_model(task_type: str) -> str:
    """Get a recommended model for a given task type.

    This returns the dynamic selector's recommendation from currently enabled
    models. It intentionally defaults to the deployment/admin default unless
    the task type clearly maps to a specialized capability.
    """
    return select_model_for_task(agent_type=task_type).model_id


def get_api_model_id(model_id: str) -> str:
    """Get the provider API model ID for a given Lucent model ID.

    When using LangChain/direct API access, models may need different
    identifiers than when using the Copilot SDK. Returns the api_model_id
    if set, otherwise falls back to the Lucent model ID.
    """
    model = _MODEL_BY_ID.get(model_id)
    if model and model.api_model_id:
        return model.api_model_id
    return model_id


def get_provider(model_id: str) -> str | None:
    """Get the provider name for a model ID.

    Returns 'anthropic', 'openai', 'google', or None if unknown.
    """
    model = _MODEL_BY_ID.get(model_id)
    if model:
        return model.provider
    # Infer from name
    if model_id.startswith("claude"):
        return "anthropic"
    elif model_id.startswith("gpt") or model_id.startswith("o1") or model_id.startswith("o3"):
        return "openai"
    elif model_id.startswith("gemini"):
        return "google"
    elif model_id.startswith("grok"):
        return "xai"
    elif model_id in {"goldeneye", "raptor-mini"}:
        return "copilot"
    return None
