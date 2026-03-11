---
name: onboarding-specialized
description: 'Guide new contributors through Lucent project setup, architecture overview, coding conventions, and first-contribution workflow'
---

# Onboarding

Step-by-step guide for setting up the Lucent development environment, understanding the architecture, and making a first contribution.

## When to Use

- New contributor joining the project
- Setting up a fresh development environment
- Needing an architecture refresher
- Preparing to make a first pull request

## What is Lucent?

Lucent is an MCP (Model Context Protocol) server that provides persistent memory for LLMs. It stores memories with types, tags, and importance levels, and retrieves them via PostgreSQL trigram fuzzy search. It runs as a single-port service exposing MCP tools (`/mcp`), a REST API (`/api/*`), and a web dashboard (`/`).

## Environment Setup

### Prerequisites

- Python 3.12+
- Docker & Docker Compose
- Git

### Option 1: Local Development (Recommended)

```bash
# Clone the repository
git clone https://github.com/kahinton/lucent.git && cd lucent

# Start only PostgreSQL
docker compose up -d postgres

# Install with dev dependencies
pip install -e ".[dev]"

# Set database connection
export DATABASE_URL="postgresql://lucent:lucent_dev_password@localhost:5433/lucent"

# Run the server
lucent
```

The server starts on port 8766. Verify: `curl http://localhost:8766/api/health`

### Option 2: Full Docker

```bash
docker compose up
```

This uses `Dockerfile.dev` which mounts the source for live code reloading. The database runs on port 5433 (mapped from container port 5432).

### Verify Setup

```bash
# Server health
curl http://localhost:8766/api/health

# Database connectivity
docker exec lucent-db pg_isready -U lucent

# Run tests
pytest

# Run linter
ruff check src/ tests/
```

## Architecture Overview

### Single-Port Design

Everything runs on port 8766:
- **`/mcp`** — MCP protocol endpoint (FastMCP with auth middleware)
- **`/api/*`** — REST API for CRUD, search, users, orgs, audit
- **`/`** — Web UI dashboard

### Source Structure

```
src/lucent/
├── server.py                  # Entry point — unified MCP + API + Web server
├── auth.py                    # Authentication logic
├── auth_providers.py          # Pluggable auth (basic, API key)
├── api/
│   ├── routers/               # FastAPI routers
│   │   ├── memories.py        # /api/memories CRUD endpoints
│   │   ├── search.py          # /api/memories/search
│   │   ├── users.py           # User management
│   │   ├── orgs.py            # Organization management
│   │   └── audit.py           # Audit log
│   └── deps.py                # Shared FastAPI dependencies
├── db/
│   ├── memory.py              # Memory repository — CRUD + fuzzy search
│   ├── user.py                # User data access
│   ├── api_key.py             # API key management
│   ├── audit.py               # Audit logging
│   └── migrations/            # Numbered SQL migration files (001_init.sql, ...)
├── models/
│   ├── memory.py              # MemoryType enum, memory data model
│   └── validation.py          # Pydantic metadata validation per type
├── tools/
│   └── memories.py            # MCP tool implementations (16 tools)
└── prompts/
    └── memory_usage.py        # System prompts for LLM clients
```

### Core Concepts

**Memory Types** (defined in `src/lucent/models/memory.py`):
| Type | Purpose |
|------|---------|
| `experience` | Things that happened — decisions, lessons, events |
| `technical` | Code patterns, solutions, architecture knowledge |
| `procedural` | Step-by-step processes that work |
| `goal` | Objectives tracked over time |
| `individual` | Info about people (system-managed, cannot be created/deleted via tools) |

**Search**: PostgreSQL trigram similarity (`pg_trgm` extension) for fuzzy content matching. Configured in `src/lucent/db/memory.py`.

**Versioning**: Every memory update creates a new version. Optimistic locking via `expected_version` parameter prevents conflicts.

**Auth**: API keys with `hs_` prefix, validated by `MCPAuthMiddleware` in `server.py`. Rate limiting per key.

### Daemon Architecture (Optional Reading)

The daemon (`daemon/daemon.py`) is a separate process that runs autonomously:
- **Cognitive loop**: Perceive → Reason → Decide → Act (creates tasks as memories)
- **Task dispatch**: Claims `daemon-task` memories, runs sub-agent sessions
- **Autonomic layer**: Periodic memory maintenance and learning extraction
- Sub-agent roles defined in `daemon/agents/*.agent.md`

## Coding Conventions

### Style (enforced by Ruff)

- **Python target**: 3.12
- **Line length**: 100 characters
- **Rules**: E (errors), F (pyflakes), I (imports), N (naming), W (warnings)
- **Format**: `ruff format src/ tests/`
- **Lint**: `ruff check src/ tests/`

### Import Order

1. Standard library
2. Third-party packages
3. Local imports

Ruff's `I` rule enforces this automatically.

### Type Hints

All public functions should have type annotations. Use `str | None` style (Python 3.12 union syntax).

### Database Migrations

Migrations are numbered SQL files in `src/lucent/db/migrations/`:
```
001_init.sql
002_add_users.sql
...
```

- Never modify existing migration files — always create new ones
- Use `IF NOT EXISTS` / `IF EXISTS` for idempotent SQL
- Applied automatically on server startup

## Running Tests

```bash
# All tests
pytest

# Specific test file
pytest tests/test_mcp_tools.py -v

# With short tracebacks
pytest --tb=short

# Tests require a running PostgreSQL instance
docker compose up -d postgres
```

Test files live in `tests/` and cover:
- MCP tool operations (`test_mcp_tools.py`)
- Auth and API keys
- Models and validation
- Database operations
- Rate limiting and RBAC
- Daemon coordination

## First Contribution Workflow

### 1. Pick a Task

Good first areas:
- Documentation improvements
- Adding test coverage for edge cases in `src/lucent/tools/memories.py`
- Improving validation messages in `src/lucent/models/validation.py`
- Adding missing type hints

### 2. Branch and Code

```bash
git checkout -b feature/your-feature main
# Make changes
ruff format src/ tests/
ruff check src/ tests/
pytest
```

### 3. Commit

Use clear, descriptive commit messages. Reference issues if applicable.

### 4. Submit PR

- Fork → push branch → open PR against `main`
- Describe what changed and why
- Ensure tests pass and linting is clean
- Use GitHub Issues for bug reports

### Key Files to Read First

If you're exploring the codebase, start with these in order:
1. `README.md` — project overview
2. `src/lucent/models/memory.py` — core data model
3. `src/lucent/tools/memories.py` — MCP tool interface
4. `src/lucent/db/memory.py` — database operations
5. `tests/test_mcp_tools.py` — how tools are tested
