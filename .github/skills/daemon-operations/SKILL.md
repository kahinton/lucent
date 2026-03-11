---
name: daemon-operations
description: 'Monitor, troubleshoot, and optimize daemon cognitive cycles — task throughput, memory usage patterns, cycle timing'
---

# Daemon Operations

Operational procedures for monitoring and maintaining the Lucent daemon. For detailed runbooks and tuning guides, see `daemon-operations-specialized`.

## When to Use

- Checking if the daemon is running and healthy
- Investigating why tasks aren't being processed
- Reviewing cycle timing and throughput
- Monitoring memory usage growth

## Quick Health Check

1. **Process running?** Check heartbeat memory (tags: `daemon-heartbeat`) — timestamp should be within 2× `DAEMON_INTERVAL_MINUTES`
2. **Server up?** `curl http://localhost:8766/api/health`
3. **Recent activity?** `tail -5 daemon/daemon.log`
4. **Queue depth?** Search memories with tags `["daemon-task", "pending"]`

## Key Metrics

| Metric | Where to Find | Healthy |
|--------|--------------|---------|
| Cycle count | Heartbeat memory `cycle_count` | Incrementing |
| Pending tasks | Search `daemon-task` + `pending` | Stable or decreasing |
| Stuck tasks | Search `daemon-task` + `in-progress` older than 30min | Zero |
| Memory count | Total memories in store | Not growing unboundedly |

## Configuration Reference

| Env Var | Default | Effect |
|---------|---------|--------|
| `LUCENT_DAEMON_INTERVAL` | 15 | Minutes between cycles |
| `LUCENT_MAX_SESSIONS` | 3 | Max concurrent sub-agent sessions |
| `LUCENT_AUTONOMIC_INTERVAL` | 8 | Cycles between maintenance runs |
| `LUCENT_SESSION_TIMEOUT` | 720 | Seconds before session timeout |

## Escalation

If the daemon is unhealthy and quick checks don't resolve it, use `daemon-operations-specialized` for detailed runbooks on cycle timing analysis, throughput tuning, multi-instance coordination, and memory cleanup.
