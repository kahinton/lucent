"""Structured logging configuration for Lucent.

This module provides centralized logging configuration with support for:
- JSON formatted logs for production (machine-readable)
- Human-readable logs for development
- Configurable log levels via environment variables
- Request correlation IDs for tracing across the stack
"""

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

# Custom log levels for daemon visibility
THOUGHT = 15  # Between DEBUG(10) and INFO(20) — full output dumps
STREAM = 12  # Between DEBUG(10) and THOUGHT(15) — real-time event tracking

logging.addLevelName(THOUGHT, "THOUGHT")
logging.addLevelName(STREAM, "STREAM")

# Correlation ID context variable — set per-request by middleware
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Get the current correlation ID from context."""
    return correlation_id_var.get()


_UNSET = object()


def set_correlation_id(cid: str | None = _UNSET) -> str | None:
    """Set a correlation ID in context. Generates one if not provided.

    Pass None explicitly to clear the correlation ID.
    """
    if cid is _UNSET:
        cid = uuid.uuid4().hex[:12]
    correlation_id_var.set(cid)
    return cid


def clear_correlation_id() -> None:
    """Clear the correlation ID from context."""
    correlation_id_var.set(None)


class CorrelationIdFilter(logging.Filter):
    """Inject correlation_id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()  # type: ignore[attr-defined]
        return True


class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging in production."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_data: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add correlation ID if present
        cid = getattr(record, "correlation_id", None)
        if cid:
            log_data["correlation_id"] = cid

        # Add location info
        if record.pathname:
            log_data["location"] = {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            }

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Add stack info if present (from stack_info=True)
        if record.stack_info:
            log_data["stack_info"] = record.stack_info

        # Add any extra fields
        for key, value in record.__dict__.items():
            if key not in (
                "name",
                "msg",
                "args",
                "created",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "exc_info",
                "exc_text",
                "message",
                "thread",
                "threadName",
                "taskName",
                "correlation_id",
            ):
                log_data[key] = value

        return json.dumps(log_data, default=str)


class HumanFormatter(logging.Formatter):
    """Human-readable log formatter for development."""

    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "STREAM": "\033[90m",  # Bright black (gray) — real-time events
        "THOUGHT": "\033[94m",  # Bright blue — full output dumps
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def __init__(self, use_colors: bool = True):
        super().__init__()
        self.use_colors = use_colors and sys.stderr.isatty()

    def format(self, record: logging.LogRecord) -> str:
        """Format log record for human readability."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        level = record.levelname

        if self.use_colors:
            color = self.COLORS.get(level, "")
            level_str = f"{color}{level:<8}{self.RESET}"
        else:
            level_str = f"{level:<8}"

        message = record.getMessage()
        cid = getattr(record, "correlation_id", None)
        cid_str = f" [{cid}]" if cid else ""
        base = f"{timestamp} {level_str} [{record.name}]{cid_str} {message}"

        # Add exception info if present
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)

        # Add stack info if present (from stack_info=True)
        if record.stack_info:
            base += "\n" + record.stack_info

        return base


def _parse_level(level_str: str) -> int:
    """Parse a log level string to a logging constant."""
    level_map = {
        "DEBUG": logging.DEBUG,
        "STREAM": STREAM,
        "THOUGHT": THOUGHT,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    return level_map.get(level_str.upper(), logging.INFO)


def _make_handler(
    log_format: str,
    log_level: int,
    log_file: str | None = None,
    max_bytes: int = 10_485_760,
    backup_count: int = 5,
) -> logging.Handler:
    """Create a configured log handler (stderr or rotating file).

    Args:
        log_format: 'json' or 'human'.
        log_level: Logging level constant.
        log_file: If provided, write to this file with rotation.
        max_bytes: Max bytes per file before rotation (default 10MB).
        backup_count: Number of rotated files to keep (default 5).
    """
    if log_file:
        handler: logging.Handler = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count
        )
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setLevel(log_level)

    if log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(HumanFormatter(use_colors=not bool(log_file)))

    handler.addFilter(CorrelationIdFilter())
    return handler


def configure_logging() -> None:
    """Configure logging based on environment variables.

    Environment Variables:
        LUCENT_LOG_LEVEL: Root log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
                          Default: INFO
        LUCENT_LOG_FORMAT: Log format ('json' or 'human').
                           Default: 'human'
        LUCENT_LOG_FILE: Optional file path for log output with automatic rotation.
                         When set, logs go to both stderr AND the file.
        LUCENT_LOG_FILE_MAX_BYTES: Max bytes per log file before rotation.
                                   Default: 10485760 (10 MB)
        LUCENT_LOG_FILE_BACKUP_COUNT: Number of rotated log files to keep.
                                      Default: 5
        LUCENT_LOG_MODULES: Per-module log level overrides, comma-separated.
                            Format: 'module:LEVEL,module:LEVEL'
                            Example: 'lucent.api:DEBUG,lucent.tools:WARNING'
    """
    log_level_str = os.environ.get("LUCENT_LOG_LEVEL", "INFO").upper()
    log_format = os.environ.get("LUCENT_LOG_FORMAT", "human").lower()
    log_file = os.environ.get("LUCENT_LOG_FILE")
    max_bytes = int(os.environ.get("LUCENT_LOG_FILE_MAX_BYTES", "10485760"))
    backup_count = int(os.environ.get("LUCENT_LOG_FILE_BACKUP_COUNT", "5"))
    module_overrides = os.environ.get("LUCENT_LOG_MODULES", "")

    log_level = _parse_level(log_level_str)

    # Always create stderr handler
    stderr_handler = _make_handler(log_format, log_level)

    handlers: list[logging.Handler] = [stderr_handler]

    # Optionally add rotating file handler
    if log_file:
        file_handler = _make_handler(
            log_format,
            log_level,
            log_file=log_file,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
        handlers.append(file_handler)

    # Configure root logger for lucent
    logger = logging.getLogger("lucent")
    logger.setLevel(log_level)
    logger.handlers.clear()
    for h in handlers:
        logger.addHandler(h)
    logger.propagate = False

    # Also configure uvicorn loggers to use our format
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers.clear()
        for h in handlers:
            uvicorn_logger.addHandler(h)
        uvicorn_logger.propagate = False

    # Apply per-module log level overrides
    if module_overrides:
        for override in module_overrides.split(","):
            override = override.strip()
            if ":" not in override:
                continue
            module_name, level_str = override.rsplit(":", 1)
            module_logger = logging.getLogger(module_name.strip())
            module_logger.setLevel(_parse_level(level_str.strip()))


def get_logger(name: str) -> logging.Logger:
    """Get a properly configured logger.

    Args:
        name: The logger name. Will be prefixed with 'lucent.' if not already.

    Returns:
        A configured logger instance.
    """
    if not name.startswith("lucent."):
        name = f"lucent.{name}"
    return logging.getLogger(name)
