"""OpenTelemetry initialization for Lucent.

Provides centralized OTEL setup with TracerProvider, MeterProvider, and helpers.
All telemetry is opt-in via OTEL_ENABLED=true env var.
When disabled, uses no-op providers (zero overhead).

Usage:
    from lucent.telemetry import init_telemetry, get_tracer, get_meter

    init_telemetry()  # Call once at startup
    tracer = get_tracer("my.module")
    meter = get_meter("my.module")
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING

from lucent.logging import correlation_id_var

if TYPE_CHECKING:
    from opentelemetry import metrics as metrics_api
    from opentelemetry import trace as trace_api

logger = logging.getLogger("lucent.telemetry")

_initialized = False
_otel_enabled = False


class _NoOpSpan:
    """Minimal no-op span for when OTEL packages aren't installed."""

    def is_recording(self) -> bool:
        return False

    def set_attribute(self, key: str, value: object) -> None:
        pass

    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _NoOpTracer:
    """Minimal no-op tracer for when OTEL packages aren't installed."""

    def start_span(self, name: str, **kwargs: object) -> _NoOpSpan:
        return _NoOpSpan()

    def start_as_current_span(self, name: str, **kwargs: object) -> _NoOpSpan:
        return _NoOpSpan()


class _NoOpMeter:
    """Minimal no-op meter for when OTEL packages aren't installed."""

    def __init__(self, name: str = "") -> None:
        self._name = name

    def create_counter(self, name: str, **kwargs: object) -> _NoOpInstrument:
        return _NoOpInstrument()

    def create_histogram(self, name: str, **kwargs: object) -> _NoOpInstrument:
        return _NoOpInstrument()

    def create_up_down_counter(self, name: str, **kwargs: object) -> _NoOpInstrument:
        return _NoOpInstrument()

    def create_observable_gauge(self, name: str, **kwargs: object) -> _NoOpInstrument:
        return _NoOpInstrument()


class _NoOpInstrument:
    """No-op metric instrument."""

    def add(self, amount: float = 1, **kwargs: object) -> None:
        pass

    def record(self, amount: float = 0, **kwargs: object) -> None:
        pass


# Check if OTEL packages are installed (optional dependency group)
try:
    from opentelemetry import context as _otel_context  # noqa: F401
    from opentelemetry import metrics as _otel_metrics
    from opentelemetry import trace as _otel_trace
    from opentelemetry.baggage import set_baggage as _set_baggage
    from opentelemetry.context import attach as _attach
    from opentelemetry.context import detach as _detach

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False


def _is_enabled() -> bool:
    """Check if OTEL telemetry is enabled via environment."""
    return os.getenv("OTEL_ENABLED", "false").lower() == "true"


def is_enabled() -> bool:
    """Check if OTEL telemetry is actively enabled and initialized."""
    return _otel_enabled


def _get_version() -> str:
    """Get the current Lucent version from package metadata."""
    try:
        from importlib.metadata import version
        return version("lucent")
    except Exception:
        return "0.0.0"


def init_telemetry(service_name: str = "lucent") -> None:
    """Initialize OpenTelemetry providers.

    Sets up TracerProvider and MeterProvider with OTLP gRPC exporters.
    When OTEL_ENABLED is false (default), this is a no-op — the global
    providers remain NoOp implementations with zero overhead.

    Environment variables:
        OTEL_ENABLED: Set to 'true' to enable (default: false)
        OTEL_EXPORTER_OTLP_ENDPOINT: Collector address (default: http://localhost:4317)
        OTEL_SERVICE_NAME: Service name attribute (default: lucent)
        OTEL_ENVIRONMENT: Deployment environment (default: development)

    Args:
        service_name: Service name for resource attributes. Defaults to "lucent".
                      Can be overridden by OTEL_SERVICE_NAME env var.
    """
    global _initialized, _otel_enabled
    if _initialized:
        logger.debug("Telemetry already initialized, skipping")
        return
    _initialized = True

    if not _is_enabled():
        logger.info("Telemetry disabled (set OTEL_ENABLED=true to enable)")
        _otel_enabled = False
        return

    if not _HAS_OTEL:
        logger.warning(
            "OTEL_ENABLED=true but opentelemetry packages not installed. "
            "Install with: pip install lucent[otel]"
        )
        _otel_enabled = False
        return

    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    name = os.getenv("OTEL_SERVICE_NAME", service_name)
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    resource = Resource.create({
        "service.name": name,
        "service.version": _get_version(),
        "service.instance.id": uuid.uuid4().hex[:12],
        "deployment.environment": os.getenv("OTEL_ENVIRONMENT", "development"),
    })

    # Traces — BatchSpanProcessor with OTLP gRPC exporter
    span_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Metrics — PeriodicExportingMetricReader with OTLP gRPC exporter
    metric_exporter = OTLPMetricExporter(endpoint=endpoint, insecure=True)
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter, export_interval_millis=60_000
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    _otel_enabled = True
    logger.info(
        "Telemetry initialized — exporting to %s (service=%s)",
        endpoint,
        name,
    )


def shutdown_telemetry() -> None:
    """Gracefully flush and shutdown telemetry providers.

    Safe to call even when OTEL is disabled or not installed (no-op).
    """
    global _initialized, _otel_enabled

    if not _otel_enabled or not _HAS_OTEL:
        _initialized = False
        return

    tracer_provider = _otel_trace.get_tracer_provider()
    if hasattr(tracer_provider, "shutdown"):
        try:
            tracer_provider.shutdown()
        except Exception:
            logger.warning("Error shutting down TracerProvider", exc_info=True)

    meter_provider = _otel_metrics.get_meter_provider()
    if hasattr(meter_provider, "shutdown"):
        try:
            meter_provider.shutdown()
        except Exception:
            logger.warning("Error shutting down MeterProvider", exc_info=True)

    _initialized = False
    _otel_enabled = False
    logger.info("Telemetry shut down")


def get_tracer(name: str = "lucent") -> trace_api.Tracer:
    """Get a named Tracer instance.

    Returns a no-op tracer when OTEL is not enabled or not installed.
    Safe to call even if opentelemetry packages are not installed.
    """
    if _HAS_OTEL:
        return _otel_trace.get_tracer(name)
    # Return a minimal no-op when OTEL packages aren't installed
    return _NoOpTracer()  # type: ignore[return-value]


def get_meter(name: str = "lucent") -> metrics_api.Meter:
    """Get a named Meter instance.

    Returns a no-op meter when OTEL is not enabled or not installed.
    Safe to call even if opentelemetry packages are not installed.
    """
    if _HAS_OTEL:
        return _otel_metrics.get_meter(name)
    # Return a minimal no-op when OTEL packages aren't installed
    return _NoOpMeter(name)  # type: ignore[return-value]


def enrich_log_record(record: dict) -> dict:
    """Add trace context fields to a log record dict.

    Bridges OTEL trace context into structured log output. When OTEL is
    active and a span is in context, adds trace_id, span_id, and trace_flags.

    Args:
        record: Mutable log record dict (e.g., from JSONFormatter).

    Returns:
        The same dict, enriched with trace context if available.
    """
    if not _otel_enabled or not _HAS_OTEL:
        return record

    try:
        span = _otel_trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            record["trace_id"] = format(ctx.trace_id, "032x")
            record["span_id"] = format(ctx.span_id, "016x")
            record["trace_flags"] = ctx.trace_flags
    except Exception:
        pass

    return record


def bridge_correlation_id() -> object | None:
    """Bridge the current correlation_id into OTEL baggage and span attributes.

    Reads the correlation ID from logging.correlation_id_var and sets it
    as OTEL baggage under the key 'correlation.id'. Also sets it as a
    span attribute on the current active span if one exists.

    Returns a context token that should be passed to unbind_correlation_id()
    to detach the baggage, or None if OTEL is not active.

    Usage in middleware::

        token = bridge_correlation_id()
        # ... handle request ...
        if token is not None:
            unbind_correlation_id(token)
    """
    if not _otel_enabled or not _HAS_OTEL:
        return None

    cid = correlation_id_var.get()
    if cid is None:
        return None

    # Set as OTEL baggage for cross-service propagation
    ctx = _set_baggage("lucent.correlation_id", cid)
    token = _attach(ctx)

    # Also set as span attribute on the current span
    span = _otel_trace.get_current_span()
    if span.is_recording():
        span.set_attribute("lucent.correlation_id", cid)

    return token


def unbind_correlation_id(token: object) -> None:
    """Detach a previously bridged correlation ID from OTEL context.

    Args:
        token: The token returned by bridge_correlation_id().
    """
    if not _HAS_OTEL or token is None:
        return
    _detach(token)


def sync_trace_to_correlation_id() -> None:
    """Propagate the current OTEL trace_id into the correlation ID ContextVar.

    When OTEL is active and a valid trace context exists, this sets the
    32-char hex trace_id as the correlation ID. If a correlation ID already
    exists, both are preserved — the trace_id is set as a span attribute
    ``lucent.trace_id`` and the existing correlation ID is kept.

    Call this in middleware after the OTEL span has been created to ensure
    log records carry the trace_id for log-trace correlation.
    """
    if not _otel_enabled or not _HAS_OTEL:
        return

    try:
        span = _otel_trace.get_current_span()
        ctx = span.get_span_context()
        if ctx is None or not ctx.is_valid:
            return

        trace_id_hex = format(ctx.trace_id, "032x")
        existing_cid = correlation_id_var.get()

        if existing_cid is None:
            # No correlation ID yet — use trace_id
            correlation_id_var.set(trace_id_hex)
        else:
            # Keep existing correlation ID, but record trace_id on the span
            if span.is_recording():
                span.set_attribute("lucent.trace_id", trace_id_hex)
    except Exception:
        logger.debug("Failed to sync trace_id to correlation_id", exc_info=True)
