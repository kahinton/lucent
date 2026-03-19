---
name: observability-operations
description: 'Procedures for monitoring Lucent via OpenTelemetry, Prometheus metrics, Jaeger traces, and Grafana dashboards'
---

# Observability Operations — Lucent Project

## Architecture

```
Lucent Server / Daemon
  │  (OTEL SDK — TracerProvider + MeterProvider)
  │  OTLP gRPC
  ▼
OTEL Collector (:4317 gRPC, :4318 HTTP)
  ├──▶ Jaeger (:16686 UI)     — trace storage + query
  └──▶ Prometheus (:9090)      — metric scrape from collector :8889
         │
         ▼
       Grafana (:3001)         — dashboards (Prometheus + Jaeger datasources)
```

**Key files:**
- `src/lucent/telemetry.py` — OTEL SDK init (TracerProvider, MeterProvider, OTLP exporters)
- `src/lucent/metrics.py` — Central metrics registry (HTTP, memory, task instruments)
- `docker/otel-collector-config.yaml` — Collector pipeline config
- `docker/prometheus.yml` — Scrape config (targets `otel-collector:8889`)
- `docker/grafana/provisioning/datasources/datasources.yaml` — Auto-provisioned Prometheus + Jaeger
- `docker/grafana/dashboards/` — `api-health.json`, `daemon-performance.json`

## Enabling Telemetry

Set these env vars on the Lucent server and/or daemon:

| Variable | Default | Purpose |
|----------|---------|---------|
| `OTEL_ENABLED` | `false` | Master switch — set `true` to activate |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | Collector gRPC address |
| `OTEL_SERVICE_NAME` | `lucent` | Service name in traces/metrics |
| `OTEL_ENVIRONMENT` | `development` | `deployment.environment` resource attr |

When `OTEL_ENABLED=false` (default), all tracers and meters are no-op with zero overhead.

OTEL packages are an optional dependency group. Install with:
```bash
pip install lucent[otel]
```

## Starting the Observability Stack

```bash
# Start all observability containers (collector, prometheus, jaeger, grafana)
docker compose --profile observability up -d

# Verify containers are running
docker ps --filter "name=lucent-otel" --filter "name=lucent-prometheus" \
  --filter "name=lucent-jaeger" --filter "name=lucent-grafana"

# Then start Lucent with telemetry enabled
OTEL_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
  docker compose up -d server
```

To stop only the observability stack:
```bash
docker compose --profile observability down
```

## Accessing Dashboards

| Service | URL | Credentials | What it shows |
|---------|-----|-------------|---------------|
| Grafana | http://localhost:3001 | `admin` / `lucent` | Pre-built dashboards for API health and daemon perf |
| Prometheus | http://localhost:9090 | None | Raw PromQL queries, target health |
| Jaeger | http://localhost:16686 | None | Distributed traces — search by service, operation, duration |

### Grafana Dashboards

- **API Health** (`api-health.json`) — HTTP request rate, latency percentiles, error rate, memory operation throughput
- **Daemon Performance** (`daemon-performance.json`) — Cognitive cycle count, task dispatch rate, session duration, active sessions

## Common PromQL Queries

Run these in Prometheus (`http://localhost:9090/graph`) or Grafana Explore.

### HTTP Latency (p50 / p95 / p99)
```promql
histogram_quantile(0.50, rate(lucent_http_request_duration_bucket[5m]))
histogram_quantile(0.95, rate(lucent_http_request_duration_bucket[5m]))
histogram_quantile(0.99, rate(lucent_http_request_duration_bucket[5m]))
```

### HTTP Request Rate (per second)
```promql
rate(lucent_http_requests_total[5m])
```

### HTTP Error Rate (4xx + 5xx as fraction of total)
```promql
rate(lucent_http_errors_total[5m]) / rate(lucent_http_requests_total[5m])
```

### Memory Search Latency (p95)
```promql
histogram_quantile(0.95, rate(lucent_memory_search_duration_bucket[5m]))
```

### Memory Operations Rate (by type)
```promql
rate(lucent_memory_operations[5m])
```

### Task Queue Depth
```promql
lucent_tasks_queue_depth
```

### Task Execution Duration (p95)
```promql
histogram_quantile(0.95, rate(lucent_tasks_execution_duration_bucket[5m]))
```

### Daemon — Cognitive Cycles Rate
```promql
rate(daemon_cognitive_cycles_total[5m])
```

### Daemon — Tasks Dispatched (by agent_type)
```promql
rate(daemon_tasks_dispatched_total[5m])
```

### Daemon — Session Duration (p95)
```promql
histogram_quantile(0.95, rate(daemon_session_duration_seconds_bucket[5m]))
```

### Daemon — Active Sessions
```promql
daemon_sessions_active
```

### Daemon — Session Error Rate
```promql
rate(daemon_sessions_total{status="error"}[5m]) / rate(daemon_sessions_total[5m])
```

## Troubleshooting

### Collector not receiving data
1. Check the collector is running: `docker logs lucent-otel-collector --tail 50`
2. Verify `OTEL_ENABLED=true` is set in the Lucent process env
3. Confirm endpoint matches: `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`
4. If running Lucent inside Docker, use `http://otel-collector:4317` (service name, not localhost)
5. Check collector debug output — it logs received spans/metrics at `basic` verbosity

### No metrics in Prometheus
1. Check Prometheus targets: http://localhost:9090/targets — `otel-collector` should show `UP`
2. Collector exposes metrics on `:8889` for Prometheus to scrape (see `docker/prometheus.yml`)
3. Metrics export every 60s (`export_interval_millis=60_000` in `telemetry.py`) — wait at least 2 minutes
4. Search raw metrics: http://localhost:9090/api/v1/label/__name__/values — look for `lucent_*` or `daemon_*`

### Missing traces in Jaeger
1. Open Jaeger UI: http://localhost:16686 — select service `lucent` from dropdown
2. Traces flow: OTEL SDK → Collector (OTLP) → Jaeger (OTLP). Check collector logs for export errors
3. Verify the operation ran while OTEL was enabled — no-op tracers produce nothing
4. FastAPI spans are auto-instrumented via `FastAPIInstrumentor`; DB spans via `AsyncPGInstrumentor`

### Trace-log correlation not working
1. `telemetry.enrich_log_record()` adds `trace_id` and `span_id` to JSON logs when a span is active
2. `bridge_correlation_id()` syncs the correlation ID ContextVar into OTEL baggage
3. Check that `sync_trace_to_correlation_id()` is called in middleware after span creation
4. Look for `trace_id` field in JSON log output — search Jaeger by that trace ID

### Grafana shows "No data"
1. Check datasource health: Grafana → Settings → Data Sources → Prometheus → "Test"
2. Datasource URL must be `http://prometheus:9090` (Docker internal network)
3. Verify Prometheus is scraping: http://localhost:9090/targets
4. Dashboard queries use `lucent_*` / `daemon_*` metric names — confirm they exist in Prometheus

## Adding New Metrics

All metrics live in `src/lucent/metrics.py` as a lazily-initialized singleton.

### 1. Add the instrument to `_MetricsRegistry`

Edit `src/lucent/metrics.py`, add inside `_ensure_initialized()`:

```python
# In _MetricsRegistry._ensure_initialized():
self.my_new_counter = meter.create_counter(
    "lucent.my_feature.operations",
    description="Count of my feature operations",
)

# Or a histogram for latency:
self.my_new_duration = meter.create_histogram(
    "lucent.my_feature.duration",
    unit="s",
    description="My feature operation duration",
)
```

### 2. Record values at call sites

```python
from lucent.metrics import metrics

# Counter
metrics.my_new_counter.add(1, {"operation": "create", "status": "success"})

# Histogram
metrics.my_new_duration.record(elapsed_seconds, {"operation": "search"})
```

### Available instrument types
- `create_counter(name)` — monotonically increasing (requests, errors)
- `create_histogram(name)` — distribution (latency, sizes)
- `create_up_down_counter(name)` — value that goes up and down (queue depth, active connections)

## Adding New Spans

### 1. Get a tracer

```python
from lucent.telemetry import get_tracer

tracer = get_tracer("lucent.my_module")
```

`get_tracer()` returns a no-op tracer when OTEL is disabled — safe to call unconditionally.

### 2. Instrument a code path

```python
# Context manager (auto-ends span):
with tracer.start_as_current_span(
    "my_module.operation_name",
    attributes={"key": "value"},
) as span:
    result = do_work()
    span.set_attribute("result.count", len(result))
```

### 3. Pattern for optional tracing (daemon style)

The daemon uses a guard pattern for spans since it holds a tracer reference:

```python
import contextlib

with self._tracer.start_as_current_span(
    "daemon.my_operation",
    attributes={"daemon.instance_id": self.instance_id},
) if self._tracer else contextlib.nullcontext():
    await do_async_work()
```

### 4. Auto-instrumented paths (no manual spans needed)

These are already instrumented automatically:
- **FastAPI HTTP requests** — `FastAPIInstrumentor` creates spans for every route
- **asyncpg DB queries** — `AsyncPGInstrumentor` wraps connection pool queries
- **Correlation IDs** — bridged to OTEL baggage via `bridge_correlation_id()`

## Log Analysis (Supplementary)

Structured logs remain available alongside OTEL telemetry:

```bash
# Server errors
docker logs lucent-server --since 1h 2>&1 | grep -i error

# Daemon session outcomes
docker compose logs daemon-1 --since 1h | grep "Session.*completed"

# Patterns indicating issues
# HARD TIMEOUT — session lifecycle hung
# at session limit — all slots occupied
# force_stop — client.stop() had to be force-killed
# Connection refused — DB or MCP server unreachable
```
