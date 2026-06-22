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
from typing import Any, Callable

from lucent.llm.engine import LLMEngine, SessionEvent, SessionEventType
from lucent.llm.hooks import HookManager, append_hook_context
from lucent.logging import get_logger

logger = get_logger("llm.langchain")

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

    If the model isn't in our map, try to infer the provider from the name.
    """
    if model_id in PROVIDER_MODEL_MAP:
        return PROVIDER_MODEL_MAP[model_id]

    # Check runtime registry (DB-populated Ollama/custom models)
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

    # Check DB for provider (handles ollama and other custom providers)
    return "", model_id


def _schema_tool_name(schema: dict[str, Any]) -> str | None:
    """Extract a tool name from an OpenAI/LangChain-style function schema."""
    if not isinstance(schema, dict):
        return None
    function = schema.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        return str(name) if name else None
    name = schema.get("name")
    return str(name) if name else None


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


async def _provider_api_key(
    provider: str,
    audit_context: dict[str, Any] | None = None,
) -> str | None:
    """Return a saved model-provider API key for the request organization, if any."""
    org_id = str((audit_context or {}).get("organization_id") or "").strip()
    if not org_id:
        return None
    provider_key = "google" if provider == "google_genai" else provider
    try:
        from lucent.model_discovery import (
            model_provider_credential_definitions,
            model_provider_secret_scope,
        )
        from lucent.secrets import SecretRegistry

        definition = next(
            (
                item
                for item in model_provider_credential_definitions()
                if item.provider == provider_key
            ),
            None,
        )
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


async def _get_chat_model(
    model_id: str,
    timeout: int = 300,
    reasoning_effort: str | None = None,
    audit_context: dict[str, Any] | None = None,
) -> Any:
    """Create a LangChain chat model from a Lucent model ID."""
    try:
        from langchain.chat_models import init_chat_model
    except ImportError:
        raise RuntimeError(
            "LangChain engine requires the 'langchain' package. "
            "Install with: pip install langchain langchain-anthropic "
            "(or whichever provider you need)"
        )

    provider, provider_model = _resolve_model(model_id)

    kwargs: dict[str, Any] = {"timeout": timeout}
    if provider:
        kwargs["model_provider"] = provider

    api_key = await _provider_api_key(provider, audit_context)
    if api_key and provider in {"openai", "anthropic", "google_genai"}:
        kwargs["api_key"] = api_key

    if reasoning_effort:
        if provider == "openai":
            kwargs["reasoning_effort"] = reasoning_effort
        elif provider == "anthropic":
            kwargs["effort"] = reasoning_effort
        elif provider == "google_genai":
            kwargs["thinking_level"] = reasoning_effort

    # Ollama: pass base_url from env so Docker containers can reach the host
    if provider == "ollama":
        import os
        ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        kwargs["base_url"] = ollama_host

    return init_chat_model(provider_model, **kwargs)


class LangChainEngine(LLMEngine):
    """LLM engine backed by LangChain.

    Uses init_chat_model() for provider-agnostic model access and
    MCPToolBridge for MCP tool integration. Supports streaming via
    the standard LangChain stream() interface.
    """

    MAX_TOOL_ROUNDS = 25  # Safety limit on tool-calling loops

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
    ) -> str | None:
        """Run a streaming session with event callbacks (daemon pattern)."""
        try:
            return await self._run_with_tools(
                model=model,
                system_message=system_message,
                prompt=prompt,
                mcp_config=mcp_config,
                timeout=timeout,
                reasoning_effort=reasoning_effort,
                on_event=on_event,
                message_history=message_history,
                hooks=hooks,
                audit_context=audit_context,
                attachments=attachments,
                approve_permissions=approve_permissions,
            )
        except Exception as e:
            logger.error("LangChain streaming session failed: %s", e)
            if on_event:
                on_event(SessionEvent(type=SessionEventType.ERROR, content=str(e)))
            return None

    async def _run_with_tools(
        self,
        model: str,
        system_message: str,
        prompt: str,
        mcp_config: dict | None = None,
        timeout: int = 300,
        reasoning_effort: str | None = None,
        on_event: Callable[[SessionEvent], None] | None = None,
        message_history: list[dict[str, Any]] | None = None,
        hooks: list[dict[str, Any]] | None = None,
        audit_context: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        approve_permissions: bool = True,
    ) -> str | None:
        """Core implementation: run model with MCP tool loop.

        Implements the tool-calling loop: invoke model → check for tool_calls
        → execute via MCP bridge or built-in tool → feed results back → repeat.

        Tools come from two sources: MCP bridges (memory/requests/etc.) and the
        engine's built-in toolset (file/shell/web), which gives local/LangChain
        models parity with the Copilot SDK's provider-native built-ins.
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
        hook_manager = HookManager(hooks)

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
                if name in tool_to_bridge or name in builtin_names:
                    logger.warning(
                        "Built-in tool %s shadowed by an MCP tool; skipping built-in", name
                    )
                    continue
                builtin_names.add(name)
                tool_schemas.append(schema)

        async def _dispatch_tool(name: str, arguments: dict) -> str | None:
            """Route a tool call to a built-in tool or an MCP bridge.

            Returns None when the named tool exists in neither source.
            """
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
            for _round in range(self.MAX_TOOL_ROUNDS):
                before_model = await hook_manager.before_model_call(
                    messages=_messages_for_hooks(messages),
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
                                raw={"hook_event": "before_model_call", "blocked": True},
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

                # Run model with timeout
                ai_msg: AIMessage = await asyncio.wait_for(
                    model_with_tools.ainvoke(messages),
                    timeout=timeout,
                )

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
                if not ai_msg.tool_calls or (not tool_to_bridge and not builtin_names):
                    # No tool calls, or no tools available at all — we're done
                    break

                messages.append(ai_msg)

                # Execute each tool call
                for tool_call in ai_msg.tool_calls:
                    tool_name = tool_call["name"]
                    tool_args = tool_call["args"]
                    tool_id = tool_call.get("id", "")
                    if tool_name not in builtin_names and tool_name not in tool_to_bridge:
                        result = f"Error calling tool {tool_name}: tool is not available"
                        messages.append(ToolMessage(content=result, tool_call_id=tool_id))
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
                        hook_events = list(before_tool.executions)
                        if after_tool is not None:
                            hook_events.extend(after_tool.executions)
                        for hook_execution in hook_events:
                            on_event(
                                SessionEvent(
                                    type=SessionEventType.OTHER,
                                    tool_name="_hook",
                                    content=hook_execution.text[:2000],
                                    raw={
                                        "hook": hook_execution.hook_name,
                                        "decision": hook_execution.decision,
                                        **hook_execution.metadata,
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

            if on_event:
                on_event(SessionEvent(type=SessionEventType.SESSION_IDLE))

            return "\n".join(full_response_parts) if full_response_parts else None

        finally:
            for bridge in bridges:
                await bridge.close()

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
                try:
                    bridge = MCPToolBridge(
                        mcp_url=server_conf["url"],
                        headers=server_conf.get("headers"),
                        allowed_tools=server_conf.get("tools"),
                        audit_context={**(audit_context or {}), "mcp_server": server_name},
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
