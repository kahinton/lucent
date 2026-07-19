"""Secret-safe observability helpers for task MCP tool activity."""

from __future__ import annotations

import re

from lucent.tool_policy import (
    BASE_TASK_MEMORY_SERVER_TOOLS,
    CAPABILITY_ACTIVATION_AGENT_TYPES,
    DEFINITION_ACTIVATION_TOOLS,
    WORK_ACTIVATION_TOOLS,
    memory_server_tools_for_task,
)

_SECRET_PATTERNS = re.compile(
    r"hs_[A-Za-z0-9_\-]{8,}"
    r"|vault:v1:[A-Za-z0-9+/=]{4,}"
    r"|hvs\.[A-Za-z0-9]{20,}"
    r"|[A-Fa-f0-9]{40,}"
    r"|[A-Za-z0-9+/]{40,}={0,2}"
)

_MEMORY_TOOL_NAMES = frozenset({
    "search_memories", "search_memories_full", "get_memory", "get_memories",
    "get_current_user_context", "create_memory", "update_memory", "delete_memory",
    "get_existing_tags", "get_tag_suggestions", "export_memories",
})
_MEMORY_SEARCH_TOOLS = frozenset({
    "search_memories", "search_memories_full", "get_memory", "get_memories",
    "get_current_user_context",
})
_MEMORY_CAPTURE_TOOLS = frozenset({"create_memory", "update_memory"})
_NON_OPERATIONAL_TOOL_NAMES = frozenset({"report_intent"})

_BASE_TASK_MEMORY_SERVER_TOOLS = sorted(BASE_TASK_MEMORY_SERVER_TOOLS)
_DEFINITION_ACTIVATION_TOOLS = sorted(DEFINITION_ACTIVATION_TOOLS)
_WORK_ACTIVATION_TOOLS = sorted(WORK_ACTIVATION_TOOLS)
_CAPABILITY_ACTIVATION_AGENT_TYPES = CAPABILITY_ACTIVATION_AGENT_TYPES
_TASK_MEMORY_SERVER_TOOLS = sorted(
    set(_BASE_TASK_MEMORY_SERVER_TOOLS)
    | set(_DEFINITION_ACTIVATION_TOOLS)
    | set(_WORK_ACTIVATION_TOOLS)
)

_HANDOFF_TOOL_REQUIRED_SIGNALS = frozenset({
    "send_handoff", "send a handoff", "create a handoff", "provide a handoff",
    "as a handoff", "handoff to the user", "post to handoffs",
})
_HANDOFF_TOOL_REQUIRED_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(?:send|create|provide|post|publish|share|deliver|return|write)\b.{0,120}\bhandoff\b",
        r"\bhandoff\b.{0,80}\b(?:to|for)\b.{0,40}\b(?:user|human|person|owner|requester)\b",
    )
)


def _redact_secrets(text: str) -> str:
    """Replace known secret patterns with a safe placeholder."""
    return _SECRET_PATTERNS.sub("[REDACTED]", text)


def _normalize_tool_name(tool_name: str | None) -> str | None:
    """Normalize Copilot SDK MCP tool names to Lucent tool names."""
    if not tool_name:
        return None
    if tool_name.startswith("memory-server-"):
        return tool_name[len("memory-server-"):]
    return tool_name


def _is_memory_server_tool(tool_name: str | None) -> bool:
    """Return whether a tool is served by Lucent's memory-server MCP."""
    normalized = _normalize_tool_name(tool_name)
    return bool(tool_name and (
        tool_name.startswith("memory-server-")
        or normalized in _MEMORY_TOOL_NAMES
        or normalized in _TASK_MEMORY_SERVER_TOOLS
    ))


def _is_operational_tool_call(entry: dict) -> bool:
    """Return whether a tool event represents real tool execution."""
    tool = _normalize_tool_name(entry.get("tool") or entry.get("raw_tool"))
    return bool(tool and tool not in _NON_OPERATIONAL_TOOL_NAMES)


def _memory_server_tools_for_task(
    agent_type: str | None,
    title: str | None = None,
    request_title: str | None = None,
    description: str | None = None,
) -> list[str]:
    """Return memory-server tools that are appropriate for a dispatched task."""
    return memory_server_tools_for_task(agent_type, title, request_title, description)


def _summarize_memory_tool_params(tool_name: str, arguments: dict) -> str:
    """Build a concise, loggable summary without emitting content bodies."""
    tool_name = _normalize_tool_name(tool_name) or tool_name
    parts: list[str] = []
    if tool_name in ("search_memories", "search_memories_full"):
        for key in ("query", "type", "tags", "limit"):
            if (value := arguments.get(key)) is not None:
                parts.append(f"{key}={value!r}" if key == "query" else f"{key}={value}")
    elif tool_name in ("get_memory", "get_memories", "delete_memory"):
        for key in ("memory_id", "memory_ids"):
            if value := arguments.get(key):
                parts.append(f"{key}={value}")
    elif tool_name in ("create_memory", "update_memory"):
        for key in ("memory_id", "type", "tags", "importance"):
            if value := arguments.get(key):
                parts.append(f"{key}={value}")
        if content := arguments.get("content"):
            parts.append(f"content_len={len(content)}")
    elif tool_name == "send_handoff":
        for key in ("title", "interaction_type"):
            if value := arguments.get(key):
                parts.append(f"{key}={value!r}" if key == "title" else f"{key}={value}")
        if arguments.get("requires_response"):
            parts.append("requires_response=True")
        if body := arguments.get("body"):
            parts.append(f"body_len={len(body)}")
    elif tool_name in ("get_current_user_context",):
        pass
    elif tool_name in ("get_existing_tags", "get_tag_suggestions"):
        if query := arguments.get("query"):
            parts.append(f"query={query!r}")
    else:
        parts.append(f"keys={list(arguments.keys())}")
    return ", ".join(parts) if parts else "(no params)"


def _build_mcp_tool_summary(tracker: list[dict]) -> str:
    """Summarize memory-tool use during one agent session."""
    if not tracker:
        return "No memory-server tools were called during this session."
    counts: dict[str, int] = {}
    for entry in tracker:
        tool = _normalize_tool_name(entry["tool"]) or entry["tool"]
        counts[tool] = counts.get(tool, 0) + 1
    searches = sum(counts.get(tool, 0) for tool in _MEMORY_SEARCH_TOOLS)
    captures = sum(counts.get(tool, 0) for tool in _MEMORY_CAPTURE_TOOLS)
    breakdown = ", ".join(f"{tool}={count}" for tool, count in sorted(counts.items()))
    return (
        f"Memory tool calls: {len(tracker)} total "
        f"(search={searches}, capture={captures}). Breakdown: {breakdown}"
    )
