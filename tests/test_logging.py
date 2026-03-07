"""Tests for the logging module."""

import json
import logging

from lucent.logging import (
    HumanFormatter,
    JSONFormatter,
    configure_logging,
    get_logger,
)


class TestJSONFormatter:
    """Tests for JSON log formatter."""

    def _make_record(self, msg="test message", level=logging.INFO, **kwargs):
        """Helper to create a log record."""
        record = logging.LogRecord(
            name="lucent.test",
            level=level,
            pathname="test.py",
            lineno=42,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for key, value in kwargs.items():
            setattr(record, key, value)
        return record

    def test_basic_format_is_valid_json(self):
        formatter = JSONFormatter()
        record = self._make_record()
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["message"] == "test message"
        assert data["logger"] == "lucent.test"
        assert "timestamp" in data

    def test_includes_location(self):
        formatter = JSONFormatter()
        record = self._make_record()
        data = json.loads(formatter.format(record))
        assert data["location"]["file"] == "test.py"
        assert data["location"]["line"] == 42

    def test_includes_exception(self):
        formatter = JSONFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = self._make_record(exc_info=sys.exc_info())
        data = json.loads(formatter.format(record))
        assert "exception" in data
        assert "ValueError: boom" in data["exception"]

    def test_includes_stack_info(self):
        formatter = JSONFormatter()
        record = self._make_record()
        record.stack_info = "Stack (most recent call last):\n  File test.py, line 1"
        data = json.loads(formatter.format(record))
        assert "stack_info" in data
        assert "Stack (most recent call last)" in data["stack_info"]

    def test_extra_fields_included(self):
        formatter = JSONFormatter()
        record = self._make_record()
        record.request_id = "abc-123"
        data = json.loads(formatter.format(record))
        assert data["request_id"] == "abc-123"

    def test_standard_fields_excluded_from_extras(self):
        formatter = JSONFormatter()
        record = self._make_record()
        data = json.loads(formatter.format(record))
        # Standard LogRecord attributes should not appear as top-level extras
        assert "msg" not in data
        assert "args" not in data
        assert "created" not in data


class TestHumanFormatter:
    """Tests for human-readable log formatter."""

    def _make_record(self, msg="test message", level=logging.INFO, **kwargs):
        record = logging.LogRecord(
            name="lucent.test",
            level=level,
            pathname="test.py",
            lineno=42,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for key, value in kwargs.items():
            setattr(record, key, value)
        return record

    def test_basic_format(self):
        formatter = HumanFormatter(use_colors=False)
        record = self._make_record()
        output = formatter.format(record)
        assert "INFO" in output
        assert "[lucent.test]" in output
        assert "test message" in output

    def test_no_colors_when_disabled(self):
        formatter = HumanFormatter(use_colors=False)
        record = self._make_record()
        output = formatter.format(record)
        assert "\033[" not in output

    def test_includes_exception(self):
        formatter = HumanFormatter(use_colors=False)
        try:
            raise RuntimeError("test error")
        except RuntimeError:
            import sys
            record = self._make_record(exc_info=sys.exc_info())
        output = formatter.format(record)
        assert "RuntimeError: test error" in output

    def test_includes_stack_info(self):
        formatter = HumanFormatter(use_colors=False)
        record = self._make_record()
        record.stack_info = "Stack (most recent call last):\n  File test.py, line 1"
        output = formatter.format(record)
        assert "Stack (most recent call last)" in output

    def test_warning_level(self):
        formatter = HumanFormatter(use_colors=False)
        record = self._make_record(level=logging.WARNING)
        output = formatter.format(record)
        assert "WARNING" in output


class TestConfigureLogging:
    """Tests for configure_logging()."""

    def _cleanup_loggers(self):
        """Reset loggers to avoid test pollution."""
        for name in ("lucent", "uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.setLevel(logging.WARNING)

    def test_default_config(self, monkeypatch):
        monkeypatch.delenv("LUCENT_LOG_LEVEL", raising=False)
        monkeypatch.delenv("LUCENT_LOG_FORMAT", raising=False)
        configure_logging()
        logger = logging.getLogger("lucent")
        assert logger.level == logging.INFO
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, HumanFormatter)
        self._cleanup_loggers()

    def test_json_format(self, monkeypatch):
        monkeypatch.setenv("LUCENT_LOG_FORMAT", "json")
        configure_logging()
        logger = logging.getLogger("lucent")
        assert isinstance(logger.handlers[0].formatter, JSONFormatter)
        self._cleanup_loggers()

    def test_custom_log_level(self, monkeypatch):
        monkeypatch.setenv("LUCENT_LOG_LEVEL", "DEBUG")
        monkeypatch.delenv("LUCENT_LOG_FORMAT", raising=False)
        configure_logging()
        logger = logging.getLogger("lucent")
        assert logger.level == logging.DEBUG
        self._cleanup_loggers()

    def test_invalid_level_defaults_to_info(self, monkeypatch):
        monkeypatch.setenv("LUCENT_LOG_LEVEL", "NONSENSE")
        monkeypatch.delenv("LUCENT_LOG_FORMAT", raising=False)
        configure_logging()
        logger = logging.getLogger("lucent")
        assert logger.level == logging.INFO
        self._cleanup_loggers()

    def test_configures_uvicorn_loggers(self, monkeypatch):
        monkeypatch.delenv("LUCENT_LOG_LEVEL", raising=False)
        monkeypatch.delenv("LUCENT_LOG_FORMAT", raising=False)
        configure_logging()
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            assert len(logger.handlers) == 1
            assert logger.propagate is False
        self._cleanup_loggers()

    def test_case_insensitive_format(self, monkeypatch):
        monkeypatch.setenv("LUCENT_LOG_FORMAT", "JSON")
        configure_logging()
        logger = logging.getLogger("lucent")
        assert isinstance(logger.handlers[0].formatter, JSONFormatter)
        self._cleanup_loggers()


class TestGetLogger:
    """Tests for get_logger()."""

    def test_prefixes_name(self):
        logger = get_logger("test")
        assert logger.name == "lucent.test"

    def test_already_prefixed(self):
        logger = get_logger("lucent.test")
        assert logger.name == "lucent.test"

    def test_returns_logger_instance(self):
        logger = get_logger("test")
        assert isinstance(logger, logging.Logger)
