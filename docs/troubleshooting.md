# Troubleshooting

Common issues and solutions for running Lucent.

## Database Connection

### `DATABASE_URL environment variable is required`

The server won't start without a PostgreSQL connection string.

```bash
export DATABASE_URL=postgresql://lucent:lucent_dev_password@localhost:5433/lucent
```

If using Docker Compose, this is set automatically via the `docker-compose.yml` environment block. Check that the `postgres` service is healthy:

```bash
docker compose ps
docker compose logs postgres
```

### `connection refused` or `could not connect to server`

The database isn't reachable. Common causes:

1. **PostgreSQL isn't running:**

```bash
docker compose up -d postgres
# Wait for healthy status
docker compose ps
```

2. **Wrong port:** Docker Compose maps PostgreSQL to host port `5433` (not the default 5432). If connecting from outside Docker:

```bash
DATABASE_URL=postgresql://lucent:lucent_dev_password@localhost:5433/lucent
```

3. **Networking between containers:** Inside Docker Compose, the hostname is `postgres` (the service name), not `localhost`:

```
DATABASE_URL=postgresql://lucent:password@postgres:5432/lucent
```

### `extension "pg_trgm" is not available`

The `pg_trgm` extension is required for fuzzy search. It's included in the Docker PostgreSQL image by default. For external databases:

```sql
-- Requires superuser privileges
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
```

On some managed databases (e.g., RDS), you may need to enable this through the provider's dashboard.

### Migration Errors

Migrations run automatically on startup and are tracked in `schema_migrations`.
If a migration fails:

1. Check the logs for the specific SQL error:

```bash
docker compose logs lucent | grep -i migration
```

2. Verify your PostgreSQL version is 16+ and extensions are installed.

3. If a migration partially applied, you may need to manually fix the state:

```sql
-- Check applied migrations
SELECT name, checksum, applied_at FROM schema_migrations ORDER BY applied_at;

-- Remove a failed migration entry to retry (only after fixing DB state)
DELETE FROM schema_migrations WHERE name = 'failed_migration_file.sql';
```

Then restart the server to re-apply.

## Authentication

### `API key required. Use Authorization: Bearer hs_your_key_here`

All API and MCP requests require an API key. Get one from:

- **First-run setup page** at http://localhost:8766 (shown once on first launch)
- **Settings page** at http://localhost:8766/settings (generate additional keys)

### `Invalid or expired API key`

- Verify the key starts with `hs_` and is complete (no truncation)
- Check you're using `Bearer` prefix: `Authorization: Bearer hs_...`
- Keys may have been rotated — generate a new one from `/settings`

### Can't Log In to Web UI

- **Forgot password:** There's no password reset flow yet. Access the database directly:

```sql
-- Find your user
SELECT id, display_name, email FROM users;
```

Then use the Python shell to reset:

```bash
python -c "
import asyncio, os
os.environ['DATABASE_URL'] = 'postgresql://lucent:lucent_dev_password@localhost:5433/lucent'
from lucent.db import init_db
from lucent.auth_providers import set_user_password
async def reset():
    pool = await init_db()
    await set_user_password(pool, 'YOUR_USER_UUID', 'new_password')
asyncio.run(reset())
"
```

- **Using API key auth provider:** If `LUCENT_AUTH_PROVIDER=api_key`, the login page expects an API key instead of username/password.

### `Permission denied` or `403 Forbidden`

- **Role-based:** Some endpoints require `admin` or `owner` roles (team mode only)
- **Scope-based:** Daemon-scoped API keys (`daemon-tasks`) can only access `/api/daemon/*` endpoints
- **Ownership:** You can only update/delete your own memories

## MCP Client Connection

### Client Can't Connect to `/mcp`

1. Verify the server is running and healthy:

```bash
curl http://localhost:8766/api/health
# Should return: {"status": "healthy"}
```

2. Test MCP endpoint authentication:

```bash
curl -H "Authorization: Bearer hs_your_key" http://localhost:8766/mcp
```

3. Check your MCP client configuration matches the server URL and port.

### VS Code MCP Extension Not Connecting

Verify `.vscode/mcp.json`:

```json
{
  "servers": {
    "lucent": {
      "url": "http://localhost:8766/mcp",
      "type": "http",
      "headers": {
        "Authorization": "Bearer hs_your_key_here"
      }
    }
  }
}
```

Restart VS Code after changing MCP configuration.

## Docker Issues

### Container Keeps Restarting

Check logs for the root cause:

```bash
docker compose logs --tail=50 lucent
```

Common causes:
- Missing `DATABASE_URL` — the server exits immediately
- Database not ready — the health check for `postgres` should prevent this, but check `docker compose ps` for status

### Port Already in Use

```bash
# Find what's using port 8766
lsof -i :8766

# Or change the port
LUCENT_PORT=9000 docker compose up -d
```

### Data Persistence

Data is stored in the `lucent_data` Docker volume. It survives `docker compose down` but **not** `docker compose down -v` (which removes volumes).

```bash
# Verify volume exists
docker volume ls | grep lucent

# Inspect volume
docker volume inspect lucent_data
```

## Performance

### Slow Search Queries

Fuzzy search uses PostgreSQL's `pg_trgm` extension. If searches are slow:

1. Check that the extension is enabled and the similarity threshold is set:

```sql
SHOW pg_trgm.similarity_threshold;
-- Should be 0.3
```

2. Ensure the GIN trigram index exists on the content column (created by migrations).

3. For large memory sets, use filters (`type`, `tags`, `importance_min`) to narrow the search scope.

### High Memory Usage

The rate limiter uses in-memory sliding windows. With many API keys, this can grow. The default cleanup runs automatically, but for very high-traffic deployments consider lowering `LUCENT_RATE_LIMIT_PER_MINUTE` or upgrading to Redis-based rate limiting.

## Logs

### Enabling Debug Logs

```bash
LUCENT_LOG_LEVEL=DEBUG lucent
```

### JSON Log Format

For production log aggregation:

```bash
LUCENT_LOG_FORMAT=json lucent
```

### Correlation IDs

Every request gets an `X-Request-ID` header. Pass your own via the same header for end-to-end tracing:

```bash
curl -H "X-Request-ID: my-trace-id" -H "Authorization: Bearer hs_..." \
  http://localhost:8766/api/health
```

## Getting Help

- [GitHub Issues](https://github.com/kahinton/lucent/issues) — bug reports and feature requests
- [API Docs](http://localhost:8766/api/docs) — interactive Swagger UI when the server is running

## Daemon Issues

### Daemon Won't Start

Verify prerequisites:

```bash
# Check GitHub token is set (required for Copilot SDK)
echo $GITHUB_TOKEN

# Check MCP API key is set
echo $LUCENT_MCP_API_KEY

# Test connectivity to the server
curl -H "Authorization: Bearer $LUCENT_MCP_API_KEY" http://localhost:8766/api/health
```

### Tasks Stuck in "pending" or "claimed"

Tasks may get stuck if the daemon crashes mid-execution. The dispatch loop has a stale task recovery mechanism, but you can also release them manually:

```bash
# Check for stuck tasks
curl -H "Authorization: Bearer hs_..." http://localhost:8766/api/requests/queue/pending

# Check daemon log for errors
tail -100 daemon/daemon.log | grep -i "error\|fail\|warn"
```

The daemon writes to `daemon/daemon.log` and auto-rotates at 5MB.

### Cognitive Cycle Produces No Output

- Check that the MCP API key has full `read`+`write` scopes (not just `daemon-tasks`)
- Verify the daemon can reach the MCP endpoint: the daemon log will show `PG LISTEN established` on successful startup
- Check the model is valid: `LUCENT_DAEMON_MODEL` must be a model ID from the model registry

### Agent Definition Not Found

The dispatch loop requires **approved** agent definitions. If you see `"No approved definition for agent 'xyz'"`:

1. Go to http://localhost:8766/definitions
2. Find the agent in the "Pending" tab
3. Review and approve it

### Event Loop Freeze / Watchdog Kill

The daemon includes a watchdog that kills the process after 900 seconds of inactivity. This usually means:

- An LLM session timed out without returning
- A network issue prevented the Copilot SDK from responding
- The daemon will auto-restart via the file watcher or process supervisor

## Sandbox Issues

### `Docker socket not available`

The server needs access to the Docker socket for sandbox management:

```bash
# Check Docker is running
docker info

# If running in Docker, mount the socket:
# volumes:
#   - /var/run/docker.sock:/var/run/docker.sock
```

### Sandbox Creation Fails

Common causes:

1. **Image not found**: The sandbox template references a Docker image that isn't pulled locally:

```bash
docker pull python:3.12-slim  # or whatever image the template uses
```

2. **Resource limits**: The host may not have enough memory/CPU for the sandbox limits configured in the template.

3. **Network policy**: If the sandbox needs internet access (e.g., `pip install`), set `network_mode` to `bridge` or add specific hosts to `allowed_hosts` in the template.

### Sandbox Persists After Task Completion

Sandboxes should auto-destroy after task completion. If orphaned containers remain:

```bash
# List Lucent sandbox containers
docker ps -a --filter "label=lucent-sandbox=true"

# Clean up orphaned sandboxes
docker rm -f $(docker ps -a --filter "label=lucent-sandbox=true" -q)
```

## Slack Integration

### Webhooks Not Arriving

1. Verify the Request URL in your Slack app's Event Subscriptions matches your Lucent server:

```
https://your-lucent-host/integrations/webhook/slack
```

2. Check that the URL is publicly reachable from the internet (Slack can't reach `localhost`).

3. Look for verification errors in the server logs:

```bash
docker compose logs lucent | grep -i "webhook\|signature\|slack"
```

4. Re-verify the URL in Slack's Event Subscriptions page — Slack will send a fresh `url_verification` challenge.

### `401 Invalid signature` on Webhooks

The signing secret in Lucent doesn't match the one in your Slack app.

1. Go to your Slack app → **Basic Information** → copy the **Signing Secret**
2. Update the integration config:

```bash
curl -X PATCH https://your-lucent-host/api/v1/integrations/{integration_id} \
  -H "Authorization: Bearer hs_..." \
  -H "Content-Type: application/json" \
  -d '{"config": {"bot_token": "xoxb-...", "signing_secret": "correct-secret"}}'
```

### `500 Credential encryption not configured`

The `LUCENT_CREDENTIAL_KEY` environment variable is missing or invalid.

```bash
# Generate a key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Set it
export LUCENT_CREDENTIAL_KEY=your-key-here
```

Restart the server after setting the variable.

### Bot Doesn't Respond in Channels

1. **Bot not invited**: Use `/invite @Lucent` in the channel
2. **Channel not allowlisted**: Check the integration's `allowed_channels` list. If it's non-empty, the channel ID must be included.
3. **User not linked**: The user's Slack identity must be linked to their Lucent account. See [User Linking Guide](slack-user-linking.md).

### Pairing Code Issues

- **Code expired**: Codes expire after 10 minutes. Generate a new one.
- **Code exhausted**: After 5 failed attempts, the code is exhausted. Generate a new one.
- **Rate limited**: Max 10 codes per user per hour. Wait before generating another.
- **"Integration not found"**: The integration ID in the pairing request doesn't match any active integration in your organization.

### User Link Shows "orphaned" Status

User links become orphaned when their parent integration is disabled or revoked. To fix:

1. Re-enable the integration (set status to `active`)
2. Have affected users re-link with new pairing codes

### Decryption Errors After Server Migration

If you moved the Lucent server and forgot to bring the `LUCENT_CREDENTIAL_KEY`:

1. Set the same key on the new server
2. If the key is lost, delete the integration and re-create it with fresh Slack credentials

---

## Schedule Issues

### Schedule Fires Multiple Times

This was fixed by using the time guard (`next_run_at > now`) in the trigger endpoint. If you see duplicate runs:

1. Check that the server container is running the latest code
2. Verify the schedule's `next_run_at` advances correctly after each run:

```sql
SELECT next_run_at, last_run_at, run_count FROM schedules WHERE id = 'your-schedule-id';
```

### Schedule Never Fires

Check these conditions:

- `enabled = true` and `status = 'active'`
- `next_run_at` is in the past (for the scheduler to pick it up)
- The daemon is running with the `scheduler` role enabled
- The server is reachable from the daemon (check daemon logs for `Failed to trigger schedule`)

### Cron Timezone Issues

Cron expressions are evaluated in the schedule's timezone. Common IANA names:

| Alias | IANA Name |
|-------|-----------|
| `US/Eastern` | `America/New_York` |
| `US/Central` | `America/Chicago` |
| `US/Pacific` | `America/Los_Angeles` |

Both alias and IANA formats are supported.
