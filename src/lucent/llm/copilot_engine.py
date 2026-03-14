"""GitHub Copilot SDK engine implementation.

Wraps the existing CopilotClient usage into the LLMEngine interface.
This is the default engine and preserves all existing behavior.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from lucent.llm.engine import LLMEngine, SessionEvent, SessionEventType
from lucent.logging import get_logger

logger = get_logger("llm.copilot")

# Lazy import — only loaded when this engine is actually used
_CopilotClient: Any = None
_PermissionHandler: Any = None
_sdk_available: bool | None = None


def _ensure_sdk() -> bool:
    """Lazily import the Copilot SDK. Returns True if available."""
    global _CopilotClient, _PermissionHandler, _sdk_available
    if _sdk_available is not None:
        return _sdk_available
    try:
        from copilot import CopilotClient, PermissionHandler

        _CopilotClient = CopilotClient
        _PermissionHandler = PermissionHandler
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
            client_opts: dict[str, Any] = {"log_level": self._log_level}
            if self._github_token:
                client_opts["github_token"] = self._github_token

            client = _CopilotClient(client_opts)
            await client.start()

            session = await client.create_session(
                {
                    "model": model,
                    "system_message": {"content": system_message},
                    "on_permission_request": _PermissionHandler.approve_all,
                    "mcp_servers": mcp_config or {},
                }
            )

            response = await session.send_and_wait(
                {"prompt": prompt},
                timeout=timeout,
            )

            result = response.data.content if response and response.data else None

            try:
                await session.disconnect()
            except Exception:
                pass

            return result

        except asyncio.TimeoutError:
            logger.warning("Copilot session timed out after %ds", timeout)
            return None
        except Exception as e:
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
        timeout: int = 600,
    ) -> str | None:
        """Run a streaming session using send + event callbacks (daemon pattern)."""
        if not _ensure_sdk():
            raise RuntimeError(
                "Copilot engine requires the github-copilot-sdk package. "
                "Install with: pip install github-copilot-sdk"
            )

        client = None
        try:
            client = _CopilotClient({"log_level": self._log_level})
            await client.start()

            session = await client.create_session(
                {
                    "model": model,
                    "system_message": {"content": system_message},
                    "on_permission_request": _PermissionHandler.approve_all,
                    "mcp_servers": mcp_config or {},
                }
            )

            response_parts: list[str] = []
            done = asyncio.Event()

            def _on_sdk_event(event: Any) -> None:
                """Translate Copilot SDK events to normalized SessionEvents."""
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
                    # Tool calls and other events
                    tool_name = getattr(event.data, "tool_name", None) or getattr(
                        event.data, "name", None
                    )
                    tool_output = None
                    if hasattr(event.data, "output"):
                        tool_output = str(event.data.output)[:300]
                    elif hasattr(event.data, "result"):
                        tool_output = str(event.data.result)[:300]
                    normalized = SessionEvent(
                        type=SessionEventType.OTHER,
                        tool_name=tool_name,
                        tool_output=tool_output,
                        raw=event,
                    )

                if on_event:
                    on_event(normalized)

            session.on(_on_sdk_event)
            await session.send({"prompt": prompt})

            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("Copilot streaming session timed out after %ds", timeout)

            # Cleanup session
            try:
                await session.destroy()
            except Exception:
                pass

            return "\n".join(response_parts) if response_parts else None

        except Exception as e:
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
                pass
