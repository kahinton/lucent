# Configuration

Lucent is configured entirely through environment variables. Copy `.env.example` to `.env` and customize as needed.

## Server Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | *(required)* | PostgreSQL connection string |
| `LUCENT_HOST` | `0.0.0.0` | Server bind address |
| `LUCENT_PORT` | `8766` | Server port |
| `LUCENT_MODE` | `personal` | Deployment mode (`personal` or `team`; team requires a license key) |
| `LUCENT_LICENSE_KEY` | — | License key for team mode |
| `LUCENT_LOG_FORMAT` | `human` | Log output format (`human` or `json`) |
| `LUCENT_LOG_LEVEL` | `INFO` | Logging verbosity |
| `LUCENT_LOG_FILE` | *(none)* | Path to rotating log file (in addition to stdout) |
| `LUCENT_LOG_FILE_MAX_BYTES` | `10485760` | Max bytes per log file before rotation (10 MB) |
| `LUCENT_LOG_FILE_BACKUP_COUNT` | `5` | Number of rotated log files to keep |
| `LUCENT_LOG_MODULES` | *(none)* | Per-module log level overrides (comma-separated, e.g. `lucent.api=DEBUG,lucent.db=WARNING`) |

## Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_AUTH_PROVIDER` | `basic` | Auth backend (`basic` or `api_key`) |
| `LUCENT_SESSION_TTL_HOURS` | `24` | Web session cookie lifetime in hours |
| `LUCENT_SECURE_COOKIES` | `true` | Cookie `Secure` flag. Set to `false` for local HTTP development without HTTPS |
| `LUCENT_SIGNING_SECRET` | *(random)* | HMAC key for signing session cookies. Auto-generated if not set; set explicitly for multi-instance deployments |

## Networking & Security

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_RATE_LIMIT_PER_MINUTE` | `100` | Max requests per minute per API key |
| `LUCENT_LOGIN_RATE_LIMIT` | `5` | Max failed login attempts per IP before throttling |
| `LUCENT_TRUSTED_PROXIES` | *(none)* | Comma-separated trusted proxy IPs/CIDRs for `X-Forwarded-For` parsing (e.g. `172.17.0.0/16,10.0.0.1`) |
| `LUCENT_CORS_ORIGINS` | *(none)* | Allowed CORS origins (comma-separated). `*` allows all but logs a security warning |

## Secret Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_SECRET_PROVIDER` | `builtin` | Secret storage backend (`builtin`, `transit`, `vault`, `aws`, or `azure`) |
| `LUCENT_SECRET_KEY` | *(required for builtin)* | Fernet encryption key for the builtin provider |
| `LUCENT_CREDENTIAL_KEY` | *(none)* | Fernet key for encrypting integration credentials (Slack/Discord tokens). Separate from `LUCENT_SECRET_KEY` |
| `VAULT_ADDR` | `http://openbao:8200` | OpenBao/Vault server address |
| `VAULT_TOKEN` | `root` | OpenBao/Vault authentication token |

For detailed secret storage configuration, see [Secret Storage](secret-storage.md).

## LLM Engine

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_LLM_ENGINE` | `copilot` | LLM backend: `copilot` (GitHub Copilot SDK) or `langchain` |
| `LUCENT_CHAT_MODEL` | `claude-opus-4.6` | Default model for chat sessions |
| `LUCENT_MODEL_VALIDATION` | `strict` | Model validation mode: `strict` rejects unknown models, `lenient` allows them |
| `LUCENT_CHAT_MCP_URL` | `http://localhost:8766/mcp` | MCP URL for chat-initiated tool calls |
| `LUCENT_CHAT_TIMEOUT` | `300` | Chat session timeout in seconds |
| `ANTHROPIC_API_KEY` | — | Anthropic API key (only when `LUCENT_LLM_ENGINE=langchain`) |
| `OPENAI_API_KEY` | — | OpenAI API key (only when `LUCENT_LLM_ENGINE=langchain`) |
| `GOOGLE_API_KEY` | — | Google API key (only when `LUCENT_LLM_ENGINE=langchain`) |
| `OLLAMA_HOST` | `http://host.docker.internal:11434` | Ollama host URL for local models |
| `GITHUB_TOKEN` | — | GitHub token for Copilot SDK authentication |

## Daemon Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_MAX_SESSIONS` | `3` | Max concurrent sub-agent sessions |
| `LUCENT_DAEMON_INTERVAL` | `15` | Minutes between cognitive cycles |
| `LUCENT_DAEMON_MODEL` | `claude-opus-4.6` | Default model for daemon sessions |
| `LUCENT_DAEMON_ROLES` | `all` | Enable specific loops: `cognitive`, `dispatcher`, `scheduler`, `autonomic` (comma-separated, or `all`) |
| `LUCENT_MCP_URL` | `http://localhost:8766/mcp` | MCP server URL for memory access |
| `LUCENT_MCP_API_KEY` | — | API key for MCP authentication |
| `LUCENT_REVIEW_MODELS` | — | Comma-separated models for multi-model review (optional) |
| `LUCENT_STALE_HEARTBEAT_MINUTES` | `30` | Minutes before a daemon heartbeat is considered stale |
| `DAEMON_DATABASE_URL` | — | Restricted DB connection for daemon API key provisioning (uses `lucent_daemon` role) |

### Daemon Timing

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_DISPATCH_POLL_SECONDS` | `60` | How often the dispatcher checks for pending tasks |
| `LUCENT_SCHEDULER_CHECK_SECONDS` | `60` | How often the scheduler checks for due schedules |
| `LUCENT_AUTONOMIC_INTERVAL` | `8` | Cognitive cycles between autonomic maintenance runs |
| `LUCENT_AUTONOMIC_MINUTES` | *(calculated)* | Time-based autonomic interval (overrides cycle-based if set) |
| `LUCENT_LEARNING_INTERVAL` | *(2× autonomic)* | Cognitive cycles between learning extraction runs |
| `LUCENT_LEARNING_MINUTES` | *(calculated)* | Time-based learning interval (overrides cycle-based if set) |

### Daemon Sessions

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_SESSION_TIMEOUT` | `3600` | Overall session timeout in seconds (1 hour) |
| `LUCENT_SESSION_IDLE_TIMEOUT` | `300` | Idle session timeout in seconds (5 minutes) |
| `LUCENT_WATCHDOG_TIMEOUT` | `900` | Watchdog timeout for stuck sessions (15 minutes) |
| `LUCENT_MAX_RESULT_LENGTH` | `8000` | Maximum characters stored from sub-agent results |

### Daemon Review & Approval

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_AUTO_APPROVE` | `true` | When `false`, daemon-created requests (source: cognitive, daemon, schedule) require human approval via the review queue before work begins. User/API-created requests are always auto-approved. |
| `LUCENT_SKIP_POST_REVIEW` | `false` | When `true`, bypasses the daemon's automatic post-completion quality review. By default, finished work goes through an internal review task that auto-approves or sends back for rework. |
| `LUCENT_REQUEST_REVIEW_MODEL` | *(daemon default)* | Model for request-level post-completion review |
| `LUCENT_REQUEST_REVIEW_AGENT_TYPE` | `request-review` | Agent type for post-completion review |
| `LUCENT_REQUEST_REVIEW_FALLBACK_AGENT_TYPE` | `code` | Fallback agent type if review agent not found |

#### Approval Flow

Requests go through a two-stage gate:

1. **Pre-work approval** (`LUCENT_AUTO_APPROVE`): Controls whether daemon-initiated requests need human approval before any tasks are dispatched. When disabled (`false`), these requests appear in the Review Queue with an "Approve — Start Work" button. Rejected requests are cancelled and generate a learning memory.

2. **Post-completion review** (`LUCENT_REQUIRE_APPROVAL`): Controls whether finished requests need human sign-off. When enabled (`true`), requests that complete successfully go to `review` status and appear in the Review Queue's "Completed Work Review" section.

### Daemon Git Operations

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_ALLOW_GIT_COMMIT` | `false` | Allow the daemon to create git commits |
| `LUCENT_ALLOW_GIT_PUSH` | `false` | Allow the daemon to push to remote (requires `LUCENT_ALLOW_GIT_COMMIT=true`) |

## Sandbox Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_SANDBOX_BACKEND` | `docker` | Sandbox backend (currently only `docker`) |
| `LUCENT_SANDBOX_BRIDGE_API_URL` | `http://host.docker.internal:8766/api` | API URL accessible from inside sandboxes |
| `DOCKER_HOST` | *(system default)* | Docker daemon socket URL for sandbox creation |

## Docker Compose

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_PASSWORD` | `lucent_dev_password` | PostgreSQL password |
| `LUCENT_DB_PORT` | `5433` | Host port for the PostgreSQL container |
| `DAEMON_DB_PASSWORD` | `lucent_daemon_dev_password` | Password for the restricted `lucent_daemon` database role |

### Observability Stack

Enable with `docker compose --profile observability up -d`:

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_ENABLED` | `false` | Enable OpenTelemetry instrumentation |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4317` | OTLP collector endpoint |
| `OTEL_SERVICE_NAME` | `lucent` | Service name for traces |
| `OTEL_GRPC_PORT` | `4317` | Host port for OTLP gRPC |
| `OTEL_HTTP_PORT` | `4318` | Host port for OTLP HTTP |
| `PROMETHEUS_PORT` | `9090` | Host port for Prometheus UI |
| `JAEGER_UI_PORT` | `16686` | Host port for Jaeger UI |
| `GRAFANA_PORT` | `3001` | Host port for Grafana |
| `GRAFANA_ADMIN_USER` | `admin` | Grafana admin username |
| `GRAFANA_ADMIN_PASSWORD` | `lucent` | Grafana admin password |

For observability setup details, see [Observability](observability.md).

### Multi-Daemon

Enable with `docker compose --profile multi-daemon up`:

Uses the same daemon environment variables listed above. See `docker-compose.yml` for the `daemon-1` and `daemon-2` service definitions.

## Docker Deployment

### Full Stack

Run PostgreSQL, OpenBao, and the Lucent server:

```bash
docker compose up -d
docker compose logs -f lucent
```

### Database Only

For local development, run just the database:

```bash
docker compose up -d postgres
```

### Persistent Storage

Data is stored in Docker volumes (`lucent_data`, `openbao_shared`). To backup:

```bash
docker compose exec postgres pg_dump -U lucent lucent > backup.sql
```

## Related Documentation

- [Getting Started](getting-started.md) — installation and first-run setup
- [Deployment Guide](deployment-guide.md) — production deployment
- [Secret Storage](secret-storage.md) — secret storage providers in detail
- [Observability](observability.md) — metrics, traces, dashboards
- [Security Model](security-model.md) — authentication and access control
