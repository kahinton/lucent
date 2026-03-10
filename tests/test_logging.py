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
        monkeypatch.delenv("LUCENT_LOG_FILE", raising=False)
        monkeypatch.delenv("LUCENT_LOG_MODULES", raising=False)
        configure_logging()
        logger = logging.getLogger("lucent")
        assert logger.level == logging.INFO
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, HumanFormatter)
        self._cleanup_loggers()

    def test_json_format(self, monkeypatch):
        monkeypatch.setenv("LUCENT_LOG_FORMAT", "json")
        monkeypatch.delenv("LUCENT_LOG_FILE", raising=False)
        monkeypatch.delenv("LUCENT_LOG_MODULES", raising=False)
        configure_logging()
        logger = logging.getLogger("lucent")
        assert isinstance(logger.handlers[0].formatter, JSONFormatter)
        self._cleanup_loggers()

    def test_custom_log_level(self, monkeypatch):
        monkeypatch.setenv("LUCENT_LOG_LEVEL", "DEBUG")
        monkeypatch.delenv("LUCENT_LOG_FORMAT", raising=False)
        monkeypatch.delenv("LUCENT_LOG_FILE", raising=False)
        monkeypatch.delenv("LUCENT_LOG_MODULES", raising=False)
        configure_logging()
        logger = logging.getLogger("lucent")
        assert logger.level == logging.DEBUG
        self._cleanup_loggers()

    def test_invalid_level_defaults_to_info(self, monkeypatch):
        monkeypatch.setenv("LUCENT_LOG_LEVEL", "NONSENSE")
        monkeypatch.delenv("LUCENT_LOG_FORMAT", raising=False)
        monkeypatch.delenv("LUCENT_LOG_FILE", raising=False)
        monkeypatch.delenv("LUCENT_LOG_MODULES", raising=False)
        configure_logging()
        logger = logging.getLogger("lucent")
        assert logger.level == logging.INFO
        self._cleanup_loggers()

    def test_configures_uvicorn_loggers(self, monkeypatch):
        monkeypatch.delenv("LUCENT_LOG_LEVEL", raising=False)
        monkeypatch.delenv("LUCENT_LOG_FORMAT", raising=False)
        monkeypatch.delenv("LUCENT_LOG_FILE", raising=False)
        monkeypatch.delenv("LUCENT_LOG_MODULES", raising=False)
        configure_logging()
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            assert len(logger.handlers) == 1
            assert logger.propagate is False
        self._cleanup_loggers()

    def test_case_insensitive_format(self, monkeypatch):
        monkeypatch.setenv("LUCENT_LOG_FORMAT", "JSON")
        monkeypatch.delenv("LUCENT_LOG_FILE", raising=False)
        monkeypatch.delenv("LUCENT_LOG_MODULES", raising=False)
        configure_logging()
        logger = logging.getLogger("lucent")
        assert isinstance(logger.handlers[0].formatter, JSONFormatter)
        self._cleanup_loggers()

    def test_file_handler_with_rotation(self, monkeypatch, tmp_path):
        from logging.handlers import RotatingFileHandler
        log_file = str(tmp_path / "test.log")
        monkeypatch.setenv("LUCENT_LOG_FILE", log_file)
        monkeypatch.setenv("LUCENT_LOG_FILE_MAX_BYTES", "1024")
        monkeypatch.setenv("LUCENT_LOG_FILE_BACKUP_COUNT", "3")
        monkeypatch.delenv("LUCENT_LOG_MODULES", raising=False)
        configure_logging()
        logger = logging.getLogger("lucent")
        # Should have both stderr and file handlers
        assert len(logger.handlers) == 2
        file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) == 1
        assert file_handlers[0].maxBytes == 1024
        assert file_handlers[0].backupCount == 3
        self._cleanup_loggers()

    def test_per_module_log_level_overrides(self, monkeypatch):
        monkeypatch.delenv("LUCENT_LOG_FILE", raising=False)
        monkeypatch.setenv("LUCENT_LOG_MODULES", "lucent.api:DEBUG,lucent.tools:WARNING")
        configure_logging()
        api_logger = logging.getLogger("lucent.api")
        tools_logger = logging.getLogger("lucent.tools")
        assert api_logger.level == logging.DEBUG
        assert tools_logger.level == logging.WARNING
        self._cleanup_loggers()
        # Reset module loggers too
        api_logger.setLevel(logging.NOTSET)
        tools_logger.setLevel(logging.NOTSET)

    def test_file_handler_writes_logs(self, monkeypatch, tmp_path):
        log_file = tmp_path / "test.log"
        monkeypatch.setenv("LUCENT_LOG_FILE", str(log_file))
        monkeypatch.delenv("LUCENT_LOG_MODULES", raising=False)
        configure_logging()
        logger = logging.getLogger("lucent")
        logger.info("test log message")
        # Flush handlers
        for h in logger.handlers:
            h.flush()
        assert log_file.exists()
        content = log_file.read_text()
        assert "test log message" in content
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


class TestCorrelationId:
    """Tests for correlation ID support."""

    def test_set_and_get_correlation_id(self):
        from lucent.logging import clear_correlation_id, get_correlation_id, set_correlation_id
        cid = set_correlation_id("test-123")
        assert cid == "test-123"
        assert get_correlation_id() == "test-123"
        clear_correlation_id()

    def test_auto_generate_correlation_id(self):
        from lucent.logging import clear_correlation_id, get_correlation_id, set_correlation_id
        cid = set_correlation_id()
        assert cid is not None
        assert len(cid) == 12
        assert get_correlation_id() == cid
        clear_correlation_id()

    def test_correlation_id_in_json_output(self):
        from lucent.logging import (
            CorrelationIdFilter,
            clear_correlation_id,
            set_correlation_id,
        )
        set_correlation_id("req-abc-123")
        try:
            formatter = JSONFormatter()
            filt = CorrelationIdFilter()
            record = logging.LogRecord(
                name="lucent.test",
                level=logging.INFO,
                pathname="test.py",
                lineno=1,
                msg="hello",
                args=(),
                exc_info=None,
            )
            filt.filter(record)
            data = json.loads(formatter.format(record))
            assert data["correlation_id"] == "req-abc-123"
        finally:
            clear_correlation_id()

    def test_correlation_id_in_human_output(self):
        from lucent.logging import (
            CorrelationIdFilter,
            clear_correlation_id,
            set_correlation_id,
        )
        set_correlation_id("req-xyz")
        try:
            formatter = HumanFormatter(use_colors=False)
            filt = CorrelationIdFilter()
            record = logging.LogRecord(
                name="lucent.test",
                level=logging.INFO,
                pathname="test.py",
                lineno=1,
                msg="hello",
                args=(),
                exc_info=None,
            )
            filt.filter(record)
            output = formatter.format(record)
            assert "[req-xyz]" in output
        finally:
            clear_correlation_id()

    def test_no_correlation_id_when_not_set(self):
        from lucent.logging import (
            CorrelationIdFilter,
            clear_correlation_id,
        )
        clear_correlation_id()
        formatter = JSONFormatter()
        filt = CorrelationIdFilter()
        record = logging.LogRecord(
            name="lucent.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        filt.filter(record)
        data = json.loads(formatter.format(record))
        assert "correlation_id" not in data
