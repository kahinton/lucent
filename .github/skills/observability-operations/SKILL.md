---
name: observability-operations
description: 'OTEL-based observability for Lucent — traces, metrics, dashboards, and troubleshooting via OpenTelemetry Collector, Prometheus, Jaeger, and Grafana'
---

# Observability Operations — Lucent OTEL Stack

## Architecture Overview

```
┌──────────────┐   ┌──────────────┐
│  Lucent API  │   │   Daemon     │
│  (FastAPI)   │   │  (4 loops)   │
│              │   │              │
│ OTEL SDK     │   │ OTEL SDK     │
│ ─traces      │   │ ─traces      │
│ ─metrics     │   │ ─metrics     │
└──────┬───────┘   └──────┬───────┘
       │  OTLP gRPC :4317 │
       └────────┬──────────┘
                ▼
  ┌─────────────────────────┐
  │   OTEL Collector        │
  │   (contrib:0.96.0)      │
  │                         │
  │  Receivers: otlp        │
  │  Processors: batch      │
  │  Exporters:             │
  │   ─prometheus (:8889)   │
  │   ─otlp/jaeger          │
  └────┬──────────┬─────────┘
       │          │
       ▼          ▼
┌───────────┐ ┌──────────┐
│Prometheus │ │  Jaeger  │
│  (:9090)  │ │ (:16686) │
└─────┬─────┘ └──────────┘
      │
      ▼
┌───────────┐
│  Grafana  │
│  (:3001)  │
└───────────┘
```

**Data flow**: Lucent services emit traces and metrics via the OTEL SDK using OTLP gRPC to the Collector. The Collector batches data, then exports metrics to Prometheus (scraped on `:8889`) and traces to Jaeger (OTLP gRPC). Grafana reads from both Prometheus and Jaeger as data sources.

**Key files**:
- `src/lucent/telemetry.py` — OTEL SDK initialization (TracerProvider, MeterProvider, OTLP exporters)
- `src/lucent/metrics.py` — Application metric instruments registry
- `src/lucent/api/app.py` — FastAPI auto-instrumentation + HTTP metrics middleware
- `docker/otel-collector-config.yaml` — Collector pipeline config
- `docker/prometheus.yml` — Prometheus scrape config (targets `otel-collector:8889`)
- `docker/grafana/provisioning/` — Auto-provisioned Prometheus + Jaeger data sources
- `docker/grafana/dashboards/` — Pre-built dashboards (`api-health.json`, `daemon-performance.json`)

## Enabling Telemetry

Telemetry is **opt-in** — disabled by default so it doesn't affect existing deployments.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_ENABLED` | `false` | Master switch. Set to `true` to enable all telemetry. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | Collector gRPC address. In docker-compose: `http://otel-collector:4317` |
| `OTEL_SERVICE_NAME` | `lucent` | Service name in resource attributes |
| `OTEL_ENVIRONMENT` | `development` | `deployment.environment` resource attribute |

### Enable in docker-compose

Add to `.env` or export before running:

```bash
export OTEL_ENABLED=true
```

The `docker-compose.yml` already passes this through:
```yaml
OTEL_ENABLED: ${OTEL_ENABLED:-false}
OTEL_EXPORTER_OTLP_ENDPOINT: ${OTEL_EXPORTER_OTLP_ENDPOINT:-http://otel-collector:4317}
```

### Enable for local development (no Docker)

```bash
pip install lucent[otel]
export OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
```

### What happens when enabled

1. `init_telemetry()` creates a `TracerProvider` with `BatchSpanProcessor` → OTLP gRPC exporter
2. `init_telemetry()` creates a `MeterProvider` with `PeriodicExportingMetricReader` (60s interval) → OTLP gRPC exporter
3. `FastAPIInstrumentor` auto-instruments all HTTP routes (excluding `/api/health`)
4. `AsyncPGInstrumentor` auto-instruments all database queries
5. HTTP metrics middleware records `lucent.http.request.duration`, `.requests.total`, `.errors.total`
6. Correlation IDs bridge into OTEL: `sync_trace_to_correlation_id()` sets trace_id as correlation ID, `bridge_correlation_id()` propagates to baggage

### What happens when disabled (default)

- Zero OTEL SDK imports — `get_tracer()` and `get_meter()` return no-op stubs
- `_NoOpTracer`, `_NoOpMeter`, `_NoOpInstrument` handle all API calls with no overhead
- No auto-instrumentation middleware attached
- Existing structured JSON logging + correlation IDs work unchanged

## Starting the Observability Stack

The observability services use a Docker Compose profile — they only start when explicitly requested.

```bash
# Start everything: core services + observability
OTEL_ENABLED=true docker compose --profile observability up -d

# Or start observability separately from an already-running stack
docker compose --profile observability up -d

# Verify all services are running
docker compose --profile observability ps
```

**Expected containers**:

| Container | Image | Ports |
|-----------|-------|-------|
| `lucent-otel-collector` | `otel/opentelemetry-collector-contrib:0.96.0` | `:4317` (gRPC), `:4318` (HTTP), `:8889` (Prometheus) |
| `lucent-prometheus` | `prom/prometheus:v2.51.0` | `:9090` |
| `lucent-jaeger` | `jaegertracing/all-in-one:1.55` | `:16686` (UI) |
| `lucent-grafana` | `grafana/grafana:10.4.1` | `:3001` |

**Startup order**: `jaeger` → `otel-collector` → `prometheus` → `grafana`

### Stop the observability stack

```bash
docker compose --profile observability down
# This does not affect the core lucent and postgres services
```

## Accessing Dashboards

### Grafana — `http://localhost:3001`

- **Default login**: `admin` / `lucent` (override with `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`)
- **Pre-provisioned data sources**: Prometheus (`http://prometheus:9090`), Jaeger (`http://jaeger:16686`)
- **Pre-provisioned dashboards** (in Lucent folder):
  - **API Health** (`api-health.json`) — HTTP request rate, latency percentiles, error rate, memory operation throughput
  - **Daemon Performance** (`daemon-performance.json`) — Cognitive cycle count, task dispatch rate, session duration, active sessions

### Jaeger — `http://localhost:16686`

- Select service `lucent` (or `lucent-api`) from the dropdown
- View distributed traces across HTTP requests → DB queries
- Find slow requests by sorting by duration

### Prometheus — `http://localhost:9090`

- Direct PromQL query interface at `/graph`
- Useful for ad-hoc queries and verifying metrics are being collected
- Targets page (`/targets`) shows scrape health for the OTEL Collector

## Common PromQL Queries

Run these in Prometheus (`http://localhost:9090/graph`) or Grafana Explore.

### HTTP Latency (p50 / p95 / p99)
```promql
histogram_quantile(0.50, rate(lucent_http_request_duration_bucket[5m]))
histogram_quantile(0.95, rate(lucent_http_request_duration_bucket[5m]))
histogram_quantile(0.99, rate(lucent_http_request_duration_bucket[5m]))
```

### Average latency by route
```promql
rate(lucent_http_request_duration_sum[5m]) / rate(lucent_http_request_duration_count[5m])
```

### HTTP Request Rate (per second)
```promql
rate(lucent_http_requests_total[5m])

# By method
sum by (method) (rate(lucent_http_requests_total[5m]))
```

### HTTP Error Rate (4xx + 5xx as fraction of total)
```promql
rate(lucent_http_errors_total[5m]) / rate(lucent_http_requests_total[5m])

# Errors by method and route
rate(lucent_http_errors_total[5m]) > 0
```

### Memory Search Latency (p95)
```promql
histogram_quantile(0.95, rate(lucent_memory_search_duration_bucket[5m]))
```

### Memory Operations Rate (by type)
```promql
sum by (operation) (rate(lucent_memory_operations[5m]))
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

### Auto-Instrumented Metrics (from FastAPI/asyncpg OTEL instrumentation)
```promql
# DB query duration P95
histogram_quantile(0.95, rate(db_client_operation_duration_bucket[5m]))

# HTTP server request duration P95 (OTEL auto-instrumented, separate from custom)
histogram_quantile(0.95, rate(http_server_request_duration_bucket[5m]))
```

## Troubleshooting

### Collector not receiving data

**Symptoms**: No metrics in Prometheus, no traces in Jaeger.

1. Check the collector is running:
   ```bash
   docker logs lucent-otel-collector --since 5m
   ```
2. Verify the Lucent server can reach the collector:
   ```bash
   docker exec lucent-server python -c "
   import socket; s = socket.socket(); s.settimeout(3)
   s.connect(('otel-collector', 4317)); print('OK'); s.close()
   "
   ```
3. Verify `OTEL_ENABLED=true` is set:
   ```bash
   docker exec lucent-server printenv | grep OTEL
   ```
4. If running Lucent outside Docker, use `http://localhost:4317` (not `otel-collector`)
5. Increase collector debug verbosity — edit `docker/otel-collector-config.yaml`:
   ```yaml
   exporters:
     debug:
       verbosity: detailed  # was: basic
   ```

### No metrics in Prometheus

1. Check Prometheus targets: `http://localhost:9090/targets` — `otel-collector` should show `UP`
2. Verify the collector's Prometheus exporter is responding:
   ```bash
   curl -s http://localhost:8889/metrics | head -20
   ```
3. Metrics export every 60s (`export_interval_millis=60_000` in `telemetry.py`) — wait at least 2 minutes
4. Search raw metrics: `http://localhost:9090/api/v1/label/__name__/values` — look for `lucent_*` or `daemon_*`
5. Confirm the scrape config in `docker/prometheus.yml` targets `otel-collector:8889`

### Missing traces in Jaeger

1. Open Jaeger UI: `http://localhost:16686` — select service `lucent` from dropdown
2. If no services appear, check collector logs:
   ```bash
   docker logs lucent-otel-collector --since 5m 2>&1 | grep -i "trace\|export\|error"
   ```
3. Verify the operation ran while OTEL was enabled — no-op tracers produce nothing
4. `/api/health` is excluded from OTEL instrumentation — use any other endpoint to test:
   ```bash
   curl http://localhost:8766/api/memories/search -H "Content-Type: application/json" -d '{}'
   ```

### Trace propagation between services

- The API auto-instruments via `FastAPIInstrumentor` which reads W3C `traceparent` headers
- Daemon httpx calls need `opentelemetry-instrumentation-httpx` for cross-service propagation
- Until then, check `lucent.correlation_id` span attribute to manually correlate

### Trace-log correlation not working

1. `telemetry.enrich_log_record()` adds `trace_id` and `span_id` to JSON logs when a span is active
2. `bridge_correlation_id()` syncs the correlation ID ContextVar into OTEL baggage
3. Check that `sync_trace_to_correlation_id()` is called in middleware after span creation
4. Look for `trace_id` field in JSON log output:
   ```bash
   docker logs lucent-server --since 1h 2>&1 | python -c "
   import sys, json
   for line in sys.stdin:
       try:
           r = json.loads(line)
           if 'trace_id' in r and 'error' in r.get('level','').lower():
               print(r['trace_id'], r['message'][:80])
       except: pass
   "
   # Then open: http://localhost:16686/trace/<trace_id>
   ```

### Grafana shows "No data"

1. Check datasource health: Grafana → Settings → Data Sources → Prometheus → "Test"
2. Datasource URL must be `http://prometheus:9090` (Docker internal network)
3. Verify Prometheus is scraping: `http://localhost:9090/targets`
4. OTEL Collector converts dots to underscores: `lucent.http.request.duration` → `lucent_http_request_duration`
5. Check Grafana time range covers when the server was running

### High memory / CPU from telemetry

- Reduce batch size in `docker/otel-collector-config.yaml` → `processors.batch.send_batch_size`
- Increase export interval in `telemetry.py` (`export_interval_millis` — default 60000ms)
- Disable when not needed: `OTEL_ENABLED=false` — everything reverts to zero-cost no-ops

## Adding New Metrics

All metrics live in `src/lucent/metrics.py` as a lazily-initialized singleton.

### Step 1: Add the instrument to `_MetricsRegistry`

Edit `src/lucent/metrics.py`, add inside `_ensure_initialized()`:

```python
# In _MetricsRegistry._ensure_initialized():
self.my_new_counter = meter.create_counter(
    "lucent.my_feature.operations",
    description="Count of my feature operations",
)

self.my_new_duration = meter.create_histogram(
    "lucent.my_feature.duration",
    unit="s",
    description="My feature operation duration",
)
```

### Step 2: Record values at call sites

```python
from lucent.metrics import metrics
import time

start = time.monotonic()
result = do_work()
elapsed = time.monotonic() - start

metrics.my_new_duration.record(elapsed, {"operation": "search"})
metrics.my_new_counter.add(1, {"operation": "search", "status": "success"})
```

### Available instrument types

| Type | Method | Use for |
|------|--------|---------|
| `create_counter` | `.add(amount, attributes)` | Monotonically increasing values (requests, errors, bytes) |
| `create_histogram` | `.record(value, attributes)` | Distributions (latency, sizes) |
| `create_up_down_counter` | `.add(amount, attributes)` | Values that go up and down (queue depth, active connections) |
| `create_observable_gauge` | callback-based | Point-in-time readings (pool size, memory usage) |

### Naming conventions

- Prefix all metrics with `lucent.` (daemon metrics use `daemon.`)
- Use dots as separators: `lucent.http.request.duration`
- Include `unit` for histograms: `"s"` for seconds, `"bytes"` for sizes
- OTEL Collector converts dots to underscores for Prometheus: `lucent.http.request.duration` → `lucent_http_request_duration`

### Current metrics reference

| Metric | Type | Attributes |
|--------|------|------------|
| `lucent.http.request.duration` | histogram (s) | method, route, status_code |
| `lucent.http.requests.total` | counter | method, route, status_code |
| `lucent.http.errors.total` | counter | method, route, status_code |
| `lucent.memory.operations` | counter | operation |
| `lucent.memory.search.duration` | histogram (s) | — |
| `lucent.tasks.queue_depth` | up_down_counter | — |
| `lucent.tasks.execution.duration` | histogram (s) | — |

## Adding New Spans

Use `get_tracer()` from `src/lucent/telemetry.py` to create spans for tracing.

### Basic span

```python
from lucent.telemetry import get_tracer

tracer = get_tracer("lucent.my_module")

def my_function():
    with tracer.start_as_current_span("my_operation") as span:
        span.set_attribute("my.key", "value")
        result = do_work()
        span.set_attribute("result.count", len(result))
```

When OTEL is disabled, `get_tracer()` returns a `_NoOpTracer` — the `with` block still works but does nothing.

### Async span

```python
async def my_async_function():
    with tracer.start_as_current_span("async_operation") as span:
        result = await some_async_call()
        span.set_attribute("result.size", len(result))
        return result
```

### Recording errors on spans

```python
from opentelemetry.trace import StatusCode

with tracer.start_as_current_span("risky_operation") as span:
    try:
        result = do_something()
    except Exception as e:
        span.set_status(StatusCode.ERROR, str(e))
        span.record_exception(e)
        raise
```

### Nesting spans (automatic parent-child linking)

```python
with tracer.start_as_current_span("parent_operation"):
    with tracer.start_as_current_span("child_step_1"):
        step_1()
    with tracer.start_as_current_span("child_step_2"):
        step_2()
```

### Daemon-style guard pattern

The daemon uses a guard for optional tracing:

```python
import contextlib

with self._tracer.start_as_current_span(
    "daemon.my_operation",
    attributes={"daemon.instance_id": self.instance_id},
) if self._tracer else contextlib.nullcontext():
    await do_async_work()
```

### Auto-instrumented paths (no manual spans needed)

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

# Patterns indicating issues:
# HARD TIMEOUT — session lifecycle hung
# at session limit — all slots occupied
# force_stop — client.stop() had to be force-killed
# Connection refused — DB or MCP server unreachable
```

## Quick Reference

| Action | Command |
|--------|---------|
| Start observability | `OTEL_ENABLED=true docker compose --profile observability up -d` |
| Stop observability | `docker compose --profile observability down` |
| Grafana | `http://localhost:3001` (admin/lucent) |
| Jaeger | `http://localhost:16686` |
| Prometheus | `http://localhost:9090` |
| Collector metrics | `curl http://localhost:8889/metrics` |
| Check OTEL status | `docker exec lucent-server printenv \| grep OTEL` |
| Collector logs | `docker logs lucent-otel-collector --since 5m` |
