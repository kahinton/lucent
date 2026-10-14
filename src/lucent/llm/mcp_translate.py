"""Translate Lucent's MCP config into engine-native MCP server descriptions.

Lucent composes a per-session ``mcp_config`` dict (see
``lucent.mcp_config.build_internal_mcp_server`` and
``api/routers/chat._build_agent_mcp_config``)::

    {"<name>": {"type": "http", "url": ..., "headers": {...},
                "tools": ["*"], "internal": True?}}
    {"<name>": {"type": "stdio", "command": ..., "args": [...], "env": {...}}}

Lucent-only keys (``internal``, ``tools``, ``exclude_tools``) are dropped before
handing the config to an engine. The shapes line up closely with Claude's
``McpHttpServerConfig`` / ``McpStdioServerConfig`` and Codex's ``mcp_servers``
table, so translation is mostly key cleaning.
"""

from __future__ import annotations

from typing import Any

_LUCENT_ONLY_KEYS = ("internal", "tools", "exclude_tools")


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k not in _LUCENT_ONLY_KEYS}


def _server_type(entry: dict[str, Any]) -> str:
    return str(entry.get("type") or ("stdio" if "command" in entry else "http")).lower()


def to_claude_mcp_servers(mcp_config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Translate to Claude Agent SDK ``mcp_servers`` (Mcp*ServerConfig shapes)."""
    servers: dict[str, dict[str, Any]] = {}
    for name, entry in (mcp_config or {}).items():
        if not isinstance(entry, dict):
            continue
        kind = _server_type(entry)
        if kind == "stdio":
            server: dict[str, Any] = {"type": "stdio", "command": entry.get("command", "")}
            if entry.get("args"):
                server["args"] = list(entry["args"])
            if entry.get("env"):
                server["env"] = dict(entry["env"])
        elif kind == "sse":
            server = {"type": "sse", "url": entry.get("url", "")}
            if entry.get("headers"):
                server["headers"] = dict(entry["headers"])
        else:  # http
            server = {"type": "http", "url": entry.get("url", "")}
            if entry.get("headers"):
                server["headers"] = dict(entry["headers"])
        servers[name] = _clean(server)
    return servers


def to_codex_mcp_servers(mcp_config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Translate to Codex ``mcp_servers`` config entries.

    Codex uses ``command``/``args``/``env`` for stdio servers and ``url`` plus
    ``http_headers`` for remote servers.
    """
    servers: dict[str, dict[str, Any]] = {}
    for name, entry in (mcp_config or {}).items():
        if not isinstance(entry, dict):
            continue
        kind = _server_type(entry)
        if kind == "stdio":
            server: dict[str, Any] = {"command": entry.get("command", "")}
            if entry.get("args"):
                server["args"] = list(entry["args"])
            if entry.get("env"):
                server["env"] = dict(entry["env"])
            servers[name] = server
        else:  # http / sse
            server = {"url": entry.get("url", "")}
            if entry.get("headers"):
                server["http_headers"] = dict(entry["headers"])
            servers[name] = server
    return servers
