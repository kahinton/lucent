"""Codex engine — the official OpenAI Codex Python SDK (``openai-codex``).

Runs Codex turns through the SDK (which drives its own bundled CLI), mapping SDK
notifications to Lucent's normalized ``SessionEvent`` stream. Supports the
provider-native OpenAI/ChatGPT models and models from a local provider (Ollama et
al.) via ``model_provider`` + an env base URL — see ``lucent.llm.engine_injection``.

Notification payload types are the SDK's generated pydantic models; item/type
shapes can drift between Codex releases, so mapping is defensive (attribute-or-dict
access, substring type matching) rather than pinned to exact class names.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from lucent.llm.engine import (
    LLMEngine,
    SessionEvent,
    SessionEventType,
)
from lucent.llm.engine_injection import backend_local_injection
from lucent.llm.mcp_translate import to_codex_mcp_servers
from lucent.llm.token_usage import normalize_token_usage
from lucent.logging import get_logger

logger = get_logger("llm.codex")

_AsyncCodex: Any = None
_CodexConfig: Any = None
_Sandbox: Any = None
_sdk_available: bool | None = None


def _ensure_sdk() -> bool:
    """Lazily import the Codex SDK. Returns True if available."""
    global _AsyncCodex, _CodexConfig, _Sandbox, _sdk_available
    if _sdk_available is not None:
        return _sdk_available
    try:
        from openai_codex import AsyncCodex, CodexConfig, Sandbox

        _AsyncCodex, _CodexConfig, _Sandbox = AsyncCodex, CodexConfig, Sandbox
        _sdk_available = True
    except ImportError:
        _sdk_available = False
    return _sdk_available


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a pydantic model or a plain dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


class CodexEngine(LLMEngine):
    """LLM engine backed by the OpenAI Codex SDK."""

    @property
    def name(self) -> str:
        return "codex"

    # ── thread construction ────────────────────────────────────────────────

    @staticmethod
    async def _native_env(audit_context: dict[str, Any] | None) -> dict[str, str]:
        """OpenAI API key for the native (non-local) path, from the provider store."""
        org_id = str((audit_context or {}).get("organization_id") or "").strip() or None
        try:
            from lucent.model_discovery import resolve_provider_credential

            key = await resolve_provider_credential("openai", org_id=org_id)
        except Exception:
            return {}
        return {"OPENAI_API_KEY": key} if key else {}

    async def _native_env_for(self, audit_context: dict[str, Any] | None) -> dict[str, str]:
        """Native provider env, or empty when a local provider is in play."""
        from lucent.llm.engine_injection import local_provider_for_backend

        backend = (audit_context or {}).get("engine_backend")
        if local_provider_for_backend(backend):
            return {}
        return await self._native_env(audit_context)

    def _thread_kwargs(
        self,
        *,
        model: str,
        mcp_config: dict | None,
        reasoning_effort: str | None,
        approve_permissions: bool,
        audit_context: dict[str, Any] | None,
        native_env: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Build thread_start kwargs (+ subprocess env) for a turn."""
        if not _ensure_sdk():
            raise RuntimeError(
                "Codex engine requires the openai-codex package. "
                "Install with: pip install openai-codex"
            )
        backend = (audit_context or {}).get("engine_backend")
        injection = backend_local_injection("codex", backend, model=model)

        sandbox = _Sandbox.workspace_write if approve_permissions else _Sandbox.read_only
        kwargs: dict[str, Any] = {"model": model, "sandbox": sandbox}
        config: dict[str, Any] = {}
        env: dict[str, str] = {}
        if injection is None and native_env:
            env.update(native_env)

        if injection is not None:
            if injection.kwargs.get("model_provider"):
                kwargs["model_provider"] = injection.kwargs["model_provider"]
            config.update(injection.kwargs.get("config_overrides") or {})
            env.update(injection.kwargs.get("env") or {})

        mcp_servers = to_codex_mcp_servers(mcp_config)
        if mcp_servers:
            config["mcp_servers"] = mcp_servers
        if reasoning_effort:
            config["model_reasoning_effort"] = reasoning_effort
        if config:
            kwargs["config"] = config
        return kwargs, env

    async def _open_thread(
        self,
        codex: Any,
        thread_kwargs: dict[str, Any],
        *,
        provider_session_id: str | None,
        resume: bool,
    ) -> Any:
        if provider_session_id and resume:
            try:
                return await codex.thread_resume(provider_session_id, **thread_kwargs)
            except Exception as exc:
                logger.warning(
                    "Failed to resume Codex thread %s; starting fresh: %s",
                    provider_session_id,
                    exc,
                )
        return await codex.thread_start(**thread_kwargs)

    # ── session entry points ───────────────────────────────────────────────

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
    ) -> str | None:
        """Run a blocking Codex turn and return the final response text."""
        if not _ensure_sdk():
            raise RuntimeError(
                "Codex engine requires the openai-codex package. "
                "Install with: pip install openai-codex"
            )

        native_env = await self._native_env_for(audit_context)
        thread_kwargs, env = self._thread_kwargs(
            model=model,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
            approve_permissions=approve_permissions,
            audit_context=audit_context,
            native_env=native_env,
        )
        text = _combine_prompt(system_message, prompt, attachments)

        try:
            async with _AsyncCodex(_CodexConfig(env=env) if env else None) as codex:
                thread = await self._open_thread(
                    codex, thread_kwargs, provider_session_id=provider_session_id, resume=resume
                )
                result = await asyncio.wait_for(thread.run(text), timeout=timeout)
            return _attr(result, "final_response") or None
        except TimeoutError:
            logger.warning("Codex session timed out after %ss", timeout)
            return None
        except Exception as exc:
            logger.warning("Codex session failed: %s", exc, exc_info=True)
            return None

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
    ) -> str | None:
        """Run a streaming Codex turn, emitting normalized events."""
        if not _ensure_sdk():
            raise RuntimeError(
                "Codex engine requires the openai-codex package. "
                "Install with: pip install openai-codex"
            )

        def emit(event: SessionEvent) -> None:
            if on_event is not None:
                on_event(event)

        native_env = await self._native_env_for(audit_context)
        thread_kwargs, env = self._thread_kwargs(
            model=model,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
            approve_permissions=approve_permissions,
            audit_context=audit_context,
            native_env=native_env,
        )
        text = _combine_prompt(system_message, prompt, attachments)

        state = {"final": None, "deltas": [], "idle": False, "error": None}

        try:
            async with _AsyncCodex(_CodexConfig(env=env) if env else None) as codex:
                thread = await self._open_thread(
                    codex, thread_kwargs, provider_session_id=provider_session_id, resume=resume
                )
                handle = await thread.turn(text)

                async def _consume() -> None:
                    async for note in handle.stream():
                        event = self._map_notification(note, state)
                        if event is not None:
                            emit(event)

                await asyncio.wait_for(_consume(), timeout=timeout)

            if state["error"]:
                return None
            if state["final"]:
                return state["final"]
            joined = "".join(state["deltas"])
            return joined or None
        except TimeoutError:
            logger.warning("Codex session timed out after %ss", timeout)
            emit(SessionEvent(type=SessionEventType.ERROR, content="Codex session timed out"))
            return None
        except Exception as exc:
            logger.warning("Codex session failed: %s", exc, exc_info=True)
            emit(SessionEvent(type=SessionEventType.ERROR, content=str(exc)))
            return None

    # ── event mapping ──────────────────────────────────────────────────────

    def _map_notification(self, note: Any, state: dict[str, Any]) -> SessionEvent | None:
        """Map one Codex notification to a normalized SessionEvent."""
        method = str(_attr(note, "method", "") or "")
        payload = _attr(note, "payload", note)

        if method.endswith("agentMessage/delta") or "agentMessage/delta" in method:
            delta = _attr(payload, "delta", "") or ""
            state["deltas"].append(delta)
            return SessionEvent(type=SessionEventType.MESSAGE_DELTA, content=delta)

        if method in ("item/started", "item/completed"):
            item = _attr(payload, "item", payload)
            kind = str(_attr(item, "type", "") or "").lower()
            completed = method == "item/completed"

            if "agent_message" in kind:
                item_text = _attr(item, "text") or _attr(item, "content")
                text = _as_text(item_text)
                if text:
                    state["final"] = text
                return (
                    SessionEvent(type=SessionEventType.MESSAGE, content=text) if completed else None
                )

            if "reasoning" in kind:
                return SessionEvent(
                    type=SessionEventType.OTHER,
                    tool_name="_reasoning",
                    content=_as_text(_attr(item, "text") or _attr(item, "summary")),
                )

            if any(k in kind for k in ("command", "shell", "exec")):
                if completed:
                    return SessionEvent(
                        type=SessionEventType.TOOL_RESULT,
                        tool_name="shell",
                        tool_output=_as_text(
                            _attr(item, "output") or _attr(item, "aggregated_output")
                        ),
                    )
                return SessionEvent(
                    type=SessionEventType.TOOL_CALL,
                    tool_name="shell",
                    tool_input={"command": _attr(item, "command")},
                )

            if "mcp" in kind or "tool_call" in kind:
                if completed:
                    return SessionEvent(
                        type=SessionEventType.TOOL_RESULT,
                        tool_name=_attr(item, "tool") or _attr(item, "name"),
                        tool_output=_as_text(_attr(item, "result")),
                    )
                return SessionEvent(
                    type=SessionEventType.TOOL_CALL,
                    tool_name=_attr(item, "tool") or _attr(item, "name"),
                    tool_input=_attr(item, "arguments") or {},
                )

            if "file_change" in kind:
                if completed:
                    return SessionEvent(
                        type=SessionEventType.TOOL_RESULT,
                        tool_name="apply_patch",
                        tool_output=_as_text(_attr(item, "changes") or _attr(item, "output")),
                    )
                return SessionEvent(
                    type=SessionEventType.TOOL_CALL, tool_name="apply_patch", tool_input={}
                )

            return SessionEvent(type=SessionEventType.OTHER, tool_name=kind or None)

        if "tokenUsage" in method or "token_usage" in method:
            usage = normalize_token_usage(_attr(payload, "token_usage") or payload)
            if usage:
                return SessionEvent(type=SessionEventType.USAGE, usage=usage)
            return None

        if method == "turn/completed" or method.endswith("turn/completed"):
            state["idle"] = True
            return SessionEvent(type=SessionEventType.SESSION_IDLE)

        if method == "error" or "error" in method:
            message = _as_text(_attr(_attr(payload, "error", payload), "message") or payload)
            state["error"] = message
            return SessionEvent(type=SessionEventType.ERROR, content=message)

        return None

    async def cleanup(self) -> None:
        """No persistent resources — each turn opens and closes its own client."""
        return None


def _combine_prompt(
    system_message: str, prompt: str, attachments: list[dict[str, Any]] | None
) -> str:
    """Compose the turn input: system preamble + prompt + inlined text docs."""
    parts: list[str] = []
    if system_message:
        parts.append(system_message)
    if attachments:
        from lucent.llm.attachments import inline_text_documents

        prompt = inline_text_documents(prompt, attachments)
    parts.append(prompt)
    return "\n\n".join(p for p in parts if p)
