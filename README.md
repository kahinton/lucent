# Hindsight

An MCP (Model Context Protocol) server providing persistent memory functionality for LLMs. Store, search, and retrieve memories across conversations to enhance AI assistant capabilities.

## Features

- **Five Memory Types**: Experience, Technical, Procedural, Goal, and Individual memories with type-specific metadata
- **Fuzzy Search**: PostgreSQL trigram-based similarity search for natural language queries
- **Dual Search Modes**: Content-only search (`search_memories`) or full-field search (`search_memories_full`)
- **Tag Management**: Built-in tools to promote tag consistency across memories
- **Memory Linking**: Connect related memories for contextual retrieval
- **User Management**: OAuth support (Google, GitHub) and SAML for enterprises
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

- Python 3.12+
- Docker and Docker Compose
- An MCP-compatible client (e.g., Claude Desktop)

### 1. Start the Database

```bash
# Clone and enter the repository
cd hindsight

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
# Set database URL and enable dev mode
export DATABASE_URL="postgresql://hindsight:hindsight_dev_password@localhost:5432/hindsight"
export HINDSIGHT_DEV_MODE=true

# Run the MCP server
hindsight
```

### 5. Configure Your MCP Client

For VS Code with the MCP extension, add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "memory-server": {
      "url": "http://localhost:8765/mcp",
      "type": "http"
    }
  }
}
```

For Claude Desktop, add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hindsight": {
      "command": "hindsight",
      "env": {
        "DATABASE_URL": "postgresql://hindsight:hindsight_dev_password@localhost:5432/hindsight",
        "HINDSIGHT_DEV_MODE": "true"
      }
    }
  }
}
```

## Authentication

### Development Mode

For local development and testing, enable dev mode to bypass authentication:

```bash
export HINDSIGHT_DEV_MODE=true
```

This creates a local "dev-user" that all memories are associated with.

### Production Mode

In production, disable dev mode and configure OAuth or SAML:

```bash
export HINDSIGHT_DEV_MODE=false
# Configure your OAuth provider(s)
```

Supported providers:
- **Google OAuth**
- **GitHub OAuth**
- **SAML** (for enterprise SSO)

Each user gets a unique user_id, and all memories are linked to their user account via foreign key.

## Available Tools

### Memory CRUD

| Tool | Purpose |
|------|---------|
| `create_memory` | Create a new memory with type, content, tags, importance, and metadata |
| `get_memory` | Retrieve a full memory by its UUID |
| `update_memory` | Update an existing memory |
| `delete_memory` | Soft delete a memory (can be recovered) |

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

### Tool Parameters

#### create_memory

```
Arguments:
- username (required): Username of the person this memory is for
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

Hindsight provides prompt templates to help LLMs use the memory system effectively:

- **memory_usage_guide**: Comprehensive guidance on memory types, importance ratings, and best practices
- **memory_usage_guide_short**: Condensed version for limited prompt space

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
docker compose logs -f hindsight
```

### Database Only

For local development, run just the database:

```bash
docker compose up -d postgres
```

### Persistent Storage

Data is stored in a Docker volume (`hindsight_data`). To backup:

```bash
docker compose exec postgres pg_dump -U hindsight hindsight > backup.sql
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
src/hindsight/
├── server.py          # MCP server entry point
├── db/
│   ├── client.py      # asyncpg connection pool & repository
│   └── migrations/    # SQL migration files
├── models/
│   └── memory.py      # Pydantic models for all memory types
├── tools/
│   └── memories.py    # MCP tool implementations
└── prompts/
    └── memory_usage.py # System prompt templates
```

## License

MIT