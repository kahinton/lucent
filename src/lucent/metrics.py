"""Central OTEL metrics registry for Lucent.

Exposes pre-defined metric instruments via a lazily-initialized singleton.
All instruments use ``get_meter("lucent")`` from :mod:`lucent.telemetry`,
which returns a no-op meter when OTEL is disabled (zero cost).

Usage::

    from lucent.metrics import metrics
    metrics.http_request_duration.record(
        0.5, {"method": "GET", "route": "/api/health", "status_code": 200}
    )
"""

from __future__ import annotations

from lucent.telemetry import get_meter


class _MetricsRegistry:
    """Lazily-initialized container for all Lucent metric instruments."""

    def __init__(self) -> None:
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        meter = get_meter("lucent")

        # HTTP metrics
        self.http_request_duration = meter.create_histogram(
            "lucent.http.request.duration",
            unit="s",
            description="HTTP request duration",
        )
        self.http_requests_total = meter.create_counter(
            "lucent.http.requests.total",
            description="Total HTTP requests",
        )
        self.http_errors_total = meter.create_counter(
            "lucent.http.errors.total",
            description="Total HTTP errors (4xx + 5xx)",
        )

        # Memory / MCP metrics
        self.memory_operations = meter.create_counter(
            "lucent.memory.operations",
            description="Memory operations",
        )
        self.memory_search_duration = meter.create_histogram(
            "lucent.memory.search.duration",
            unit="s",
            description="Memory search latency",
        )

        # Task metrics
        self.tasks_queue_depth = meter.create_up_down_counter(
            "lucent.tasks.queue_depth",
            description="Pending tasks in queue",
        )
        self.tasks_execution_duration = meter.create_histogram(
            "lucent.tasks.execution.duration",
            unit="s",
            description="Task execution duration",
        )

        # Wake / pg_notify metrics
        self.wake_notify_total = meter.create_counter(
            "lucent.wake.notify.total",
            description="pg_notify wake signal attempts",
        )
        self.wake_notify_failures = meter.create_counter(
            "lucent.wake.notify.failures",
            description="pg_notify wake signal failures",
        )

        # Shadow forgetting comparison metrics
        self.shadow_forget_top_k_agreement = meter.create_histogram(
            "lucent.shadow_forget.top_k_agreement",
            description="Top-K agreement rate between vitality and shadow strategy",
        )
        self.shadow_forget_orphan_reclaim = meter.create_histogram(
            "lucent.shadow_forget.orphan_reclaim",
            description="Orphan reclaim rate for shadow forgetting candidates",
        )
        self.shadow_forget_load_bearing_protection = meter.create_histogram(
            "lucent.shadow_forget.load_bearing_protection",
            description="Rate of load-bearing memories vitality places in archive band",
        )
        self.shadow_forget_ldr_edges_at_risk = meter.create_histogram(
            "lucent.shadow_forget.ldr_edges_at_risk",
            description="LDR observation edges-at-risk over comparison window",
        )
        self.shadow_forget_compute_overhead = meter.create_histogram(
            "lucent.shadow_forget.compute_overhead",
            unit="s",
            description="Wall time overhead for shadow forgetting batch computation",
        )

    def __getattr__(self, name: str) -> object:
        # Trigger lazy init when any instrument attribute is accessed
        if name.startswith("_"):
            raise AttributeError(name)
        self._ensure_initialized()
        return object.__getattribute__(self, name)


metrics = _MetricsRegistry()
