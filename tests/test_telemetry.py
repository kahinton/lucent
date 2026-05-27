"""Tests for lucent.telemetry module.

Validates OTEL initialization, shutdown, provider helpers, and
correlation-ID bridging. Uses in-memory exporters when OTEL is enabled
and verifies no-op behaviour when disabled.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers to reset module-level state between tests
# ---------------------------------------------------------------------------

def _reset_telemetry():
    """Reset telemetry module singletons so each test starts clean."""
    import lucent.telemetry as mod
    mod._initialized = False
    mod._otel_enabled = False


@pytest.fixture(autouse=True)
def _clean_telemetry():
    """Ensure every test starts with fresh telemetry state."""
    _reset_telemetry()
    yield
    _reset_telemetry()


# ---------------------------------------------------------------------------
# Disabled / default path
# ---------------------------------------------------------------------------

class TestTelemetryDisabled:
    """When OTEL_ENABLED is unset or false, telemetry is a no-op."""

    def test_init_disabled_by_default(self):
        from lucent.telemetry import init_telemetry, is_enabled
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OTEL_ENABLED", None)
            init_telemetry()
        assert is_enabled() is False

    def test_init_disabled_explicit(self):
        from lucent.telemetry import init_telemetry, is_enabled
        with patch.dict(os.environ, {"OTEL_ENABLED": "false"}):
            init_telemetry()
        assert is_enabled() is False

    def test_get_tracer_returns_noop(self):
        from lucent.telemetry import get_tracer
        tracer = get_tracer("test")
        # NoOpTracer should not raise on start_span
        span = tracer.start_span("noop")
        span.end()

    def test_get_meter_returns_noop(self):
        from lucent.telemetry import get_meter
        meter = get_meter("test")
        counter = meter.create_counter("test.counter")
        counter.add(1)  # should not raise

    def test_shutdown_noop_safe(self):
        from lucent.telemetry import shutdown_telemetry
        shutdown_telemetry()  # should not raise

    def test_enrich_log_record_noop(self):
        from lucent.telemetry import enrich_log_record
        record = {"message": "hello"}
        result = enrich_log_record(record)
        assert result is record
        assert "trace_id" not in result

    def test_bridge_correlation_id_noop(self):
        from lucent.telemetry import bridge_correlation_id
        assert bridge_correlation_id() is None

    def test_unbind_correlation_id_noop(self):
        from lucent.telemetry import unbind_correlation_id
        unbind_correlation_id(None)  # should not raise


# ---------------------------------------------------------------------------
# Enabled path (requires opentelemetry packages)
# ---------------------------------------------------------------------------

otel_available = pytest.importorskip("opentelemetry", reason="opentelemetry not installed")


class TestTelemetryEnabled:
    """When OTEL_ENABLED=true and packages are installed."""

    @pytest.fixture(autouse=True)
    def _enable_otel(self):
        """Enable OTEL for every test in this class."""
        with patch.dict(os.environ, {
            "OTEL_ENABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
        }):
            yield

    def test_init_sets_enabled(self):
        from lucent.telemetry import init_telemetry, is_enabled
        init_telemetry()
        assert is_enabled() is True

    def test_init_idempotent(self):
        from lucent.telemetry import init_telemetry, is_enabled
        init_telemetry()
        init_telemetry()  # second call should be no-op
        assert is_enabled() is True

    def test_get_tracer_returns_real(self):
        from lucent.telemetry import get_tracer, init_telemetry
        init_telemetry()
        tracer = get_tracer("test.module")
        assert tracer is not None
        span = tracer.start_span("test-span")
        ctx = span.get_span_context()
        assert ctx.is_valid
        span.end()

    def test_get_meter_returns_real(self):
        from lucent.telemetry import get_meter, init_telemetry
        init_telemetry()
        meter = get_meter("test.module")
        counter = meter.create_counter("test.counter", unit="1", description="test")
        counter.add(1)  # should not raise

    def test_shutdown_flushes(self):
        from lucent.telemetry import init_telemetry, is_enabled, shutdown_telemetry
        init_telemetry()
        assert is_enabled() is True
        shutdown_telemetry()
        assert is_enabled() is False

    def test_enrich_log_record_adds_trace(self):
        from opentelemetry import trace

        from lucent.telemetry import enrich_log_record, init_telemetry

        init_telemetry()
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("log-test"):
            record: dict = {"message": "hello"}
            result = enrich_log_record(record)
            assert "trace_id" in result
            assert "span_id" in result
            assert len(result["trace_id"]) == 32  # 128-bit hex

    def test_bridge_correlation_id(self):
        from opentelemetry import trace

        from lucent.logging import correlation_id_var
        from lucent.telemetry import (
            bridge_correlation_id,
            init_telemetry,
            unbind_correlation_id,
        )

        init_telemetry()
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("bridge-test") as span:
            correlation_id_var.set("abc123def456")
            token = bridge_correlation_id()
            assert token is not None
            # Verify span attribute was set
            assert span.attributes.get("lucent.correlation_id") == "abc123def456"
            unbind_correlation_id(token)

        # Clean up ContextVar
        correlation_id_var.set(None)

    def test_resource_attributes(self):
        from opentelemetry import trace

        from lucent.telemetry import init_telemetry

        init_telemetry()
        provider = trace.get_tracer_provider()
        if hasattr(provider, "resource"):
            attrs = dict(provider.resource.attributes)
            # service.name is set (value depends on global provider state)
            assert "service.name" in attrs
            assert "service.version" in attrs
            assert "deployment.environment" in attrs
