# Deployment Guide

This guide covers deploying Lucent in production. For local development, see the [README](../README.md#development).

## Quick Start (Docker Compose)

The fastest way to get running:

```bash
git clone https://github.com/kahinton/lucent.git
cd lucent
docker compose up -d
```

Open http://localhost:8766 to create your account and get your API key.

## Architecture Overview

Lucent runs as a single process serving three interfaces on one port (default 8766):

| Path | Purpose |
|------|---------|
| `/mcp` | MCP protocol for AI clients |
| `/api/*` | REST API |
| `/` | Web dashboard |

The only external dependency is PostgreSQL 16+.

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
| `POSTGRES_PASSWORD` | `lucent_dev_password` | Database password |
| `LUCENT_DB_PORT` | `5433` | Host port for PostgreSQL |
| `LUCENT_PORT` | `8766` | Host port for Lucent |
| `LUCENT_MODE` | `personal` | `personal` or `team` |

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

Lucent runs migrations automatically on startup. A `_migrations` table tracks which SQL files have been applied. No manual migration steps are needed.

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
| `LUCENT_SESSION_TTL_HOURS` | `72` | Web session cookie lifetime |
| `LUCENT_SECURE_COOKIES` | `false` | Set `true` behind HTTPS |
| `LUCENT_SIGNING_SECRET` | *(random)* | HMAC secret for impersonation cookies — set a fixed value for persistence across restarts |

### Rate Limiting & CORS

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_RATE_LIMIT_PER_MINUTE` | `100` | Max requests per minute per API key |
| `LUCENT_CORS_ORIGINS` | `*` | Allowed origins (comma-separated) |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_LOG_FORMAT` | `human` | `human` or `json` |
| `LUCENT_LOG_LEVEL` | `INFO` | Standard Python log levels |

### Daemon (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_MAX_SESSIONS` | `3` | Max concurrent daemon sessions |
| `LUCENT_DAEMON_INTERVAL` | `15` | Minutes between daemon cycles |
| `LUCENT_DAEMON_MODEL` | `claude-opus-4.6` | Model for standard tasks |
| `LUCENT_DAEMON_RESEARCH_MODEL` | `claude-opus-4.6` | Model for research tasks |
| `LUCENT_MCP_URL` | `http://localhost:8766/mcp` | MCP endpoint URL |
| `LUCENT_MCP_API_KEY` | — | API key for daemon MCP access |

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

### Security Checklist

- [ ] Change `POSTGRES_PASSWORD` from the default
- [ ] Set `LUCENT_SECURE_COOKIES=true` if using HTTPS
- [ ] Set `LUCENT_CORS_ORIGINS` to your actual domain(s)
- [ ] Set `LUCENT_SIGNING_SECRET` to a fixed value
- [ ] Use `LUCENT_LOG_FORMAT=json` for log aggregation
- [ ] Set up regular database backups
- [ ] Use scoped API keys (`daemon-tasks`) for external agent integrations

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
```

The `lucent` command is the entry point defined in `pyproject.toml`. It starts uvicorn with the unified server.
