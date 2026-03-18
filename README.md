# Lucent

An MCP (Model Context Protocol) server that gives AI assistants persistent memory, autonomous task execution, and the ability to work between conversations. More than a memory store — Lucent is the infrastructure for AI teammates that learn, plan, and act independently.

## Features

### Memory System
- **Five Memory Types**: Experience, Technical, Procedural, Goal, and Individual memories with type-specific metadata
- **Fuzzy Search**: PostgreSQL trigram-based similarity search for natural language queries
- **Dual Search Modes**: Content-only search (`search_memories`) or full-field search (`search_memories_full`)
- **Tag Management**: Built-in tools to promote tag consistency across memories
- **Memory Versioning**: Full version history with rollback support
- **Memory Linking**: Connect related memories for contextual retrieval

### Autonomous Daemon
- **Four Independent Loops**: Cognitive reasoning, event-driven task dispatch, cron-like scheduling, and background learning
- **Request/Task Pipeline**: Structured work decomposition with full event tracking and audit trails
- **Multi-Model Support**: Per-task model selection from a registry of 20+ LLMs (OpenAI, Anthropic, Google)
- **Environment Adaptation**: Auto-generates domain-appropriate agents and skills based on workspace assessment

### Execution Infrastructure
- **Sandboxed Execution**: Docker-based isolated environments with resource limits, network policies, and auto-cleanup
- **Sandbox Templates**: Reusable environment configs (image, setup commands, repo cloning, env vars) linked to schedules
- **Scheduling**: Cron, interval, and one-time schedules with timezone support and run history tracking
- **Agent Definitions**: Approval-gated registry for agent and skill definitions — human-vetted before daemon use

### Platform
- **Pluggable Auth**: Basic auth, API key auth, session management, RBAC roles
- **Web Dashboard**: Full management UI for memories, agents, schedules, sandboxes, activity tracking, and review queues
- **CI/CD**: GitHub Actions with linting (Ruff) and full pytest suite against PostgreSQL
- **Docker Ready**: Single `docker compose up -d` to run everything

## Memory Types

| Type | Purpose | Key Metadata |
|------|---------|--------------|
| **experience** | Store interactions and their outcomes | context, outcome, lessons_learned |
| **technical** | Code patterns, solutions, technical knowledge | language, code_snippet, repo, filename |
| **procedural** | Step-by-step processes and workflows | steps, prerequisites, estimated_time |
| **goal** | Track long-term objectives | status, deadline, milestones, blockers |
| **individual** | Information about people | name, relationship, organization, preferences |

## Quick Start

### Prerequisites

- Docker and Docker Compose
- An MCP-compatible client (e.g., VS Code, Claude Desktop)
- Python 3.12+ (only if running outside Docker)

### 1. Clone and Start

```bash
# Clone the repository
git clone https://github.com/kahinton/lucent.git
cd lucent

# Start everything with Docker Compose
docker compose up -d
```

### 2. First-Time Setup

Open http://localhost:8766 in your browser. On first run, you'll see a setup page where you:

1. Create your user account (username, password)
2. Receive your MCP API key (shown once — copy it!)

### 3. Configure Your MCP Client

For VS Code with the MCP extension, add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "lucent": {
      "url": "http://localhost:8766/mcp",
      "type": "http",
      "headers": {
        "Authorization": "Bearer hs_your_api_key_here"
      }
    }
  }
}
```

Replace `hs_your_api_key_here` with the API key from the setup page. You can also generate additional keys at http://localhost:8766/settings.

For **GitHub Copilot CLI**, add to your `.mcp.json` in your project root:

```json
{
  "servers": {
    "lucent": {
      "url": "http://localhost:8766/mcp",
      "type": "http",
      "headers": {
        "Authorization": "Bearer hs_your_api_key_here"
      }
    }
  }
}
```

For Claude Desktop, add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "lucent": {
      "url": "http://localhost:8766/mcp",
      "headers": {
        "Authorization": "Bearer hs_your_api_key_here"
      }
    }
  }
}
```

## Authentication

Lucent uses a pluggable authentication system configured via `LUCENT_AUTH_PROVIDER`.

### Basic Auth (default)

Username/password authentication with bcrypt hashing. Configured automatically during first-run setup.

- Web UI: Session cookie (72-hour TTL)
- MCP/API: API key (`Authorization: Bearer hs_...`)

### API Key Auth

For simpler setups, authenticate the web UI with an API key instead of username/password:

```bash
export LUCENT_AUTH_PROVIDER=api_key
```

### Future Providers

- **OAuth**: GitHub/Google authentication
- **SAML/SCIM**: Enterprise SSO (team mode)

## Available MCP Tools

### Memory Tools

| Tool | Purpose |
|------|---------|
| `create_memory` | Create a new memory with type, content, tags, importance, and metadata |
| `get_memory` | Retrieve a full memory by its UUID |
| `get_memories` | Retrieve multiple memories by ID in a single call |
| `update_memory` | Update an existing memory |
| `delete_memory` | Soft delete a memory (can be recovered) |
| `search_memories` | Fuzzy search on content field — faster, focused results |
| `search_memories_full` | Fuzzy search across all fields (content, tags, metadata) |
| `get_existing_tags` | List all tags with usage counts |
| `get_tag_suggestions` | Fuzzy search for similar existing tags |
| `get_memory_versions` | Browse version history for a memory |
| `restore_memory_version` | Restore a memory to a previous version |
| `get_current_user_context` | Get the current user's info and individual memory |

### Request & Task Tools

| Tool | Purpose |
|------|---------|
| `create_request` | Create a tracked work request |
| `create_task` | Create a task under a request (agent type, model, sandbox) |
| `get_request_details` | Get request with tasks and events |
| `list_pending_requests` | List requests awaiting work |
| `list_pending_tasks` | List tasks ready for dispatch |
| `log_task_event` | Record an event in a task's timeline |
| `link_task_memory` | Link a memory to a task (created/read/updated) |

### Schedule Tools

| Tool | Purpose |
|------|---------|
| `create_schedule` | Create a cron, interval, or one-time schedule |
| `list_schedules` | List schedules with optional status filter |
| `get_schedule_details` | Get schedule config, run history, and next run time |
| `toggle_schedule` | Enable or disable a schedule |

### Tool Parameters

#### create_memory

```
Arguments:
- type (required): experience | technical | procedural | goal
- content (required): Main content of the memory
- username: Owner username (defaults to authenticated user)
- tags: List of categorization tags
- importance: Rating 1-10 (default: 5)
- related_memory_ids: UUIDs of related memories
- metadata: Type-specific metadata object
```

> **Note:** `individual` memories cannot be created directly — they are
> automatically created when a user account is added to the system.

#### update_memory

```
Arguments:
- memory_id (required): UUID of the memory to update
- content: New content (replaces existing)
- tags: New list of tags (replaces existing)
- importance: New importance rating 1-10
- related_memory_ids: New list of related UUIDs (replaces existing)
- metadata: New metadata object (replaces existing, must match memory type schema)
```

Only the memory owner can update it. All changes are versioned — use
`get_memory_versions` and `restore_memory_version` to browse or roll back.

#### search_memories

```
Arguments:
- query: Fuzzy search query (optional — omit to browse/filter without text matching)
- username: Filter by username
- type: Filter by memory type
- tags: Filter by tags (any match)
- importance_min/max: Filter by importance range
- created_after/before: Filter by date range (ISO format)
- memory_ids: List of specific memory UUIDs to retrieve
- offset: Pagination offset (default: 0)
- limit: Results per page (default: 5, max: 50)
```

#### search_memories_full

```
Arguments:
- query: Fuzzy search query (required — searches content, tags, and metadata)
- username: Filter by username
- type: Filter by memory type
- importance_min/max: Filter by importance range
- offset: Pagination offset (default: 0)
- limit: Results per page (default: 5, max: 50)
```

## System Prompts

Lucent provides prompt templates to help LLMs use the memory system effectively:

- **memory_usage_guide**: Comprehensive guidance on memory types, importance ratings, and best practices
- **memory_usage_guide_short**: Condensed version for limited prompt space
- **user_introduction**: Guidance for greeting users and personalizing interactions based on their individual memory

## Importance Scale

| Rating | Level | Use For |
|--------|-------|---------|
| 1-3 | Routine | Minor details, temporary context |
| 4-6 | Useful | Standard practices, general knowledge |
| 7-8 | Important | Key decisions, significant learnings |
| 9-10 | Critical | Essential knowledge, major breakthroughs |

## Configuration

Lucent is configured via environment variables. Copy `.env.example` to `.env` and customize:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | *(required)* | PostgreSQL connection string |
| `LUCENT_HOST` | `0.0.0.0` | Server bind address |
| `LUCENT_PORT` | `8766` | Server port |
| `LUCENT_MODE` | `personal` | Deployment mode (`personal` or `team`; team requires a license key) |
| `LUCENT_LICENSE_KEY` | — | License key for team mode |
| `LUCENT_AUTH_PROVIDER` | `basic` | Auth backend (`basic` or `api_key`) |
| `LUCENT_SESSION_TTL_HOURS` | `72` | Web session cookie lifetime in hours |
| `LUCENT_LOG_FORMAT` | `human` | Log output format (`human` or `json`) |
| `LUCENT_LOG_LEVEL` | `INFO` | Logging verbosity |
| `LUCENT_DB_PORT` | `5433` | Host port for the PostgreSQL container |
| `LUCENT_RATE_LIMIT_PER_MINUTE` | `100` | Max requests per minute per API key |
| `LUCENT_SECURE_COOKIES` | `true` | Cookie `Secure` flag. Set to `false` for local HTTP development without HTTPS. |
| `LUCENT_CORS_ORIGINS` | *(none)* | Allowed CORS origins (comma-separated). `*` allows all but logs a security warning. |

## Docker Deployment

### Full Docker Setup

Run both PostgreSQL and the MCP server in containers:

```bash
# Build and start all services
docker compose up -d

# View logs
docker compose logs -f lucent
```

### Database Only

For local development, run just the database:

```bash
docker compose up -d postgres
```

### Persistent Storage

Data is stored in a Docker volume (`lucent_data`). To backup:

```bash
docker compose exec postgres pg_dump -U lucent lucent > backup.sql
```

## Development

### Local Setup (without Docker for the server)

If you prefer running the server directly instead of in a container:

```bash
# Start just the database
docker compose up -d postgres

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install the package with dev dependencies
pip install -e ".[dev]"

# Copy and configure environment
cp .env.example .env

# Run the server
export DATABASE_URL="postgresql://lucent:lucent_dev_password@localhost:5433/lucent"
lucent
```

### Run Tests

```bash
pytest
```

### Code Quality

```bash
ruff check src/
ruff format src/
```

## Architecture

```
src/lucent/
├── server.py           # Unified server entry point (MCP + API + Web)
├── auth.py             # User context management (ContextVars)
├── auth_providers.py   # Pluggable auth backends + session management
├── mode.py             # Deployment mode (personal/team)
├── model_registry.py   # LLM model catalog (20+ models, providers, categories)
├── rate_limit.py       # API key rate limiting
├── rbac.py             # Role-based access control
├── logging.py          # Structured logging configuration
├── api/
│   ├── app.py          # FastAPI application
│   ├── deps.py         # Authentication dependencies
│   ├── models.py       # API request/response models
│   └── routers/
│       ├── memories.py      # Memory CRUD endpoints
│       ├── search.py        # Search endpoints
│       ├── requests.py      # Request/task tracking endpoints
│       ├── schedules.py     # Schedule management endpoints
│       ├── definitions.py   # Agent/skill definition endpoints
│       ├── sandboxes.py     # Sandbox instance + template endpoints
│       ├── users.py         # User management endpoints
│       ├── organizations.py # Organization endpoints
│       ├── audit.py         # Audit log endpoints
│       ├── access.py        # Access tracking endpoints
│       └── chat.py          # Streaming chat endpoint
├── web/
│   ├── routes.py       # Web UI routes (dashboard, memories, activity, etc.)
│   ├── static/         # Favicons and logos
│   └── templates/      # Jinja2 templates
├── db/
│   ├── pool.py         # Connection pool + migration runner
│   ├── memory.py       # Memory repository (CRUD + search)
│   ├── requests.py     # Request/task repository + event tracking
│   ├── schedules.py    # Schedule repository + cron parser
│   ├── definitions.py  # Agent/skill definition repository
│   ├── sandbox_template.py # Sandbox template repository
│   ├── user.py         # User repository
│   ├── audit.py        # Audit log + versioning repository
│   ├── api_key.py      # API key repository
│   ├── access.py       # Access tracking repository
│   ├── organization.py # Organization repository
│   ├── types.py        # TypedDict definitions
│   └── migrations/     # SQL migration files (auto-applied on startup)
├── sandbox/
│   ├── manager.py      # Sandbox lifecycle management
│   ├── backend.py      # Abstract sandbox backend interface
│   ├── docker_backend.py # Docker container implementation
│   ├── k8s_backend.py  # Kubernetes backend (stub)
│   └── models.py       # SandboxConfig, SandboxInfo, ExecResult
├── llm/
│   ├── engine.py       # Abstract LLM engine interface
│   ├── copilot_engine.py # GitHub Copilot SDK implementation
│   ├── langchain_engine.py # LangChain fallback
│   └── factory.py      # Engine selection factory
├── tools/
│   ├── memories.py     # Memory MCP tools
│   ├── requests.py     # Request/task MCP tools
│   └── schedules.py    # Schedule MCP tools
├── models/             # Pydantic models
└── prompts/            # System prompt templates

daemon/
├── daemon.py           # Autonomous daemon (4 loops: cognitive, dispatch, scheduler, autonomic)
├── adaptation.py       # Environment assessment + capability generation
├── cognitive.md        # Cognitive governance context
├── agents/             # Runtime agent workspace
├── tasks/              # Task module stubs
└── templates/          # Jinja2 templates for domain-specific agents/skills
    ├── agents/         # Agent definition templates (.md.j2)
    └── skills/         # Skill definition templates (.md.j2)
```

## Daemon

Lucent includes an autonomous daemon that runs as a background process via the GitHub Copilot SDK. It operates four independent loops that give Lucent the ability to reason, execute tasks, run schedules, and learn — all between conversations.

### Architecture

| Loop | Interval | Purpose |
|------|----------|---------|
| **Cognitive** | 15 min | Perceive → Reason → Decide → Act cycle. Reads system state, reasons about goals, creates requests/tasks via MCP tools |
| **Dispatch** | Event-driven | Claims pending tasks, resolves agent definitions, creates sandboxes, runs sub-agent LLM sessions, validates results |
| **Scheduler** | 60s | Checks for due schedules (cron/interval/once), creates request+task pairs, advances schedule state |
| **Autonomic** | ~2 hours | Background maintenance: memory cleanup, learning extraction, environment adaptation |

The dispatch loop uses PostgreSQL `LISTEN/NOTIFY` for near-instant task pickup (with 60s polling fallback). Multiple daemon instances can run in parallel — task claims are atomic.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_MAX_SESSIONS` | `3` | Max concurrent sub-agent sessions |
| `LUCENT_DAEMON_INTERVAL` | `15` | Minutes between cognitive cycles |
| `LUCENT_DAEMON_MODEL` | `claude-opus-4.6` | Default model for daemon sessions |
| `LUCENT_DAEMON_ROLES` | `all` | Enable specific loops: `cognitive`, `dispatcher`, `scheduler`, `autonomic` (comma-separated, or `all`) |
| `LUCENT_MCP_URL` | `http://localhost:8766/mcp` | MCP server URL for memory access |
| `LUCENT_MCP_API_KEY` | — | API key for MCP authentication |
| `LUCENT_REVIEW_MODELS` | — | Comma-separated models for multi-model review (optional) |

### Running the Daemon

```bash
# Run continuously (all loops)
python -m daemon.daemon

# Run a single cognitive cycle and exit
python -m daemon.daemon --once

# Override cycle interval (in minutes)
python -m daemon.daemon --interval 30
```

### Request/Task Pipeline

The daemon uses a two-level work hierarchy:

1. **Requests** — the "what": created by users (MCP), schedules, or the cognitive loop
2. **Tasks** — the "how": execution units with agent type, model override, and sandbox config

Each task flows through: `pending` → `claimed` → `running` → `completed`/`failed`, with full event tracking visible on the Activity page.

### Agent Definitions

The daemon only dispatches tasks to **approved** agent definitions. Definitions are created via the adaptation system or manually, reviewed in the web UI, and must be approved before use. Each definition includes:

- System prompt (role, tools, guardrails)
- Linked skills and MCP server access
- Per-agent tool allowlists

### Environment Adaptation

On first run in a new workspace, the daemon assesses the environment (tech stack, tools, domain), classifies it, and auto-generates domain-appropriate agent and skill definitions. These are proposed for human approval before use.

## Schedules

Create recurring or one-time tasks via MCP tools or the web UI:

| Type | Description | Example |
|------|-------------|---------|
| `cron` | Standard cron expression (5 fields) | `30 5 * * *` = daily at 5:30 AM |
| `interval` | Repeat every N seconds (min 60) | Every 5 minutes |
| `once` | Run once at a specific time | One-shot task |

Schedules support timezone-aware cron (e.g., `US/Eastern`), max run limits, expiration dates, per-schedule model overrides, and sandbox template linking. When a schedule fires, it atomically creates a request + task that flows through the dispatch loop.

## Sandboxes

Isolated Docker containers for task execution:

- **Templates**: Reusable environment configs (image, repo URL, setup commands, env vars, resource limits)
- **Instances**: Running containers with exec, file read/write, and lifecycle management
- **Resource Limits**: Memory (default 2GB), CPU (2 cores), disk (10GB), network (none/bridge/allowlist)
- **Auto-cleanup**: Destroyed after task completion or timeout (default 30 min)

Sandboxes can be linked to schedules via `sandbox_template_id` — every scheduled run creates a fresh container.

## Endpoints

All services run on a single port (default 8766):

| Endpoint | Purpose |
|----------|--------|
| `/mcp` | MCP protocol (requires API key) |
| `/api/*` | REST API (requires API key) |
| `/api/health` | Health check (no auth required) |
| `/api/docs` | OpenAPI documentation |
| `/` | Web dashboard |
| `/login` | Authentication |
| `/setup` | First-run account creation |
| `/memories` | Memory management UI |
| `/activity` | Request/task tracking and event timeline |
| `/definitions` | Agent, skill, and MCP server management |
| `/schedules` | Schedule creation and monitoring |
| `/sandboxes` | Sandbox template and instance management |
| `/daemon/review` | Review queue for daemon-generated content |
| `/audit` | Audit log viewer |
| `/users` | User management (admin) |
| `/settings` | API keys, password, profile |

## License

Lucent Source Available License 1.0 — free for non-commercial use. Commercial use requires a separate license. Release converts to Apache 2.0 after 2 years. See [LICENSE](LICENSE) for full terms.
