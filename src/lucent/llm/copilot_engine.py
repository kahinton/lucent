"""GitHub Copilot SDK engine implementation.

Wraps the existing CopilotClient usage into the LLMEngine interface.
This is the default engine and preserves all existing behavior.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from lucent.llm.engine import LLMEngine, ModelNotAvailableError, SessionEvent, SessionEventType
from lucent.logging import get_logger

logger = get_logger("llm.copilot")

RESTRICTED_WEB_CHAT_EXCLUDED_TOOLS = [
    "bash",
    "grep",
    "view",
    "str_replace_editor",
    "create_file",
    "edit_file",
    "run_in_terminal",
]

# Lazy import — only loaded when this engine is actually used
_CopilotClient: Any = None
_PermissionHandler: Any = None
_SubprocessConfig: Any = None
_SystemMessageReplaceConfig: Any = None
_sdk_available: bool | None = None

# Cache the resolved CLI path for the life of the process so we only run the
# filesystem / PATH checks once. Use a module-level singleton sentinel for the
# "not resolved yet" state; creating a new object() per comparison would break
# the `is` check.
_UNRESOLVED = object()
_cli_path_cache: str | None | object = _UNRESOLVED
_permission_compat_installed = False
_session_event_compat_installed = False


def _coerce_copilot_timestamp_ms(value: Any) -> int:
    """Return a millisecond timestamp for SDK/CLI payloads with mixed timestamp shapes."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Unsupported Copilot timestamp: {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return int(stripped)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"Unsupported Copilot timestamp: {value!r}") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
    raise ValueError(f"Unsupported Copilot timestamp: {value!r}")


def _value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _permission_compat_mode() -> str:
    return os.environ.get("LUCENT_COPILOT_MCP_PERMISSION_COMPAT", "auto").strip().lower()


def _legacy_permission_kind(result_kind: str) -> str | None:
    """Map SDK final permission result kinds to newer CLI UI-response kinds."""
    if result_kind == "approved":
        return "approve-once"
    if result_kind == "denied-no-approval-rule-and-could-not-request-from-user":
        return "user-not-available"
    if result_kind.startswith("denied"):
        return "reject"
    return None


def _should_use_legacy_permission_response(permission_request: Any, result: Any) -> bool:
    """Return True when the SDK/CLI permission compatibility shim should run.

    VS Code-managed Copilot CLI 1.0.4x currently routes some tool permission
    approvals through a UI-response converter that expects values such as
    ``approve-once``. The Python SDK sends final permission values such as
    ``approved``, which the CLI accepts for the permission-completed event but
    later rejects when executing the tool with ``unexpected user permission
    response``. This has been observed for mutable MCP tools and built-in
    bash/view tool calls in daemon SDK sessions.
    """
    mode = _permission_compat_mode()
    if mode in {"0", "false", "off", "disabled"}:
        return False
    if mode not in {"auto", "legacy-ui-result", "on", "true", "1"}:
        return False
    result_kind = _value(getattr(result, "kind", "")).lower()
    return _legacy_permission_kind(result_kind) is not None


async def _send_legacy_permission_response(session: Any, request_id: str, result: Any) -> None:
    result_kind = _value(getattr(result, "kind", "")).lower()
    legacy_kind = _legacy_permission_kind(result_kind)
    if not legacy_kind:
        raise ValueError(f"Unsupported permission result kind: {result_kind}")
    payload_result: dict[str, Any] = {"kind": legacy_kind}
    for attr in ("feedback", "message", "path"):
        value = getattr(result, attr, None)
        if value is not None:
            payload_result[attr] = value
    payload = {
        "requestId": request_id,
        "result": payload_result,
        "sessionId": session.session_id,
    }
    await session.rpc.permissions._client.request(  # noqa: SLF001 - SDK compat shim
        "session.permissions.handlePendingPermissionRequest",
        payload,
    )


def _install_copilot_permission_compat() -> None:
    """Install mutable-MCP permission compatibility for newer Copilot CLIs."""
    global _permission_compat_installed
    if _permission_compat_installed:
        return
    if _permission_compat_mode() in {"0", "false", "off", "disabled"}:
        _permission_compat_installed = True
        return
    try:
        import copilot.session as _session_mod
    except Exception:
        return

    session_cls = getattr(_session_mod, "CopilotSession", None)
    if session_cls is None:
        return
    original = getattr(session_cls, "_lucent_original_execute_permission_and_respond", None)
    if original is None:
        original = session_cls._execute_permission_and_respond
        setattr(session_cls, "_lucent_original_execute_permission_and_respond", original)

    async def _patched_execute_permission_and_respond(
        self, request_id, permission_request, handler
    ):
        try:
            result = handler(permission_request, {"session_id": self.session_id})
            if inspect.isawaitable(result):
                result = await result
            if _value(getattr(result, "kind", "")).lower() == "no-result":
                return
            if _should_use_legacy_permission_response(permission_request, result):
                await _send_legacy_permission_response(self, request_id, result)
                return

            async def _constant_handler(_request, _invocation):
                return result

            return await original(self, request_id, permission_request, _constant_handler)
        except Exception:
            return await original(self, request_id, permission_request, handler)

    session_cls._execute_permission_and_respond = _patched_execute_permission_and_respond
    _permission_compat_installed = True


def _install_copilot_session_event_compat() -> None:
    """Tolerate newer Copilot CLI event payloads with older SDK schemas.

    VS Code-managed Copilot CLI builds can add fields or evolve event field
    shapes before the Python SDK's generated dataclasses are regenerated. A
    recent example is `compactionTokensUsed`, which may arrive without all of
    the `cachedInput`/`input`/`output` fields the bundled schema marks as
    required. The SDK decodes notifications in an asyncio callback before
    Lucent sees the event; an AssertionError there can kill daemon sessions.
    Patch only that narrow decoder to preserve the rest of the SDK behavior.
    """
    global _session_event_compat_installed
    if _session_event_compat_installed:
        return
    try:
        import copilot.client as _client_mod
        import copilot.generated.session_events as _events_mod
    except Exception:
        return

    ping_cls = getattr(_client_mod, "PingResponse", None)
    if ping_cls is not None:
        original_ping = getattr(ping_cls, "_lucent_original_from_dict", None)
        if original_ping is None:
            original_ping = ping_cls.from_dict
            setattr(ping_cls, "_lucent_original_from_dict", original_ping)

        @staticmethod
        def _patched_ping_response_from_dict(obj: Any):
            try:
                return original_ping(obj)
            except ValueError as exc:
                if not isinstance(obj, dict) or "timestamp" not in obj:
                    raise
                try:
                    timestamp = _coerce_copilot_timestamp_ms(obj.get("timestamp"))
                except ValueError:
                    raise exc
                return ping_cls(
                    str(obj.get("message")),
                    timestamp,
                    int(obj.get("protocolVersion")),
                )

        ping_cls.from_dict = _patched_ping_response_from_dict

    compaction_classes = [
        cls
        for cls in (
            getattr(_events_mod, "CompactionTokensUsed", None),
            getattr(_events_mod, "CompactionCompleteCompactionTokensUsed", None),
        )
        if cls is not None
    ]
    if not compaction_classes:
        _session_event_compat_installed = True
        return

    def _num(obj: Any, *names: str) -> float:
        if not isinstance(obj, dict):
            return 0.0
        for name in names:
            value = obj.get(name)
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    continue
        return 0.0

    def _build_compaction_tokens(compaction_cls: Any, obj: Any) -> Any:
        if isinstance(obj, (int, float)) and not isinstance(obj, bool):
            cached_input = 0.0
            input_tokens = float(obj)
            output_tokens = 0.0
        elif isinstance(obj, str):
            cached_input = 0.0
            input_tokens = float(obj)
            output_tokens = 0.0
        elif isinstance(obj, dict):
            cached_input = _num(obj, "cachedInput", "cached_input", "cached", "cacheReadTokens")
            input_tokens = _num(obj, "input", "inputTokens", "tokens", "total", "totalTokens")
            output_tokens = _num(obj, "output", "outputTokens")
        else:
            raise ValueError(f"Unsupported compaction token payload: {obj!r}")

        fields = getattr(compaction_cls, "__dataclass_fields__", {})
        if "input_tokens" in fields:
            return compaction_cls(
                cache_read_tokens=cached_input,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        return compaction_cls(cached_input, input_tokens, output_tokens)

    for compaction_cls in compaction_classes:
        original = getattr(compaction_cls, "_lucent_original_from_dict", None)
        if original is None:
            original = compaction_cls.from_dict
            setattr(compaction_cls, "_lucent_original_from_dict", original)

        def _make_patched_compaction_tokens_from_dict(cls: Any, original_from_dict: Any):
            @staticmethod
            def _patched_compaction_tokens_from_dict(obj: Any):
                try:
                    return original_from_dict(obj)
                except Exception:
                    return _build_compaction_tokens(cls, obj)

            return _patched_compaction_tokens_from_dict

        compaction_cls.from_dict = _make_patched_compaction_tokens_from_dict(
            compaction_cls,
            original,
        )
    _session_event_compat_installed = True


def resolve_copilot_cli_path() -> str | None:
    """Return the path to the Copilot CLI this process should use, or None.

    Resolution order:
      1. ``COPILOT_CLI_PATH`` env var. If set to the sentinel value
         ``"bundled"`` (case-insensitive), skip auto-resolution and let the
         SDK use whatever binary it ships with. Any other non-empty value
         is taken literally and must point to an executable — we log a
         warning if it doesn't exist but still pass it through so failures
         are visible.
      2. ``shutil.which("copilot")`` — picks up the binary the user's shell
         would run. This is typically the VS Code–managed
         ``copilotCli/copilot`` build that tracks the latest model list.
      3. The canonical VS Code Insiders / stable paths under
         ``~/Library/Application Support/Code*/User/globalStorage/
         github.copilot-chat/copilotCli/copilot`` on macOS, and the
         equivalent on Linux (``~/.config/Code*/...``).
      4. None — let the SDK fall back to its bundled CLI.

    Rationale: the SDK currently ships a pinned CLI that can lag behind the
    VS Code–managed one by weeks, so new models (e.g. ``claude-opus-4.7``)
    aren't visible until we point at the fresher binary. In general the
    bundled install should be trusted, but we keep the override available
    for exactly this kind of lag.
    """
    global _cli_path_cache
    if _cli_path_cache is not _UNRESOLVED:
        return _cli_path_cache  # type: ignore[return-value]

    env_value = os.environ.get("COPILOT_CLI_PATH", "").strip()
    if env_value.lower() == "bundled":
        logger.info("COPILOT_CLI_PATH=bundled — using SDK's bundled CLI")
        _cli_path_cache = None
        return None
    if env_value:
        if not os.path.exists(env_value):
            logger.warning(
                "COPILOT_CLI_PATH=%s does not exist; passing through anyway so "
                "the SDK raises a clear error",
                env_value,
            )
        else:
            logger.info("Using Copilot CLI from COPILOT_CLI_PATH=%s", env_value)
        _cli_path_cache = env_value
        return env_value

    which_path = shutil.which("copilot")
    if which_path:
        logger.info("Using Copilot CLI from PATH: %s", which_path)
        _cli_path_cache = which_path
        return which_path

    candidates: list[Path] = []
    home = Path.home()
    # macOS
    candidates.extend(
        [
            home
            / "Library/Application Support/Code - Insiders/User/globalStorage/"
            "github.copilot-chat/copilotCli/copilot",
            home
            / "Library/Application Support/Code/User/globalStorage/"
            "github.copilot-chat/copilotCli/copilot",
        ]
    )
    # Linux
    candidates.extend(
        [
            home
            / ".config/Code - Insiders/User/globalStorage/"
            "github.copilot-chat/copilotCli/copilot",
            home
            / ".config/Code/User/globalStorage/"
            "github.copilot-chat/copilotCli/copilot",
        ]
    )
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            logger.info("Using Copilot CLI discovered at %s", c)
            _cli_path_cache = str(c)
            return str(c)

    logger.info(
        "No external Copilot CLI found; falling back to SDK-bundled CLI. "
        "Set COPILOT_CLI_PATH to override."
    )
    _cli_path_cache = None
    return None


def _ensure_sdk() -> bool:
    """Lazily import the Copilot SDK. Returns True if available."""
    global _CopilotClient, _PermissionHandler, _SubprocessConfig
    global _SystemMessageReplaceConfig, _sdk_available
    if _sdk_available is not None:
        return _sdk_available
    try:
        from copilot import CopilotClient, SubprocessConfig

        _CopilotClient = CopilotClient
        _SubprocessConfig = SubprocessConfig
        # PermissionHandler and SystemMessageReplaceConfig moved to
        # copilot.session in SDK >=0.2.1 (removed from top-level copilot)
        try:
            from copilot.session import PermissionHandler, SystemMessageReplaceConfig
            _PermissionHandler = PermissionHandler
            _SystemMessageReplaceConfig = SystemMessageReplaceConfig
        except ImportError:
            # Older SDK had these at top level or in copilot.types
            try:
                from copilot import PermissionHandler
                _PermissionHandler = PermissionHandler
            except ImportError:
                _PermissionHandler = None
            try:
                from copilot.types import SystemMessageReplaceConfig
                _SystemMessageReplaceConfig = SystemMessageReplaceConfig
            except ImportError:
                _SystemMessageReplaceConfig = None
        _install_copilot_permission_compat()
        _install_copilot_session_event_compat()
        _sdk_available = True
    except ImportError:
        _sdk_available = False
    return _sdk_available


class CopilotEngine(LLMEngine):
    """LLM engine backed by the GitHub Copilot SDK.

    This wraps the existing CopilotClient patterns used throughout
    the codebase into the standardized LLMEngine interface.
    """

    def __init__(self, github_token: str | None = None, log_level: str = "warning"):
        self._github_token = github_token
        self._log_level = log_level

    async def _provider_github_token(
        self,
        audit_context: dict[str, Any] | None = None,
    ) -> str | None:
        """Return a saved Copilot provider token for the request organization, if any."""
        org_id = str((audit_context or {}).get("organization_id") or "").strip()
        if not org_id:
            return None
        try:
            from lucent.model_discovery import model_provider_secret_scope
            from lucent.secrets import SecretRegistry

            secret_provider = SecretRegistry.get()
            token = await secret_provider.get(
                "model_providers.copilot.github_token",
                model_provider_secret_scope(org_id),
            )
            return (token or "").strip() or None
        except KeyError:
            return None
        except Exception:
            logger.warning("Failed to load saved Copilot provider credential", exc_info=True)
            return None

    def _make_client(self, github_token: str | None = None) -> Any:
        """Create a CopilotClient with SubprocessConfig."""
        config_kwargs: dict[str, Any] = {"log_level": self._log_level}
        token = github_token or self._github_token
        if token:
            config_kwargs["github_token"] = token
        cli_path = resolve_copilot_cli_path()
        if cli_path:
            config_kwargs["cli_path"] = cli_path
        return _CopilotClient(config=_SubprocessConfig(**config_kwargs))

    def _make_session_kwargs(
        self,
        model: str,
        system_message: str,
        mcp_config: dict | None,
        reasoning_effort: str | None = None,
        enable_config_discovery: bool = False,
        approve_permissions: bool = True,
    ) -> dict:
        """Build create_session kwargs."""
        kwargs: dict[str, Any] = {
            "model": model,
            "mcp_servers": mcp_config or {},
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if enable_config_discovery:
            kwargs["enable_config_discovery"] = True
        if not approve_permissions:
            # Web chat should use only explicitly configured MCP tools.  The
            # Copilot SDK's excluded_tools list disables provider-native tools
            # such as bash/view without suppressing configured MCP tools.
            kwargs["excluded_tools"] = list(RESTRICTED_WEB_CHAT_EXCLUDED_TOOLS)
        if _PermissionHandler is not None:
            kwargs["on_permission_request"] = _PermissionHandler.approve_all
        if _SystemMessageReplaceConfig is not None:
            kwargs["system_message"] = _SystemMessageReplaceConfig(
                mode="replace", content=system_message
            )
        else:
            kwargs["system_message"] = system_message
        return kwargs

    async def _open_session(
        self,
        client: Any,
        session_kwargs: dict[str, Any],
        *,
        provider_session_id: str | None,
        resume: bool,
    ) -> Any:
        """Create or resume a Copilot SDK session.

        Lucent owns the durable session row; the Copilot SDK owns provider-side
        history and artifacts. When resume fails (for example, the SDK session
        was pruned from disk), fall back to creating a new provider session with
        the same identifier so Lucent can continue from its DB transcript.
        """
        if provider_session_id and resume:
            try:
                return await client.resume_session(provider_session_id, **session_kwargs)
            except Exception as exc:
                logger.warning(
                    "Failed to resume Copilot session %s; creating a fresh session: %s",
                    provider_session_id,
                    exc,
                )

        create_kwargs = dict(session_kwargs)
        if provider_session_id:
            create_kwargs["session_id"] = provider_session_id
        return await client.create_session(**create_kwargs)

    @property
    def name(self) -> str:
        return "copilot"

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
    ) -> str | None:
        """Run a blocking session using send_and_wait (chat pattern)."""
        if not _ensure_sdk():
            raise RuntimeError(
                "Copilot engine requires the github-copilot-sdk package. "
                "Install with: pip install github-copilot-sdk"
            )

        client = None
        try:
            github_token = await self._provider_github_token(audit_context)
            client = self._make_client(github_token)
            await client.start()

            session_kwargs = self._make_session_kwargs(
                model,
                system_message,
                mcp_config,
                reasoning_effort=reasoning_effort,
                enable_config_discovery=enable_config_discovery,
                approve_permissions=approve_permissions,
            )
            session = await self._open_session(
                client,
                session_kwargs,
                provider_session_id=provider_session_id,
                resume=resume,
            )

            response = await session.send_and_wait(
                prompt,
                timeout=timeout,
            )

            result = response.data.content if response and response.data else None

            try:
                await session.disconnect()
            except Exception:
                logger.debug("Failed to disconnect Copilot session", exc_info=True)

            return result

        except asyncio.TimeoutError:
            logger.warning("Copilot session timed out after %ds", timeout)
            return None
        except Exception as e:
            error_msg = str(e)
            if re.search(r"Model\s+\S+\s+is not available", error_msg) or (
                "-32603" in error_msg and "not available" in error_msg.lower()
            ):
                match = re.search(r"Model\s+(\S+)\s+is not available", error_msg)
                failed_model = match.group(1) if match else model
                logger.warning(
                    "Model '%s' is not available (JSON-RPC -32603): %s",
                    failed_model,
                    error_msg,
                )
                raise ModelNotAvailableError(failed_model, e) from e
            logger.error("Copilot session failed: %s", e)
            return None
        finally:
            await self._cleanup_client(client)

    async def run_session_streaming(
        self,
        model: str,
        system_message: str,
        prompt: str,
        mcp_config: dict | None = None,
        on_event: Callable[[SessionEvent], None] | None = None,
        timeout: int = 3600,
        idle_timeout: int = 300,
        reasoning_effort: str | None = None,
        provider_session_id: str | None = None,
        resume: bool = False,
        message_history: list[dict[str, Any]] | None = None,
        hooks: list[dict[str, Any]] | None = None,
        audit_context: dict[str, Any] | None = None,
        enable_config_discovery: bool = False,
        approve_permissions: bool = True,
    ) -> str | None:
        """Run a streaming session using send + event callbacks (daemon pattern).

        Uses activity-based timeout: the session stays alive as long as events
        keep arriving. Only times out after `idle_timeout` seconds of silence.
        The hard `timeout` is a safety net for runaway sessions.
        """
        if not _ensure_sdk():
            raise RuntimeError(
                "Copilot engine requires the github-copilot-sdk package. "
                "Install with: pip install github-copilot-sdk"
            )

        client = None
        event_callback_error: Exception | None = None
        try:
            github_token = await self._provider_github_token(audit_context)
            client = self._make_client(github_token)
            await client.start()

            session_kwargs = self._make_session_kwargs(
                model,
                system_message,
                mcp_config,
                reasoning_effort=reasoning_effort,
                enable_config_discovery=enable_config_discovery,
                approve_permissions=approve_permissions,
            )
            session = await self._open_session(
                client,
                session_kwargs,
                provider_session_id=provider_session_id,
                resume=resume,
            )

            response_parts: list[str] = []
            done = asyncio.Event()
            last_activity = time.monotonic()
            # Map tool_call_id → tool_name for matching start/complete events
            _tool_call_names: dict[str, str] = {}

            def _on_sdk_event(event: Any) -> None:
                """Translate Copilot SDK events to normalized SessionEvents."""
                nonlocal event_callback_error, last_activity
                last_activity = time.monotonic()

                etype = event.type.value if hasattr(event.type, "value") else str(event.type)

                if etype == "assistant.message":
                    content = getattr(event.data, "content", None)
                    if content:
                        response_parts.append(content)
                    normalized = SessionEvent(
                        type=SessionEventType.MESSAGE,
                        content=content,
                        raw=event,
                    )
                elif etype == "assistant.message_delta":
                    normalized = SessionEvent(
                        type=SessionEventType.MESSAGE_DELTA,
                        content=getattr(event.data, "content", None),
                        raw=event,
                    )
                elif etype == "tool.execution_start":
                    tool_name = (
                        getattr(event.data, "tool_name", None)
                        or getattr(event.data, "mcp_tool_name", None)
                        or getattr(event.data, "name", None)
                    )
                    # Track tool_call_id → name for matching with completion
                    call_id = getattr(event.data, "tool_call_id", None)
                    if call_id and tool_name:
                        _tool_call_names[str(call_id)] = tool_name
                    # Extract input/arguments
                    raw_tool_input = (
                        getattr(event.data, "arguments", None)
                        or getattr(event.data, "input", None)
                    )
                    tool_input = raw_tool_input
                    if tool_input and not isinstance(tool_input, str):
                        import json as _json
                        try:
                            tool_input = _json.dumps(tool_input)
                        except Exception:
                            tool_input = str(tool_input)[:500]
                    normalized = SessionEvent(
                        type=SessionEventType.TOOL_CALL,
                        tool_name=tool_name,
                        tool_input=raw_tool_input if isinstance(raw_tool_input, dict) else None,
                        content=(
                            tool_input
                            if isinstance(tool_input, str)
                            else str(tool_input or "")[:500]
                        ),
                        raw=event,
                    )
                elif etype == "tool.execution_complete":
                    # Recover tool name via tool_call_id mapping
                    call_id = getattr(event.data, "tool_call_id", None)
                    tool_name = None
                    if call_id:
                        tool_name = _tool_call_names.pop(str(call_id), None)
                    if not tool_name:
                        tool_name = (
                            getattr(event.data, "tool_name", None)
                            or getattr(event.data, "mcp_tool_name", None)
                            or getattr(event.data, "name", None)
                        )
                    # Extract result content from Result object
                    raw_result = getattr(event.data, "result", None)
                    tool_output = None
                    raw_error = getattr(event.data, "error", None)
                    if raw_error is not None:
                        error_message = getattr(raw_error, "message", None) or str(raw_error)
                        tool_output = f"Error: {error_message}"[:2000]
                    if raw_result is not None:
                        # SDK Result objects have a .content field
                        content_val = getattr(raw_result, "content", None)
                        if content_val is not None:
                            tool_output = str(content_val)[:2000]
                        else:
                            tool_output = str(raw_result)[:2000]
                    normalized = SessionEvent(
                        type=SessionEventType.TOOL_RESULT,
                        tool_name=tool_name,
                        tool_output=tool_output,
                        raw=event,
                    )
                elif etype in ("assistant.reasoning", "assistant.reasoning_delta"):
                    normalized = SessionEvent(
                        type=SessionEventType.OTHER,
                        content=getattr(event.data, "content", None),
                        tool_name="_reasoning",
                        raw=event,
                    )
                elif etype == "session.idle":
                    done.set()
                    normalized = SessionEvent(
                        type=SessionEventType.SESSION_IDLE,
                        raw=event,
                    )
                elif "error" in etype.lower():
                    done.set()
                    normalized = SessionEvent(
                        type=SessionEventType.ERROR,
                        content=getattr(event.data, "message", str(event.data)[:200]),
                        raw=event,
                    )
                else:
                    # Other events (turn boundaries, etc.)
                    tool_name = getattr(event.data, "tool_name", None) or getattr(
                        event.data, "name", None
                    )
                    normalized = SessionEvent(
                        type=SessionEventType.OTHER,
                        tool_name=tool_name,
                        raw=event,
                    )

                if on_event:
                    try:
                        on_event(normalized)
                    except Exception as exc:
                        event_callback_error = exc
                        done.set()

            session.on(_on_sdk_event)
            await session.send(prompt)

            # Activity-based timeout loop: keep waiting as long as events arrive
            start_time = time.monotonic()
            while not done.is_set():
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    logger.warning(
                        "Copilot streaming session hit hard timeout after %ds", int(elapsed)
                    )
                    break

                idle_elapsed = time.monotonic() - last_activity
                wait_time = min(idle_timeout - idle_elapsed, timeout - elapsed, 10.0)
                if wait_time <= 0:
                    logger.warning(
                        "Copilot streaming session idle timeout after %ds of inactivity "
                        "(total elapsed: %ds)",
                        idle_timeout,
                        int(elapsed),
                    )
                    break

                try:
                    await asyncio.wait_for(done.wait(), timeout=max(wait_time, 0.1))
                except asyncio.TimeoutError:
                    # Check if we got activity during the wait
                    if event_callback_error:
                        break
                    idle_elapsed = time.monotonic() - last_activity
                    if idle_elapsed >= idle_timeout:
                        logger.warning(
                            "Copilot streaming session idle timeout after %ds of inactivity "
                            "(total elapsed: %ds)",
                            idle_timeout,
                            int(time.monotonic() - start_time),
                        )
                        break
                    # Activity happened — keep going
                    continue

            # Cleanup session
            try:
                await session.disconnect()
            except Exception:
                logger.debug("Failed to destroy Copilot streaming session", exc_info=True)

            if event_callback_error:
                raise event_callback_error

            return "\n".join(response_parts) if response_parts else None

        except Exception as e:
            # Detect model-not-available JSON-RPC errors (-32603) and raise
            # a specific exception instead of silently returning None.
            if event_callback_error is e:
                raise
            error_msg = str(e)
            if re.search(r"Model\s+\S+\s+is not available", error_msg) or (
                "-32603" in error_msg and "not available" in error_msg.lower()
            ):
                # Extract model name from the error message if possible
                match = re.search(r"Model\s+(\S+)\s+is not available", error_msg)
                failed_model = match.group(1) if match else model
                logger.warning(
                    "Model '%s' is not available (JSON-RPC -32603): %s",
                    failed_model,
                    error_msg,
                )
                raise ModelNotAvailableError(failed_model, e) from e
            logger.error("Copilot streaming session failed: %s", e)
            return None
        finally:
            await self._cleanup_client(client)

    async def cleanup(self) -> None:
        """No persistent resources to clean up for Copilot engine."""

    async def _cleanup_client(self, client: Any) -> None:
        """Clean up a CopilotClient, using force_stop as fallback."""
        if not client:
            return
        try:
            await asyncio.wait_for(client.stop(), timeout=10)
        except (asyncio.TimeoutError, Exception):
            try:
                await client.force_stop()
            except Exception:
                logger.debug("Failed to force-stop Copilot client", exc_info=True)
