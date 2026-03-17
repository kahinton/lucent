# Changelog

All notable changes to Lucent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - 2026-03-17

### Added

#### Daemon Architecture
- Four independent daemon loops: cognitive, dispatch, scheduler, autonomic
- Event-driven task dispatch via PostgreSQL LISTEN/NOTIFY (`task_ready`, `request_ready` channels)
- Multi-instance support with atomic task claiming
- File watcher for auto-restart on source changes
- Watchdog thread to detect and recover from event loop freezes
- Role-based daemon deployment (`LUCENT_DAEMON_ROLES` — cognitive, dispatcher, scheduler, autonomic)

#### Request/Task Pipeline
- Structured request tracking with title, description, priority, source
- Task decomposition under requests with agent type, model override, sandbox config
- Full event timeline for each task (created → claimed → running → completed/failed)
- Memory lineage tracking (task_memory_links) for audit trails
- Stale task recovery and retry mechanisms

#### Sandboxes
- Docker-based isolated execution environments with the `SandboxManager`
- Sandbox templates: reusable configs (image, repo URL, setup commands, env vars, resource limits)
- Resource limits: memory (2GB default), CPU (2 cores), disk (10GB), network policies
- File operations: exec, read_file, write_file, list_files within containers
- Auto-cleanup on task completion or timeout (30 min default)
- REST API for sandbox instances and templates
- Web UI for template and instance management

#### Schedules
- Cron, interval, and one-time schedule types
- Timezone-aware cron evaluation (IANA names + US aliases)
- Schedule run history with request linking
- Max run limits and expiration dates
- Per-schedule model and sandbox template overrides
- MCP tools: `create_schedule`, `list_schedules`, `toggle_schedule`, `get_schedule_details`
- Web UI for schedule creation, editing, and monitoring

#### Agent Definitions
- Database-backed registry for agent, skill, and MCP server definitions
- Approval workflow: proposed → active/rejected (human-vetted before daemon use)
- Agent-skill linking with per-agent tool allowlists
- Web UI with tabbed management and approval actions
- Sync from `.github/agents/definitions/` on server startup

#### Environment Adaptation
- Automated workspace assessment (tech stack, tools, domain classification)
- Domain archetype mapping (software, legal, support, research)
- Jinja2-templated agent and skill generation
- Validation of generated definitions (required sections check)
- Auto-proposal to definitions system for human approval

#### LLM Engine
- Abstract `LLMEngine` interface with blocking and streaming patterns
- GitHub Copilot SDK implementation (`CopilotEngine`)
- LangChain fallback stub (`LangChainEngine`)
- Engine factory with `LUCENT_LLM_ENGINE` selection
- Normalized session events (MESSAGE, TOOL_CALL, SESSION_IDLE, ERROR)
- Streaming chat endpoint (`/api/chat`)

#### Model Registry
- Catalog of 20+ LLM models across OpenAI, Anthropic, and Google
- Categories: fast, general, reasoning, agentic, visual
- Per-task model selection and validation
- `get_recommended_model()` for task-type based selection

#### Web UI
- Activity page: request/task tracking with event timelines and status filters
- Definitions page: agent, skill, MCP server management with approval workflow
- Schedules page: create/edit/monitor schedules with run history
- Sandboxes page: template and instance management with launch/destroy actions
- Daemon review queue: approve/reject/comment on daemon-generated memories
- Dashboard: stats cards (memories, agents, skills, active requests), recent memories, top tags

#### MCP Tools
- Request tools: `create_request`, `create_task`, `log_task_event`, `link_task_memory`, `get_request_details`, `list_pending_requests`, `list_pending_tasks`
- Schedule tools: `create_schedule`, `list_schedules`, `toggle_schedule`, `get_schedule_details`

#### Infrastructure
- GitHub Actions CI: linting (Ruff) + pytest with PostgreSQL service
- CONTRIBUTING.md and SECURITY.md
- `.env.example` with all configuration variables

### Changed
- Daemon architecture from single cognitive loop to four independent loops
- Task system from memory-based daemon tasks to structured request/task pipeline
- Daemon invocation from `python daemon/daemon.py` to `python -m daemon.daemon`

## [0.1.0] - 2026-03-10

### Added

- MCP server with memory CRUD operations (create, read, update, delete)
- Fuzzy search with PostgreSQL full-text search and trigram matching
- Memory linking and relationship tracking
- Soft delete with restore capability
- Memory versioning and audit trail
- API key authentication
- Rate limiting
- Personal deployment mode (single user)
- Team deployment mode with organizations and RBAC
- REST API alongside MCP protocol
- Web UI for memory browsing and management
- Docker Compose deployment with PostgreSQL
- Export/import functionality for memory backup
- Structured logging with configurable output
- Daemon process for autonomous cognitive cycles
- 687+ tests covering core functionality

[0.2.0]: https://github.com/kahinton/lucent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kahinton/lucent/releases/tag/v0.1.0
