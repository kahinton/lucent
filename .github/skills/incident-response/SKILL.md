---
name: incident-response
description: 'Handle production incidents — diagnose failures, restore service, and conduct post-mortem analysis. Use when the service is down, degraded, or exhibiting unexpected behavior in production.'
---

# Incident Response

**Rule #1: Restore service first, investigate second.**

## Triage (First 2 Minutes)

Run these checks in order. Stop at the first failure — that's where the problem is.

```bash
# 1. Are the containers running?
docker compose ps

# 2. Is the application healthy?
curl -s http://localhost:<port>/health

# 3. Is the database reachable?
docker exec <db-container> pg_isready -U <db_user>

# 4. What's in the logs?
docker compose logs <service> --since 5m --tail 50

# 5. Resource pressure?
docker stats --no-stream
```

Classify severity:
- **Critical**: Service unreachable, data operations failing, all users affected
- **Degraded**: Partial functionality, some operations failing, workaround possible
- **Warning**: Anomaly detected, no user impact yet

## Diagnose

```bash
# Detailed logs for the failing service
docker compose logs <service> --tail 200

# Recent code changes (could be the cause)
git --no-pager log --oneline -10

# Check for failed database migrations
docker compose logs <service> 2>&1 | grep -i "migration"
```

Search memory for known issues matching the symptoms:
```
search_memories(query="<error message or symptom>", tags=["incident", "bugs"], limit=10)
```

## Restore

| Symptom | Most likely fix |
|---------|----------------|
| Container exiting immediately | Rebuild image: `docker compose build <service> && docker compose up -d <service>` |
| Database connection refused | Restart DB container, check connection pool settings |
| Process hung / not responding | Kill and restart: `docker compose restart <service>` |
| Permission / auth failure | Check API keys, token expiry, environment variables |
| Out of memory (OOM) | Restart service, investigate the query or operation that caused it |
| Disk full | `docker system prune -f`, check for log rotation |

## Post-Mortem

After service is restored, create an incident memory:

```
create_memory(
  type="experience",
  content="## Incident: <title>\n\n**Timeline**: <when detected, when restored>\n**Symptom**: <what was observed>\n**Root cause**: <why it happened>\n**Resolution**: <what fixed it>\n**Prevention**: <what would prevent recurrence>\n**Duration**: <total downtime>",
  tags=["incident", "<severity>"],
  importance=8,
  shared=true
)
```

## Anti-Patterns

- Don't make speculative fixes because applying undiagnosed changes risks making the incident worse and obscures the real root cause.
- Don't investigate root cause while the service is down because every minute of downtime has user impact — restore first, investigate after.
- Don't attempt heroics alone when data loss is possible because irreversible data operations require a second set of eyes — escalate immediately.
- Don't skip the post-mortem memory because even trivial incidents carry lessons that prevent future recurrence — the record is the institutional memory.