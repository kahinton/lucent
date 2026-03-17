---
name: docker-operations
description: 'Build, debug, and manage Docker containers and compose services for local dev and deployment'
---

# Docker Operations — Lucent Project

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

## Debugging Container Issues

1. **Container won't start**: Check `docker compose logs lucent` for Python import errors or missing env vars
2. **Database connection refused**: Ensure postgres is healthy — `docker compose ps` should show postgres as healthy
3. **Hot-reload not working**: Source files are volume-mounted (`./src:/app/src:cached`). If changes aren't reflected, the volume mount may be stale — rebuild
4. **Out of disk space**: `docker system prune -f` to clean unused images/containers

## Database Operations

```bash
# Connect to postgres directly
docker exec -it hindsight-postgres-1 psql -U lucent -d lucent

# Run migrations
docker exec lucent-server python -c "from lucent.db import run_migrations; import asyncio; asyncio.run(run_migrations())"
```
