# Observability

Lucent ships with an OpenTelemetry-based observability stack — traces, metrics, and pre-built dashboards — all managed through a Docker Compose profile.

## Quick Start

```bash
OTEL_ENABLED=true docker compose --profile observability up -d
```

Open the dashboards:

| Service | URL | Credentials |
|---------|-----|-------------|
| **Grafana** | http://localhost:3001 | `admin` / `lucent` |
| **Prometheus** | http://localhost:9090 | — |
| **Jaeger** | http://localhost:16686 | — |

## Telemetry Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_ENABLED` | `false` | Enable OpenTelemetry instrumentation |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP collector endpoint (gRPC) |
| `OTEL_SERVICE_NAME` | `lucent` | Service name in traces and metrics |
| `OTEL_ENVIRONMENT` | `development` | Environment tag (`development`, `staging`, `production`) |

When `OTEL_ENABLED` is not set or `false`, all telemetry calls are zero-cost no-ops — no spans are created and no metrics are recorded.

## Observability Profile Services

The `--profile observability` flag starts four additional containers:

| Service | Image | Ports | Purpose |
|---------|-------|-------|---------|
| **OTEL Collector** | `otel/opentelemetry-collector-contrib:0.96.0` | `4317` (gRPC), `4318` (HTTP), `8889` (Prometheus exporter) | Receives OTLP telemetry, exports to Jaeger and Prometheus |
| **Prometheus** | `prom/prometheus:v2.51.0` | `9090` | Metrics storage and querying |
| **Jaeger** | `jaegertracing/all-in-one:1.55` | `16686` (UI) | Distributed trace storage and visualization |
| **Grafana** | `grafana/grafana:10.4.1` | `3001` | Dashboards and alerting |

Port overrides via environment variables:

```bash
GRAFANA_PORT=3001        # default
PROMETHEUS_PORT=9090     # default
JAEGER_UI_PORT=16686     # default
OTEL_GRPC_PORT=4317      # default
OTEL_HTTP_PORT=4318      # default
```

## Dashboards

Grafana is pre-provisioned with datasources and dashboards:

- **API Health** (`docker/grafana/dashboards/api-health.json`) — request rates, latencies, error rates
- **Daemon Performance** (`docker/grafana/dashboards/daemon-performance.json`) — cognitive cycle timing, task throughput, sub-agent dispatch

Datasources (Prometheus, Jaeger) are auto-configured via `docker/grafana/provisioning/datasources/`.

## Docker Configuration Files

All observability configs live in `docker/`:

```
docker/
├── otel-collector-config.yaml   # Collector pipelines (receivers, exporters, processors)
├── prometheus.yml               # Scrape targets (OTEL Collector metrics exporter)
└── grafana/
    ├── provisioning/
    │   └── datasources/         # Auto-configured Prometheus + Jaeger datasources
    └── dashboards/
        ├── api-health.json
        └── daemon-performance.json
```

## Disabling Telemetry

Unset or set `OTEL_ENABLED=false` (the default). No code changes needed — instrumentation uses zero-cost no-ops when disabled.

```bash
# Run without telemetry (default)
docker compose up -d

# Or explicitly disable
OTEL_ENABLED=false docker compose up -d
```

The observability containers only start when `--profile observability` is passed, so omitting the profile flag also avoids running the collector stack.

## Production Considerations

### Use a Persistent Trace Backend

The default Jaeger `all-in-one` image stores traces in memory. For production, replace it with a persistent backend:

- **Grafana Tempo** — integrates natively with Grafana, S3/GCS storage
- **Elasticsearch** — use Jaeger's `es` storage backend
- **Jaeger with Cassandra/Badger** — self-hosted persistent storage

Update `docker-compose.yml` or point the OTEL Collector's trace exporter to your backend.

### Configure Prometheus Retention

Default retention is 15 days. Adjust in `docker/prometheus.yml` or via command flags:

```yaml
# docker-compose.yml override
prometheus:
  command:
    - "--config.file=/etc/prometheus/prometheus.yml"
    - "--storage.tsdb.retention.time=30d"
    - "--storage.tsdb.retention.size=10GB"
```

### Secure Grafana

The default credentials (`admin`/`lucent`) are for development only. In production:

```bash
GRAFANA_ADMIN_USER=your_admin
GRAFANA_ADMIN_PASSWORD=your_secure_password
```

Consider placing Grafana behind your reverse proxy with SSO/OAuth integration and setting `GF_USERS_ALLOW_SIGN_UP=false` (already the default in the compose file).

### Sampling for High-Traffic Deployments

To reduce trace volume, configure sampling in the OTEL Collector:

```yaml
# docker/otel-collector-config.yaml
processors:
  probabilistic_sampler:
    sampling_percentage: 10  # Keep 10% of traces
```

Or use tail-based sampling to keep only slow/errored traces:

```yaml
processors:
  tail_sampling:
    policies:
      - name: errors
        type: status_code
        status_code: { status_codes: [ERROR] }
      - name: slow
        type: latency
        latency: { threshold_ms: 1000 }
```

### OTEL Collector as a Standalone Service

For multi-host deployments, run the OTEL Collector as a standalone service and point all Lucent instances to it:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.internal:4317
```

This centralizes telemetry ingestion and lets you scale the collector independently.
