#!/usr/bin/env python3
"""VS Code/Copilot hook that injects Lucent memories for file-related tool calls.

The hook is deliberately best-effort: if Lucent is not running, an API key is not
configured, the event is not file-related, or search fails, it exits successfully
without output. That keeps normal agent work fast and non-fragile while giving
Lucent instances automatic memory context when local credentials are available.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

FILE_ARGUMENT_KEYS = frozenset(
    {
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
        "query",
    }
)

FILE_TOOL_HINTS = (
    "file",
    "read",
    "edit",
    "write",
    "grep",
    "search",
    "open",
    "notebook",
)

MAX_FILE_REFS = 8
MAX_QUERIED_REFS = 3
MAX_MEMORIES = 3
DEFAULT_BASE_URL = "http://localhost:8766"
PLACEHOLDER_TOKENS = ("YOUR_API_KEY", "CHANGE_ME", "REPLACE_ME", "<", ">")


def main() -> int:
    try:
        payload = _read_stdin_json()
        tool_name, arguments = _extract_tool_call(payload)
        file_refs = extract_file_references(tool_name, arguments)
        if not file_refs:
            return 0

        config = discover_lucent_config()
        if not config.get("api_key"):
            _debug("No Lucent API key available for memory hook")
            return 0

        memories = search_relevant_memories(
            base_url=str(config.get("base_url") or DEFAULT_BASE_URL),
            api_key=str(config["api_key"]),
            file_refs=file_refs,
            limit=MAX_MEMORIES,
        )
        if not memories:
            return 0

        message = format_system_message(file_refs, memories)
        print(json.dumps({"continue": True, "systemMessage": message}))
        return 0
    except Exception as exc:  # pragma: no cover - defensive no-op path
        _debug(f"Memory hook failed: {exc}")
        return 0


def _read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _extract_tool_call(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    tool_name = _first_string(
        payload,
        "tool_name",
        "toolName",
        "tool",
        "name",
    )
    arguments = _first_dict(
        payload,
        "arguments",
        "args",
        "input",
        "tool_input",
        "toolInput",
        "parameters",
    )

    for key in ("tool", "toolCall", "tool_call", "message"):
        nested = payload.get(key)
        if not isinstance(nested, dict):
            continue
        if tool_name is None:
            tool_name = _first_string(nested, "name", "tool_name", "toolName")
        if not arguments:
            arguments = _first_dict(
                nested,
                "arguments",
                "args",
                "input",
                "tool_input",
                "toolInput",
                "parameters",
            )

    return tool_name, arguments


def extract_file_references(tool_name: str | None, arguments: dict[str, Any] | None) -> list[str]:
    if not arguments:
        return []

    tool_is_fileish = any(hint in (tool_name or "").lower() for hint in FILE_TOOL_HINTS)
    refs: list[str] = []

    def visit(value: Any, key: str | None = None) -> None:
        normalized_key = (key or "").replace("-", "_").lower()
        key_is_fileish = (
            normalized_key in FILE_ARGUMENT_KEYS
            or "path" in normalized_key
            or "file" in normalized_key
        )
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key)
            return
        if not isinstance(value, str):
            return
        if not (tool_is_fileish or key_is_fileish):
            return
        for candidate in _split_path_candidates(value):
            if _looks_like_file_reference(candidate):
                refs.append(candidate)

    visit(arguments)
    return _dedupe_strings(refs)[:MAX_FILE_REFS]


def discover_lucent_config() -> dict[str, str | None]:
    api_key = _first_env(
        "LUCENT_API_KEY",
        "LUCENT_MCP_API_KEY",
        "MEMORY_SERVER_API_KEY",
        "LUCENT_MEMORY_API_KEY",
    )
    base_url = _first_env(
        "LUCENT_BASE_URL",
        "LUCENT_URL",
        "LUCENT_API_URL",
        "LUCENT_SERVER_URL",
    )

    if base_url and base_url.endswith("/mcp"):
        base_url = base_url[: -len("/mcp")]

    for config_path in _candidate_mcp_config_paths():
        if api_key and base_url:
            break
        discovered = _read_mcp_config(config_path)
        if not discovered:
            continue
        api_key = api_key or discovered.get("api_key")
        base_url = base_url or discovered.get("base_url")

    if api_key and api_key.startswith("Bearer "):
        api_key = api_key[7:]
    if api_key and not _is_real_secret(api_key):
        api_key = None

    return {
        "api_key": api_key,
        "base_url": (base_url or DEFAULT_BASE_URL).rstrip("/"),
    }


def search_relevant_memories(
    *,
    base_url: str,
    api_key: str,
    file_refs: list[str],
    limit: int = MAX_MEMORIES,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in file_refs[:MAX_QUERIED_REFS]:
        for query in _queries_for_file_ref(ref):
            if len(found) >= limit:
                break
            response = _post_json(
                f"{base_url.rstrip('/')}/api/search/full",
                {
                    "query": query,
                    "type": "technical",
                    "limit": limit,
                    "include_archived": False,
                },
                api_key=api_key,
                timeout=2.0,
            )
            for memory in response.get("memories", []):
                if not isinstance(memory, dict):
                    continue
                memory_id = str(memory.get("id") or "")
                if not memory_id or memory_id in seen:
                    continue
                seen.add(memory_id)
                found.append(memory)
                if len(found) >= limit:
                    break
        if len(found) >= limit:
            break
    return found


def format_system_message(file_refs: list[str], memories: list[dict[str, Any]]) -> str:
    lines = [
        "Lucent memory hook context:",
        "Relevant accessible memories for files referenced by this tool call:",
        *(f"- `{ref}`" for ref in file_refs[:MAX_QUERIED_REFS]),
        "",
    ]
    for memory in memories[:MAX_MEMORIES]:
        tags = ", ".join((memory.get("tags") or [])[:5])
        content = _single_line(str(memory.get("content") or ""))[:500]
        memory_id = str(memory.get("id") or "")[:8]
        prefix = f"- {memory_id}" if memory_id else "- memory"
        if tags:
            prefix += f" [{tags}]"
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines).strip()


def _candidate_mcp_config_paths() -> list[Path]:
    paths: list[Path] = []
    env_path = os.environ.get("LUCENT_MCP_CONFIG")
    if env_path:
        paths.append(Path(env_path).expanduser())
    cwd = Path.cwd()
    paths.extend(
        [
            cwd / ".vscode" / "mcp.json",
            cwd / ".github" / "plugin" / ".mcp.json",
        ]
    )
    return paths


def _read_mcp_config(path: Path) -> dict[str, str] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return None

    preferred_names = ("memory-server", "lucent", "hindsight")
    ordered = [
        *(servers.get(name) for name in preferred_names if isinstance(servers.get(name), dict)),
        *(value for value in servers.values() if isinstance(value, dict)),
    ]
    for server in ordered:
        url = str(server.get("url") or "").strip()
        headers = server.get("headers") if isinstance(server.get("headers"), dict) else {}
        authorization = str(headers.get("Authorization") or headers.get("authorization") or "")
        api_key = authorization[7:] if authorization.startswith("Bearer ") else authorization
        if url or api_key:
            return {
                "base_url": _base_url_from_mcp_url(url) if url else DEFAULT_BASE_URL,
                "api_key": api_key,
            }
    return None


def _base_url_from_mcp_url(url: str) -> str:
    stripped = url.rstrip("/")
    if stripped.endswith("/mcp"):
        return stripped[: -len("/mcp")]
    return stripped or DEFAULT_BASE_URL


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    api_key: str,
    timeout: float,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
    ) as exc:
        _debug(f"Memory search request failed: {exc}")
        return {}


def _queries_for_file_ref(ref: str) -> list[str]:
    normalized = ref.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    candidates = [normalized]
    if path.name and path.name != normalized:
        candidates.append(path.name)
    if path.suffix:
        candidates.append(path.stem)
    parent_name = path.parent.name
    if parent_name and path.name:
        candidates.append(f"{parent_name}/{path.name}")
    return _dedupe_strings(c for c in candidates if c)


def _split_path_candidates(value: str) -> list[str]:
    cleaned = value.strip().strip("'\"`.,;:()[]{}<>")
    if not cleaned:
        return []
    tokens = re.split(r"[\s,]+", cleaned)
    if len(tokens) == 1:
        return [cleaned]
    return [token.strip("'\"`.,;:()[]{}<>") for token in tokens if token.strip()]


def _looks_like_file_reference(candidate: str) -> bool:
    if not candidate or len(candidate) > 500:
        return False
    lowered = candidate.lower()
    if lowered.startswith(("http://", "https://")):
        return False
    if "://" in lowered and not lowered.startswith("file://"):
        return False
    if candidate.startswith("file://"):
        return True
    if "/" in candidate or "\\" in candidate:
        return True
    return bool(re.search(r"\.[a-z0-9][a-z0-9_-]{0,12}$", candidate, re.IGNORECASE))


def _first_string(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_dict(mapping: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip().rstrip("/")
    return None


def _is_real_secret(value: str) -> bool:
    upper = value.upper()
    return bool(value and not any(token in upper for token in PLACEHOLDER_TOKENS))


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _dedupe_strings(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _debug(message: str) -> None:
    if os.environ.get("LUCENT_FILE_MEMORY_HOOK_DEBUG"):
        print(f"file-memory-lookup: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
