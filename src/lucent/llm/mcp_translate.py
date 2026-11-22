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


def to_claude_allowed_tools(mcp_config: dict[str, Any] | None) -> list[str]:
    """Translate Lucent's per-server tool policy into Claude Code allow rules.

    ``mcp_servers`` alone only *attaches* a server. Claude Code additionally
    requires each MCP tool to be allowlisted, either by the session's permission
    mode or by ``allowed_tools``; a tool that is attached but not allowed is
    reported to the model as ``... but you haven't granted it yet.`` and the call
    fails. That is precisely the failure a dispatched daemon task hit: the
    carrier declared ``tools: ["*"]`` (see ``lucent.tool_policy``) and the
    policy was dropped here, so every Lucent memory-server call was denied.

    ``"*"`` becomes a server-wide rule (``mcp__<server>``); named tools become
    ``mcp__<server>__<tool>``. ``exclude_tools`` is not expressed as a rule —
    the MCP server itself refuses excluded tools, and an allowlist cannot
    express "everything except".

    Only Lucent's own internal server is allowlisted. An external connector's
    tools must keep their existing approval semantics: an allow rule is a
    blanket pre-approval that also bypasses ``can_use_tool``, which would
    silently remove the chat approval prompt for those servers.
    """
    from lucent.llm.tool_approval import is_internal_mcp_server

    allowed: list[str] = []
    for name, entry in (mcp_config or {}).items():
        if not isinstance(entry, dict) or not is_internal_mcp_server(name):
            continue
        tools = entry.get("tools")
        if not isinstance(tools, list) or not tools:
            continue
        if "*" in tools:
            # A wildcard already covers every tool on the server; the daemon
            # also appends managed-tool names to the same list, and emitting
            # them again as individual rules would only be noise.
            if f"mcp__{name}" not in allowed:
                allowed.append(f"mcp__{name}")
            continue
        for tool in tools:
            rule = f"mcp__{name}__{tool}"
            if rule not in allowed:
                allowed.append(rule)
    return allowed


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
