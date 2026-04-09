"""Lightweight MCP bridge server for sandbox containers.

Runs inside a sandbox and proxies selected tool calls back to Lucent's REST API.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger("sandbox.mcp_bridge")

# Memory-server tools tracked for observability
_MEMORY_TOOL_NAMES = frozenset({
    "create_memory", "search_memories", "update_memory",
})


def _tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "create_memory",
            "description": "Create a memory in Lucent",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "importance": {"type": "integer"},
                    "related_memory_ids": {"type": "array", "items": {"type": "string"}},
                    "metadata": {"type": "object"},
                    "shared": {"type": "boolean"},
                },
                "required": ["type", "content"],
            },
        },
        {
            "name": "search_memories",
            "description": "Search memories in Lucent",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "username": {"type": "string"},
                    "type": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "importance_min": {"type": "integer"},
                    "importance_max": {"type": "integer"},
                    "created_after": {"type": "string"},
                    "created_before": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "update_memory",
            "description": "Update an existing memory",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "importance": {"type": "integer"},
                    "related_memory_ids": {"type": "array", "items": {"type": "string"}},
                    "metadata": {"type": "object"},
                    "expected_version": {"type": "integer"},
                },
                "required": ["memory_id"],
            },
        },
        {
            "name": "log_task_event",
            "description": "Log an event on a tracked task",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "event_type": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["event_type"],
            },
        },
        {
            "name": "link_task_memory",
            "description": "Link a memory to a tracked task",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "memory_id": {"type": "string"},
                    "relation": {"type": "string"},
                },
                "required": ["memory_id"],
            },
        },
    ]


class BridgeServer:
    def __init__(self, api_url: str, api_key: str, task_id: str | None):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.task_id = task_id
        self._specs = {tool["name"]: tool for tool in _tool_specs()}

    def handle_tool_call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self._specs:
            raise ValueError(f"Unknown tool: {name}")

        is_memory_tool = name in _MEMORY_TOOL_NAMES
        start = time.monotonic()

        try:
            if name == "create_memory":
                result = self._proxy("POST", "/memories", arguments)
            elif name == "search_memories":
                result = self._proxy("POST", "/search", arguments)
            elif name == "update_memory":
                memory_id = arguments.get("memory_id")
                if not memory_id:
                    raise ValueError("memory_id is required")
                payload = {k: v for k, v in arguments.items() if k != "memory_id"}
                result = self._proxy("PATCH", f"/memories/{memory_id}", payload)
            elif name == "log_task_event":
                task_id = self._resolve_task_id(arguments)
                payload = {
                    "event_type": arguments.get("event_type"),
                    "detail": arguments.get("detail", ""),
                }
                result = self._proxy("POST", f"/requests/tasks/{task_id}/events", payload)
            elif name == "link_task_memory":
                task_id = self._resolve_task_id(arguments)
                payload = {
                    "memory_id": arguments.get("memory_id"),
                    "relation": arguments.get("relation", "created"),
                }
                result = self._proxy("POST", f"/requests/tasks/{task_id}/memories", payload)
            else:
                raise ValueError(f"Unsupported tool: {name}")

            if is_memory_tool:
                elapsed_ms = (time.monotonic() - start) * 1000
                logger.info(
                    "mcp.memory_tool tool=%s task=%s status=ok duration_ms=%.0f",
                    name,
                    self.task_id or "unknown",
                    elapsed_ms,
                )
            return result
        except Exception:
            if is_memory_tool:
                elapsed_ms = (time.monotonic() - start) * 1000
                logger.warning(
                    "mcp.memory_tool tool=%s task=%s status=error duration_ms=%.0f",
                    name,
                    self.task_id or "unknown",
                    elapsed_ms,
                )
            raise

    def tool_list(self) -> list[dict[str, Any]]:
        return list(self._specs.values())

    def _resolve_task_id(self, arguments: dict[str, Any]) -> str:
        passed = arguments.get("task_id")
        if passed and self.task_id and passed != self.task_id:
            raise ValueError("task_id does not match bridge scope")
        task_id = passed or self.task_id
        if not task_id:
            raise ValueError("task_id is required")
        return task_id

    def _proxy(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        req = urllib.request.Request(
            f"{self.api_url}{path}",
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            data=json.dumps(payload).encode("utf-8"),
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.error("API error %d: %s", exc.code, detail)
            raise RuntimeError(f"API error {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Connection error: {exc.reason}") from exc


class MCPBridgeHandler(BaseHTTPRequestHandler):
    bridge: BridgeServer | None = None

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json({"status": "ok"})
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self._send_json({"error": "not found"}, status=404)
            return

        bridge = self.bridge
        if bridge is None:
            self._send_json({"error": "bridge not configured"}, status=500)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8"))
            request_id = payload.get("id")
            method = payload.get("method")
            params = payload.get("params") or {}

            if method == "tools/list":
                result = {"tools": bridge.tool_list()}
                self._send_json({"jsonrpc": "2.0", "id": request_id, "result": result})
                return

            if method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                data = bridge.handle_tool_call(tool_name, arguments)
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"content": [{"type": "text", "text": json.dumps(data)}]},
                    }
                )
                return

            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
        except Exception as exc:
            logger.exception("Bridge request failed")
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32000, "message": "Internal server error"},
                }
            )

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        logger.debug("%s - %s", self.address_string(), format % args)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lucent sandbox MCP bridge")
    parser.add_argument("--host", default=os.environ.get("LUCENT_SANDBOX_MCP_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LUCENT_SANDBOX_MCP_PORT", "8765")),
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("LUCENT_API_URL", "http://host.docker.internal:8766/api"),
    )
    parser.add_argument("--api-key", default=os.environ.get("LUCENT_SANDBOX_MCP_API_KEY", ""))
    parser.add_argument("--task-id", default=os.environ.get("LUCENT_SANDBOX_TASK_ID"))
    args = parser.parse_args()

    if not args.api_key:
        raise RuntimeError("LUCENT_SANDBOX_MCP_API_KEY is required")

    logging.basicConfig(
        level=os.environ.get("LUCENT_SANDBOX_MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bridge = BridgeServer(api_url=args.api_url, api_key=args.api_key, task_id=args.task_id)
    MCPBridgeHandler.bridge = bridge

    server = ThreadingHTTPServer((args.host, args.port), MCPBridgeHandler)
    logger.info("Sandbox MCP bridge listening on %s:%s", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
