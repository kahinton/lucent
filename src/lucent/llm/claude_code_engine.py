"""Claude Code engine — the official Claude Agent SDK (``claude-agent-sdk``).

Drives the Claude Code CLI (bundled with the SDK) via the SDK's ``query()`` /
``ClaudeSDKClient`` API, mapping SDK messages to Lucent's normalized
``SessionEvent`` stream. Supports Anthropic-native models and models from a local
provider (Ollama >= 0.14 serves the Anthropic Messages API) via ``ANTHROPIC_BASE_URL``
— see ``lucent.llm.engine_injection``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from lucent.llm.engine import (
    LLMEngine,
    SessionEvent,
    SessionEventType,
)
from lucent.llm.engine_injection import backend_local_injection
from lucent.llm.mcp_translate import to_claude_mcp_servers
from lucent.llm.token_usage import normalize_token_usage
from lucent.logging import get_logger

logger = get_logger("llm.claude_code")

_ClaudeAgentOptions: Any = None
_query: Any = None
_sdk_available: bool | None = None


def _ensure_sdk() -> bool:
    """Lazily import the Claude Agent SDK. Returns True if available."""
    global _ClaudeAgentOptions, _query, _sdk_available
    if _sdk_available is not None:
        return _sdk_available
    try:
        from claude_agent_sdk import ClaudeAgentOptions, query

        _ClaudeAgentOptions, _query = ClaudeAgentOptions, query
        _sdk_available = True
    except ImportError:
        _sdk_available = False
    return _sdk_available


def resolve_claude_cli_path(audit_context: dict[str, Any] | None = None) -> str | None:
    """Optional CLI override. The SDK bundles its own CLI, so this is opt-in.

    Precedence: backend config ``cli_path`` → ``CLAUDE_CLI_PATH`` env → None (use
    the SDK's bundled CLI).
    """
    backend = (audit_context or {}).get("engine_backend") or {}
    configured = (backend.get("config") or {}).get("cli_path")
    if configured:
        return str(configured)
    env_value = os.environ.get("CLAUDE_CLI_PATH", "").strip()
    return env_value or None


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


#: Permission modes the Claude Agent SDK accepts.
_PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}
)


def _resolve_permission_mode(
    audit_context: dict[str, Any] | None, *, approve_permissions: bool
) -> str:
    """Pick the permission mode for this session.

    Precedence: an explicit ``permission_mode`` in the audit context (used by an
    unattended remote session, which has no human to approve anything) →
    ``CLAUDE_PERMISSION_MODE`` env → ``acceptEdits`` when permissions were
    granted, else ``default``.

    ``acceptEdits`` auto-approves file edits but still prompts for shell
    commands. A daemon or remote session has nobody to answer that prompt, so it
    must not rely on the default; callers that know they are unattended pass
    ``bypassPermissions`` explicitly.
    """
    requested = str((audit_context or {}).get("permission_mode") or "").strip()
    if requested in _PERMISSION_MODES:
        return requested
    env_value = os.environ.get("CLAUDE_PERMISSION_MODE", "").strip()
    if env_value in _PERMISSION_MODES:
        return env_value
    return "acceptEdits" if approve_permissions else "default"


def _claude_approval_callback(approval: Any) -> Any:
    """Build the SDK's ``can_use_tool`` callback around an ``EngineApproval``.

    Claude Code reports MCP tools as ``mcp__<server>__<tool>``, which is exactly
    the shape ``EngineApproval.should_gate`` understands: Lucent's own server is
    skipped (its tools are already gated server-side), external servers and
    built-ins (``Bash``, ``Write``, …) prompt.

    A denial returns ``PermissionResultDeny`` with the note, so the model reads
    the refusal as a tool error with a reason instead of silently retrying.
    """

    async def can_use_tool(tool_name: str, tool_input: Any, _context: Any) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        if not approval.should_gate(tool_name):
            return PermissionResultAllow()
        decision = await approval.request(
            tool_name, tool_input if isinstance(tool_input, dict) else {}
        )
        if decision.approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message=decision.as_tool_result(tool_name))

    return can_use_tool


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # ToolResultBlock content can be a list of text blocks.
        chunks = []
        for part in value:
            if isinstance(part, dict) and part.get("type") == "text":
                chunks.append(str(part.get("text", "")))
            elif isinstance(part, str):
                chunks.append(part)
        return "\n".join(c for c in chunks if c) or None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


class ClaudeCodeEngine(LLMEngine):
    """LLM engine backed by the Claude Agent SDK (Claude Code)."""

    @property
    def name(self) -> str:
        return "claude_code"

    @staticmethod
    async def _native_env(audit_context: dict[str, Any] | None) -> dict[str, str]:
        """Anthropic API key for the native (non-local) path, from the provider store."""
        org_id = str((audit_context or {}).get("organization_id") or "").strip() or None
        try:
            from lucent.model_discovery import resolve_provider_credential

            key = await resolve_provider_credential("anthropic", org_id=org_id)
        except Exception:
            return {}
        return {"ANTHROPIC_API_KEY": key} if key else {}

    def _build_options(
        self,
        *,
        model: str,
        system_message: str,
        mcp_config: dict | None,
        reasoning_effort: str | None,
        approve_permissions: bool,
        provider_session_id: str | None,
        resume: bool,
        audit_context: dict[str, Any] | None,
        include_partial: bool,
        native_env: dict[str, str] | None = None,
        approval: Any = None,
    ) -> Any:
        if not _ensure_sdk():
            raise RuntimeError(
                "Claude Code engine requires the claude-agent-sdk package. "
                "Install with: pip install claude-agent-sdk"
            )
        backend = (audit_context or {}).get("engine_backend")
        injection = backend_local_injection("claude_code", backend, model=model)

        env: dict[str, str] = {}
        if injection is not None:
            env.update(injection.kwargs.get("env") or {})
        elif native_env:
            env.update(native_env)

        kwargs: dict[str, Any] = {
            "model": model,
            "permission_mode": _resolve_permission_mode(
                audit_context, approve_permissions=approve_permissions
            ),
            "include_partial_messages": include_partial,
        }
        # The SDK's can_use_tool callback is the native seam for "may this run?".
        # It is only wired when the conversation asked for approval, because a
        # callback also disables the CLI's own auto-approval shortcuts.
        if approval is not None and getattr(approval, "enabled", False):
            kwargs["can_use_tool"] = _claude_approval_callback(approval)
        if system_message:
            kwargs["system_prompt"] = system_message
        mcp_servers = to_claude_mcp_servers(mcp_config)
        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers
        if reasoning_effort:
            kwargs["effort"] = reasoning_effort
        if env:
            kwargs["env"] = env
        cli_path = resolve_claude_cli_path(audit_context)
        if cli_path:
            kwargs["cli_path"] = cli_path
        if provider_session_id:
            if resume:
                kwargs["resume"] = provider_session_id
            else:
                kwargs["session_id"] = provider_session_id
        return _ClaudeAgentOptions(**kwargs)

    async def run_session(
        self,
        model: str,
        system_message: str,
        prompt: str,
        mcp_config: dict | None = None,
        timeout: int = 300,
        reasoning_effort: str | None = None,
        provider_session_id: str | None = None,
        resume: bool = False,
        message_history: list[dict[str, Any]] | None = None,
        hooks: list[dict[str, Any]] | None = None,
        audit_context: dict[str, Any] | None = None,
        enable_config_discovery: bool = False,
        approve_permissions: bool = True,
        attachments: list[dict[str, Any]] | None = None,
        managed_tools: list[dict[str, Any]] | None = None,
        approval: Any = None,
    ) -> str | None:
        """Run a blocking Claude Code turn and return the final response text."""
        return await self._run(
            model=model,
            system_message=system_message,
            prompt=prompt,
            mcp_config=mcp_config,
            timeout=timeout,
            reasoning_effort=reasoning_effort,
            provider_session_id=provider_session_id,
            resume=resume,
            audit_context=audit_context,
            approve_permissions=approve_permissions,
            attachments=attachments,
            on_event=None,
            include_partial=False,
            approval=approval,
        )

    async def run_session_streaming(
        self,
        model: str,
        system_message: str,
        prompt: str,
        mcp_config: dict | None = None,
        on_event: Callable[[SessionEvent], None] | None = None,
        timeout: int = 600,
        idle_timeout: int = 300,
        reasoning_effort: str | None = None,
        provider_session_id: str | None = None,
        resume: bool = False,
        message_history: list[dict[str, Any]] | None = None,
        hooks: list[dict[str, Any]] | None = None,
        audit_context: dict[str, Any] | None = None,
        enable_config_discovery: bool = False,
        approve_permissions: bool = True,
        attachments: list[dict[str, Any]] | None = None,
        managed_tools: list[dict[str, Any]] | None = None,
        current_user_text: str | None = None,
        approval: Any = None,
    ) -> str | None:
        """Run a streaming Claude Code turn, emitting normalized events."""
        return await self._run(
            model=model,
            system_message=system_message,
            prompt=prompt,
            mcp_config=mcp_config,
            timeout=timeout,
            reasoning_effort=reasoning_effort,
            provider_session_id=provider_session_id,
            resume=resume,
            audit_context=audit_context,
            approve_permissions=approve_permissions,
            attachments=attachments,
            on_event=on_event,
            include_partial=True,
            approval=approval,
        )

    async def _run(
        self,
        *,
        model: str,
        system_message: str,
        prompt: str,
        mcp_config: dict | None,
        timeout: int,
        reasoning_effort: str | None,
        provider_session_id: str | None,
        resume: bool,
        audit_context: dict[str, Any] | None,
        approve_permissions: bool,
        attachments: list[dict[str, Any]] | None,
        on_event: Callable[[SessionEvent], None] | None,
        include_partial: bool,
        approval: Any = None,
    ) -> str | None:
        if not _ensure_sdk():
            raise RuntimeError(
                "Claude Code engine requires the claude-agent-sdk package. "
                "Install with: pip install claude-agent-sdk"
            )

        def emit(event: SessionEvent) -> None:
            if on_event is not None:
                on_event(event)

        from lucent.llm.engine_injection import local_provider_for_backend

        backend = (audit_context or {}).get("engine_backend")
        native_env = {} if local_provider_for_backend(backend) else await self._native_env(
            audit_context
        )

        options = self._build_options(
            model=model,
            system_message=system_message,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
            approve_permissions=approve_permissions,
            provider_session_id=provider_session_id,
            resume=resume,
            audit_context=audit_context,
            include_partial=include_partial,
            native_env=native_env,
            approval=approval,
        )

        text = _combine_prompt(prompt, attachments)
        state = {"final": None, "error": None}

        try:
            async for message in _query(prompt=text, options=options):
                for event in self._map_message(message, state):
                    emit(event)
        except Exception as exc:
            logger.warning("Claude Code session failed: %s", exc, exc_info=True)
            emit(SessionEvent(type=SessionEventType.ERROR, content=str(exc)))
            return None

        if state["error"]:
            return None
        return state["final"]

    # ── message mapping ─────────────────────────────────────────────────────

    def _map_message(self, message: Any, state: dict[str, Any]) -> list[SessionEvent]:
        """Map one SDK message to zero or more normalized events."""
        cls = type(message).__name__
        events: list[SessionEvent] = []

        if cls == "AssistantMessage":
            for block in _attr(message, "content", []) or []:
                events.extend(self._map_block(block))
            usage = normalize_token_usage(_attr(message, "usage"))
            if usage:
                events.append(SessionEvent(type=SessionEventType.USAGE, usage=usage))
            return events

        if cls == "StreamEvent":
            event = _attr(message, "event", {}) or {}
            if _attr(event, "type") == "content_block_delta":
                delta = _attr(event, "delta", {}) or {}
                if _attr(delta, "type") == "text_delta":
                    events.append(
                        SessionEvent(
                            type=SessionEventType.MESSAGE_DELTA,
                            content=_attr(delta, "text", ""),
                        )
                    )
            return events

        if cls == "ResultMessage":
            result_text = _attr(message, "result")
            if result_text:
                state["final"] = result_text
            if _attr(message, "is_error"):
                state["error"] = _as_text(result_text) or "Claude Code reported an error"
                events.append(SessionEvent(type=SessionEventType.ERROR, content=state["error"]))
            else:
                events.append(SessionEvent(type=SessionEventType.SESSION_IDLE))
            usage = normalize_token_usage(_attr(message, "usage"))
            if usage:
                events.append(SessionEvent(type=SessionEventType.USAGE, usage=usage))
            return events

        if cls in ("SystemMessage", "UserMessage", "ConversationResetMessage", "RateLimitEvent"):
            # System/notification traffic — surfaced as OTHER for visibility.
            return [SessionEvent(type=SessionEventType.OTHER, tool_name=cls)]

        return events

    def _map_block(self, block: Any) -> list[SessionEvent]:
        cls = type(block).__name__
        if cls == "TextBlock":
            return [SessionEvent(type=SessionEventType.MESSAGE, content=_attr(block, "text"))]
        if cls == "ThinkingBlock":
            return [
                SessionEvent(
                    type=SessionEventType.OTHER,
                    tool_name="_reasoning",
                    content=_attr(block, "thinking"),
                )
            ]
        if cls == "ToolUseBlock":
            tool_input = _attr(block, "input")
            content = None
            if tool_input is not None:
                try:
                    content = json.dumps(tool_input, default=str)
                except (TypeError, ValueError):
                    content = str(tool_input)
            return [
                SessionEvent(
                    type=SessionEventType.TOOL_CALL,
                    tool_name=_attr(block, "name"),
                    tool_input=tool_input if isinstance(tool_input, dict) else None,
                    content=content,
                )
            ]
        if cls == "ToolResultBlock":
            return [
                SessionEvent(
                    type=SessionEventType.TOOL_RESULT,
                    tool_name=None,
                    tool_output=_as_text(_attr(block, "content")),
                )
            ]
        return []

    async def cleanup(self) -> None:
        """No persistent resources — each turn opens and closes its own session."""
        return None


def _combine_prompt(prompt: str, attachments: list[dict[str, Any]] | None) -> str:
    """Inline text documents into the prompt (Claude Code reads images natively)."""
    if attachments:
        from lucent.llm.attachments import inline_text_documents

        return inline_text_documents(prompt, attachments)
    return prompt
