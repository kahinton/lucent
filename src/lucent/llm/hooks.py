"""LLM hook runtime.

Hooks are Lucent's agent middleware layer: approved definitions that can observe
model/tool events and inject extra context. Built-in/declarative hooks can look
up memory or inject static context; command hooks run approved shell commands or
scripts out-of-process with timeout/output limits and receive the hook event as
JSON on stdin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

FILE_ARGUMENT_KEYS = frozenset({
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
})

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

DEFAULT_FILE_MEMORY_HOOK: dict[str, Any] = {
    "name": "file-memory-lookup",
    "description": "Inject memories relevant to files referenced by tool calls.",
    "trigger_event": "tool_call",
    "action_type": "memory_lookup",
    "content": "",
    "config": {
        "tool_names": ["*"],
        "max_memories": 3,
        "memory_type": "technical",
        "include_archived": False,
    },
}

LEGACY_TOOL_CALL_EVENT = "tool_call"
BEFORE_TOOL_CALL = "before_tool_call"
AFTER_TOOL_CALL = "after_tool_call"
BEFORE_MODEL_CALL = "before_model_call"
AFTER_MODEL_CALL = "after_model_call"

INJECT_DECISIONS = frozenset({"inject", "allow", "replace_args"})


@dataclass(slots=True)
class HookExecution:
    """A hook output that should be injected into model-visible context."""

    hook_name: str
    text: str
    metadata: dict[str, Any]
    decision: str = "inject"
    replacement_arguments: dict[str, Any] | None = None
    replacement_result: str | None = None


@dataclass(slots=True)
class HookOutcome:
    """Aggregate result from one hook lifecycle phase.

    The class intentionally behaves like a sequence of ``HookExecution`` so
    older call sites/tests that treated hook output as a plain list continue to
    work while newer code can inspect blocking/rewrite decisions.
    """

    executions: list[HookExecution]
    block_message: str | None = None
    modified_arguments: dict[str, Any] | None = None
    modified_result: str | None = None

    def __iter__(self):
        return iter(self.executions)

    def __len__(self) -> int:
        return len(self.executions)

    def __getitem__(self, index: int) -> HookExecution:
        return self.executions[index]

    @property
    def blocked(self) -> bool:
        return self.block_message is not None

    @property
    def injectable_executions(self) -> list[HookExecution]:
        return [
            execution
            for execution in self.executions
            if execution.text and execution.decision in INJECT_DECISIONS
        ]


class HookManager:
    """Run approved hooks around LLM/model/tool lifecycle events."""

    def __init__(self, hooks: list[dict[str, Any]] | None = None):
        self.hooks = _dedupe_hooks([DEFAULT_FILE_MEMORY_HOOK, *(hooks or [])])

    async def before_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        memory_bridge: Any | None,
    ) -> HookOutcome:
        """Run before-tool hooks and return context/decision output."""
        return await self._run_event_hooks(
            event=BEFORE_TOOL_CALL,
            tool_name=tool_name,
            arguments=arguments,
            memory_bridge=memory_bridge,
        )

    async def after_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        tool_result: str,
        memory_bridge: Any | None,
    ) -> HookOutcome:
        """Run after-tool hooks and return context/result rewrite decisions."""
        return await self._run_event_hooks(
            event=AFTER_TOOL_CALL,
            tool_name=tool_name,
            arguments=arguments,
            tool_result=tool_result,
            memory_bridge=memory_bridge,
        )

    async def before_model_call(
        self,
        *,
        messages: list[dict[str, Any]],
    ) -> HookOutcome:
        """Run hooks immediately before a model invocation."""
        return await self._run_event_hooks(event=BEFORE_MODEL_CALL, messages=messages)

    async def after_model_call(
        self,
        *,
        messages: list[dict[str, Any]],
        model_text: str,
    ) -> HookOutcome:
        """Run hooks immediately after a model response."""
        return await self._run_event_hooks(
            event=AFTER_MODEL_CALL,
            messages=messages,
            model_text=model_text,
        )

    async def _run_event_hooks(
        self,
        *,
        event: str,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
        tool_result: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        model_text: str | None = None,
        memory_bridge: Any | None = None,
    ) -> HookOutcome:
        """Run all hooks matching a lifecycle event."""
        outputs: list[HookExecution] = []
        current_arguments = dict(arguments or {})
        modified_result: str | None = None
        block_message: str | None = None
        for hook in self.hooks:
            if not _event_matches(str(hook.get("trigger_event") or ""), event):
                continue
            config = _merged_config(hook)
            if not _tool_matches(tool_name, config):
                continue
            try:
                if hook.get("action_type") == "memory_lookup":
                    if not tool_name:
                        continue
                    result = await _run_memory_lookup_hook(
                        hook=hook,
                        config=config,
                        tool_name=tool_name,
                        arguments=current_arguments,
                        memory_bridge=memory_bridge,
                    )
                elif hook.get("action_type") == "static_context":
                    result = _run_static_context_hook(
                        hook=hook,
                        config=config,
                        tool_name=tool_name,
                        arguments=current_arguments,
                    )
                elif hook.get("action_type") == "command":
                    result = await _run_command_hook(
                        hook=hook,
                        config=config,
                        event=event,
                        tool_name=tool_name,
                        arguments=current_arguments,
                        tool_result=modified_result if modified_result is not None else tool_result,
                        messages=messages,
                        model_text=model_text,
                    )
                else:
                    continue
                if result:
                    outputs.append(result)
                    if result.decision == "block":
                        block_message = result.text or f"Blocked by hook {result.hook_name}."
                        break
                    if (
                        result.decision == "replace_args"
                        and result.replacement_arguments is not None
                    ):
                        current_arguments = result.replacement_arguments
                    if (
                        result.decision == "replace_result"
                        and result.replacement_result is not None
                    ):
                        modified_result = result.replacement_result
            except Exception:
                logger.debug("Hook %s failed", hook.get("name"), exc_info=True)
        return HookOutcome(
            executions=outputs,
            block_message=block_message,
            modified_arguments=(
                current_arguments if current_arguments != (arguments or {}) else None
            ),
            modified_result=modified_result,
        )


def append_hook_context(tool_result: str, executions: list[HookExecution] | HookOutcome) -> str:
    """Append hook context to a tool result for model-visible injection."""
    if not executions:
        return tool_result
    chunks = [tool_result or ""]
    chunks.append("\n---\nLucent hook context:")
    for execution in executions:
        chunks.append(f"\n[{execution.hook_name}]\n{execution.text}")
    return "\n".join(chunks).strip()


def extract_file_references(tool_name: str | None, arguments: dict[str, Any] | None) -> list[str]:
    """Extract likely file/path references from a tool call.

    The extractor is intentionally conservative about triggering on arbitrary
    strings: either the tool name must look file-ish, or the argument key must
    be a path/file key. Values are normalized and deduplicated.
    """
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
    return _dedupe_strings(refs)[:8]


async def _run_memory_lookup_hook(
    *,
    hook: dict[str, Any],
    config: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    memory_bridge: Any | None,
) -> HookExecution | None:
    if memory_bridge is None:
        return None
    file_refs = extract_file_references(tool_name, arguments)
    if not file_refs:
        return None

    max_memories = int(config.get("max_memories") or 3)
    max_memories = max(1, min(max_memories, 10))
    memory_type = config.get("memory_type") or "technical"
    include_archived = bool(config.get("include_archived", False))

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in file_refs[:3]:
        queries = _queries_for_file_ref(ref)
        for query in queries:
            if len(found) >= max_memories:
                break
            payload = {
                "query": query,
                "type": memory_type,
                "limit": max_memories,
                "include_archived": include_archived,
            }
            raw = await memory_bridge.call_tool("search_memories_full", payload)
            for memory in _parse_memory_search_result(raw):
                memory_id = str(memory.get("id"))
                if memory_id in seen:
                    continue
                seen.add(memory_id)
                found.append(memory)
                if len(found) >= max_memories:
                    break
        if len(found) >= max_memories:
            break

    if not found:
        return None

    lines = [
        "Relevant accessible memories for files referenced by this tool call:",
        *(f"- `{ref}`" for ref in file_refs[:3]),
        "",
    ]
    for memory in found:
        tags = ", ".join((memory.get("tags") or [])[:5])
        content = _single_line(str(memory.get("content") or ""))[:500]
        mid = str(memory.get("id", ""))[:8]
        prefix = f"- {mid}"
        if tags:
            prefix += f" [{tags}]"
        lines.append(f"{prefix}: {content}")

    return HookExecution(
        hook_name=str(hook.get("name") or "memory_lookup"),
        text="\n".join(lines),
        metadata={"file_refs": file_refs, "memory_count": len(found)},
    )


def _run_static_context_hook(
    *,
    hook: dict[str, Any],
    config: dict[str, Any],
    tool_name: str | None,
    arguments: dict[str, Any],
) -> HookExecution | None:
    file_refs = extract_file_references(tool_name, arguments)
    if config.get("require_file_reference", False) and not file_refs:
        return None
    content = str(hook.get("content") or config.get("content") or "").strip()
    if not content:
        return None
    return HookExecution(
        hook_name=str(hook.get("name") or "static_context"),
        text=content,
        metadata={"file_refs": file_refs},
    )


async def _run_command_hook(
    *,
    hook: dict[str, Any],
    config: dict[str, Any],
    event: str,
    tool_name: str | None,
    arguments: dict[str, Any],
    tool_result: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    model_text: str | None = None,
) -> HookExecution | None:
    """Run an approved command hook and inject bounded stdout/stderr.

    Command hooks may provide either:
    - ``config.command`` as a shell string or argv list, or
    - hook ``content`` as a shell script body.

    The hook event is provided on stdin as JSON by default. The same values are
    also exposed through LUCENT_HOOK_* environment variables for shell scripts.
    """
    file_refs = extract_file_references(tool_name, arguments)
    if config.get("require_file_reference", False) and not file_refs:
        return None

    command = config.get("command")
    script = str(hook.get("content") or "").strip()
    if not command and not script:
        return None

    timeout_seconds = _clamped_int(config.get("timeout_seconds"), default=10, minimum=1, maximum=60)
    max_output_chars = _clamped_int(
        config.get("max_output_chars"), default=4000, minimum=500, maximum=20000,
    )
    include_stderr = bool(config.get("include_stderr", True))

    payload = {
        "event": event,
        "hook_name": str(hook.get("name") or "command"),
        "tool_name": tool_name,
        "arguments": arguments,
        "tool_result": tool_result,
        "messages": messages or [],
        "model_text": model_text,
        "file_refs": file_refs,
    }
    stdin_bytes = None
    if config.get("pass_input", True):
        stdin_bytes = (json.dumps(payload, default=str) + "\n").encode()

    env = os.environ.copy()
    env.update({
        "LUCENT_HOOK_NAME": payload["hook_name"],
        "LUCENT_HOOK_EVENT": payload["event"],
        "LUCENT_TOOL_NAME": tool_name or "",
        "LUCENT_FILE_REFS": json.dumps(file_refs),
    })
    extra_env = config.get("env")
    if isinstance(extra_env, dict):
        env.update({str(k): str(v) for k, v in extra_env.items()})

    cwd = config.get("cwd") if isinstance(config.get("cwd"), str) else None
    stdin_pipe = asyncio.subprocess.PIPE if stdin_bytes is not None else None
    try:
        if isinstance(command, list):
            argv = [str(part) for part in command if str(part)]
            if not argv:
                return None
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=stdin_pipe,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        else:
            shell_command = str(command or script).strip()
            if not shell_command:
                return None
            proc = await asyncio.create_subprocess_shell(
                shell_command,
                stdin=stdin_pipe,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        stdout, stderr = await asyncio.wait_for(proc.communicate(stdin_bytes), timeout_seconds)
        timed_out = False
    except TimeoutError:
        proc.kill()
        stdout, stderr = await proc.communicate()
        timed_out = True

    stdout_text = stdout.decode(errors="replace").strip()
    stderr_text = stderr.decode(errors="replace").strip()
    text_parts: list[str] = []
    if timed_out:
        text_parts.append(f"Command timed out after {timeout_seconds} seconds.")
    elif proc.returncode:
        text_parts.append(f"Command exited with code {proc.returncode}.")
    if stdout_text:
        text_parts.append(stdout_text)
    if stderr_text and (include_stderr or timed_out or proc.returncode):
        text_parts.append("STDERR:\n" + stderr_text)

    text = "\n".join(text_parts).strip()
    if not text:
        return None
    if len(text) > max_output_chars:
        text = text[:max_output_chars].rstrip() + "\n… output truncated …"

    decision, parsed_text, replacement_arguments, replacement_result, extra_metadata = (
        _parse_command_output(text)
    )
    if len(parsed_text) > max_output_chars:
        parsed_text = parsed_text[:max_output_chars].rstrip() + "\n… output truncated …"

    return HookExecution(
        hook_name=payload["hook_name"],
        text=parsed_text,
        metadata={
            "file_refs": file_refs,
            "return_code": proc.returncode,
            "timed_out": timed_out,
            **extra_metadata,
        },
        decision=decision,
        replacement_arguments=replacement_arguments,
        replacement_result=replacement_result,
    )


def _event_matches(trigger_event: str, event: str) -> bool:
    if trigger_event == event:
        return True
    return trigger_event == LEGACY_TOOL_CALL_EVENT and event == BEFORE_TOOL_CALL


def _tool_matches(tool_name: str | None, config: dict[str, Any]) -> bool:
    if tool_name is None:
        return True
    tool_names = config.get("tool_names") or ["*"]
    if isinstance(tool_names, str):
        tool_names = [tool_names]
    normalized = {str(name) for name in tool_names}
    return "*" in normalized or tool_name in normalized


def _parse_command_output(
    text: str,
) -> tuple[str, str, dict[str, Any] | None, str | None, dict[str, Any]]:
    """Parse command-hook stdout as optional JSON decision protocol.

    Plain text remains an ``inject`` decision. JSON output can request:
    - ``{"action": "block", "message": "..."}``
    - ``{"action": "replace_args", "arguments": {...}}``
    - ``{"action": "replace_result", "result": "..."}``
    - ``{"action": "inject", "context": "..."}``
    - ``{"action": "allow"}``
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return "inject", text, None, None, {}
    if not isinstance(data, dict):
        return "inject", text, None, None, {}

    decision = str(data.get("action") or data.get("decision") or "inject")
    if decision in {"continue", "pass"}:
        decision = "allow"
    if decision not in {"allow", "inject", "block", "replace_args", "replace_result"}:
        decision = "inject"

    output_text = str(
        data.get("context")
        or data.get("message")
        or data.get("text")
        or data.get("output")
        or ""
    ).strip()
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}

    replacement_arguments = None
    if decision == "replace_args":
        candidate = None
        for key in ("arguments", "tool_args", "replacement_arguments"):
            if key in data:
                candidate = data[key]
                break
        if isinstance(candidate, dict):
            replacement_arguments = candidate
        else:
            decision = "inject"

    replacement_result = None
    if decision == "replace_result":
        candidate = None
        for key in ("result", "tool_result", "replacement_result"):
            if key in data:
                candidate = data[key]
                break
        if candidate is not None:
            replacement_result = str(candidate)
            if not output_text:
                output_text = replacement_result
        else:
            decision = "inject"

    if decision == "block" and not output_text:
        output_text = "Blocked by hook."

    return decision, output_text, replacement_arguments, replacement_result, metadata


def _clamped_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _merged_config(hook: dict[str, Any]) -> dict[str, Any]:
    config = _ensure_dict(hook.get("config"))
    override = _ensure_dict(hook.get("config_override"))
    return {**config, **override}


def _ensure_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _dedupe_hooks(hooks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for hook in hooks:
        name = str(hook.get("name") or hook.get("id") or "")
        if not name or name in seen:
            continue
        if hook.get("status") not in (None, "active"):
            continue
        seen.add(name)
        out.append(hook)
    return out


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = value.strip().strip('"\'`')
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _split_path_candidates(value: str) -> list[str]:
    if "\n" in value:
        parts = re.split(r"[\s,]+", value)
        return [p for p in parts if p]
    return [value.strip()]


def _looks_like_file_reference(value: str) -> bool:
    if not value or len(value) > 500:
        return False
    lower = value.lower()
    if lower.startswith(("http://", "https://")):
        return False
    if lower.startswith("file://"):
        return True
    if "/" in value or "\\" in value:
        return True
    return bool(re.search(r"\.[a-z0-9]{1,12}$", value, flags=re.IGNORECASE))


def _queries_for_file_ref(ref: str) -> list[str]:
    cleaned = ref.removeprefix("file://")
    path = PurePosixPath(cleaned.replace("\\", "/"))
    queries = [cleaned]
    if path.name and path.name != cleaned:
        queries.append(path.name)
    parent = str(path.parent)
    if parent and parent not in (".", "/"):
        queries.append(parent)
    return _dedupe_strings(queries)


def _parse_memory_search_result(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict) or data.get("error"):
        return []
    memories = data.get("memories") or []
    return [m for m in memories if isinstance(m, dict)]


def _single_line(value: str) -> str:
    return " ".join(value.split())
