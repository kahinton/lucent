"""Model registry for Lucent.

Tracks available LLM models and their capabilities.
Used by the daemon, MCP tools, and API to validate model selections.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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


# ── Model Registry ────────────────────────────────────────────────────────
# Source: https://docs.github.com/en/copilot/reference/ai-models/model-comparison
# Last updated: 2026-03-12

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
        notes="Anthropic's most powerful model. Improves on Claude Opus 4.5.",
        tags=["reasoning", "analysis", "premium"],
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
    ),
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
    ),
]

# Build lookup indexes
_MODEL_BY_ID: dict[str, ModelInfo] = {m.id: m for m in MODELS}
_MODELS_BY_CATEGORY: dict[str, list[ModelInfo]] = {}
_MODELS_BY_PROVIDER: dict[str, list[ModelInfo]] = {}
for _m in MODELS:
    _MODELS_BY_CATEGORY.setdefault(_m.category, []).append(_m)
    _MODELS_BY_PROVIDER.setdefault(_m.provider, []).append(_m)

# DB-sourced model cache (populated by load_models_from_db)
_db_models: list[ModelInfo] | None = None
_db_model_by_id: dict[str, ModelInfo] = {}
_db_enabled_ids: set[str] = set()


async def load_models_from_db(pool) -> list[ModelInfo]:
    """Load models from the database, replacing the hardcoded registry.

    Call this at startup (after DB is available). Returns the loaded models.
    Falls back silently to hardcoded MODELS on any error.
    """
    global _db_models, _db_model_by_id, _db_enabled_ids, _MODEL_BY_ID
    try:
        from lucent.db.models import ModelRepository

        repo = ModelRepository(pool)
        rows = await repo.list_models()
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
            )
            loaded.append(m)
            by_id[m.id] = m
            if r.get("is_enabled", True):
                enabled_ids.add(m.id)

        _db_models = loaded
        _db_model_by_id = by_id
        _db_enabled_ids = enabled_ids
        _MODEL_BY_ID = by_id
        return loaded
    except Exception:
        return MODELS


def is_model_enabled(model_id: str) -> bool:
    """Check if a model is enabled. Returns True if DB not loaded (permissive)."""
    if _db_models is None:
        return True  # DB not loaded yet, allow all
    return model_id in _db_enabled_ids


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
    if not include_disabled and _db_models is not None:
        models = [m for m in models if m.id in _db_enabled_ids]
    if category:
        models = [m for m in models if m.category == category]
    if provider:
        models = [m for m in models if m.provider == provider]
    return models


def validate_model(model_id: str) -> str | None:
    """Validate a model selection. Returns an error message, or None if valid."""
    if _db_models is not None and model_id in _db_model_by_id:
        if model_id not in _db_enabled_ids:
            return f"Model '{model_id}' is disabled by admin. Choose an enabled model."
    return None


def get_recommended_model(task_type: str) -> str:
    """Get a recommended model for a given task type.

    This is a simple heuristic — the model-selection skill provides
    more nuanced guidance for the cognitive loop.
    """
    recommendations = {
        "code": "claude-sonnet-4.6",
        "research": "gemini-3-pro",
        "memory": "claude-haiku-4.5",
        "reflection": "claude-opus-4.6",
        "documentation": "claude-sonnet-4.6",
        "planning": "claude-opus-4.6",
        "review": "claude-sonnet-4.5",
        "fast": "claude-haiku-4.5",
        "agentic": "gpt-5.3-codex",
    }
    return recommendations.get(task_type, "claude-sonnet-4.6")


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
    return None
