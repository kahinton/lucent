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

| Anti-Pattern | Why It Fails | What To Do Instead |
|---|---|---|
| **Investigating root cause while the service is down** | Every minute spent debugging is a minute of user-facing downtime — the root cause can wait, but the users can't. | Restore service first using known-good rollback or restart procedures. Investigate root cause only after service is stable. |
| **Applying speculative fixes without diagnosis** | Untested changes during an incident risk making things worse and obscure the actual root cause — you end up debugging your fix on top of the original failure. | Follow the triage checklist (§Triage) to identify the failure point before changing anything. If a fix isn't obvious, rollback to the last known-good state. |
| **Skipping the post-mortem memory** | Without a documented post-mortem, the same incident will recur — institutional knowledge lives in memory, not in people's heads. | Create an incident memory (§Post-Mortem) within 24 hours of resolution. Include timeline, root cause, resolution, and prevention steps. |
| **Expanding blast radius with heroic manual fixes** | Running ad-hoc SQL, restarting unrelated services, or deploying untested hotfixes during an incident can turn a single-service outage into a system-wide failure. | Limit changes to the minimum required to restore the affected service. Don't touch anything outside the blast radius of the original failure. |
| **Not checking memory for similar past incidents** | The same failure pattern often recurs — skipping the memory search means you're debugging from scratch instead of applying a known fix in minutes. | Search `search_memories(query="<symptom>", tags=["incident"])` immediately during triage. Past incidents often contain the exact resolution steps needed. |