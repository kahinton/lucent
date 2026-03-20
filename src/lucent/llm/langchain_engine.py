"""LangChain engine implementation.

Uses LangChain's init_chat_model() for provider-agnostic LLM access,
with MCPToolBridge for MCP tool integration.

All major provider packages (langchain-anthropic, langchain-openai,
langchain-google-genai) are included in the base Lucent install.
For local models via Ollama, install: pip install lucent[ollama]
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from lucent.llm.engine import LLMEngine, SessionEvent, SessionEventType
from lucent.logging import get_logger

logger = get_logger("llm.langchain")

# Provider mapping: our model IDs → LangChain provider + provider model ID
# The Copilot SDK uses short names; direct API providers need different IDs.
PROVIDER_MODEL_MAP: dict[str, tuple[str, str]] = {
    # Anthropic — use dated model IDs for the API
    "claude-haiku-4.5": ("anthropic", "claude-haiku-4-5-20251001"),
    "claude-opus-4.5": ("anthropic", "claude-opus-4-5-20251101"),
    "claude-opus-4.6": ("anthropic", "claude-opus-4-6-20260301"),
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
    # Google
    "gemini-2.5-pro": ("google_genai", "gemini-2.5-pro"),
    "gemini-3-flash": ("google_genai", "gemini-3-flash"),
    "gemini-3-pro": ("google_genai", "gemini-3-pro"),
    "gemini-3.1-pro": ("google_genai", "gemini-3.1-pro"),
}


def _resolve_model(model_id: str) -> tuple[str, str]:
    """Resolve a Lucent model ID to (provider, provider_model_id).

    If the model isn't in our map, try to infer the provider from the name.
    """
    if model_id in PROVIDER_MODEL_MAP:
        return PROVIDER_MODEL_MAP[model_id]

    # Infer provider from model name prefix
    if model_id.startswith("claude"):
        return "anthropic", model_id
    elif model_id.startswith("gpt") or model_id.startswith("o1") or model_id.startswith("o3"):
        return "openai", model_id
    elif model_id.startswith("gemini"):
        return "google_genai", model_id

    # Default: try as-is with init_chat_model's auto-detection
    return "", model_id


def _get_chat_model(model_id: str, timeout: int = 300) -> Any:
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
    ) -> str | None:
        """Run a blocking session (chat pattern)."""
        try:
            return await self._run_with_tools(
                model=model,
                system_message=system_message,
                prompt=prompt,
                mcp_config=mcp_config,
                timeout=timeout,
                on_event=None,
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
    ) -> str | None:
        """Run a streaming session with event callbacks (daemon pattern)."""
        try:
            return await self._run_with_tools(
                model=model,
                system_message=system_message,
                prompt=prompt,
                mcp_config=mcp_config,
                timeout=timeout,
                on_event=on_event,
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
        on_event: Callable[[SessionEvent], None] | None = None,
    ) -> str | None:
        """Core implementation: run model with MCP tool loop.

        Implements the tool-calling loop: invoke model → check for tool_calls
        → execute via MCP bridge → feed results back → repeat.
        """
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
            ToolMessage,
        )

        chat_model = _get_chat_model(model, timeout=timeout)

        # Set up MCP tool bridge if config is provided
        bridge = None
        tool_schemas: list[dict] = []
        if mcp_config:
            bridge = await self._create_bridge(mcp_config)
            if bridge:
                tool_schemas = await bridge.discover_tools()

        try:
            # Bind tools to model if available
            model_with_tools = chat_model
            if tool_schemas:
                model_with_tools = chat_model.bind_tools(tool_schemas)

            # Build initial messages
            messages: list = [
                SystemMessage(content=system_message),
                HumanMessage(content=prompt),
            ]

            # Tool-calling loop
            full_response_parts: list[str] = []
            for _round in range(self.MAX_TOOL_ROUNDS):
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
                if not ai_msg.tool_calls or not bridge:
                    # No tool calls or no bridge — we're done
                    break

                messages.append(ai_msg)

                # Execute each tool call
                for tool_call in ai_msg.tool_calls:
                    tool_name = tool_call["name"]
                    tool_args = tool_call["args"]
                    tool_id = tool_call.get("id", "")

                    if on_event:
                        on_event(
                            SessionEvent(
                                type=SessionEventType.TOOL_CALL,
                                tool_name=tool_name,
                            )
                        )

                    result = await bridge.call_tool(tool_name, tool_args)

                    if on_event:
                        on_event(
                            SessionEvent(
                                type=SessionEventType.TOOL_RESULT,
                                tool_name=tool_name,
                                tool_output=result[:300] if result else None,
                            )
                        )

                    messages.append(
                        ToolMessage(
                            content=result,
                            tool_call_id=tool_id,
                        )
                    )

            if on_event:
                on_event(SessionEvent(type=SessionEventType.SESSION_IDLE))

            return "\n".join(full_response_parts) if full_response_parts else None

        finally:
            if bridge:
                await bridge.close()

    async def _create_bridge(self, mcp_config: dict) -> Any:
        """Create an MCPToolBridge from MCP config dict."""
        from lucent.llm.mcp_bridge import MCPToolBridge

        # MCP config format: {"server-name": {"type": "http", "url": "...", "headers": {...}}}
        for _server_name, server_conf in mcp_config.items():
            if isinstance(server_conf, dict) and server_conf.get("url"):
                return MCPToolBridge(
                    mcp_url=server_conf["url"],
                    headers=server_conf.get("headers"),
                )
        return None

    async def cleanup(self) -> None:
        """No persistent resources for LangChain engine."""
