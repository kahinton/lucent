"""MCP Tool Bridge — converts MCP server tools to LangChain tools.

Connects to a Lucent MCP server via HTTP, discovers available tools,
and wraps each as a LangChain tool that the LLM can call.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import AsyncExitStack
from typing import Any

from lucent.url_validation import validate_url

logger = logging.getLogger(__name__)

# Memory-server tools we specifically track for observability.
# These are the tools whose usage (or absence) indicates whether agents
# follow memory integration patterns prescribed in their definitions.
MEMORY_TOOL_NAMES = frozenset({
    "search_memories",
    "search_memories_full",
    "get_memory",
    "get_memories",
    "get_current_user_context",
    "create_memory",
    "update_memory",
    "delete_memory",
    "get_existing_tags",
    "get_tag_suggestions",
    "export_memories",
})


class MCPToolBridge:
    """Bridge between an MCP server and LangChain tool-calling models.

    Discovers tools from the MCP server and provides methods to:
    1. List tools as LangChain-compatible tool definitions
    2. Execute tool calls by forwarding them to the MCP server
    """

    def __init__(
        self,
        mcp_url: str,
        headers: dict[str, str] | None = None,
        *,
        allowed_tools: list[str] | None = None,
        skip_url_validation: bool = False,
        audit_context: dict[str, Any] | None = None,
    ):
        if not skip_url_validation:
            validate_url(mcp_url, purpose="MCP bridge")
        self._mcp_url = mcp_url
        self._headers = headers or {}
        self._allowed_tools = set(allowed_tools or ["*"])
        self._audit_context = audit_context or {}
        self._tools: list[dict[str, Any]] = []
        self._exit_stack: AsyncExitStack | None = None
        self._session: Any | None = None

    def _is_tool_allowed(self, tool_name: str) -> bool:
        return "*" in self._allowed_tools or tool_name in self._allowed_tools

    @staticmethod
    def _is_terminated_session_error(error: Exception) -> bool:
        """Return whether an MCP client stream was closed before a tool call."""
        return "session terminated" in str(error).lower()

    async def _ensure_session(self) -> Any:
        """Open an MCP streamable HTTP session if one is not already active."""
        if self._session is not None:
            return self._session

        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise RuntimeError("MCP client package is required for MCP tool bridge") from exc

        stack = AsyncExitStack()
        try:
            read_stream, write_stream, _get_session_id = await stack.enter_async_context(
                streamablehttp_client(
                    self._mcp_url,
                    headers=self._headers,
                    timeout=30,
                    sse_read_timeout=300,
                )
            )
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
        except Exception:
            await stack.aclose()
            raise

        self._exit_stack = stack
        self._session = session
        return session

    async def discover_tools(self) -> list[dict[str, Any]]:
        """Discover available tools from the MCP server.

        Calls the MCP tools/list endpoint and converts the response
        to LangChain-compatible tool schemas.
        """
        try:
            session = await self._ensure_session()
            listed = await session.list_tools()
            raw_tools = getattr(listed, "tools", []) or []
            mcp_tools = [
                tool
                for tool in (_normalize_tool(tool) for tool in raw_tools)
                if self._is_tool_allowed(str(tool.get("name", "")))
            ]
            self._tools = mcp_tools
            return self._to_langchain_tools(mcp_tools)
        except Exception as e:
            # If tool discovery fails, continue without tools
            import logging

            logging.getLogger("llm.mcp_bridge").warning("MCP tool discovery failed: %s", e)
            self._tools = []
            return []

    def _to_langchain_tools(self, mcp_tools: list[dict]) -> list[dict[str, Any]]:
        """Convert MCP tool definitions to OpenAI-style function schemas.

        These are the format LangChain's bind_tools() expects for
        any ChatModel provider.
        """
        langchain_tools = []
        for tool in mcp_tools:
            params = tool.get("inputSchema") or tool.get(
                "input_schema", {"type": "object", "properties": {}}
            )
            schema = {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": params,
                },
            }
            langchain_tools.append(schema)
        return langchain_tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool call by forwarding it to the MCP server.

        Args:
            tool_name: Name of the MCP tool to call.
            arguments: Tool arguments dict.

        Returns:
            The tool result as a string.
        """
        is_memory_tool = tool_name in MEMORY_TOOL_NAMES
        start = time.monotonic()

        try:
            if not self._is_tool_allowed(tool_name):
                result_text = f"Error calling tool {tool_name}: tool is not allowed in this session"
                await self._audit_tool_call(
                    tool_name=tool_name,
                    arguments=arguments or {},
                    result_text=result_text,
                    duration_ms=(time.monotonic() - start) * 1000,
                )
                return result_text

            session = await self._ensure_session()
            try:
                result = await session.call_tool(tool_name, arguments or {})
            except Exception as error:
                if not self._is_terminated_session_error(error):
                    raise
                # Streamable HTTP sessions can be closed by the peer between
                # otherwise valid tool calls. Recreate the session once rather
                # than turning an available MCP tool into a false capability
                # failure for the running agent.
                logger.warning(
                    "MCP session terminated while calling %s; reconnecting once",
                    tool_name,
                )
                await self.close()
                session = await self._ensure_session()
                result = await session.call_tool(tool_name, arguments or {})
            result_text = _call_result_to_text(result)
            await self._audit_tool_call(
                tool_name=tool_name,
                arguments=arguments or {},
                result_text=result_text,
                duration_ms=(time.monotonic() - start) * 1000,
                result_is_error=_call_result_is_error(result),
            )

            if is_memory_tool:
                elapsed_ms = (time.monotonic() - start) * 1000
                log_params = _summarize_memory_tool_params(tool_name, arguments)
                logger.info(
                    "mcp.memory_tool tool=%s params={%s} status=ok duration_ms=%.0f",
                    tool_name,
                    log_params,
                    elapsed_ms,
                )

            return result_text
        except Exception as e:
            await self._audit_tool_call(
                tool_name=tool_name,
                arguments=arguments or {},
                result_text=f"Error calling tool {tool_name}: {e}",
                duration_ms=(time.monotonic() - start) * 1000,
                exception=e,
            )
            if is_memory_tool:
                elapsed_ms = (time.monotonic() - start) * 1000
                log_params = _summarize_memory_tool_params(tool_name, arguments)
                logger.warning(
                    "mcp.memory_tool tool=%s params={%s} status=error duration_ms=%.0f error=%s",
                    tool_name,
                    log_params,
                    elapsed_ms,
                    e,
                )
            logger.error("Error calling MCP tool %s", tool_name, exc_info=e)
            return f"Error calling tool {tool_name}: {e}"

    async def _audit_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result_text: str,
        duration_ms: float,
        exception: Exception | None = None,
        result_is_error: bool = False,
    ) -> None:
        """Best-effort operational audit logging for MCP tool execution."""
        try:
            from lucent.db import init_db
            from lucent.db.tool_audit import ToolAuditRepository, classify_tool_result

            context = dict(self._audit_context)
            context.setdefault("source", "mcp_bridge")
            context.setdefault("tool_namespace", "mcp")
            if self._headers:
                context.setdefault(
                    "session_id",
                    _header_value(self._headers, "X-Lucent-LLM-Session-Id"),
                )
                context.setdefault(
                    "turn_id",
                    _header_value(self._headers, "X-Lucent-LLM-Turn-Id"),
                )
                context.setdefault(
                    "message_id",
                    _header_value(self._headers, "X-Lucent-LLM-Message-Id"),
                )

            if exception is not None:
                status = "failed"
                failure_class = exception.__class__.__name__
                error_message = str(exception)
            else:
                status, failure_class, error_message = classify_tool_result(
                    result_text,
                    is_error=result_is_error,
                )

            pool = await init_db()
            repo = ToolAuditRepository(pool)
            await repo.log_tool_call(
                tool_name=tool_name,
                status=status,
                source=str(context.pop("source", "mcp_bridge")),
                duration_ms=int(duration_ms),
                input_payload=arguments or {},
                output_payload=result_text,
                failure_class=failure_class,
                error_message=error_message,
                context=context,
                metadata={"mcp_url": self._mcp_url},
            )
        except Exception:
            logger.debug("Failed to write tool call audit row", exc_info=True)

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._exit_stack = None
        self._session = None


def _normalize_tool(tool: Any) -> dict[str, Any]:
    """Normalize MCP SDK tool objects and dicts to the bridge's dict shape."""
    if hasattr(tool, "model_dump"):
        data = tool.model_dump(by_alias=True)
    elif isinstance(tool, dict):
        data = dict(tool)
    else:
        data = {
            "name": getattr(tool, "name", None),
            "description": getattr(tool, "description", ""),
            "inputSchema": getattr(tool, "inputSchema", None)
            or getattr(tool, "input_schema", None),
        }

    if "inputSchema" not in data and "input_schema" in data:
        data["inputSchema"] = data["input_schema"]
    if not data.get("inputSchema"):
        data["inputSchema"] = {"type": "object", "properties": {}}
    return data


def _header_value(headers: dict[str, str], name: str) -> str | None:
    """Fetch a header value case-insensitively from bridge config headers."""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value) if value else None
    return None


def _call_result_to_text(result: Any) -> str:
    """Serialize an MCP call result into text for model follow-up messages."""
    content_blocks = getattr(result, "content", None)
    if isinstance(result, dict):
        content_blocks = result.get("content", content_blocks)

    if content_blocks:
        parts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or json.dumps(block, default=str)))
            elif hasattr(block, "text"):
                parts.append(str(block.text))
            elif hasattr(block, "model_dump"):
                parts.append(json.dumps(block.model_dump(mode="json"), default=str))
            else:
                parts.append(str(block))
        return "\n".join(parts)

    if hasattr(result, "model_dump"):
        return json.dumps(result.model_dump(mode="json"), default=str)
    return json.dumps(result, default=str)


def _call_result_is_error(result: Any) -> bool:
    """Return the MCP protocol-level error flag without inspecting result prose."""
    if isinstance(result, dict):
        return result.get("isError") is True or result.get("is_error") is True
    return bool(getattr(result, "isError", False) or getattr(result, "is_error", False))


def _summarize_memory_tool_params(tool_name: str, arguments: dict[str, Any]) -> str:
    """Build a concise, loggable summary of memory tool parameters.

    Extracts the operationally meaningful fields for each tool type while
    omitting large content bodies. Designed for structured log lines.
    """
    parts: list[str] = []

    if tool_name in ("search_memories", "search_memories_full"):
        if q := arguments.get("query"):
            parts.append(f"query={q!r}")
        if t := arguments.get("type"):
            parts.append(f"type={t}")
        if tags := arguments.get("tags"):
            parts.append(f"tags={tags}")
        if lim := arguments.get("limit"):
            parts.append(f"limit={lim}")

    elif tool_name in ("get_memory", "get_memories"):
        if mid := arguments.get("memory_id"):
            parts.append(f"memory_id={mid}")
        if mids := arguments.get("memory_ids"):
            parts.append(f"memory_ids={mids}")

    elif tool_name == "create_memory":
        if t := arguments.get("type"):
            parts.append(f"type={t}")
        if tags := arguments.get("tags"):
            parts.append(f"tags={tags}")
        if imp := arguments.get("importance"):
            parts.append(f"importance={imp}")
        # Log content length, not content (can be very long)
        if content := arguments.get("content"):
            parts.append(f"content_len={len(content)}")

    elif tool_name == "update_memory":
        if mid := arguments.get("memory_id"):
            parts.append(f"memory_id={mid}")
        if tags := arguments.get("tags"):
            parts.append(f"tags={tags}")
        if imp := arguments.get("importance"):
            parts.append(f"importance={imp}")
        if content := arguments.get("content"):
            parts.append(f"content_len={len(content)}")

    elif tool_name == "delete_memory":
        if mid := arguments.get("memory_id"):
            parts.append(f"memory_id={mid}")

    elif tool_name == "get_current_user_context":
        parts.append("(no params)")

    elif tool_name in ("get_existing_tags", "get_tag_suggestions"):
        if q := arguments.get("query"):
            parts.append(f"query={q!r}")

    else:
        # Fallback: log all keys (not values) for unknown memory tools
        parts.append(f"keys={list(arguments.keys())}")

    return ", ".join(parts) if parts else "(no params)"
