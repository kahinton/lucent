"""Request-scoped logging context based on contextvars.

This module stores request_id and user_id in ``ContextVar`` values so each
asyncio task keeps isolated values.
"""

from contextvars import ContextVar

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


def set_request_id(request_id: str | None) -> None:
    """Set the request ID for the current execution context."""
    _request_id_var.set(request_id)


def get_request_id() -> str | None:
    """Get the request ID for the current execution context."""
    return _request_id_var.get()


def clear_request_id() -> None:
    """Clear the request ID for the current execution context."""
    _request_id_var.set(None)


def set_user_id(user_id: str | None) -> None:
    """Set the user ID for the current execution context."""
    _user_id_var.set(user_id)


def get_user_id() -> str | None:
    """Get the user ID for the current execution context."""
    return _user_id_var.get()


def clear_user_id() -> None:
    """Clear the user ID for the current execution context."""
    _user_id_var.set(None)


def clear_log_context() -> None:
    """Clear all request-scoped logging context values."""
    clear_request_id()
    clear_user_id()
