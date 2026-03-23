---
name: docker-operations
description: 'Build, debug, and manage Docker containers and compose services for local development and deployment.'
---

# Docker Operations

## Quick Reference

```bash
# Service lifecycle
docker compose up -d                    # Start all services
docker compose down                     # Stop all services
docker compose restart <service>        # Restart one service
docker compose build <service>          # Rebuild one service image
docker compose logs <service> --tail 50 # Recent logs

# Status
docker compose ps                       # Running services and health
docker stats --no-stream                # Resource usage snapshot

# Database access (use actual container name from docker compose ps)
docker exec -it <postgres-container> psql -U <db_user> -d <db_name>
docker exec <postgres-container> pg_isready -U <db_user>
```

## Debugging Procedures

### Container Won't Start

1. Run in foreground to see the exit reason: `docker compose up <service>` (no `-d`)
2. Check logs: `docker compose logs <service> --tail 100`
3. Common causes:
   - **Missing environment variables** — check `docker-compose.yml` and `.env`
   - **Import/syntax error** — check the language-specific error in logs
   - **Port conflict** — `lsof -i :<port>` to find what's using the port
   - **Missing dependency** — rebuild the image: `docker compose build <service>`

### Container Starts but Service Fails

1. Check health endpoint: `curl http://localhost:<port>/health` (or `/api/health`)
2. Check logs for runtime errors: `docker compose logs <service> --since 5m`
3. Verify database connectivity: check that the DB container shows `healthy`, not just `running`
4. Check if source files are volume-mounted — changes may need a restart vs. a rebuild

### Hot Reload Not Working

- Verify volume mounts in `docker-compose.yml` — source directory should be mounted into the container
- If stale: `docker compose restart <service>`
- If new files or dependencies were added: `docker compose build <service> && docker compose up -d <service>`

### Out of Disk Space

```bash
docker system prune -f                  # Remove unused images, containers, networks
docker volume prune -f                  # Remove unused volumes (careful — this removes data)
docker system df                        # See what's using space
```

## When to Restart vs. Rebuild

| Change type | Action |
|------------|--------|
| Source code edit (volume-mounted) | Service may auto-reload, or `docker compose restart <service>` |
| New file added | `docker compose restart <service>` |
| Dependency added/updated | `docker compose build <service> && docker compose up -d <service>` |
| Dockerfile changed | `docker compose build <service> && docker compose up -d <service>` |
| docker-compose.yml changed | `docker compose up -d <service>` (recreates with new config) |
| Environment variable changed | `docker compose up -d <service>` (recreates with new env) |

## Recording Issues

When you solve a Docker issue that wasn't obvious:

```
create_memory(
  type="technical",
  content="## Docker Issue: <description>\n\n**Symptom**: <what you observed>\n**Root cause**: <why it happened>\n**Fix**: <what solved it>",
  tags=["docker", "debugging"],
  importance=6,
  shared=true
)
```