---
name: docker-operations
description: 'Build, debug, and manage Docker containers and compose services for local dev and deployment'
---

# Docker Operations — Lucent Project

## MCP Tools Used

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `memory-server-search_memories` | Find past Docker issue resolutions | `query="docker container [service] issue"`, `tags=["docker"]` |
| `memory-server-create_memory` | Save container debugging findings | `type="technical"`, `tags=["docker", "lucent"]`, `importance=6` |

## Project Docker Setup

Lucent uses Docker Compose with these services:

| Service | File | Purpose |
|---------|------|---------|
| `lucent` (lucent-server) | `Dockerfile.dev` | Main MCP server + FastAPI web app |
| `postgres` | stock postgres image | Database |
| `daemon-1`, `daemon-2` | `Dockerfile.dev` (multi-daemon profile) | Autonomous daemon instances |

Key files: `docker-compose.yml`, `Dockerfile`, `Dockerfile.dev`, `docker/init.sql`

## Common Operations

### Rebuild after code changes (hot-reload handles most, but dependency changes need rebuild)
```bash
docker compose build lucent
docker compose up -d lucent
```

### View logs
```bash
docker logs lucent-server --since 5m    # Recent server logs
docker logs lucent-server -f            # Follow live
docker compose logs daemon-1 --since 5m # Daemon logs
```

### Restart a service
```bash
docker restart lucent-server
docker compose restart daemon-1
```

### Full stack restart
```bash
docker compose down && docker compose up -d
```

### Run tests inside container
```bash
docker exec lucent-server pytest tests/ -q --tb=short
```

### Check container health
```bash
docker compose ps                       # Service status
docker exec lucent-server python -c "import httpx; print(httpx.get('http://localhost:8766/api/health').json())"
```

## Multi-Daemon Setup
```bash
docker compose --profile multi-daemon up -d
```
This starts `daemon-1` and `daemon-2` with role-based coordination.

## Environment Variables

Key env vars in `docker-compose.yml`:
- `DATABASE_URL` — PostgreSQL connection
- `GITHUB_TOKEN` — Copilot SDK auth
- `LUCENT_LLM_ENGINE` — `copilot` (default) or `langchain`
- `LUCENT_CHAT_MODEL` — default model for chat
- `LUCENT_DAEMON_MODEL` — default model for daemon sessions

## Decision: What's Wrong with the Container?

- IF container not starting → `docker compose logs lucent` for Python import errors or missing env vars
- ELIF database connection refused → `docker compose ps` — verify postgres shows as "healthy", not just "running"
- ELIF hot-reload not working → source files are volume-mounted (`./src:/app/src:cached`); if stale, `docker compose build lucent && docker compose up -d lucent`
- ELIF out of disk space → `docker system prune -f` to clean unused images/containers
- ELIF container starts but API fails → `curl http://localhost:8766/api/health` to diagnose

## Database Operations

```bash
# Connect to postgres directly (container name varies — check docker compose ps)
docker exec -it <postgres-container> psql -U <db_user> -d <db_name>

# Run migrations manually
docker exec <server-container> python -c "from lucent.db import run_migrations; import asyncio; asyncio.run(run_migrations())"

# Check database health
docker exec <postgres-container> pg_isready -U <db_user>
```

## Debugging Container Issues

### Procedure: Debug Container Startup Failure

1. Check if it exits immediately: `docker compose up lucent` (foreground)
2. Check logs: `docker compose logs lucent`
3. Common causes:
   - Missing env vars → verify all required vars in `docker-compose.yml`
   - Import error → check Python syntax in recently changed files
   - Port conflict → `lsof -i :8766` to see what's using the port
4. If issue is novel, save findings:
   ```
   memory-server-create_memory(
     type="technical",
     content="## Docker Issue: [description]\n\n**Symptom**: ...\n**Root cause**: ...\n**Fix**: ...",
     tags=["docker", "lucent", "debugging"],
     importance=6,
     shared=true
   )
   ```

## Example: Good Docker Debug Session

```
1. memory-server-search_memories(query="docker lucent container startup", tags=["docker"])
   → No past issues found

2. docker compose logs lucent | tail -30
   → "ModuleNotFoundError: No module named 'httpx'"

3. docker compose build lucent  # Rebuild to pick up new dependency
4. docker compose up -d lucent
5. curl http://localhost:8766/api/health → {"status": "ok"}

6. memory-server-create_memory(
     type="technical",
     content="## Docker: Module not found after pip install\n\nWhen adding new Python dependencies, must run 'docker compose build' — hot-reload does NOT pick up new packages.",
     tags=["docker", "lucent"],
     importance=6
   )
```
