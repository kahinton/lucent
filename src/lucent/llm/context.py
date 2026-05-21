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
_current_request_id: ContextVar[str | None] = ContextVar("current_request_id", default=None)
_current_task_id: ContextVar[str | None] = ContextVar("current_task_id", default=None)
_current_schedule_run_id: ContextVar[str | None] = ContextVar(
    "current_schedule_run_id", default=None
)
_current_agent_definition_id: ContextVar[str | None] = ContextVar(
    "current_agent_definition_id", default=None
)


def set_llm_context(
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    message_id: str | None = None,
    request_id: str | None = None,
    task_id: str | None = None,
    schedule_run_id: str | None = None,
    agent_definition_id: str | None = None,
) -> None:
    """Set request-scoped LLM lineage values."""
    _current_llm_session_id.set(session_id)
    _current_llm_turn_id.set(turn_id)
    _current_llm_message_id.set(message_id)
    _current_request_id.set(request_id)
    _current_task_id.set(task_id)
    _current_schedule_run_id.set(schedule_run_id)
    _current_agent_definition_id.set(agent_definition_id)


def clear_llm_context() -> None:
    """Clear request-scoped LLM lineage values."""
    set_llm_context(
        session_id=None,
        turn_id=None,
        message_id=None,
        request_id=None,
        task_id=None,
        schedule_run_id=None,
        agent_definition_id=None,
    )


def get_llm_context() -> dict[str, str | None]:
    """Return current LLM lineage values."""
    return {
        "session_id": _current_llm_session_id.get(),
        "turn_id": _current_llm_turn_id.get(),
        "message_id": _current_llm_message_id.get(),
        "request_id": _current_request_id.get(),
        "task_id": _current_task_id.get(),
        "schedule_run_id": _current_schedule_run_id.get(),
        "agent_definition_id": _current_agent_definition_id.get(),
    }
