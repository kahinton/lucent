---
name: incident-response
description: 'Handle production incidents for Lucent deployments — diagnose failures, restore service, post-mortem analysis'
---

# Incident Response

Handle production incidents for Lucent server deployments.

## When to Use

- Lucent server is down or unhealthy
- Memory operations are failing or timing out
- Daemon processes are hung or crashing
- Database connectivity issues
- Docker container failures

## Response Process

### Step 1: Triage

1. Check container health: `docker ps` — are all containers running?
2. Check server health: `curl http://localhost:8766/api/health`
3. Check database: `docker exec lucent-db pg_isready -U lucent`
4. Check daemon log: `tail -50 daemon/daemon.log`
5. Classify severity: **critical** (service down), **degraded** (partial failure), **warning** (anomaly)

### Step 2: Diagnose

1. Read container logs: `docker logs lucent-server --tail 100`
2. Check for recent changes: `git --no-pager log --oneline -10`
3. Search memory for known issues: search for similar errors in memory
4. Check resource usage: `docker stats --no-stream`
5. Verify database migrations: check for failed or pending migrations

### Step 3: Restore Service

1. **Container crash**: `docker-compose up -d --build <service>` to rebuild
2. **Database issue**: Check connection pool, restart if needed
3. **Daemon hang**: Kill stuck process, check for timeout issues
4. **Permission/auth failure**: Verify API keys, check token expiry
5. **Dependency missing**: Check Dockerfile, rebuild image

### Step 4: Post-Mortem

Create a memory documenting:
- **What happened**: Timeline of the incident
- **Root cause**: Why it happened
- **Resolution**: What fixed it
- **Prevention**: How to prevent recurrence
- Tag with `incident`, `daemon`, and severity level

## Common Issues

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Server won't start | Missing dependency | Rebuild Docker image |
| MCP permission denied | Expired API key | Generate new key |
| Daemon hung | Session timeout | Kill process, check watchdog |
| Search returns empty | Database connection lost | Restart server |
| Container OOM | Memory leak in search | Restart, investigate query |

## Guardrails

- Restore service FIRST, investigate SECOND
- Don't make speculative fixes — diagnose before changing code
- Tag all incident memories with `incident` and `daemon`
- Escalate to Kyle if data loss is possible
