"""Structured logging configuration for Lucent.

This module provides centralized logging configuration with support for:
- JSON formatted logs for production (machine-readable)
- Human-readable logs for development
- Configurable log levels via environment variables
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


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
            ):
                log_data[key] = value

        return json.dumps(log_data, default=str)


class HumanFormatter(logging.Formatter):
    """Human-readable log formatter for development."""

    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
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
        base = f"{timestamp} {level_str} [{record.name}] {message}"

        # Add exception info if present
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)

        return base


def configure_logging() -> None:
    """Configure logging based on environment variables.

    Environment Variables:
        LUCENT_LOG_LEVEL: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
                            Default: INFO
        LUCENT_LOG_FORMAT: Log format ('json' or 'human')
                             Default: 'human' in dev mode, 'json' otherwise
    """
    log_level_str = os.environ.get("LUCENT_LOG_LEVEL", "INFO").upper()
    log_format = os.environ.get("LUCENT_LOG_FORMAT", "").lower()

    # Map string level to logging constant
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    log_level = level_map.get(log_level_str, logging.INFO)

    # Determine format based on environment
    # Default to human-readable format unless explicitly set to json
    if not log_format:
        log_format = os.environ.get("LUCENT_LOG_FORMAT", "human")

    # Create handler
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(log_level)

    # Set formatter based on format
    if log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(HumanFormatter())

    # Configure root logger for lucent
    logger = logging.getLogger("lucent")
    logger.setLevel(log_level)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False

    # Also configure uvicorn loggers to use our format
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.addHandler(handler)
        uvicorn_logger.propagate = False


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
