#!/usr/bin/env python3
"""Inject Lucent memory context into VS Code/Claude hook turns.

The script is intentionally quiet on configuration or network failure so hooks
never interrupt normal chat. Set LUCENT_API_KEY in the VS Code/Claude environment
to enable memory lookup against the local Lucent REST API.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


MAX_QUERY_CHARS = 360
MAX_MEMORIES = 3
MAX_CONTENT_CHARS = 500

FILE_KEYS = {
    "file",
    "filepath",
    "file_path",
    "filename",
    "path",
    "pathspec",
    "uri",
    "url",
    "absolute_path",
    "relative_path",
    "target_path",
    "target_paths",
    "include_pattern",
    "includepattern",
}


def main() -> int:
    event = _read_event()
    api_key = _get_api_key()
    if not api_key:
        return 0

    query = _query_for_event(event)
    if not query:
        return 0

    memories = _search_memories(query, api_key)
    if not memories:
        return 0

    hook_event_name = _event_name(event)
    context = _format_context(query, memories)
    print(json.dumps({
        "additionalContext": context,
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": context,
        }
    }))
    return 0


def _read_event() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def _get_api_key() -> str:
    env_values = _load_env_files()
    return (
        os.environ.get("LUCENT_API_KEY")
        or env_values.get("LUCENT_API_KEY")
        or _api_key_from_mcp_configs()
        or ""
    ).strip()


def _load_env_files() -> dict[str, str]:
    values: dict[str, str] = {}
    for env_path in (Path.cwd() / ".env", Path.cwd() / ".env.local"):
        if not env_path.exists():
            continue
        try:
            lines = env_path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            if key != "LUCENT_API_KEY":
                continue
            values[key] = value.strip().strip('"\'')
    return values


def _api_key_from_mcp_configs() -> str:
    for config_path in (
        Path.cwd() / ".vscode" / "mcp.json",
        Path.cwd() / ".github" / "plugin" / ".mcp.json",
    ):
        try:
            data = json.loads(config_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        server_blocks = []
        for key in ("servers", "mcpServers"):
            value = data.get(key)
            if isinstance(value, dict):
                server_blocks.extend(value.values())
        for server in server_blocks:
            if not isinstance(server, dict):
                continue
            headers = server.get("headers")
            if not isinstance(headers, dict):
                continue
            auth = str(headers.get("Authorization") or "").strip()
            if not auth.lower().startswith("bearer "):
                continue
            token = auth.split(None, 1)[1].strip()
            if token.startswith("${"):
                continue
            return token
    return ""


def _event_name(event: dict[str, Any]) -> str:
    return str(
        event.get("hookEventName")
        or event.get("hook_event_name")
        or ""
    )


def _query_for_event(event: dict[str, Any]) -> str:
    event_name = _event_name(event)
    if event_name == "SessionStart":
        cwd = str(event.get("cwd") or os.getcwd())
        repo_name = Path(cwd).name or "lucent"
        return f"{repo_name} repository technical workflow architecture"

    if event_name in {"PostToolUse", "PreToolUse"}:
        tool_input = (
            event.get("tool_input")
            or event.get("toolInput")
            or event.get("tool_args")
            or event.get("toolArgs")
            or {}
        )
        refs = _extract_file_refs(tool_input)
        if refs:
            return " ".join(refs[:5])[:MAX_QUERY_CHARS]

    return ""


def _clean_query(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 8:
        return ""
    return text[:MAX_QUERY_CHARS]


def _file_refs_from_text(text: str) -> list[str]:
    refs = []
    for match in re.finditer(r"#file:([^\s,;]+)|`([^`]+)`", text):
        candidate = match.group(1) or match.group(2) or ""
        if _looks_like_file_reference(candidate):
            refs.append(candidate)
    return _dedupe(refs)


def _extract_file_refs(value: Any, key: str | None = None) -> list[str]:
    refs: list[str] = []
    normalized_key = (key or "").replace("-", "_").lower()
    key_is_fileish = (
        normalized_key in FILE_KEYS
        or "path" in normalized_key
        or "file" in normalized_key
    )

    if isinstance(value, dict):
        for child_key, child_value in value.items():
            refs.extend(_extract_file_refs(child_value, str(child_key)))
    elif isinstance(value, list):
        for child in value:
            refs.extend(_extract_file_refs(child, key))
    elif isinstance(value, str) and key_is_fileish:
        refs.extend(_split_file_candidates(value))

    return _dedupe(refs)


def _split_file_candidates(value: str) -> list[str]:
    candidates = re.split(r"[\n,;]+", value)
    return [candidate.strip() for candidate in candidates if _looks_like_file_reference(candidate)]


def _looks_like_file_reference(value: str) -> bool:
    value = value.strip().strip('"\'')
    if not value or len(value) > 300:
        return False
    if value.startswith(("http://", "https://")):
        return False
    suffix = Path(value).suffix
    return bool(suffix or "/" in value or value.startswith("."))


def _search_memories(query: str, api_key: str) -> list[dict[str, Any]]:
    memories = _search_memories_with_type(query, api_key, "technical")
    if memories:
        return memories
    return _search_memories_with_type(query, api_key, None)


def _search_memories_with_type(
    query: str,
    api_key: str,
    memory_type: str | None,
) -> list[dict[str, Any]]:
    base_url = os.environ.get("LUCENT_API_BASE", "http://localhost:8766/api").rstrip("/")
    request_payload: dict[str, Any] = {
        "query": query,
        "limit": MAX_MEMORIES,
        "include_archived": False,
    }
    if memory_type:
        request_payload["type"] = memory_type
    payload = json.dumps(request_payload).encode()
    request = urllib.request.Request(
        f"{base_url}/search/full",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode())
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return []
    memories = data.get("memories") if isinstance(data, dict) else []
    return memories if isinstance(memories, list) else []


def _format_context(query: str, memories: list[dict[str, Any]]) -> str:
    lines = [
        "Lucent found relevant accessible memories for this turn.",
        f"Query: {query}",
        "",
    ]
    for memory in memories[:MAX_MEMORIES]:
        memory_id = str(memory.get("id") or "")[:8]
        tags = ", ".join(str(tag) for tag in (memory.get("tags") or [])[:5])
        content = _single_line(str(memory.get("content") or ""))[:MAX_CONTENT_CHARS]
        prefix = f"- {memory_id}"
        if tags:
            prefix += f" [{tags}]"
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines).strip()


def _single_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
