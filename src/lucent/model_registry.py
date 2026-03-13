"""Model registry for Lucent.

Tracks available LLM models and their capabilities.
Used by the daemon, MCP tools, and API to validate model selections.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelInfo:
    """Metadata for a single LLM model."""

    id: str  # model identifier string used by the API
    provider: str  # provider name (anthropic, openai, google, alibaba, etc.)
    name: str  # human-readable display name
    category: str  # task category: general, fast, reasoning, agentic, visual
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
        supports_vision=True,
        notes="General-purpose coding and writing. Fast, accurate code completions.",
        tags=["coding", "writing", "general"],
    ),
    ModelInfo(
        id="gpt-5-mini",
        provider="openai",
        name="GPT-5 mini",
        category="general",
        supports_vision=True,
        notes="Reliable default for most coding and writing tasks.",
        tags=["coding", "writing", "general", "fast"],
    ),
    ModelInfo(
        id="gpt-5.1",
        provider="openai",
        name="GPT-5.1",
        category="reasoning",
        notes="Multi-step problem solving and architecture-level code analysis.",
        tags=["reasoning", "debugging", "architecture"],
    ),
    ModelInfo(
        id="gpt-5.1-codex",
        provider="openai",
        name="GPT-5.1-Codex",
        category="reasoning",
        notes="Deep reasoning and debugging. Multi-step problem solving.",
        tags=["reasoning", "debugging", "code"],
    ),
    ModelInfo(
        id="gpt-5.1-codex-max",
        provider="openai",
        name="GPT-5.1 Codex Max",
        category="agentic",
        notes="Agentic software development. High premium request cost.",
        tags=["agentic", "coding", "premium"],
    ),
    ModelInfo(
        id="gpt-5.1-codex-mini",
        provider="openai",
        name="GPT-5.1-Codex-Mini",
        category="reasoning",
        notes="Deep reasoning and debugging, smaller footprint.",
        tags=["reasoning", "debugging", "code"],
    ),
    ModelInfo(
        id="gpt-5.2",
        provider="openai",
        name="GPT-5.2",
        category="reasoning",
        notes="Deep reasoning and debugging.",
        tags=["reasoning", "debugging"],
    ),
    ModelInfo(
        id="gpt-5.2-codex",
        provider="openai",
        name="GPT-5.2-Codex",
        category="agentic",
        notes="Agentic software development.",
        tags=["agentic", "coding"],
    ),
    ModelInfo(
        id="gpt-5.3-codex",
        provider="openai",
        name="GPT-5.3-Codex",
        category="agentic",
        notes="Higher-quality code on complex engineering tasks.",
        tags=["agentic", "coding"],
    ),
    ModelInfo(
        id="gpt-5.4",
        provider="openai",
        name="GPT-5.4",
        category="reasoning",
        notes="Complex reasoning, code analysis, and technical decisions.",
        tags=["reasoning", "analysis"],
    ),
    # ── Anthropic ─────────────────────────────────────────────────────────
    ModelInfo(
        id="claude-haiku-4.5",
        provider="anthropic",
        name="Claude Haiku 4.5",
        category="fast",
        notes="Fast, reliable answers to lightweight coding questions.",
        tags=["fast", "coding", "lightweight"],
    ),
    ModelInfo(
        id="claude-opus-4.5",
        provider="anthropic",
        name="Claude Opus 4.5",
        category="reasoning",
        notes="Complex problem-solving, sophisticated reasoning.",
        tags=["reasoning", "analysis", "premium"],
    ),
    ModelInfo(
        id="claude-opus-4.6",
        provider="anthropic",
        name="Claude Opus 4.6",
        category="reasoning",
        notes="Anthropic's most powerful model. Improves on Claude Opus 4.5.",
        tags=["reasoning", "analysis", "premium"],
    ),
    ModelInfo(
        id="claude-sonnet-4.0",
        provider="anthropic",
        name="Claude Sonnet 4.0",
        category="reasoning",
        supports_vision=True,
        notes="Performance and practicality, balanced for coding workflows.",
        tags=["reasoning", "coding", "balanced"],
    ),
    ModelInfo(
        id="claude-sonnet-4.5",
        provider="anthropic",
        name="Claude Sonnet 4.5",
        category="general",
        supports_vision=True,
        notes="General-purpose coding and agent tasks.",
        tags=["general", "coding", "agentic"],
    ),
    ModelInfo(
        id="claude-sonnet-4.6",
        provider="anthropic",
        name="Claude Sonnet 4.6",
        category="general",
        supports_vision=True,
        notes="Reliable completions and smarter reasoning under pressure.",
        tags=["general", "coding", "agentic", "reasoning"],
    ),
    ModelInfo(
        id="claude-opus-4.6-fast",
        provider="anthropic",
        name="Claude Opus 4.6 (fast mode)",
        category="reasoning",
        notes="Preview. Opus-level reasoning with lower latency.",
        tags=["reasoning", "fast", "preview"],
    ),
    # ── Google ────────────────────────────────────────────────────────────
    ModelInfo(
        id="gemini-2.5-pro",
        provider="google",
        name="Gemini 2.5 Pro",
        category="reasoning",
        notes="Complex code generation, debugging, and research workflows.",
        tags=["reasoning", "research", "coding"],
    ),
    ModelInfo(
        id="gemini-3-flash",
        provider="google",
        name="Gemini 3 Flash",
        category="fast",
        notes="Fast, reliable answers to lightweight coding questions.",
        tags=["fast", "coding", "lightweight"],
    ),
    ModelInfo(
        id="gemini-3-pro",
        provider="google",
        name="Gemini 3 Pro",
        category="reasoning",
        supports_vision=True,
        notes="Advanced reasoning across long contexts. Scientific and technical analysis.",
        tags=["reasoning", "research", "long-context"],
    ),
    ModelInfo(
        id="gemini-3.1-pro",
        provider="google",
        name="Gemini 3.1 Pro",
        category="reasoning",
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


# ── Public API ────────────────────────────────────────────────────────────


def get_model(model_id: str) -> ModelInfo | None:
    """Look up a model by its ID string."""
    return _MODEL_BY_ID.get(model_id)


def list_models(
    category: str | None = None,
    provider: str | None = None,
) -> list[ModelInfo]:
    """List available models, optionally filtered by category or provider."""
    models = MODELS
    if category:
        models = [m for m in models if m.category == category]
    if provider:
        models = [m for m in models if m.provider == provider]
    return models


def validate_model(model_id: str) -> str | None:
    """Validate a model selection. Returns an error message, or None if valid."""
    # We don't reject unknown model IDs — new models may be available
    # before the registry is updated. The API will reject truly invalid ones.
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
