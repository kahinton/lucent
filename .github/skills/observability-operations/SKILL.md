---
name: observability-operations
description: 'Procedures for monitoring Lucent via Prometheus metrics and Grafana dashboards — query patterns, alert tuning, dashboard creation'
---

# Observability Operations — Lucent Project

## Current Monitoring Infrastructure

Lucent exposes operational data through:
- **Structured logging** (`src/lucent/logging.py`) — JSON-formatted logs with request IDs
- **Health endpoint** (`/api/health`) — basic liveness check
- **Daemon logs** (`daemon/daemon.log`) — cognitive loop activity, session outcomes, task dispatch

Future: Prometheus metrics endpoint and Grafana dashboards are planned but not yet implemented.

## What to Monitor

### Server Health
- HTTP response codes and latency (from access logs)
- Database connection pool status
- Memory usage and request throughput
- MCP tool call success/failure rates

### Daemon Health
- Cognitive loop cycle timing (target: completes within DAEMON_INTERVAL_MINUTES)
- Session success rate (sessions that return a response vs timeout/error)
- Active session count vs MAX_CONCURRENT_SESSIONS
- Task throughput: created → dispatched → completed pipeline
- Watchdog heartbeat (detect event loop freezes)

### Database
- Connection pool utilization (asyncpg pool size vs active connections)
- Query latency for common operations (memory search, create, list)
- Migration status (are all migrations applied?)

## Log Analysis

### Server logs (inside container)
```bash
docker logs lucent-server --since 1h 2>&1 | grep -i error
docker logs lucent-server --since 1h 2>&1 | grep "status_code=5"
```

### Daemon logs
```bash
docker compose logs daemon-1 --since 1h | grep -E "ERROR|WARN|TIMEOUT"
docker compose logs daemon-1 --since 1h | grep "Session.*completed"
```

### Common patterns to watch for
- `HARD TIMEOUT` — session lifecycle hung (likely during client.start or create_session)
- `at session limit` — all session slots occupied, work being skipped
- `force_stop` — client.stop() hung, had to force-kill
- `Connection refused` — database or MCP server unreachable

## Alert Conditions (Future)

When Prometheus metrics are added, alert on:
- Health endpoint returning non-200 for > 30s
- Daemon cycle taking > 2x normal duration
- Session error rate > 20% over 15 minutes
- Database connection pool exhaustion
- Zero successful sessions in 30 minutes (daemon may be stuck)

## Adding Metrics

When implementing Prometheus metrics, instrument:
1. `src/lucent/api/app.py` — HTTP request duration histogram, request count by status
2. `src/lucent/server.py` — MCP tool call count and duration
3. `daemon/daemon.py` — session duration, cycle duration, task counts by status
