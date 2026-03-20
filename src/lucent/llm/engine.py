"""Abstract base class for LLM engines.

Defines the interface that all LLM backends must implement.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable


class SessionEventType(enum.Enum):
    """Types of events emitted during an LLM session."""

    MESSAGE = "assistant.message"
    MESSAGE_DELTA = "assistant.message_delta"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    SESSION_IDLE = "session.idle"
    ERROR = "error"
    OTHER = "other"


@dataclass
class SessionEvent:
    """Normalized event from an LLM session."""

    type: SessionEventType
    content: str | None = None
    tool_name: str | None = None
    tool_output: str | None = None
    raw: Any = None  # Original event object from the backend


class LLMEngine(ABC):
    """Abstract base class for LLM backends.

    Each engine must implement run_session (blocking, returns full text)
    and run_session_streaming (event-driven, calls on_event callback).
    """

    @abstractmethod
    async def run_session(
        self,
        model: str,
        system_message: str,
        prompt: str,
        mcp_config: dict | None = None,
        timeout: int = 300,
    ) -> str | None:
        """Run a single LLM session and return the full response text.

        This is used by the chat endpoint (send_and_wait pattern).

        Args:
            model: Model identifier (e.g. "claude-opus-4.6").
            system_message: System prompt text.
            prompt: User prompt text.
            mcp_config: MCP server configuration dict for tool access.
            timeout: Maximum seconds to wait for response.

        Returns:
            The assistant's response text, or None on error.
        """

    @abstractmethod
    async def run_session_streaming(
        self,
        model: str,
        system_message: str,
        prompt: str,
        mcp_config: dict | None = None,
        on_event: Callable[[SessionEvent], None] | None = None,
        timeout: int = 600,
        idle_timeout: int = 300,
    ) -> str | None:
        """Run an LLM session with event streaming.

        This is used by the daemon (streaming events for visibility).
        The on_event callback receives normalized SessionEvent objects
        throughout the session. The method returns the full response
        text when the session completes.

        Args:
            model: Model identifier.
            system_message: System prompt text.
            prompt: User prompt text.
            mcp_config: MCP server configuration dict for tool access.
            on_event: Callback for streaming events.
            timeout: Maximum total wall-clock seconds (hard limit).
            idle_timeout: Seconds of inactivity before timing out. If the
                agent is actively producing events, the session continues
                indefinitely (up to `timeout`). Default 300s (5 min).
            timeout: Maximum seconds to wait for completion.

        Returns:
            The assistant's full response text, or None on error.
        """

    @abstractmethod
    async def cleanup(self) -> None:
        """Release any resources held by the engine."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable engine name."""
