"""MCP Tool Bridge — converts MCP server tools to LangChain tools.

Connects to a Lucent MCP server via HTTP, discovers available tools,
and wraps each as a LangChain tool that the LLM can call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from lucent.url_validation import SSRFError, validate_url

logger = logging.getLogger(__name__)


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
        skip_url_validation: bool = False,
    ):
        if not skip_url_validation:
            validate_url(mcp_url, purpose="MCP bridge")
        self._mcp_url = mcp_url
        self._headers = headers or {}
        self._tools: list[dict[str, Any]] = []
        self._client = httpx.AsyncClient(timeout=30.0)

    async def discover_tools(self) -> list[dict[str, Any]]:
        """Discover available tools from the MCP server.

        Calls the MCP tools/list endpoint and converts the response
        to LangChain-compatible tool schemas.
        """
        try:
            response = await self._client.post(
                self._mcp_url,
                headers={**self._headers, "Content-Type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                },
            )
            response.raise_for_status()
            data = response.json()

            mcp_tools = data.get("result", {}).get("tools", [])
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
            schema = {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
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
        try:
            response = await self._client.post(
                self._mcp_url,
                headers={**self._headers, "Content-Type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                },
            )
            response.raise_for_status()
            data = response.json()

            result = data.get("result", {})
            # MCP returns content as a list of content blocks
            content_blocks = result.get("content", [])
            if content_blocks:
                return "\n".join(
                    block.get("text", json.dumps(block))
                    for block in content_blocks
                    if isinstance(block, dict)
                )
            return json.dumps(result)
        except Exception as e:
            logger.error("Error calling MCP tool %s", tool_name, exc_info=e)
            return f"Error calling tool {tool_name}: {e}"

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
