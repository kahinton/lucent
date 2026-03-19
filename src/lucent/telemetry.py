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


def init_telemetry(service_name: str | None = None) -> None:
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
        service_name: Override service name. Defaults to OTEL_SERVICE_NAME
                      env var, then "lucent".
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

    name = service_name or os.getenv("OTEL_SERVICE_NAME", "lucent")
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

    Returns a no-op tracer when OTEL is not enabled.
    """
    from opentelemetry import trace
    return trace.get_tracer(name)


def get_meter(name: str = "lucent") -> metrics_api.Meter:
    """Get a named Meter instance.

    Returns a no-op meter when OTEL is not enabled.
    """
    from opentelemetry import metrics
    return metrics.get_meter(name)


def enrich_log_record(record: dict) -> dict:
    """Add trace context fields to a log record dict.

    Bridges OTEL trace context into structured log output. When OTEL is
    active and a span is in context, adds trace_id, span_id, and trace_flags.
    Also updates the correlation_id to use the trace_id for consistency.

    Args:
        record: Mutable log record dict (e.g., from JSONFormatter).

    Returns:
        The same dict, enriched with trace context if available.
    """
    if not _is_enabled():
        return record

    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            record["trace_id"] = format(ctx.trace_id, "032x")
            record["span_id"] = format(ctx.span_id, "016x")
            record["trace_flags"] = ctx.trace_flags
    except Exception:
        pass

    return record
