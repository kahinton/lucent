"""LangChain engine implementation.

Uses LangChain's init_chat_model() for provider-agnostic LLM access,
with MCPToolBridge for MCP tool integration.

All major provider packages (langchain-anthropic, langchain-openai,
langchain-google-genai) are included in the base Lucent install.
For local models via Ollama, install: pip install lucent[ollama]
"""

from __future__ import annotations

import asyncio
import json
import re
from itertools import count
from typing import Any, Callable

from lucent import settings as runtime_settings
from lucent.llm.engine import (
    LLMEngine,
    SessionEvent,
    SessionEventType,
    validate_expected_surface,
)
from lucent.llm.hooks import HookManager, append_hook_context
from lucent.llm.project_context import (
    emit_project_context_event,
    render_project_context_block,
)
from lucent.llm.token_usage import normalize_token_usage
from lucent.logging import get_logger

logger = get_logger("llm.langchain")


class _StreamIdleTimeout(Exception):
    pass


class _ToolRoundLimitExceeded(RuntimeError):
    pass


_CONTEXT_OVERFLOW_PATTERN = re.compile(
    r"exceed_context_size|context (?:size|length)|maximum context length|"
    r"too many tokens|reduce the length of the messages|context_length_exceeded",
    re.IGNORECASE,
)


def _describe_provider_error(model: str, error: BaseException) -> str:
    """Turn a terse provider failure into something a person can act on.

    Self-hosted servers report context overflow as a bare 400 with the server's
    own numbers ("request (34994 tokens) exceeds the available context size
    (16384 tokens)"). That is the single most common failure when someone points
    Lucent at a llama.cpp/vLLM server with a default context window, and the
    useful action — raise the server's context or pick a bigger model — is not
    obvious from the raw message.
    """
    message = str(error) or type(error).__name__
    if _CONTEXT_OVERFLOW_PATTERN.search(message):
        return (
            f"The model '{model}' rejected the request because it exceeds the "
            "server's context window. Lucent's agent prompt and tool set are more "
            "than a small server default can hold — start the model server with a "
            "larger context (llama.cpp: -c 32768 or more) or choose a model with a "
            f"bigger window. Server said: {message}"
        )
    return message


# Provider mapping: our model IDs → LangChain provider + provider model ID
# The Copilot SDK uses short names; direct API providers need different IDs.
PROVIDER_MODEL_MAP: dict[str, tuple[str, str]] = {
    # Anthropic — use dated model IDs for the API
    "claude-haiku-4.5": ("anthropic", "claude-haiku-4-5-20251001"),
    "claude-opus-4.5": ("anthropic", "claude-opus-4-5-20251101"),
    "claude-opus-4.6": ("anthropic", "claude-opus-4-6-20260301"),
    "claude-opus-4.7": ("anthropic", "claude-opus-4.7"),
    "claude-sonnet-4.0": ("anthropic", "claude-sonnet-4-20250514"),
    "claude-sonnet-4.5": ("anthropic", "claude-sonnet-4-5-20250620"),
    "claude-sonnet-4.6": ("anthropic", "claude-sonnet-4-6-20260115"),
    "claude-opus-4.6-1m": ("anthropic", "claude-opus-4-6-1m-20260301"),
    # OpenAI
    "gpt-4.1": ("openai", "gpt-4.1"),
    "gpt-5-mini": ("openai", "gpt-5-mini"),
    "gpt-5.1": ("openai", "gpt-5.1"),
    "gpt-5.1-codex": ("openai", "gpt-5.1-codex"),
    "gpt-5.1-codex-max": ("openai", "gpt-5.1-codex-max"),
    "gpt-5.1-codex-mini": ("openai", "gpt-5.1-codex-mini"),
    "gpt-5.2": ("openai", "gpt-5.2"),
    "gpt-5.2-codex": ("openai", "gpt-5.2-codex"),
    "gpt-5.3-codex": ("openai", "gpt-5.3-codex"),
    "gpt-5.4": ("openai", "gpt-5.4"),
    "gpt-5.4-mini": ("openai", "gpt-5.4-mini"),
    "gpt-5.4-nano": ("openai", "gpt-5.4-nano"),
    "gpt-5.5": ("openai", "gpt-5.5"),
    # Google
    "gemini-2.5-pro": ("google_genai", "gemini-2.5-pro"),
    "gemini-3-flash": ("google_genai", "gemini-3-flash"),
    "gemini-3-pro": ("google_genai", "gemini-3-pro"),
    "gemini-3.1-pro": ("google_genai", "gemini-3.1-pro"),
}

# Runtime registry for models not in the static map (e.g. Ollama models).
# Populated from the DB at daemon startup via register_model().
_runtime_model_registry: dict[str, tuple[str, str, str | None]] = {}


def _normalize_engine(engine: str | None) -> str | None:
    """Normalize explicit engine override; None means auto-detect."""
    if engine is None:
        return None
    normalized = engine.strip().lower()
    if normalized in ("", "auto"):
        return None
    if normalized in ("copilot", "langchain"):
        return normalized
    return None


def register_model(
    model_id: str,
    provider: str,
    api_model_id: str = "",
    engine: str | None = None,
) -> None:
    """Register a model at runtime for provider resolution."""
    _runtime_model_registry[model_id] = (
        provider,
        api_model_id or model_id,
        _normalize_engine(engine),
    )


def clear_runtime_model_registry() -> None:
    """Clear DB/runtime model registrations.

    Static provider mappings remain available. This is used when the admin
    model registry is reloaded so deleted/renamed DB models do not linger in
    process-local routing state.
    """
    _runtime_model_registry.clear()


def get_registered_engine(model_id: str) -> str | None:
    """Get explicit runtime engine override for model, if present."""
    entry = _runtime_model_registry.get(model_id)
    if not entry:
        return None
    return entry[2]


def _resolve_model(model_id: str) -> tuple[str, str]:
    """Resolve a Lucent model ID to (provider, provider_model_id).

    The provider is the integration id ``init_chat_model()`` is called with, or
    the Lucent provider name stored on the DB row. Catalog lookups
    (:mod:`lucent.llm.langchain_providers`) canonicalize as needed, so
    ``google_genai`` (the integration) and ``google`` (the Lucent provider) are
    both accepted.
    """
    if model_id in PROVIDER_MODEL_MAP:
        return PROVIDER_MODEL_MAP[model_id]

    # Check runtime registry (DB-populated Ollama/custom/any provider)
    if model_id in _runtime_model_registry:
        provider, api_model_id, _engine = _runtime_model_registry[model_id]
        return provider, api_model_id

    # Infer provider from model name prefix
    if model_id.startswith("claude"):
        return "anthropic", model_id
    elif model_id.startswith("gpt") or model_id.startswith("o1") or model_id.startswith("o3"):
        return "openai", model_id
    elif model_id.startswith("gemini"):
        return "google_genai", model_id

    # Provider comes from the DB row (see register_model); nothing to infer.
    return "", model_id


def _schema_tool_name(schema: dict[str, Any]) -> str | None:
    """Extract a tool name from an OpenAI/LangChain function schema."""
    if not isinstance(schema, dict):
        return None
    function = schema.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        return str(name) if name else None
    name = schema.get("name")
    return str(name) if name else None


def _compose_surface_report(
    tool_to_bridge: dict[str, Any],
    managed_tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Build a report of the actually-composed tool surface for a session.

    The surface is what the model will really see: bridge-discovered MCP
    tools (``tool_to_bridge`` encodes the real bridge routing, including
    duplicate-name skips) plus managed MCP-proxy tools whose cached
    discovered_tools are carried by the memory-server carrier. Built-ins
    are excluded — static engine capabilities, not composed per-session.

    Mirrors the registration logic in ``_run_with_tools``: a proxy
    descriptor whose name is already bridge-routed is skipped there, so it
    is excluded from ``managed_proxies`` here too.
    """
    proxy_names: set[str] = set()
    for managed_tool in managed_tools or []:
        if not (
            isinstance(managed_tool, dict)
            and (managed_tool.get("runtime_config") or {}).get("mcp_proxy")
        ):
            continue
        for descriptor in managed_tool.get("discovered_tools") or []:
            name = descriptor.get("name") if isinstance(descriptor, dict) else None
            if name and name not in tool_to_bridge:
                proxy_names.add(str(name))
    bridge_names = sorted(tool_to_bridge)
    return {
        "tool_count": len(bridge_names),
        "tools": bridge_names,
        "carrier_present": "run_managed_tool" in tool_to_bridge,
        "managed_proxies": sorted(proxy_names),
    }


def _messages_for_hooks(messages: list[Any]) -> list[dict[str, Any]]:
    """Serialize LangChain messages into a hook-friendly JSON shape."""
    out: list[dict[str, Any]] = []
    for message in messages:
        class_name = message.__class__.__name__.removesuffix("Message").lower()
        role = {
            "system": "system",
            "human": "user",
            "ai": "assistant",
            "tool": "tool",
        }.get(class_name, class_name or "message")
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            try:
                content = json.dumps(content, default=str)
            except TypeError:
                content = str(content)
        item: dict[str, Any] = {"role": role, "content": content}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            item["tool_calls"] = tool_calls
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id:
            item["tool_call_id"] = tool_call_id
        out.append(item)
    return out


def _stream_chunk_text(content: Any) -> str:
    """Extract displayable text from one streamed chunk's content.

    Providers differ: older Ollama/OpenAI shims stream plain strings, while
    modern block-based providers (Anthropic, newer Ollama, most "cloud" models)
    stream a LIST of typed content blocks — ``{"type": "text", "text": "..."}``
    interleaved with reasoning/metadata blocks. The previous string-only check
    silently dropped every list-shaped chunk, which meant no progress events
    were ever emitted for those models even though the final answer assembled
    fine. Extracting text blocks (and ignoring everything else) makes streaming
    work for both shapes.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                # Reasoning/thinking blocks are deliberately skipped: they are
                # not the answer, and mixing them into a progress pane would
                # misrepresent what the task is producing.
                if block.get("type") in (None, "text") and isinstance(
                    block.get("text"), str
                ):
                    parts.append(block["text"])
        return "".join(parts)
    return ""


def _current_user_text_from_messages(messages: list[Any]) -> str | None:
    """Return the raw text of the LAST human message, for message-phase hooks.

    The hook loop appends this turn's user message (plain text or the
    attachment-block form) as the final entry of the history list, so the last
    human message is always the CURRENT turn's text — the same discriminator
    ``_is_first_model_call_after_user_message`` uses for the round-1 guard.
    Returns ``None`` when no human message exists (the hook then falls back to
    its legacy extraction sources).
    """
    for message in reversed(messages or []):
        if message.__class__.__name__.removesuffix("Message").lower() == "human":
            content = getattr(message, "content", "")
            if isinstance(content, str):
                return content
            try:
                return json.dumps(content, default=str)
            except (TypeError, ValueError):
                return str(content)
    return None


async def _provider_api_key(
    provider: str,
    audit_context: dict[str, Any] | None = None,
) -> str | None:
    """Return a saved model-provider API key for the request organization, if any."""
    org_id = str((audit_context or {}).get("organization_id") or "").strip()
    if not org_id:
        return None
    from lucent.llm.langchain_providers import normalize_provider

    provider_key = normalize_provider(provider)
    try:
        from lucent.model_discovery import (
            model_provider_credential_definition,
            model_provider_secret_scope,
        )
        from lucent.secrets import SecretRegistry

        definition = model_provider_credential_definition(provider_key)
        if not definition:
            return None
        token = await SecretRegistry.get().get(
            definition.secret_key,
            model_provider_secret_scope(org_id),
        )
        return (token or "").strip() or None
    except KeyError:
        return None
    except Exception:
        logger.warning("Failed to load saved %s provider credential", provider_key, exc_info=True)
        return None


async def _backend_secret(
    backend: dict[str, Any] | None,
    field: str,
    audit_context: dict[str, Any] | None,
) -> str | None:
    """Read a secret value a backend references (``secret_refs[field]``).

    Backend secrets are stored in the secret provider under the key recorded in
    ``secret_refs``; the row itself carries only key names. A missing org scope
    or a cleared field simply yields ``None`` so the env fallback can apply.
    """
    if not backend:
        return None
    key = (backend.get("secret_refs") or {}).get(field)
    if not key:
        return None
    org_id = str((audit_context or {}).get("organization_id") or "").strip()
    if not org_id:
        return None
    try:
        from lucent.model_discovery import model_provider_secret_scope
        from lucent.secrets import SecretRegistry

        token = await SecretRegistry.get().get(
            key, model_provider_secret_scope(org_id)
        )
        return (token or "").strip() or None
    except KeyError:
        return None
    except Exception:
        logger.warning("Failed to load backend secret %s", field, exc_info=True)
        return None


def _reasoning_kwarg(provider_spec: str, reasoning_effort: str) -> dict[str, Any]:
    """Map Lucent's reasoning effort onto a provider's constructor kwarg.

    Providers name the knob differently; this is the one place that knows, so
    callers pass one Lucent value. Providers not listed simply ignore effort
    (they either have no such control or expose it at call time).
    """
    if provider_spec in {"openai"}:
        return {"reasoning_effort": reasoning_effort}
    if provider_spec in {"anthropic"}:
        return {"effort": reasoning_effort}
    if provider_spec in {"google", "google_genai", "google_vertexai"}:
        return {"thinking_level": reasoning_effort}
    if provider_spec == "ollama":
        return {"reasoning": reasoning_effort}
    if provider_spec in {"groq", "deepseek", "xai", "openrouter", "litellm"}:
        # OpenAI-wire providers all accept the OpenAI spelling.
        return {"reasoning_effort": reasoning_effort}
    return {}


async def _get_chat_model(
    model_id: str,
    timeout: int = 300,
    reasoning_effort: str | None = None,
    audit_context: dict[str, Any] | None = None,
) -> Any:
    """Create a LangChain chat model from a Lucent model ID.

    Everything provider-specific is resolved here from two inputs:

    * the model's provider (DB row → runtime registry → name inference), and
    * the resolved **engine backend**, when the caller supplied one — a backend
      row carries the endpoint URL and the key for a self-hosted server, so a
      model on ``provider='custom'`` (llama.cpp, vLLM, LM Studio, a LiteLLM
      gateway) or any other LangChain provider runs with no extra code path.

    Credential precedence is backend secret → org provider credential (Settings →
    Models → LLM Providers) → provider environment variable → the integration's
    own default.
    """
    try:
        from langchain.chat_models import init_chat_model
    except ImportError:
        raise RuntimeError(
            "LangChain engine requires the 'langchain' package. "
            "Install with: pip install langchain langchain-anthropic "
            "(or whichever provider you need)"
        )

    from lucent.llm.langchain_providers import (
        CUSTOM_PROVIDER,
        langchain_model_provider,
        normalize_provider,
        provider_accepts_api_key,
        provider_accepts_base_url,
        resolve_provider_base_url,
    )

    raw_provider, provider_model = _resolve_model(model_id)
    provider = normalize_provider(raw_provider)
    backend = (audit_context or {}).get("engine_backend") or {}
    backend_config = backend.get("config") or {}
    backend_provider = normalize_provider(backend.get("provider") or "")

    # ``custom`` maps onto LangChain's ``openai`` integration; every other
    # catalog provider names its own integration. Unknown ids pass through so an
    # operator-supplied provider is not silently rewritten.
    integration = langchain_model_provider(provider)

    kwargs: dict[str, Any] = {"timeout": timeout}
    if integration:
        kwargs["model_provider"] = integration

    # The backend's pinned model wins over the row's api_model_id: a backend that
    # names its served model is describing a concrete deployment (llama.cpp serves
    # one GGUF under one alias). Falling back keeps discovery-created rows working.
    backend_model = str(backend_config.get("model") or "").strip()
    if backend_model and (not backend_provider or backend_provider == provider):
        provider_model = backend_model

    reasoning_kwargs = _reasoning_kwarg(provider, reasoning_effort or "") if reasoning_effort else {}
    if reasoning_kwargs:
        kwargs.update(reasoning_kwargs)

    # ── Endpoint ───────────────────────────────────────────────────────────
    # A backend's base_url is authoritative for the provider it targets; without
    # one, fall back to the provider's own env/default resolution.
    base_url = ""
    if provider_accepts_base_url(provider) or provider_accepts_base_url(integration):
        if backend and backend_provider in ("", provider):
            base_url = str(backend_config.get("base_url") or "").strip()
        if not base_url:
            base_url = resolve_provider_base_url(provider)
        if base_url:
            kwargs.setdefault("base_url", base_url)

    if provider == CUSTOM_PROVIDER and not base_url:
        # There is no vendor to fall back to: without an address this model would
        # be sent to api.openai.com with the operator's own key. Refuse loudly.
        raise RuntimeError(
            f"Model '{model_id}' uses the 'custom' provider but no endpoint is "
            "configured. Create an engine backend for it with a base URL "
            "(Settings → Models → Custom endpoints) and bind the model to it."
        )

    # ── Credentials ───────────────────────────────────────────────────────
    if provider_accepts_api_key(provider) or provider_accepts_api_key(integration):
        api_key = await _backend_secret(backend, "api_key", audit_context)
        if not api_key:
            api_key = await _provider_api_key(provider, audit_context)
        if not api_key and backend_provider in ("", provider):
            # A backend may also carry its key inline in ``config`` for
            # deployments whose secret store is unavailable. Accept it only
            # after the encrypted stores, never instead of them.
            api_key = str(backend_config.get("api_key") or "").strip() or None
        if api_key:
            kwargs["api_key"] = api_key
        elif provider == CUSTOM_PROVIDER:
            # OpenAI-compatible servers commonly require *some* key even when
            # they ignore it. Sending none makes the openai client raise before
            # the request leaves the process, so supply a documented placeholder.
            kwargs["api_key"] = "not-required"

    return init_chat_model(provider_model, **kwargs)


class LangChainEngine(LLMEngine):
    """LLM engine backed by LangChain.

    Uses init_chat_model() for provider-agnostic model access and
    MCPToolBridge for MCP tool integration. Supports streaming via
    the standard LangChain stream() interface.
    """

    @property
    def name(self) -> str:
        return "langchain"

    async def run_session(
        self,
        model: str,
        system_message: str,
        prompt: str,
        mcp_config: dict | None = None,
        timeout: int = 300,
        reasoning_effort: str | None = None,
        provider_session_id: str | None = None,
        resume: bool = False,
        message_history: list[dict[str, Any]] | None = None,
        hooks: list[dict[str, Any]] | None = None,
        audit_context: dict[str, Any] | None = None,
        enable_config_discovery: bool = False,
        approve_permissions: bool = True,
        attachments: list[dict[str, Any]] | None = None,
        managed_tools: list[dict[str, Any]] | None = None,
        expected_surface: dict[str, Any] | None = None,
        session_state: dict[str, Any] | None = None,
        approval: Any = None,
    ) -> str | None:
        """Run a blocking session (chat pattern)."""
        try:
            return await self._run_with_tools(
                model=model,
                system_message=system_message,
                prompt=prompt,
                mcp_config=mcp_config,
                timeout=timeout,
                reasoning_effort=reasoning_effort,
                on_event=None,
                message_history=message_history,
                hooks=hooks,
                audit_context=audit_context,
                attachments=attachments,
                approve_permissions=approve_permissions,
                managed_tools=managed_tools,
                expected_surface=expected_surface,
                session_state=session_state,
                approval=approval,
            )
        except Exception as e:
            logger.error("LangChain session failed: %s", e)
            return None

    async def run_session_streaming(
        self,
        model: str,
        system_message: str,
        prompt: str,
        mcp_config: dict | None = None,
        on_event: Callable[[SessionEvent], None] | None = None,
        timeout: int = 3600,
        idle_timeout: int = 300,
        reasoning_effort: str | None = None,
        provider_session_id: str | None = None,
        resume: bool = False,
        message_history: list[dict[str, Any]] | None = None,
        hooks: list[dict[str, Any]] | None = None,
        audit_context: dict[str, Any] | None = None,
        enable_config_discovery: bool = False,
        approve_permissions: bool = True,
        attachments: list[dict[str, Any]] | None = None,
        managed_tools: list[dict[str, Any]] | None = None,
        expected_surface: dict[str, Any] | None = None,
        session_state: dict[str, Any] | None = None,
        current_user_text: str | None = None,
        approval: Any = None,
    ) -> str | None:
        """Run a streaming session with event callbacks (daemon pattern).

        ``session_state`` is an optional per-conversation dict owned by the
        session caller: the engine does not mutate it, but the HookManager it
        seeds persists hook state (e.g. injected-memory dedup ids) across the
        turns of one conversation. Each call without one starts fresh.

        ``current_user_text`` is the raw text of the user message that started
        this turn; it is threaded into the message-memory-lookup hook so its
        search terms are anchored to the current turn (2026-09-11
        turn-anchoring fix) instead of depending on a session-state side
        channel being written before the engine call.
        """
        try:
            return await self._run_with_tools(
                model=model,
                system_message=system_message,
                prompt=prompt,
                mcp_config=mcp_config,
                timeout=timeout,
                idle_timeout=idle_timeout,
                reasoning_effort=reasoning_effort,
                on_event=on_event,
                message_history=message_history,
                hooks=hooks,
                audit_context=audit_context,
                attachments=attachments,
                approve_permissions=approve_permissions,
                managed_tools=managed_tools,
                expected_surface=expected_surface,
                session_state=session_state,
                current_user_text=current_user_text,
                approval=approval,
            )
        except Exception as e:
            error_message = _describe_provider_error(model, e)
            logger.error("LangChain streaming session failed: %s", error_message)
            if on_event:
                on_event(SessionEvent(type=SessionEventType.ERROR, content=error_message))
            return None

    async def _run_with_tools(
        self,
        model: str,
        system_message: str,
        prompt: str,
        mcp_config: dict | None = None,
        timeout: int = 300,
        idle_timeout: int | None = None,
        reasoning_effort: str | None = None,
        on_event: Callable[[SessionEvent], None] | None = None,
        message_history: list[dict[str, Any]] | None = None,
        hooks: list[dict[str, Any]] | None = None,
        audit_context: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        approve_permissions: bool = True,
        managed_tools: list[dict[str, Any]] | None = None,
        expected_surface: dict[str, Any] | None = None,
        session_state: dict[str, Any] | None = None,
        current_user_text: str | None = None,
        approval: Any = None,
    ) -> str | None:
        """Core implementation: run model with MCP tool loop.

        Implements the tool-calling loop: invoke model → check for tool_calls
        → execute via MCP bridge or built-in tool → feed results back → repeat.

        Tools come from two sources: MCP bridges (memory/requests/etc.) and the
        engine's built-in toolset (file/shell/web), which gives local/LangChain
        models parity with the Copilot SDK's provider-native built-ins.

        ``expected_surface`` optionally asserts which granted tools must be
        present in the composed surface. When the model is about to run with a
        granted tool missing — e.g. discovery lost the run_managed_tool
        carrier during a transient server window — the session refuses BEFORE
        the model loop instead of failing later with vague per-call
        "tool is not available" errors. Validation is delegated to
        ``validate_expected_surface``; see its docs for the empty-allowlist
        semantics.

        ``current_user_text`` overrides the prompt-derived extraction source
        for the message-memory-lookup hook (chat.py threads the raw current
        turn text; callers without hooks may leave it unset).
        """
        from lucent.llm.builtin_tools import build_default_toolset
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
            ToolMessage,
        )

        chat_model = await _get_chat_model(
            model,
            timeout=timeout,
            reasoning_effort=reasoning_effort,
            audit_context=audit_context,
        )

        # Set up MCP tool bridges if config is provided
        bridges: list[Any] = []
        tool_to_bridge: dict[str, Any] = {}
        memory_bridge = None
        tool_schemas: list[dict] = []
        if mcp_config:
            bridges, tool_to_bridge, memory_bridge, tool_schemas = await self._create_bridges(
                mcp_config,
                audit_context={
                    **(audit_context or {}),
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "engine": self.name,
                },
            )
        hook_manager = HookManager(hooks, session_state=session_state)

        # Projects (v2 redesign, 2026-09-12): the chat's project standing
        # context rides the system prompt itself — appended AFTER the global
        # prompt, composing alongside hook-mediated memory injections (never
        # replacing them). Refreshed every turn: the session caller re-reads
        # the (org, user)-scoped project snapshot from the DB each turn and
        # threads it via session_state; this recomposition happens per turn,
        # before the first model call. Metadata-only block (name,
        # instructions, file manifest) — file contents are fetched on demand
        # through read_user_file, never injected here. No hook mediates this:
        # absent/malformed/disabled context renders no block and no event.
        if session_state:
            project_block = render_project_context_block(
                session_state,
                audit_context=audit_context,
            )
            if project_block:
                system_message = f"{system_message}\n\n{project_block}".strip() + "\n"
                emit_project_context_event(
                    on_event,
                    session_state,
                    block=project_block,
                )

        from lucent.services.managed_tools import normalize_discovered_mcp_tools

        managed_tool_to_definition: dict[str, dict[str, Any]] = {}
        for managed_tool in managed_tools or []:
            if not (managed_tool.get("runtime_config") or {}).get("mcp_proxy"):
                continue
            try:
                descriptors = normalize_discovered_mcp_tools(
                    {"tools": managed_tool.get("discovered_tools") or []}
                )
            except Exception:
                logger.warning(
                    "Skipping invalid cached MCP tools for managed proxy %s",
                    managed_tool.get("name", "unknown"),
                    exc_info=True,
                )
                continue
            for descriptor in descriptors:
                name = descriptor["name"]
                if name in tool_to_bridge or name in managed_tool_to_definition:
                    logger.warning(
                        "Skipping managed MCP proxy tool %s because its name is already in use",
                        name,
                    )
                    continue
                managed_tool_to_definition[name] = managed_tool
                tool_schemas.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": descriptor["description"],
                        "parameters": descriptor["input_schema"],
                    },
                })

        # Built-in tools (file/shell/web) give LangChain models parity with the
        # Copilot SDK's provider-native built-ins. Disabled for restricted web
        # chat (approve_permissions=False) so it uses only configured MCP tools.
        builtin_toolset = build_default_toolset(approve_permissions=approve_permissions)
        builtin_names: set[str] = set()
        if builtin_toolset is not None:
            for schema in builtin_toolset.schemas():
                name = _schema_tool_name(schema)
                if not name:
                    continue
                if name in tool_to_bridge or name in managed_tool_to_definition or name in builtin_names:
                    logger.warning(
                        "Built-in tool %s shadowed by an MCP tool; skipping built-in", name
                    )
                    continue
                builtin_names.add(name)
                tool_schemas.append(schema)

        # Composition telemetry + fail-fast refusal. This is Layer 2 of the
        # hardening design: Layer 1 (daemon.py) checks the dispatch-time
        # config; here we check what bridge discovery ACTUALLY produced.
        # discover_tools() now raises loudly on discovery failure instead of
        # silently composing an empty surface — but even with that, a
        # transient window can leave the carrier off the composed surface
        # (its server skipped, or allowed_tools filtered it out). Rather
        # than run the model loop and fail later with vague per-call
        # "tool is not available" errors, record the actual surface and
        # refuse BEFORE the model starts.
        surface_report = _compose_surface_report(tool_to_bridge, managed_tools)
        if on_event:
            on_event(
                SessionEvent(
                    type=SessionEventType.COMPOSITION_SURFACE,
                    content=(
                        f"Composed surface: {surface_report['tool_count']} MCP tool(s); "
                        f"run_managed_tool "
                        f"{'present' if surface_report['carrier_present'] else 'ABSENT'}"
                    ),
                    raw=surface_report,
                )
            )
        try:
            validate_expected_surface(
                surface_report["tools"],
                expected_surface=expected_surface,
                carrier_present=surface_report["carrier_present"],
                logger=logger,
            )
        except Exception as e:
            if on_event:
                on_event(
                    SessionEvent(
                        type=SessionEventType.ERROR,
                        content=str(e),
                    )
                )
            raise
        if surface_report["carrier_present"] is False:
            logger.warning(
                "run_managed_tool carrier missing from composed surface "
                "(managed proxies: %s); session proceeding without managed tools",
                surface_report["managed_proxies"],
            )

        async def _dispatch_tool(name: str, arguments: dict) -> str | None:
            """Route a tool call to a built-in tool or an MCP bridge.

            Returns None when the named tool exists in neither source.
            """
            managed_tool = managed_tool_to_definition.get(name)
            if managed_tool is not None:
                from lucent.db import DefinitionRepository, get_pool
                from lucent.services.managed_tools import ManagedToolExecutor

                context = audit_context or {}
                org_id = str(context.get("organization_id") or "")
                user_id = str(context.get("user_id") or "")
                agent_id = str(context.get("agent_definition_id") or "")
                if not org_id or not user_id or not agent_id:
                    return json.dumps({"error": "Managed MCP proxy context is unavailable"})
                try:
                    executor = ManagedToolExecutor(DefinitionRepository(await get_pool()))
                    result = await executor.execute(
                        tool=managed_tool,
                        arguments={"tool": name, "arguments": arguments},
                        org_id=org_id,
                        user_id=user_id,
                        user_role=context.get("user_role"),
                        agent_id=agent_id,
                    )
                    return json.dumps(result.to_dict(), default=str)
                except Exception:
                    logger.exception("Managed MCP proxy tool %s failed", name)
                    return json.dumps({"error": "Managed MCP proxy execution failed"})
            if name in builtin_names and builtin_toolset is not None:
                return await builtin_toolset.call_tool(name, arguments)
            bridge = tool_to_bridge.get(name)
            if bridge is not None:
                return await bridge.call_tool(name, arguments)
            return None

        try:
            # Bind tools to model if available
            model_with_tools = chat_model
            if tool_schemas:
                model_with_tools = chat_model.bind_tools(tool_schemas)

            # Build initial messages from Lucent's persisted transcript.
            # LangChain providers do not expose a universal resumable session
            # primitive, so Lucent is the source of truth for history.
            messages: list = [SystemMessage(content=system_message)]
            for persisted in message_history or []:
                role = persisted.get("role")
                content = persisted.get("content") or ""
                if role == "user":
                    messages.append(HumanMessage(content=content))
                elif role == "assistant":
                    messages.append(AIMessage(content=content))
            if attachments:
                from lucent.llm.attachments import to_langchain_blocks

                messages.append(HumanMessage(content=to_langchain_blocks(prompt, attachments)))
            else:
                messages.append(HumanMessage(content=prompt))

            # Tool-calling loop
            full_response_parts: list[str] = []
            organization_id = (audit_context or {}).get("organization_id")
            max_tool_rounds = runtime_settings.langchain_max_tool_rounds(
                organization_id=organization_id
            )
            tool_rounds = range(max_tool_rounds) if max_tool_rounds else count()
            for _round in tool_rounds:
                before_model = await hook_manager.before_model_call(
                    messages=_messages_for_hooks(messages),
                    memory_bridge=memory_bridge,
                    # THIS turn's raw user text: prefer the explicit text the
                    # session caller threaded (chat.py passes the current
                    # message every turn); when absent, derive it from the
                    # last human message in the loop — `_run_with_tools`
                    # appends this turn's message (or its attachment-block
                    # form) last, so that is the current turn either way,
                    # never the flattened transcript prompt. (2026-09-11 fix.)
                    current_user_text=current_user_text
                    or _current_user_text_from_messages(messages),
                )
                if before_model.blocked:
                    blocked_text = before_model.block_message or "Model call blocked by hook."
                    full_response_parts.append(blocked_text)
                    if on_event:
                        on_event(
                            SessionEvent(
                                type=SessionEventType.OTHER,
                                tool_name="_hook",
                                content=blocked_text[:2000],
                                raw={
                                    "hook": "model_call_guard",
                                    "phase": "before_model_call",
                                    "blocked": True,
                                },
                            )
                        )
                    break
                if before_model.injectable_executions:
                    messages.append(
                        SystemMessage(
                            content=append_hook_context(
                                "", before_model.injectable_executions,
                            )
                        )
                    )
                    if on_event:
                        for execution in before_model.injectable_executions:
                            on_event(
                                SessionEvent(
                                    type=SessionEventType.OTHER,
                                    tool_name="_hook",
                                    content=execution.text[:2000],
                                    raw={
                                        "hook": execution.hook_name,
                                        "phase": "before_model_call",
                                        "decision": execution.decision,
                                        **execution.metadata,
                                    },
                                )
                            )

                # Streaming callers consume provider chunks so slow local models
                # can show progress and use activity-based timeout handling.
                if on_event:
                    async def consume_stream() -> AIMessage:
                        aggregate = None
                        stream = model_with_tools.astream(messages).__aiter__()
                        while True:
                            try:
                                chunk = await asyncio.wait_for(
                                    anext(stream),
                                    timeout=idle_timeout or timeout,
                                )
                            except StopAsyncIteration:
                                break
                            except asyncio.TimeoutError as exc:
                                raise _StreamIdleTimeout(
                                    "LangChain stream idle timeout after "
                                    f"{idle_timeout or timeout}s of inactivity"
                                ) from exc

                            aggregate = chunk if aggregate is None else aggregate + chunk
                            chunk_text = _stream_chunk_text(chunk.content)
                            if chunk_text:
                                on_event(
                                    SessionEvent(
                                        type=SessionEventType.MESSAGE_DELTA,
                                        content=chunk_text,
                                    )
                                )

                        return aggregate or AIMessage(content="")

                    try:
                        ai_msg = await asyncio.wait_for(consume_stream(), timeout=timeout)
                    except _StreamIdleTimeout:
                        raise
                    except asyncio.TimeoutError as exc:
                        raise TimeoutError(
                            f"LangChain stream hard timeout after {timeout}s"
                        ) from exc
                else:
                    ai_msg = await asyncio.wait_for(
                        model_with_tools.ainvoke(messages),
                        timeout=timeout,
                    )

                usage = normalize_token_usage(getattr(ai_msg, "usage_metadata", None))
                if usage and on_event:
                    on_event(SessionEvent(type=SessionEventType.USAGE, usage=usage))

                # Extract text content
                text = ai_msg.content if isinstance(ai_msg.content, str) else ""
                if isinstance(ai_msg.content, list):
                    # Content blocks format — extract text blocks
                    text = " ".join(
                        block.get("text", "")
                        for block in ai_msg.content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )

                after_model = await hook_manager.after_model_call(
                    messages=_messages_for_hooks([*messages, ai_msg]),
                    model_text=text,
                )
                if after_model.blocked:
                    text = after_model.block_message or "Model response blocked by hook."
                elif after_model.modified_result is not None:
                    text = after_model.modified_result
                if after_model.injectable_executions:
                    text = append_hook_context(text, after_model.injectable_executions)

                if text:
                    full_response_parts.append(text)
                    if on_event:
                        on_event(
                            SessionEvent(
                                type=SessionEventType.MESSAGE,
                                content=text,
                            )
                        )

                # Check for tool calls
                if not ai_msg.tool_calls or (
                    not tool_to_bridge and not managed_tool_to_definition and not builtin_names
                ):
                    # No tool calls, or no tools available at all — we're done
                    break

                messages.append(ai_msg)

                # Execute each tool call
                for tool_call in ai_msg.tool_calls:
                    tool_name = tool_call["name"]
                    tool_args = tool_call["args"]
                    tool_id = tool_call.get("id", "")
                    if (
                        tool_name not in builtin_names
                        and tool_name not in tool_to_bridge
                        and tool_name not in managed_tool_to_definition
                    ):
                        result = f"Error calling tool {tool_name}: tool is not available"
                        messages.append(ToolMessage(content=result, tool_call_id=tool_id))
                        continue

                    # Human-in-the-loop: the engine must gate the tools it fetched
                    # itself (an external MCP server granted to the agent, or the
                    # built-in file/shell/web set). Lucent's own tools are already
                    # gated inside the MCP server, and `should_gate` skips a tool
                    # whose bridge is the internal server so the person is never
                    # asked twice.
                    if approval is not None and getattr(approval, "enabled", False):
                        source_bridge = tool_to_bridge.get(tool_name)
                        server_name = (
                            getattr(source_bridge, "server_name", None)
                            if source_bridge is not None
                            else None
                        )
                        gate_name = (
                            f"mcp__{server_name}__{tool_name}" if server_name else tool_name
                        )
                        if approval.should_gate(gate_name):
                            if on_event:
                                on_event(
                                    SessionEvent(
                                        type=SessionEventType.TOOL_CALL,
                                        tool_name=tool_name,
                                        tool_input=tool_args,
                                    )
                                )
                            decision = await approval.request(tool_name, tool_args or {})
                            if not decision.approved:
                                result = decision.as_tool_result(tool_name)
                                messages.append(
                                    ToolMessage(content=result, tool_call_id=tool_id)
                                )
                                if on_event:
                                    on_event(
                                        SessionEvent(
                                            type=SessionEventType.TOOL_RESULT,
                                            tool_name=tool_name,
                                            tool_output=result[:300],
                                        )
                                    )
                                continue

                    if on_event:
                        on_event(
                            SessionEvent(
                                type=SessionEventType.TOOL_CALL,
                                tool_name=tool_name,
                                tool_input=tool_args,
                            )
                        )

                    before_tool = await hook_manager.before_tool_call(
                        tool_name=tool_name,
                        arguments=tool_args or {},
                        memory_bridge=memory_bridge,
                    )
                    effective_args = before_tool.modified_arguments or tool_args
                    if before_tool.blocked:
                        result = before_tool.block_message or f"Tool {tool_name} blocked by hook."
                        after_tool = None
                    else:
                        result = await _dispatch_tool(tool_name, effective_args or {})
                        if result is None:
                            result = f"Error calling tool {tool_name}: tool is not available"
                        after_tool = await hook_manager.after_tool_call(
                            tool_name=tool_name,
                            arguments=effective_args or {},
                            tool_result=result,
                            memory_bridge=memory_bridge,
                        )
                        if after_tool.blocked:
                            result = after_tool.block_message or result
                        elif after_tool.modified_result is not None:
                            result = after_tool.modified_result
                    hook_context = before_tool.injectable_executions
                    if after_tool is not None:
                        hook_context = [*hook_context, *after_tool.injectable_executions]
                    result_for_model = append_hook_context(result, hook_context)

                    if on_event:
                        for phase, phase_outcome in (
                            ("before_tool_call", before_tool),
                            ("after_tool_call", after_tool),
                        ):
                            if phase_outcome is None:
                                continue
                            for execution in phase_outcome.executions:
                                on_event(
                                    SessionEvent(
                                        type=SessionEventType.OTHER,
                                        tool_name="_hook",
                                        content=execution.text[:2000],
                                        raw={
                                            "hook": execution.hook_name,
                                            "phase": phase,
                                            "trigger_tool": tool_name,
                                            "decision": execution.decision,
                                            **execution.metadata,
                                        },
                                    )
                                )
                        on_event(
                            SessionEvent(
                                type=SessionEventType.TOOL_RESULT,
                                tool_name=tool_name,
                                tool_output=result[:300] if result else None,
                            )
                        )

                    messages.append(
                        ToolMessage(
                            content=result_for_model,
                            tool_call_id=tool_id,
                        )
                    )
            else:
                raise _ToolRoundLimitExceeded(
                    "LangChain tool round limit "
                    f"({max_tool_rounds}) reached before a final response"
                )

            if on_event:
                on_event(SessionEvent(type=SessionEventType.SESSION_IDLE))

            return "\n".join(full_response_parts) if full_response_parts else None

        finally:
            for bridge in bridges:
                # Closing an MCP transport is cleanup and must never decide the
                # outcome of a turn. The SDK's streamable-HTTP client runs its
                # streams in an internal anyio task group; tearing that down after
                # a long tool call can raise "Attempted to exit cancel scope in a
                # different task", and anyio wraps it in a BaseExceptionGroup —
                # which is NOT an Exception, so `except Exception` would let it
                # propagate and discard a response the model already produced.
                try:
                    await bridge.close()
                except asyncio.CancelledError:
                    # Bridge.close() already tolerates transport-teardown
                    # cancellation; reach here only if a future SDK raises it
                    # directly. A pending cancellation is re-delivered by asyncio at
                    # the next await, so swallowing it here cannot lose a real abort.
                    logger.debug("MCP bridge close was cancelled by transport teardown")
                except (Exception, BaseExceptionGroup):  # noqa: BLE001
                    logger.debug("MCP bridge close failed", exc_info=True)

    async def _create_bridges(
        self, mcp_config: dict, audit_context: dict[str, Any] | None = None
    ) -> tuple[list[Any], dict[str, Any], Any | None, list[dict]]:
        """Create MCPToolBridge instances and map discovered tools to bridges."""
        from lucent.llm.mcp_bridge import MCPToolBridge

        bridges: list[Any] = []
        tool_to_bridge: dict[str, Any] = {}
        memory_bridge = None
        tool_schemas: list[dict] = []
        # MCP config format: {"server-name": {"type": "http", "url": "...", "headers": {...}}}
        for server_name, server_conf in mcp_config.items():
            if isinstance(server_conf, dict) and server_conf.get("url"):
                # A single MCP server failing (e.g. URL fails SSRF validation, or
                # the server is unreachable) must not abort the whole session.
                # The model may still complete the task with built-in tools or
                # other bridges, so log and skip the failed server.
                #
                # Entries flagged ``internal`` are Lucent's own MCP endpoint
                # (operator-configured, not user-supplied), so they bypass SSRF
                # validation — otherwise the default loopback URL
                # (http://localhost:8766/mcp) is blocked and tools never load.
                try:
                    bridge = MCPToolBridge(
                        mcp_url=server_conf["url"],
                        headers=server_conf.get("headers"),
                        allowed_tools=server_conf.get("tools"),
                        excluded_tools=server_conf.get("exclude_tools"),
                        skip_url_validation=bool(server_conf.get("internal")),
                        audit_context={**(audit_context or {}), "mcp_server": server_name},
                        transport=str(server_conf.get("type") or "http"),
                        server_name=server_name,
                    )
                    discovered = await bridge.discover_tools()
                except Exception as e:
                    logger.warning(
                        "Skipping MCP server %s: bridge setup failed: %s", server_name, e
                    )
                    continue
                if not discovered:
                    await bridge.close()
                    continue
                bridges.append(bridge)
                if server_name == "memory-server" or any(
                    _schema_tool_name(schema) in {"search_memories", "search_memories_full"}
                    for schema in discovered
                ):
                    memory_bridge = bridge
                for schema in discovered:
                    tool_name = _schema_tool_name(schema)
                    if not tool_name:
                        continue
                    if tool_name in tool_to_bridge:
                        logger.warning(
                            "Duplicate MCP tool name %s; keeping first bridge", tool_name
                        )
                        continue
                    tool_to_bridge[tool_name] = bridge
                    tool_schemas.append(schema)
        return bridges, tool_to_bridge, memory_bridge, tool_schemas

    async def cleanup(self) -> None:
        """No persistent resources for LangChain engine."""
