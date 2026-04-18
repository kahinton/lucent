"""GitHub Copilot SDK engine implementation.

Wraps the existing CopilotClient usage into the LLMEngine interface.
This is the default engine and preserves all existing behavior.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable

from lucent.llm.engine import LLMEngine, ModelNotAvailableError, SessionEvent, SessionEventType
from lucent.logging import get_logger

logger = get_logger("llm.copilot")

# Lazy import — only loaded when this engine is actually used
_CopilotClient: Any = None
_PermissionHandler: Any = None
_SubprocessConfig: Any = None
_SystemMessageReplaceConfig: Any = None
_sdk_available: bool | None = None


def _ensure_sdk() -> bool:
    """Lazily import the Copilot SDK. Returns True if available."""
    global _CopilotClient, _PermissionHandler, _SubprocessConfig, _SystemMessageReplaceConfig, _sdk_available
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

    def _make_client(self) -> Any:
        """Create a CopilotClient with SubprocessConfig."""
        config_kwargs: dict[str, Any] = {"log_level": self._log_level}
        if self._github_token:
            config_kwargs["github_token"] = self._github_token
        return _CopilotClient(config=_SubprocessConfig(**config_kwargs))

    def _make_session_kwargs(self, model: str, system_message: str, mcp_config: dict | None) -> dict:
        """Build create_session kwargs."""
        kwargs: dict[str, Any] = {
            "model": model,
            "mcp_servers": mcp_config or {},
        }
        if _PermissionHandler is not None:
            kwargs["on_permission_request"] = _PermissionHandler.approve_all
        if _SystemMessageReplaceConfig is not None:
            kwargs["system_message"] = _SystemMessageReplaceConfig(
                mode="replace", content=system_message
            )
        else:
            kwargs["system_message"] = system_message
        return kwargs

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
    ) -> str | None:
        """Run a blocking session using send_and_wait (chat pattern)."""
        if not _ensure_sdk():
            raise RuntimeError(
                "Copilot engine requires the github-copilot-sdk package. "
                "Install with: pip install github-copilot-sdk"
            )

        client = None
        try:
            client = self._make_client()
            await client.start()

            session_kwargs = self._make_session_kwargs(model, system_message, mcp_config)
            session = await client.create_session(**session_kwargs)

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
        try:
            client = self._make_client()
            await client.start()

            session_kwargs = self._make_session_kwargs(model, system_message, mcp_config)
            session = await client.create_session(**session_kwargs)

            response_parts: list[str] = []
            done = asyncio.Event()
            last_activity = time.monotonic()
            # Map tool_call_id → tool_name for matching start/complete events
            _tool_call_names: dict[str, str] = {}

            def _on_sdk_event(event: Any) -> None:
                """Translate Copilot SDK events to normalized SessionEvents."""
                nonlocal last_activity
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
                    tool_input = (
                        getattr(event.data, "arguments", None)
                        or getattr(event.data, "input", None)
                    )
                    if tool_input and not isinstance(tool_input, str):
                        import json as _json
                        try:
                            tool_input = _json.dumps(tool_input)
                        except Exception:
                            tool_input = str(tool_input)[:500]
                    normalized = SessionEvent(
                        type=SessionEventType.TOOL_CALL,
                        tool_name=tool_name,
                        content=tool_input if isinstance(tool_input, str) else str(tool_input or "")[:500],
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
                    on_event(normalized)

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
                await session.destroy()
            except Exception:
                logger.debug("Failed to destroy Copilot streaming session", exc_info=True)

            return "\n".join(response_parts) if response_parts else None

        except Exception as e:
            # Detect model-not-available JSON-RPC errors (-32603) and raise
            # a specific exception instead of silently returning None.
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
