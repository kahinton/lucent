# Deployment Guide

This guide covers deploying Lucent in production. For local development, see the [Development Guide](development.md).

## Quick Start (Docker Compose)

The fastest way to get running:

```bash
git clone https://github.com/kahinton/lucent.git
cd lucent
docker compose up -d
```

Open http://localhost:8766 to create your account and get your API key.

## Architecture Overview

Lucent has two main components:

### 1. Server (required)

A single process serving three interfaces on one port (default 8766):

| Path | Purpose |
|------|---------|
| `/mcp` | MCP protocol for AI clients |
| `/api/*` | REST API |
| `/` | Web dashboard |

External dependency: PostgreSQL 16+.

### 2. OpenBao (default sidecar)

An [OpenBao](https://openbao.org/) instance (Vault-compatible, MPL-2.0) runs as a sidecar in the default Docker Compose configuration. It provides the Transit secrets engine for key-isolated encryption — Lucent encrypts and decrypts secrets through OpenBao without ever seeing the encryption key.

OpenBao starts automatically with `docker compose up` and requires no manual configuration. See the [Secret Storage Guide](secret-storage.md) for details on the Transit provider and the tiered security model.

> **Opting out:** If you don't want the OpenBao sidecar, set `LUCENT_SECRET_PROVIDER=builtin` and the builtin Fernet provider will be used instead. You can also remove the `openbao` and `openbao-init` services from your compose file.

### 3. Daemon (optional)

An autonomous background process that provides:
- **Cognitive reasoning** — perceive/reason/decide/act cycles
- **Task dispatch** — claims and executes tasks via sub-agent LLM sessions
- **Scheduling** — cron, interval, and one-time task firing
- **Learning** — background memory maintenance and lesson extraction

The daemon connects to the server via MCP and REST API. It requires a GitHub Copilot SDK token (`GITHUB_TOKEN`) for LLM access.

### 4. Sandboxes (optional)

Tasks can run in isolated Docker containers.

For security, prefer a Docker socket proxy (for example `docker-socket-proxy`) instead of mounting `/var/run/docker.sock` directly into the Lucent app container. Direct socket mounts allow broad Docker API access and can become a container-escape path if the app is compromised.

Sandbox base images must be pre-pulled or built.

## Docker Deployment

### Production Dockerfile

The production `Dockerfile` uses a multi-stage build:

1. **Builder stage** — installs build dependencies, builds the wheel
2. **Runtime stage** — minimal image with only runtime dependencies, runs as non-root user

```bash
docker build -t lucent .
docker run -d \
  -p 8766:8766 \
  -e DATABASE_URL=postgresql://lucent:password@db:5432/lucent \
  --name lucent \
  lucent
```

### Docker Compose (Recommended)

The included `docker-compose.yml` runs both PostgreSQL and Lucent:

```bash
# Start everything
docker compose up -d

# View logs
docker compose logs -f lucent

# Stop
docker compose down
```

#### Customizing with Environment Variables

Create a `.env` file (see `.env.example`):

```bash
cp .env.example .env
```

Key variables for Docker Compose:

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_PASSWORD` | `change-me-insecure-dev-password` | Database password |
| `LUCENT_DB_PORT` | `5433` | Host port for PostgreSQL |
| `LUCENT_PORT` | `8766` | Host port for Lucent |
| `LUCENT_MODE` | `personal` | `personal` or `team` |
| `LUCENT_SECRET_PROVIDER` | `auto` | Secret backend: `auto`, `builtin`, `transit`, `vault` |
| `LUCENT_SECRET_KEY` | `lucent-dev-secret-key-change-in-production` | Fernet key (only needed for `builtin` provider) |
| `VAULT_TOKEN` | `change-me-insecure-dev-root-token` | OpenBao/Vault token |

## PostgreSQL Setup

### Requirements

- PostgreSQL 16+ (earlier versions may work but are untested)
- Extensions: `uuid-ossp` and `pg_trgm` (for fuzzy search)

### Using Docker Compose (Recommended)

The included PostgreSQL container auto-installs required extensions via `docker/init.sql`:

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
ALTER DATABASE lucent SET pg_trgm.similarity_threshold = 0.3;
```

### Using an External Database

If you bring your own PostgreSQL instance:

1. Create a database and user:

```sql
CREATE USER lucent WITH PASSWORD 'your_secure_password';
CREATE DATABASE lucent OWNER lucent;
```

2. Enable required extensions (requires superuser):

```sql
\c lucent
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
ALTER DATABASE lucent SET pg_trgm.similarity_threshold = 0.3;
```

3. Set the connection URL:

```bash
export DATABASE_URL=postgresql://lucent:your_secure_password@your-host:5432/lucent
```

### Migrations

Lucent runs migrations automatically on startup. A `schema_migrations` table tracks
applied migration files and checksums.

Rollback support is available for migrations that provide paired rollback files:

- Forward migration: `NNN_description.sql`
- Rollback migration: `NNN_description.down.sql`

Migration metadata comments are supported at the top of migration files:

```sql
-- lucent: rollback=irreversible
-- lucent: warning=Data loss possible when rolling back
```

If a migration is marked irreversible (or has no `.down.sql` file), rollback fails by default.

### Backups

```bash
# Using Docker Compose
docker compose exec postgres pg_dump -U lucent lucent > backup.sql

# Restore
docker compose exec -T postgres psql -U lucent lucent < backup.sql
```

Data is stored in a Docker volume (`lucent_data`). To back up the volume directly:

```bash
docker run --rm -v lucent_data:/data -v $(pwd):/backup alpine \
  tar czf /backup/lucent-data.tar.gz /data
```

## Environment Variables

### Server Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | *(required)* | PostgreSQL connection string |
| `LUCENT_HOST` | `0.0.0.0` | Server bind address |
| `LUCENT_PORT` | `8766` | Server port |
| `LUCENT_MODE` | `personal` | `personal` or `team` (team requires license key) |
| `LUCENT_LICENSE_KEY` | — | License key for team mode |

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_AUTH_PROVIDER` | `basic` | `basic` (username/password) or `api_key` |
| `LUCENT_SESSION_TTL_HOURS` | `24` | Web session cookie lifetime |
| `LUCENT_SECURE_COOKIES` | `true` | Set `false` for local HTTP development without HTTPS |
| `LUCENT_SIGNING_SECRET` | *(random)* | HMAC secret for impersonation cookies — set a fixed value for persistence across restarts |

### Integrations

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_CREDENTIAL_KEY` | — | Fernet encryption key for integration credentials. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Required for Slack/Discord integrations. |

### Rate Limiting & CORS

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_RATE_LIMIT_PER_MINUTE` | `100` | Max requests per minute per API key |
| `LUCENT_CORS_ORIGINS` | *(none)* | Allowed origins (comma-separated). `*` allows all but logs a security warning. |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_LOG_FORMAT` | `human` | `human` or `json` |
| `LUCENT_LOG_LEVEL` | `INFO` | Standard Python log levels |

### Daemon (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_MAX_SESSIONS` | `3` | Max concurrent sub-agent sessions |
| `LUCENT_DAEMON_INTERVAL` | `15` | Minutes between cognitive cycles |
| `LUCENT_DAEMON_MODEL` | `claude-opus-4.6` | Default model for daemon sessions |
| `LUCENT_DAEMON_ROLES` | `all` | Loops to enable: `cognitive`, `dispatcher`, `scheduler`, `autonomic` (comma-separated) |
| `LUCENT_MCP_URL` | `http://localhost:8766/mcp` | MCP endpoint URL |
| `LUCENT_MCP_API_KEY` | — | API key for daemon MCP access |
| `LUCENT_REVIEW_MODELS` | — | Comma-separated models for multi-model task review |
| `GITHUB_TOKEN` | — | GitHub token for Copilot SDK access |

### Sandbox (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `DOCKER_HOST` | *(system default)* | Docker daemon URL (for sandbox containers) |

Use a socket proxy endpoint when running Lucent in a container:

```yaml
environment:
  DOCKER_HOST: tcp://docker-socket-proxy:2375
```

If you must mount `/var/run/docker.sock`, treat it as highly privileged host access, mount it read-only where possible, and isolate the service aggressively (non-root, seccomp, network policy, minimal capabilities).

## Production Configuration

### Running Behind a Reverse Proxy

When running behind nginx, Caddy, or similar:

1. Enable secure cookies:

```bash
LUCENT_SECURE_COOKIES=true
```

2. Restrict CORS origins:

```bash
LUCENT_CORS_ORIGINS=https://lucent.yourdomain.com
```

3. Set a persistent signing secret:

```bash
LUCENT_SIGNING_SECRET=$(openssl rand -base64 32)
```

#### Nginx Example

```nginx
server {
    listen 443 ssl;
    server_name lucent.yourdomain.com;

    location / {
        proxy_pass http://localhost:8766;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE support for MCP streaming
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

### Connection Pool Tuning

The default pool settings (min=2, max=10 connections) work for most single-user and small team deployments. For larger teams, increase PostgreSQL's `max_connections` accordingly.

### Observability

Lucent includes an OpenTelemetry-based observability stack (OTEL Collector, Prometheus, Jaeger, Grafana) behind a Docker Compose profile. See the [Observability Guide](observability.md) for setup, configuration, and production recommendations.

### Security Checklist

- [ ] Change `POSTGRES_PASSWORD` from the default
- [ ] Set `LUCENT_SECURE_COOKIES=true` if using HTTPS
- [ ] Set `LUCENT_CORS_ORIGINS` to your actual domain(s)
- [ ] Set `LUCENT_SIGNING_SECRET` to a fixed value
- [ ] Use `LUCENT_LOG_FORMAT=json` for log aggregation
- [ ] Set up regular database backups
- [ ] Use scoped API keys (`daemon-tasks`) for external agent integrations
- [ ] Review and approve agent definitions before enabling the daemon dispatcher

### Running the Daemon

The daemon runs as a separate process alongside the server:

```bash
# Activate your virtual environment
source .venv/bin/activate

# Run with all loops
python -m daemon.daemon

# Run specific roles only (e.g., just dispatch and scheduling)
LUCENT_DAEMON_ROLES=dispatcher,scheduler python -m daemon.daemon

# Run a single cognitive cycle and exit
python -m daemon.daemon --once
```

The daemon requires:
- `LUCENT_MCP_API_KEY` — an API key for accessing Lucent's MCP endpoint
- `GITHUB_TOKEN` — a GitHub token with Copilot access for LLM sessions

The daemon auto-restarts when source files change (file watcher) and includes a watchdog that kills the process if the event loop freezes for >15 minutes.

### Running Without Docker

If you prefer running directly on a host:

```bash
# Install Python 3.12+
python -m venv .venv
source .venv/bin/activate
pip install .

# Set required env vars
export DATABASE_URL=postgresql://lucent:password@localhost:5432/lucent

# Start the server
lucent

# In another terminal, start the daemon (optional)
export LUCENT_MCP_API_KEY=hs_your_key_here
python -m daemon.daemon
```

The `lucent` command is the entry point defined in `pyproject.toml`. It starts uvicorn with the unified server.
