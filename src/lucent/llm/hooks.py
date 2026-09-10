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

from lucent import settings as runtime_settings

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

# Design §3.4/§6.3: the message-phase pipeline queries this hook's own
# ``search_memories_full`` calls, so its query strings must not re-trigger
# lexical file extraction (defect 6 of the hook-pipeline catalog).
FILE_ARGUMENT_KEYS = frozenset(FILE_ARGUMENT_KEYS - {"query"})

MAX_FILE_REFERENCES = 8

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

MEMORY_LOOKUP_ACTION = "memory_lookup"
MESSAGE_MEMORY_LOOKUP_ACTION = "message_memory_lookup"

DEFAULT_MESSAGE_MEMORY_HOOK: dict[str, Any] = {
    "name": "message-memory-lookup",
    "description": (
        "Inject memories relevant to the user's latest message before the "
        "model is invoked."
    ),
    "trigger_event": BEFORE_MODEL_CALL,
    "action_type": MESSAGE_MEMORY_LOOKUP_ACTION,
    "content": "",
    "config": {
        "max_memories": 3,
        "include_archived": False,
    },
}

# Hook-UX (2026-09-10): message-lookup injection fetches FULL content for its
# selected results with one batched ``get_memories`` call and injects it until
# this byte budget is spent; remaining results keep the search-preview line.
MESSAGE_LOOKUP_FULL_CONTENT_BUDGET_BYTES = 6144

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
    """Run approved hooks around LLM/model/tool lifecycle events.

    ``session_state`` is an optional per-conversation dict threaded through the
    engine so hook state survives across turns of one conversation (message
    pipeline design §5). The message-lookup hook keeps its injected-memory
    dedup set at ``session_state["injected_memory_ids"]``.
    """

    def __init__(
        self,
        hooks: list[dict[str, Any]] | None = None,
        session_state: dict[str, Any] | None = None,
    ):
        self.hooks = _dedupe_hooks([
            DEFAULT_FILE_MEMORY_HOOK,
            DEFAULT_MESSAGE_MEMORY_HOOK,
            *(hooks or []),
        ])
        self.session_state = session_state if session_state is not None else {}

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
        memory_bridge: Any | None = None,
    ) -> HookOutcome:
        """Run hooks immediately before a model invocation.

        ``memory_bridge`` is optional: it is required only by memory-lookup
        hooks, which no-op when it is absent (message pipeline design §2 —
        model-phase lookups were previously unreachable because this method
        never passed a bridge or a tool name).
        """
        return await self._run_event_hooks(
            event=BEFORE_MODEL_CALL,
            messages=messages,
            memory_bridge=memory_bridge,
        )

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
                if hook.get("action_type") == MEMORY_LOOKUP_ACTION:
                    if not tool_name:
                        continue
                    result = await _run_memory_lookup_hook(
                        hook=hook,
                        config=config,
                        tool_name=tool_name,
                        arguments=current_arguments,
                        memory_bridge=memory_bridge,
                    )
                elif hook.get("action_type") == MESSAGE_MEMORY_LOOKUP_ACTION:
                    if event != BEFORE_MODEL_CALL or not messages:
                        continue
                    result = await _run_message_memory_lookup_hook(
                        hook=hook,
                        config=config,
                        messages=messages,
                        memory_bridge=memory_bridge,
                        session_state=self.session_state,
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
            except Exception as exc:
                # Hook failures must stay visible (hook-UX, 2026-09-10): a
                # vanished error is indistinguishable from a hook that never
                # ran, so surface the exception class in the hook block. The
                # warning log carries the traceback for diagnosis.
                logger.warning("Hook %s failed", hook.get("name"), exc_info=True)
                outputs.append(
                    HookExecution(
                        hook_name=str(hook.get("name") or "hook"),
                        text=f"hook error: {type(exc).__name__}",
                        metadata={"hook_error": type(exc).__name__},
                    )
                )
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


def extract_semantic_terms(text: str) -> list[str]:
    """Extract deterministic retrieval terms from a user message.

    Message-phase pipeline design (``docs/design/living-memory-inject-pipeline.md``
    §3): lowercase, split on non-word characters, then drop tokens that carry no
    retrieval signal — stopwords (D1), sub-3-char fragments (D2), pure numbers
    (D3), and path/URL/code-shaped tokens (D4). The result keeps message order
    and is capped at ``memory.inject_max_terms`` terms.

    The extractor is deterministic, dependency-free, and pure — notably it does
    NOT call the search API; querying is the injection layer's job.
    """
    survivors: list[str] = []
    seen: set[str] = set()
    cap = _message_lookup_max_terms()
    for token in re.split(r"\W+", (text or "").lower()):
        if not token:
            continue
        if token in _SEMANTIC_STOPWORDS:  # D1
            continue
        if len(token) < 3:  # D2
            continue
        if token.isdigit():  # D3
            continue
        if any(ch in token for ch in "/\\.@#{}"):  # D4
            continue
        if token in seen:
            continue
        seen.add(token)
        survivors.append(token)
        if len(survivors) >= cap:
            break
    return survivors


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
    return _dedupe_strings(refs)[:MAX_FILE_REFERENCES]


# Message-phase pipeline stopword set (design §3.3, D1). Closed-class English
# function vocabulary only — deliberately NOT config, so the set stays
# reviewable in one diff and the degenerate-gate hazard (§3.5) is unreachable.
# Contraction fragments left by ``\W+`` splitting (``don't`` → ``don``, ``t``)
# are absorbed here; extend only when live measurement shows a concrete leak.
_SEMANTIC_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when", "while",
    "of", "to", "in", "on", "for", "with", "at", "by", "from", "as",
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did",
    "have", "has", "had", "will", "would", "can", "could", "should", "shall",
    "may", "might", "must",
    "it", "its", "this", "that", "these", "those",
    "i", "im", "ive", "id", "ill", "you", "your", "youre", "youll", "youve", "youd",
    "we", "our", "us", "well", "lets", "let",
    "they", "them", "their", "theirs", "he", "she", "his", "her", "me", "my", "mine",
    "what", "which", "who", "whom", "whose", "where", "why", "how",
    "not", "no", "yes", "so", "than", "too", "very", "just", "also",
    "about", "into", "over", "under", "again", "once", "here", "there",
    "all", "any", "both", "each", "few", "more", "most", "other", "some",
    "such", "only", "own", "same", "s", "t", "d", "ll", "ve", "re", "don", "now",
    "get", "got", "make", "made", "want", "wanted", "need", "needed",
    "know", "knew", "think", "thought", "like",
    "okay", "ok", "yeah", "hey", "hi", "hello", "thanks", "thank", "please",
    # Negated-auxiliary contraction stems left by \W+ splitting: "isn't" →
    # "isn" + "t", "doesn't" → "doesn" + "t", etc. Without these the negation
    # fragments pass the filter and pollute otherwise-filler queries.
    "ain", "isn", "aren", "wasn", "weren", "hasn", "haven", "didn", "doesn",
    "didn", "couldn", "shouldn", "wouldn", "won", "shant", "mustnt", "neednt",
})


def _message_lookup_organization_id() -> Any | None:
    """Best-effort organization id for message-lookup runtime settings.

    The hook runtime runs inside an active Lucent session; the ambient
    request/daemon context resolved by ``get_runtime_setting`` covers the
    common case. Returns None (global defaults) when no context is ambient.
    """
    try:
        return runtime_settings._current_organization_id()
    except Exception:
        return None


def _message_lookup_min_similarity() -> float:
    """Injection-layer similarity threshold (design §4.2, calibrated 0.30)."""
    try:
        return float(
            runtime_settings.message_inject_min_similarity(
                organization_id=_message_lookup_organization_id(),
            )
        )
    except Exception:
        return 0.30


def _message_lookup_max_terms() -> int:
    """Maximum extracted terms per message (design §3.4)."""
    try:
        return int(
            runtime_settings.message_inject_max_terms(
                organization_id=_message_lookup_organization_id(),
            )
        )
    except Exception:
        return 8


def _message_lookup_max_memories() -> int:
    """Maximum memories injected per message (design §4.3)."""
    try:
        return int(
            runtime_settings.message_inject_max_memories(
                organization_id=_message_lookup_organization_id(),
            )
        )
    except Exception:
        return 3


def _is_first_model_call_after_user_message(
    messages: list[dict[str, Any]] | None,
) -> bool:
    """True when no assistant/tool messages follow the last human message.

    Round-1 turn guard (design §5.3 rule 1): ``before_model_call`` fires once
    per tool-loop round, so this distinguishes the first model call that sees a
    user's message from later rounds of the same turn — injection happens once
    per user message.
    """
    last_user_index = None
    for index, message in enumerate(messages or []):
        if message.get("role") == "user":
            last_user_index = index
    if last_user_index is None:
        return False
    return not any(
        message.get("role") in ("assistant", "tool")
        for message in (messages or [])[last_user_index + 1:]
    )


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
        metadata={
            "file_refs": file_refs,
            "memory_count": len(found),
            "memories": [
                {
                    "id": str(memory.get("id", ""))[:8],
                    "tags": list((memory.get("tags") or [])[:5]),
                    "content": _single_line(str(memory.get("content") or ""))[:500],
                }
                for memory in found
            ],
        },
    )


async def _run_message_memory_lookup_hook(
    *,
    hook: dict[str, Any],
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    memory_bridge: Any | None,
    session_state: dict[str, Any] | None,
) -> HookExecution | None:
    """Message-phase injection layer (living-memory pipeline, design §2–§5).

    Takes the latest user message, extracts deterministic terms, searches the
    memory API, filters results client-side on ``similarity_score`` and the
    per-conversation dedup set, and injects up to ``max_memories`` memories
    with FULL content (one batched ``get_memories`` call, ~6KB byte budget;
    search-preview lines for anything past the budget). Pre-search failure
    modes (no bridge, no terms, later rounds) no-op silently; once the search
    has run, a no-injection turn emits an explicit ``memory-lookup: 0
    injected`` silence line so it is distinguishable from a didn't-run turn.
    """
    if memory_bridge is None:
        return None
    # Round-1 turn guard (design §5.3 rule 1): inject only on the first model
    # call after the user's message, never on later tool-loop rounds.
    if not _is_first_model_call_after_user_message(messages):
        return None
    latest_user = next(
        (message for message in reversed(messages) if message.get("role") == "user"),
        None,
    )
    if latest_user is None:
        return None
    # Extraction source (living-memory M3): chat.py threads the raw latest
    # user text via session_state["_latest_user_text"] because the prompt may
    # be the flattened transcript (_history_prompt), whose head terms are not
    # the current turn's topic. Prefer the threaded text when present and
    # truthy; fall back to the message-phase extraction source (first message
    # of a fresh conversation and non-chat callers).
    threaded_text = session_state.get("_latest_user_text") if session_state else None
    source_text = (
        str(threaded_text) if threaded_text else str(latest_user.get("content") or "")
    )
    terms = extract_semantic_terms(source_text)
    if not terms:
        # Zero-term short-circuit (design §3.4): all-stopword/fragment messages
        # must never reach the search API — the SQL match gate degenerates to
        # match-all when every token is a stopword (§3.5).
        return None

    query = " ".join(terms)
    limit = max(10, _message_lookup_max_memories() * 3)
    include_archived = bool(config.get("include_archived", False))
    raw = await memory_bridge.call_tool(
        "search_memories_full",
        {
            "query": query,
            "limit": limit,
            "include_archived": include_archived,
        },
    )
    candidates = _parse_memory_search_result(raw)
    min_similarity = _message_lookup_min_similarity()
    if not candidates:
        # Search ran and returned nothing injectable: emit the explicit
        # silence line (hook-UX) so this turn is distinguishable from a
        # turn where the hook did not run at all.
        return HookExecution(
            hook_name=str(hook.get("name") or "message_memory_lookup"),
            text="memory-lookup: 0 injected (0 deduped, 0 below threshold)",
            metadata={
                "terms": terms,
                "query": query,
                "min_similarity": min_similarity,
                "memory_count": 0,
                "deduped": 0,
                "below_threshold": 0,
                "skipped": 0,
            },
        )

    max_memories = max(1, min(int(config.get("max_memories") or _message_lookup_max_memories()), 10))
    dedup_state = session_state if session_state is not None else {}
    injected_memory_ids: set[str] = dedup_state.setdefault(
        "injected_memory_ids",
        set(),
    )

    # Client-side threshold + dedup filter (design §4.3, §5.3 rule 3): score
    # gate first, then dedup before the cap so dedup never wastes a slot.
    # Skips are counted by reason so a no-injection turn can state why
    # (hook-UX silence line) instead of looking identical to a didn't-run turn.
    below_threshold = 0
    deduped = 0
    injected: list[tuple[dict[str, Any], float]] = []
    for memory in candidates:
        try:
            score = float(memory.get("similarity_score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score < min_similarity:
            below_threshold += 1
            continue
        memory_id = str(memory.get("id") or "")
        if memory_id and memory_id in injected_memory_ids:
            deduped += 1
            continue
        injected.append((memory, score))
        if len(injected) >= max_memories:
            break
    if not injected:
        return HookExecution(
            hook_name=str(hook.get("name") or "message_memory_lookup"),
            text=(
                "memory-lookup: 0 injected "
                f"({deduped} deduped, {below_threshold} below threshold)"
            ),
            metadata={
                "terms": terms,
                "query": query,
                "min_similarity": min_similarity,
                "memory_count": 0,
                "deduped": deduped,
                "below_threshold": below_threshold,
                "skipped": deduped + below_threshold,
            },
        )
    injected_memory_ids.update(str(memory.get("id") or "") for memory, _ in injected)

    # Full-content injection (hook-UX, 2026-09-10): search results are
    # truncated ~1000-char previews, which forced a truncate-then-get_memory
    # round-trip in the model. Fetch full content for the selected ids with
    # ONE batched ``get_memories`` call and inject it until the byte budget
    # is spent; results beyond the budget keep their search-preview line.
    # The search API itself is untouched (settled M2 decision: injection
    # layer only).
    full_contents = await _fetch_full_contents(memory_bridge, injected)

    lines = [
        "Relevant accessible memories for the user's message:",
        "",
    ]
    used_bytes = 0
    full_ids: set[str] = set()
    for memory, _score in injected:
        tags = ", ".join((memory.get("tags") or [])[:5])
        mid = str(memory.get("id", ""))[:8]
        prefix = f"- {mid}"
        if tags:
            prefix += f" [{tags}]"
        memory_id = str(memory.get("id") or "")
        full = full_contents.get(memory_id)
        if full is not None:
            encoded = full.encode("utf-8")
            if used_bytes + len(encoded) <= MESSAGE_LOOKUP_FULL_CONTENT_BUDGET_BYTES:
                used_bytes += len(encoded)
                full_ids.add(memory_id)
                lines.append(f"{prefix}: {full}")
                continue
            # Over budget (or oversized on its own): preview line instead.
        content = _single_line(str(memory.get("content") or ""))[:500]
        lines.append(f"{prefix}: {content}")

    return HookExecution(
        hook_name=str(hook.get("name") or "message_memory_lookup"),
        text="\n".join(lines),
        metadata={
            "terms": terms,
            "query": query,
            "min_similarity": min_similarity,
            "memory_count": len(injected),
            "deduped": deduped,
            "below_threshold": below_threshold,
            "skipped": deduped + below_threshold,
            "full_content_count": len(full_ids),
            "full_content_bytes": used_bytes,
            "memory_ids": [str(memory.get("id", ""))[:8] for memory, _ in injected],
            "scores": [round(score, 3) for _, score in injected],
            "memories": [
                {
                    "id": str(memory.get("id", ""))[:8],
                    "tags": list((memory.get("tags") or [])[:5]),
                    "content": _single_line(str(memory.get("content") or ""))[:500],
                    "full": str(memory.get("id") or "") in full_ids,
                }
                for memory, _ in injected
            ],
        },
    )


async def _fetch_full_contents(
    memory_bridge: Any,
    injected: list[tuple[dict[str, Any], float]],
) -> dict[str, str]:
    """Fetch full content for injected memories in one batched call.

    Uses ``get_memories`` (single call, all ids) so the injection layer adds
    exactly one extra memory-server round-trip per turn. Returns
    ``{memory_id: full_content}``; any failure degrades to an empty mapping,
    which makes every result fall back to its search preview.
    """
    memory_ids = [
        memory_id
        for memory_id in (str(memory.get("id") or "") for memory, _score in injected)
        if memory_id
    ]
    if not memory_ids:
        return {}
    try:
        raw = await memory_bridge.call_tool("get_memories", {"memory_ids": memory_ids})
    except Exception as exc:
        logger.warning("Full-content fetch failed for message-lookup: %s", exc)
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("error"):
        return {}
    fetched = data.get("memories")
    if not isinstance(fetched, list):
        return {}
    contents: dict[str, str] = {}
    for memory in fetched:
        if not isinstance(memory, dict):
            continue
        memory_id = str(memory.get("id") or "")
        content = memory.get("content")
        if memory_id and content:
            contents[memory_id] = str(content)
    return contents


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
