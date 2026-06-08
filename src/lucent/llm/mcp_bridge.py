"""MCP Tool Bridge — converts MCP server tools to LangChain tools.

Connects to a Lucent MCP server via HTTP, discovers available tools,
and wraps each as a LangChain tool that the LLM can call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import AsyncExitStack
from datetime import timedelta
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

# Pattern 1, Fix D: explicit per-request read timeout for MCP tool calls.
# Read at call time so operators can tune via env without restarting agents.
# Default 120s covers tail-latency searches without letting a hung server
# block a model turn indefinitely.
_DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS = 120

# Memory-tool read paths (search_memories / search_memories_full / get_*) are
# idempotent and safe to retry once on transient timeout. Pattern 1 evidence
# showed timeout → retry → success as the dominant recovery pattern.
_RETRYABLE_MEMORY_TOOLS = frozenset({
    "search_memories",
    "search_memories_full",
    "get_memory",
    "get_memories",
    "get_existing_tags",
    "get_tag_suggestions",
    "get_current_user_context",
})


def _mcp_request_timeout_seconds() -> int:
    raw = os.environ.get("LUCENT_MCP_REQUEST_TIMEOUT_SECONDS")
    if not raw:
        return _DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid LUCENT_MCP_REQUEST_TIMEOUT_SECONDS=%r; using default %ds",
            raw,
            _DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS,
        )
        return _DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS


def _mcp_retry_backoff_seconds() -> float:
    raw = os.environ.get("LUCENT_MCP_RETRY_BACKOFF_SECONDS")
    if not raw:
        return 0.5
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.5


class MCPTimeoutError(TimeoutError):
    """Raised when an MCP tool call exceeds the per-request read timeout.

    Distinct subclass so the tool-audit classifier can label these as
    ``mcp_timeout`` instead of a generic ``tool_error``.
    """


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

        Pattern 1 hardening:
        - Passes an explicit ``read_timeout_seconds`` on every call (default
          120s, ``LUCENT_MCP_REQUEST_TIMEOUT_SECONDS``) so the SDK's per-call
          deadline is bounded even when the server hangs after the SSE
          handshake.
        - For idempotent memory read tools (``search_memories``,
          ``search_memories_full``, ``get_memory*``, ``get_*_tags``,
          ``get_current_user_context``), a single timeout failure is retried
          once after ``LUCENT_MCP_RETRY_BACKOFF_SECONDS`` (default 0.5s).
          Non-idempotent tools (create/update/delete) are NOT retried.
        - Timeout failures surface as a structured ``Error calling tool ...:
          MCPTimeoutError`` string with an ``mcp_timeout`` failure class hint
          in the audit row, instead of the generic ``tool_error``.
        """
        is_memory_tool = tool_name in MEMORY_TOOL_NAMES
        is_retryable = tool_name in _RETRYABLE_MEMORY_TOOLS
        start = time.monotonic()
        timeout_seconds = _mcp_request_timeout_seconds()
        read_timeout = timedelta(seconds=timeout_seconds)

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
            attempts = 2 if is_retryable else 1
            last_exc: BaseException | None = None
            result = None
            for attempt in range(1, attempts + 1):
                try:
                    result = await self._invoke_with_timeout(
                        session, tool_name, arguments or {}, read_timeout
                    )
                    last_exc = None
                    break
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    last_exc = exc
                    if attempt >= attempts:
                        break
                    backoff = _mcp_retry_backoff_seconds()
                    logger.warning(
                        "mcp.timeout tool=%s attempt=%d/%d backoff=%.2fs",
                        tool_name,
                        attempt,
                        attempts,
                        backoff,
                    )
                    if backoff > 0:
                        await asyncio.sleep(backoff)

            if last_exc is not None:
                # Convert to a typed exception so audit/classification picks it
                # up as ``MCPTimeoutError`` rather than a bare TimeoutError.
                raise MCPTimeoutError(
                    f"MCP tool {tool_name} timed out after {timeout_seconds}s "
                    f"({attempts} attempt(s))"
                ) from last_exc

            result_text = _call_result_to_text(result)
            await self._audit_tool_call(
                tool_name=tool_name,
                arguments=arguments or {},
                result_text=result_text,
                duration_ms=(time.monotonic() - start) * 1000,
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
                    "mcp.memory_tool tool=%s params={%s} status=error "
                    "duration_ms=%.0f error_class=%s error=%s",
                    tool_name,
                    log_params,
                    elapsed_ms,
                    e.__class__.__name__,
                    e,
                )
            logger.error("Error calling MCP tool %s", tool_name, exc_info=e)
            return f"Error calling tool {tool_name}: {e}"

    async def _invoke_with_timeout(
        self,
        session: Any,
        tool_name: str,
        arguments: dict[str, Any],
        read_timeout: timedelta,
    ) -> Any:
        """Call ``session.call_tool`` with an explicit per-call read timeout.

        Older fakes/mock sessions may not accept the ``read_timeout_seconds``
        keyword, so we fall back to a positional call and wrap it in
        ``asyncio.wait_for`` to preserve the bound.
        """
        try:
            return await session.call_tool(
                tool_name, arguments, read_timeout_seconds=read_timeout
            )
        except TypeError:
            # Session.call_tool signature did not accept the kwarg. Enforce
            # the bound externally so the timeout contract still holds.
            return await asyncio.wait_for(
                session.call_tool(tool_name, arguments),
                timeout=read_timeout.total_seconds(),
            )

    async def _audit_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result_text: str,
        duration_ms: float,
        exception: Exception | None = None,
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
                    result_text, tool_name=tool_name
                )

            # Observability hook (Patterns 1 + 2): emit a structured warning
            # whenever an MCP tool call ends in a remediation-relevant failure
            # class so future regressions surface in log aggregation alongside
            # the tool_call_audit_log row that feeds analyze_tool_failure_patterns.
            _OBSERVED_FAILURE_CLASSES = {
                "mcp_timeout",
                "db_pool_acquire_timeout",
                "auth_error",
                "forbidden",
                "rate_limited",
                "invalid_input",
            }
            if status != "ok" and failure_class in _OBSERVED_FAILURE_CLASSES:
                logger.warning(
                    "mcp_tool_failure tool=%s failure_class=%s duration_ms=%d",
                    tool_name,
                    failure_class,
                    int(duration_ms),
                    extra={
                        "tool_name": tool_name,
                        "failure_class": failure_class,
                        "duration_ms": int(duration_ms),
                        "mcp_url": self._mcp_url,
                        "event": "mcp_tool_failure",
                    },
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
