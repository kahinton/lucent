# Architecture

Lucent is an MCP server with persistent memory, autonomous task execution, and a web dashboard — all served from a single process on a single port.

## High-Level Overview

```
┌─────────────────────────────────────────────────────┐
│                   Lucent Server                      │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌────────────────────┐ │
│  │ MCP /mcp │  │ REST API │  │ Web UI (Jinja2)    │ │
│  │          │  │  /api/*   │  │  / /memories /...  │ │
│  └────┬─────┘  └────┬─────┘  └────────┬───────────┘ │
│       │              │                 │              │
│  ┌────┴──────────────┴─────────────────┴───────────┐ │
│  │              Service Layer                       │ │
│  │  Memory · Requests · Schedules · Definitions     │ │
│  └─────────────────────┬───────────────────────────┘ │
│                        │                              │
│  ┌─────────────────────┴───────────────────────────┐ │
│  │              PostgreSQL (via asyncpg)            │ │
│  └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│                   Daemon Process                     │
│  Cognitive · Dispatch · Scheduler · Autonomic loops  │
│  Communicates via MCP tools → Lucent Server          │
└─────────────────────────────────────────────────────┘
```

## Source Layout

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
```

## Memory System

### Memory Types

| Type | Purpose | Key Metadata |
|------|---------|--------------|
| **experience** | Store interactions and their outcomes | context, outcome, lessons_learned |
| **technical** | Code patterns, solutions, technical knowledge | language, code_snippet, repo, filename |
| **procedural** | Step-by-step processes and workflows | steps, prerequisites, estimated_time |
| **goal** | Track long-term objectives | status, deadline, milestones, blockers |
| **individual** | Information about people | name, relationship, organization, preferences |

### Importance Scale

| Rating | Level | Use For |
|--------|-------|---------|
| 1-3 | Routine | Minor details, temporary context |
| 4-6 | Useful | Standard practices, general knowledge |
| 7-8 | Important | Key decisions, significant learnings |
| 9-10 | Critical | Essential knowledge, major breakthroughs |

### Search

- **`search_memories`** — fuzzy search on the content field only. Faster, more focused.
- **`search_memories_full`** — fuzzy search across content, tags, and metadata. Broader, catches more.

Both use PostgreSQL trigram-based similarity matching for natural language queries.

### Versioning

Every memory update is versioned. Use `get_memory_versions` to browse history and `restore_memory_version` to roll back.

## MCP Tools

### Memory Tools

| Tool | Purpose |
|------|---------|
| `create_memory` | Create a new memory with type, content, tags, importance, and metadata |
| `get_memory` | Retrieve a full memory by its UUID |
| `get_memories` | Retrieve multiple memories by ID in a single call |
| `update_memory` | Update an existing memory |
| `delete_memory` | Soft delete a memory (can be recovered) |
| `search_memories` | Fuzzy search on content field |
| `search_memories_full` | Fuzzy search across all fields |
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

Only the memory owner can update it. All changes are versioned — use `get_memory_versions` and `restore_memory_version` to browse or roll back.

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

## Autonomous Daemon

The daemon runs as a separate process that communicates with the Lucent server over MCP. It operates four independent loops:

| Loop | Interval | Purpose |
|------|----------|---------|
| **Cognitive** | 15 min | Perceive → Reason → Decide → Act cycle. Reads system state, reasons about goals, creates requests/tasks via MCP tools |
| **Dispatch** | Event-driven | Claims pending tasks, resolves agent definitions, creates sandboxes, runs sub-agent LLM sessions, validates results |
| **Scheduler** | 60s | Checks for due schedules (cron/interval/once), creates request+task pairs, advances schedule state |
| **Autonomic** | ~2 hours | Background maintenance: memory cleanup, learning extraction, environment adaptation |

The dispatch loop uses PostgreSQL `LISTEN/NOTIFY` for near-instant task pickup (with 60s polling fallback). Multiple daemon instances can run in parallel — task claims are atomic.

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

Tasks can include **output contracts** — JSON Schema definitions that the daemon validates against before completing the task. Three failure modes are available: `fail`, `fallback`, or `retry_then_fallback`.

Requests support a **review workflow**: when `LUCENT_REQUIRE_APPROVAL=true`, completed requests transition to `review` status for human approval. Reviewers can approve (→ `completed`) or reject with feedback (→ `needs_rework`).

### Agent Definitions

The daemon only dispatches tasks to **approved** agent definitions. Definitions are created via the adaptation system or manually, reviewed in the web UI, and must be approved before use. Each definition includes:

- System prompt (role, tools, guardrails)
- Linked skills and MCP server access
- Per-agent tool allowlists

### Environment Adaptation

On first run in a new workspace, the daemon assesses the environment (tech stack, tools, domain), classifies it, and auto-generates domain-appropriate agent and skill definitions. These are proposed for human approval before use.

### Daemon Source Layout

```
daemon/
├── daemon.py           # Autonomous daemon (4 loops: cognitive, dispatch, scheduler, autonomic)
├── adaptation.py       # Environment assessment + capability generation
├── output_validation.py # Output contract validation and repair
├── cognitive.md        # Cognitive governance context
├── agents/             # Runtime agent workspace
├── tasks/              # Task module stubs
└── templates/          # Jinja2 templates for domain-specific agents/skills
    ├── agents/         # Agent definition templates (.md.j2)
    └── skills/         # Skill definition templates (.md.j2)
```

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

For detailed sandbox documentation, see [Sandboxes](sandboxes.md).

## Endpoints

All services run on a single port (default 8766):

| Endpoint | Purpose |
|----------|--------|
| `/mcp` | MCP protocol (requires API key) |
| `/api/*` | REST API (requires API key) |
| `/api/health` | Health check (no auth required) |
| `/api/docs` | OpenAPI documentation |
| `/login` | Authentication |
| `/setup` | First-run account creation |

For the full REST API reference, see [API Reference](api-reference.md).

## Related Documentation

- [Getting Started](getting-started.md) — installation and first-run setup
- [Configuration](configuration.md) — environment variables and settings
- [Security Model](security-model.md) — authentication, authorization, multi-tenancy
- [Secret Storage](secret-storage.md) — pluggable encryption providers
- [Observability](observability.md) — metrics, traces, dashboards
- [Deployment Guide](deployment-guide.md) — production deployment
- [Kubernetes Deployment](kubernetes-deployment.md) — Helm chart and operator
