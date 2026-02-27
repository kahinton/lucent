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

### 1. Start the Database

```bash
# Clone and enter the repository
cd lucent

# Start PostgreSQL with Docker
docker compose up -d postgres
```

### 2. Install the Package

```bash
# Create virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install the package
pip install -e .
```

### 3. Configure Environment

```bash
# Copy example environment file
cp .env.example .env

# Edit .env with your settings (defaults work for local development)
```

### 4. Run the Server

```bash
# Using Docker Compose (recommended — runs both DB and server)
docker compose up -d

# Or run the server directly (if you started the DB separately)
export DATABASE_URL="postgresql://lucent:lucent_dev_password@localhost:5433/lucent"
lucent
```

### 5. First-Time Setup

Open http://localhost:8766 in your browser. On first run, you'll see a setup page where you:

1. Create your user account (username, password)
2. Receive your MCP API key (shown once — copy it!)

### 6. Configure Your MCP Client

For VS Code with the MCP extension, add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "lucent": {
      "url": "http://localhost:8766/mcp",
      "type": "http",
      "headers": {
        "Authorization": "Bearer mcp_your_api_key_here"
      }
    }
  }
}
```

Replace `mcp_your_api_key_here` with the API key from the setup page. You can also generate additional keys at http://localhost:8766/settings.

For Claude Desktop, add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "lucent": {
      "url": "http://localhost:8766/mcp",
      "headers": {
        "Authorization": "Bearer mcp_your_api_key_here"
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
- MCP/API: API key (`Authorization: Bearer mcp_...`)

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

### Tool Parameters

#### create_memory

```
Arguments:
- type (required): experience | technical | procedural | goal | individual
- content (required): Main content of the memory
- tags: List of categorization tags
- importance: Rating 1-10 (default: 5)
- related_memory_ids: UUIDs of related memories
- metadata: Type-specific metadata object
```

#### search_memories / search_memories_full

```
Arguments:
- query: Fuzzy search query
- username: Filter by username
- type: Filter by memory type
- tags: Filter by tags (any match)
- importance_min/max: Filter by importance range
- created_after/before: Filter by date range (ISO format)
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

### Install Development Dependencies

```bash
pip install -e ".[dev]"
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
│   └── routers/        # REST API endpoints
├── web/
│   ├── routes.py       # Web UI routes
│   └── templates/      # Jinja2 templates
├── db/
│   ├── pool.py         # Connection pool + migration runner
│   ├── memory.py       # Memory repository (CRUD + search)
│   ├── user.py         # User repository
│   ├── audit.py        # Audit log + versioning repository
│   ├── api_key.py      # API key repository
│   ├── access.py       # Access tracking repository
│   ├── organization.py # Organization repository
│   └── migrations/     # SQL migration files
├── models/
│   ├── memory.py       # Pydantic models for memory types
│   └── validation.py   # Metadata validation
├── tools/
│   └── memories.py     # MCP tool implementations
└── prompts/
    └── memory_usage.py # System prompt templates
```

## Endpoints

All services run on a single port (default 8766):

| Endpoint | Purpose |
|----------|--------|
| `/mcp` | MCP protocol (requires API key) |
| `/api/*` | REST API (requires API key) |
| `/api/docs` | OpenAPI documentation |
| `/` | Web dashboard |
| `/login` | Authentication |
| `/setup` | First-run account creation |
| `/memories` | Memory management UI |
| `/settings` | API keys, password, profile |

## License

Business Source License 1.1