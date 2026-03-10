# Lucent

An MCP (Model Context Protocol) server providing persistent memory functionality for LLMs. Store, search, and retrieve memories across conversations to enhance AI assistant capabilities.

## Features

- **Five Memory Types**: Experience, Technical, Procedural, Goal, and Individual memories with type-specific metadata
- **Fuzzy Search**: PostgreSQL trigram-based similarity search for natural language queries
- **Dual Search Modes**: Content-only search (`search_memories`) or full-field search (`search_memories_full`)
- **Tag Management**: Built-in tools to promote tag consistency across memories
- **Memory Linking**: Connect related memories for contextual retrieval
- **User Management**: Pluggable authentication (basic auth, API key), session management, RBAC roles
- **Soft Delete**: Recoverable deletions with future hard-delete cleanup planned
- **Docker Ready**: PostgreSQL with persistent storage out of the box

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

## Available Tools

### Memory CRUD

| Tool | Purpose |
|------|---------|
| `create_memory` | Create a new memory with type, content, tags, importance, and metadata |
| `get_memory` | Retrieve a full memory by its UUID |
| `get_memories` | Retrieve multiple memories by ID in a single call |
| `update_memory` | Update an existing memory |
| `delete_memory` | Soft delete a memory (can be recovered) |

### User Context

| Tool | Purpose |
|------|--------|
| `get_current_user_context` | Get the current user's info and individual memory — recommended first call in every conversation |

### Search Tools

| Tool | Purpose |
|------|---------|
| `search_memories` | Fuzzy search on **content field only** - faster, focused results |
| `search_memories_full` | Fuzzy search across **all fields** (content, tags, metadata) |

### Tag Management

| Tool | Purpose |
|------|---------|
| `get_existing_tags` | List all tags with usage counts - use before creating memories! |
| `get_tag_suggestions` | Fuzzy search for similar existing tags |

### Memory Versioning

| Tool | Purpose |
|------|--------|
| `get_memory_versions` | Browse version history for a memory |
| `restore_memory_version` | Restore a memory to a previous version |

### Sharing (Team Mode Only)

| Tool | Purpose |
|------|--------|
| `share_memory` | Share a memory with other users in your organization |
| `unshare_memory` | Stop sharing a memory with your organization |

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
| `LUCENT_SECURE_COOKIES` | `false` | Set to `true` when running behind HTTPS |
| `LUCENT_CORS_ORIGINS` | `*` | Allowed CORS origins (comma-separated) |

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
├── rate_limit.py       # API key rate limiting
├── rbac.py             # Role-based access control
├── logging.py          # Structured logging configuration
├── api/
│   ├── app.py          # FastAPI application
│   ├── deps.py         # Authentication dependencies
│   ├── models.py       # API request/response models
│   └── routers/
│       ├── memories.py     # Memory CRUD endpoints
│       ├── search.py       # Search endpoints
│       ├── users.py        # User management endpoints
│       ├── organizations.py # Organization endpoints
│       ├── audit.py        # Audit log endpoints
│       └── access.py       # Access tracking endpoints
├── web/
│   ├── routes.py       # Web UI routes
│   ├── static/         # Favicons and logos
│   └── templates/      # Jinja2 templates
├── db/
│   ├── pool.py         # Connection pool + migration runner
│   ├── memory.py       # Memory repository (CRUD + search)
│   ├── user.py         # User repository
│   ├── audit.py        # Audit log + versioning repository
│   ├── api_key.py      # API key repository
│   ├── access.py       # Access tracking repository
│   ├── organization.py # Organization repository
│   ├── types.py        # TypedDict definitions for repository return values
│   └── migrations/     # SQL migration files
├── models/
│   ├── memory.py       # Pydantic models for memory types
│   ├── validation.py   # Metadata validation
│   ├── audit.py        # Pydantic models for audit logging
│   ├── user.py         # Pydantic models for user management
│   └── organization.py # Pydantic models for organizations
├── tools/
│   └── memories.py     # MCP tool implementations
└── prompts/
    └── memory_usage.py # System prompt templates

daemon/
├── daemon.py           # Autonomous background process (Copilot CLI SDK)
└── tasks/              # Task module stubs (named tasks are inline prompts in daemon.py)
```

## Daemon

Lucent includes an autonomous daemon that runs as a background process via the Copilot CLI SDK. It gives Lucent the ability to work between conversations — performing memory maintenance, goal tracking, research, and self-improvement.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_MAX_SESSIONS` | `3` | Max concurrent daemon sessions |
| `LUCENT_DAEMON_INTERVAL` | `15` | Minutes between daemon cycles |
| `LUCENT_DAEMON_MODEL` | `claude-opus-4.6` | Model for standard tasks |
| `LUCENT_DAEMON_RESEARCH_MODEL` | `claude-opus-4.6` | Model for research tasks |
| `LUCENT_MCP_URL` | `http://localhost:8766/mcp` | MCP server URL for memory access |
| `LUCENT_MCP_API_KEY` | — | API key for MCP authentication |

### Running the Daemon

```bash
# Run continuously (default: cycles every 15 minutes)
python daemon/daemon.py

# Run a single cycle and exit
python daemon/daemon.py --once

# Run a specific named task and exit
python daemon/daemon.py --task maintenance

# Override cycle interval (in minutes)
python daemon/daemon.py --interval 30
```

#### Available Tasks

| Task | Description |
|------|-------------|
| `maintenance` | Quick memory maintenance — fix obvious issues in recent memories |
| `goals` | Review active goals, assess progress, update notes |
| `reflect` | Deep self-reflection on behavioral patterns and growth |
| `research` | Research topics relevant to active goals or recent work |
| `consolidate` | Deep memory consolidation — merge overlapping content, find connections |

When running continuously, the daemon schedules heavier tasks at fixed intervals (goal review every ~2 hours, deep research every ~3 hours, self-reflection every ~4 hours, memory consolidation every ~6 hours). Non-scheduled cycles rotate through lightweight activities: quick research, code exploration, memory maintenance, documentation review, and web research.

The daemon's status and logs are viewable at http://localhost:8766/daemon.

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
| `/logout` | End session |
| `/setup` | First-run account creation |
| `/memories` | Memory management UI |
| `/daemon` | Daemon status and management |
| `/audit` | Audit log viewer |
| `/users` | User management (admin) |
| `/settings` | API keys, password, profile |

## License

Lucent Source Available License 1.0 — free for non-commercial use. Commercial use requires a separate license. Converts to Apache 2.0 on March 3, 2028. See [LICENSE](LICENSE) for full terms.