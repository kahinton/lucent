"""Data-driven registry of LLM engines.

The registry is the single source of truth for *what an engine is*: its kind,
its capabilities, its config schema, how to construct it, and whether it is
available in this runtime. Every other module derives engine identity from here
instead of hardcoding ``"copilot"`` / ``"langchain"`` strings.

Two engine **kinds**:

* ``llm_access`` — the general LLM layer (``langchain``): provider-agnostic chat
  where Lucent owns the tool loop.
* ``coding_agent`` — product-specific agent harnesses (``copilot`` today;
  ``claude_code`` and ``codex`` declared but not yet implemented) that bring
  their own loop, tools, and auth.

Adding a coding engine later is a single ``EngineSpec`` plus its module —
nothing else in the codebase needs to learn a new engine name.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

if TYPE_CHECKING:  # avoid import cycles at runtime
    from lucent.llm.engine import LLMEngine
    from lucent.llm.engine_credentials import EngineConfigField

EngineKind = Literal["llm_access", "coding_agent"]


class UnknownEngineError(ValueError):
    """Raised when an engine name is not present in the registry."""


@dataclass(frozen=True, slots=True)
class EngineAvailability:
    """Whether an engine can run in this runtime, and why not."""

    available: bool
    detail: str = ""
    missing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EngineCapabilities:
    """What an engine can do — read instead of comparing engine-name strings."""

    native_agent_loop: bool = False  # engine runs its own tool loop
    mcp: bool = False  # accepts an MCP server config
    resume: bool = False  # provider-native session resume
    attachments: bool = False  # multimodal input
    hooks: bool = False  # consumes Lucent declarative hooks (llm/hooks.py)
    managed_tools: bool = False  # supports direct managed-tool binding
    expected_surface: bool = False  # Layer-2 composition check (llm/engine.py)
    reasoning_effort: bool = False
    injection: frozenset[str] = frozenset()  # subset of {direct, byok, gateway, oss}
    discovery: bool = False  # can enumerate its own catalog


@dataclass(frozen=True)
class EngineSpec:
    """Descriptor for one engine."""

    name: str
    display_name: str
    kind: EngineKind
    capabilities: EngineCapabilities
    implemented: bool = True
    factory: Callable[[], "LLMEngine"] | None = None
    config_schema: tuple["EngineConfigField", ...] = ()
    provider_support: frozenset[str] | None = None  # None = any provider
    default_for_providers: frozenset[str] = frozenset()
    availability: Callable[[], EngineAvailability] | None = None
    discover: Callable[..., Awaitable[Any]] | None = None
    notes: str = ""


# ── Availability probes ────────────────────────────────────────────────────


def _module_present(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _langchain_availability() -> EngineAvailability:
    if _module_present("langchain"):
        return EngineAvailability(True, "langchain installed")
    return EngineAvailability(
        False,
        "langchain is not installed",
        missing=("langchain",),
    )


def _copilot_availability() -> EngineAvailability:
    # The Copilot SDK ships a bundled CLI, so the SDK package alone is enough.
    if _module_present("copilot"):
        return EngineAvailability(True, "github-copilot-sdk installed")
    return EngineAvailability(
        False,
        "github-copilot-sdk is not installed",
        missing=("github-copilot-sdk",),
    )


def _claude_code_availability() -> EngineAvailability:
    # The Claude Agent SDK bundles the Claude Code CLI, so the SDK alone is enough
    # (the CLI need not be installed separately).
    if _module_present("claude_agent_sdk"):
        return EngineAvailability(True, "claude-agent-sdk installed")
    return EngineAvailability(
        False,
        "claude-agent-sdk is not installed",
        missing=("claude-agent-sdk",),
    )


def _codex_availability() -> EngineAvailability:
    # The openai-codex SDK pulls in the Codex CLI runtime as a dependency, so the
    # SDK package is the only requirement.
    if _module_present("openai_codex"):
        return EngineAvailability(True, "openai-codex installed")
    return EngineAvailability(
        False,
        "openai-codex is not installed",
        missing=("openai-codex",),
    )


# ── Factories (lazy imports; credentials resolved per call by the engine) ───


def _make_langchain() -> "LLMEngine":
    from lucent.llm.langchain_engine import LangChainEngine

    return LangChainEngine()


def _make_copilot() -> "LLMEngine":
    from lucent.llm.copilot_engine import CopilotEngine
    from lucent.settings import github_token

    return CopilotEngine(github_token=github_token() or None)


def _make_claude_code() -> "LLMEngine":
    from lucent.llm.claude_code_engine import ClaudeCodeEngine

    return ClaudeCodeEngine()


def _make_codex() -> "LLMEngine":
    from lucent.llm.codex_engine import CodexEngine

    return CodexEngine()


# ── Capability sets ────────────────────────────────────────────────────────

_LANGCHAIN_CAPS = EngineCapabilities(
    native_agent_loop=False,
    mcp=True,
    resume=False,  # Lucent replays message_history instead of provider resume
    attachments=True,
    hooks=True,
    managed_tools=True,
    expected_surface=True,
    reasoning_effort=True,
    injection=frozenset({"direct"}),
    discovery=True,
)

_COPILOT_CAPS = EngineCapabilities(
    native_agent_loop=True,
    mcp=True,
    resume=True,
    attachments=True,
    hooks=False,  # Copilot accepts a hooks config but does not execute it
    managed_tools=False,
    expected_surface=False,
    reasoning_effort=True,
    injection=frozenset({"byok"}),
    discovery=True,
)

_CLAUDE_CODE_CAPS = EngineCapabilities(
    native_agent_loop=True,
    mcp=True,
    resume=True,
    attachments=True,
    hooks=False,
    managed_tools=False,
    expected_surface=False,
    reasoning_effort=False,
    injection=frozenset({"gateway"}),
    discovery=True,
)

_CODEX_CAPS = EngineCapabilities(
    native_agent_loop=True,
    mcp=True,
    resume=True,
    attachments=True,
    hooks=False,
    managed_tools=False,
    expected_surface=False,
    reasoning_effort=True,
    injection=frozenset({"oss", "gateway"}),
    discovery=False,
)


# ── Built-in specs ─────────────────────────────────────────────────────────
# Order here defines the order returned by engine_names() and rendered in the UI.

_BUILTIN_SPECS: tuple[EngineSpec, ...] = (
    EngineSpec(
        name="langchain",
        display_name="LangChain (general LLM)",
        kind="llm_access",
        capabilities=_LANGCHAIN_CAPS,
        factory=_make_langchain,
        provider_support=None,
        default_for_providers=frozenset({"ollama"}),
        availability=_langchain_availability,
        notes=(
            "General text/reasoning access for Ollama, OpenAI, Anthropic, Google, "
            "and custom endpoints."
        ),
    ),
    EngineSpec(
        name="copilot",
        display_name="GitHub Copilot",
        kind="coding_agent",
        capabilities=_COPILOT_CAPS,
        factory=_make_copilot,
        provider_support=frozenset({"anthropic", "copilot", "google", "ollama", "openai", "xai"}),
        availability=_copilot_availability,
        notes="GitHub Copilot SDK harness (its own agent loop and tools).",
    ),
    EngineSpec(
        name="claude_code",
        display_name="Claude Code",
        kind="coding_agent",
        capabilities=_CLAUDE_CODE_CAPS,
        factory=_make_claude_code,
        provider_support=frozenset({"anthropic", "custom", "ollama"}),
        availability=_claude_code_availability,
        notes="Claude Agent SDK harness; native Anthropic models or a local provider.",
    ),
    EngineSpec(
        name="codex",
        display_name="OpenAI Codex",
        kind="coding_agent",
        capabilities=_CODEX_CAPS,
        factory=_make_codex,
        provider_support=frozenset({"openai", "custom", "ollama"}),
        availability=_codex_availability,
        notes="OpenAI Codex SDK harness; native OpenAI models or a local provider.",
    ),
)


# ── Registry state ─────────────────────────────────────────────────────────

ENGINE_REGISTRY: dict[str, EngineSpec] = {}
_ENGINE_SINGLETONS: dict[str, "LLMEngine"] = {}


def register_engine(spec: EngineSpec) -> None:
    """Register (or replace) an engine spec, attaching its config schema.

    If the spec does not carry a schema, the per-engine schema table is used so
    specs stay terse and the schemas live in one place.
    """
    if not spec.config_schema:
        from lucent.llm.engine_credentials import engine_config_schema

        spec = replace(spec, config_schema=engine_config_schema(spec.name))
    ENGINE_REGISTRY[spec.name] = spec


for _spec in _BUILTIN_SPECS:
    register_engine(_spec)


# ── Public API ─────────────────────────────────────────────────────────────


def engine_spec(name: str) -> EngineSpec:
    """Return the spec for an engine name, or raise UnknownEngineError."""
    spec = ENGINE_REGISTRY.get(name)
    if spec is None:
        raise UnknownEngineError(
            f"Unknown LLM engine: {name!r}. Known engines: {sorted(ENGINE_REGISTRY)}."
        )
    return spec


def engine_kind(name: str) -> EngineKind:
    """Return the kind of an engine."""
    return engine_spec(name).kind


def capabilities_for(engine_or_name: Any) -> EngineCapabilities:
    """Return capabilities for an engine instance or engine name.

    Call sites branch on these flags instead of comparing engine-name strings.
    Unknown engines yield an all-False set so a stray name never crashes a
    request.
    """
    name = (
        engine_or_name if isinstance(engine_or_name, str) else getattr(engine_or_name, "name", "")
    )
    try:
        return engine_spec(str(name)).capabilities
    except UnknownEngineError:
        return EngineCapabilities()


def engine_names(
    *,
    kind: EngineKind | None = None,
    implemented_only: bool = True,
) -> list[str]:
    """List engine names in registration order, optionally filtered."""
    names = []
    for name, spec in ENGINE_REGISTRY.items():
        if implemented_only and not spec.implemented:
            continue
        if kind is not None and spec.kind != kind:
            continue
        names.append(name)
    return names


def engines_for_kind(kind: EngineKind) -> list[str]:
    """List implemented engine names of a given kind."""
    return engine_names(kind=kind)


def get_engine(name: str) -> "LLMEngine":
    """Return the per-name singleton engine, constructing it on first use.

    Engines are stateless with respect to credentials — they resolve provider
    credentials per call from the request organization — so a single instance
    per engine is safe. Unknown or unimplemented engines raise.
    """
    spec = engine_spec(name)
    if not spec.implemented or spec.factory is None:
        raise UnknownEngineError(f"LLM engine {name!r} is declared but not implemented yet.")
    engine = _ENGINE_SINGLETONS.get(name)
    if engine is None:
        engine = spec.factory()
        _ENGINE_SINGLETONS[name] = engine
    return engine


def engine_availability(name: str) -> EngineAvailability:
    """Probe whether an engine can run in this runtime."""
    spec = engine_spec(name)
    if spec.availability is None:
        return EngineAvailability(spec.implemented)
    return spec.availability()


def engine_default_for_provider(provider: str) -> str | None:
    """Return the engine that a provider is forced onto, if any.

    Replaces the old ``_LANGCHAIN_PROVIDERS`` map: ``ollama`` is always served by
    the general LLM engine; cloud providers fall through to the configured
    default engine.
    """
    for name, spec in ENGINE_REGISTRY.items():
        if spec.implemented and provider in spec.default_for_providers:
            return name
    return None


def reset_engines() -> None:
    """Drop cached engine singletons (for testing or config reload)."""
    _ENGINE_SINGLETONS.clear()
