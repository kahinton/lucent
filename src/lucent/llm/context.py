"""Request-scoped LLM session lineage context.

Chat/API code sets these values before tool-capable model calls. MCP auth
middleware copies the headers into ContextVars so tools invoked by an LLM can
link side effects (notably create_request) back to the originating session,
turn, and user message without trusting the model to pass those IDs itself.
"""

from __future__ import annotations

from contextvars import ContextVar

_current_llm_session_id: ContextVar[str | None] = ContextVar(
    "current_llm_session_id", default=None
)
_current_llm_turn_id: ContextVar[str | None] = ContextVar(
    "current_llm_turn_id", default=None
)
_current_llm_message_id: ContextVar[str | None] = ContextVar(
    "current_llm_message_id", default=None
)


def set_llm_context(
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    message_id: str | None = None,
) -> None:
    """Set request-scoped LLM lineage values."""
    _current_llm_session_id.set(session_id)
    _current_llm_turn_id.set(turn_id)
    _current_llm_message_id.set(message_id)


def clear_llm_context() -> None:
    """Clear request-scoped LLM lineage values."""
    set_llm_context(session_id=None, turn_id=None, message_id=None)


def get_llm_context() -> dict[str, str | None]:
    """Return current LLM lineage values."""
    return {
        "session_id": _current_llm_session_id.get(),
        "turn_id": _current_llm_turn_id.get(),
        "message_id": _current_llm_message_id.get(),
    }
