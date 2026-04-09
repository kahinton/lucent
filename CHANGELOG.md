# Changelog

All notable changes to Lucent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

#### Request Approval Gate
- Pre-work approval flow for daemon-created requests (`LUCENT_AUTO_APPROVE` env var)
- When `LUCENT_AUTO_APPROVE=false`, daemon/cognitive/schedule requests require human approval before any tasks are dispatched
- User/API-created requests are always auto-approved
- New `approval_status` column on requests: `auto_approved`, `pending_approval`, `approved`, `rejected`
- Approve/reject actions in the Review Queue UI with comment support
- Rejected requests automatically cancelled and generate learning memories (tagged `approval-rejected`)
- Post-completion review now properly gated by `LUCENT_REQUIRE_APPROVAL` env var (previously always-on)
- Migration 050: adds `approval_status`, `approved_by`, `approved_at`, `approval_comment` columns

#### Review Queue UI Redesign
- Review Queue now shows two sections: "Pending Approval" (pre-work) and "Completed Work Review" (post-completion)
- Pending approval cards show request description, priority, source, task count, and creator
- Amber-bordered approval cards with prominent "Approve — Start Work" button
- Blue-bordered review cards for post-completion sign-off
- Cards fade out smoothly after approve/reject action
- Approval status badge shown on request detail page

#### Goal-Aware Request Tracking
- Request-to-memory linking (`request_memories`) for explicit goal/context/reference relationships
- Goal-aware deduplication path for tracked requests tied to memory IDs
- Dashboard and request detail UI enhancements for linked-memory visibility

#### Memory Usage Analytics
- Memory access logging infrastructure for reads and search results
- Access analytics API endpoints for history, frequency trends, and most/least accessed memories
- New Memory Analytics UI page for surfacing stale and never-accessed memories

#### Memory Lifecycle
- Memory decay module with test coverage to support lifecycle/retention behavior

#### Runtime Coordination
- Multi-daemon safety improvements for task distribution
- Migration 054: daemon instance registry plus task claim lease/heartbeat metadata

#### Configuration
- `LUCENT_CREDENTIAL_KEY` added to `.env.example`, `docker-compose.yml`, and `docker-compose.prod.yml` for integration credential encryption
- `LUCENT_SIGNING_SECRET` now included in dev compose defaults for stable cookie signing across restarts

### Changed
- `_check_request_completion` now respects `LUCENT_REQUIRE_APPROVAL` — completed requests go straight to `completed` status by default instead of `review`
- `list_pending_tasks` query now filters out tasks from unapproved requests
- Learning extraction skill updated to search for `approval-rejected` tagged memories
- Rejected request lifecycle now uses an explicit `rejection_processing` phase before cancellation (migration 053)
- `LUCENT_REQUIRE_APPROVAL` documented in `docs/configuration.md`
- `GITHUB_TOKEN` guidance expanded in `.env.example` and `docs/getting-started.md` (Copilot scope/daemon requirements)
- Documentation refresh across `docs/configuration.md` and `docs/deployment-guide.md` for launch-readiness

### Fixed
- `.env.example` session TTL comment corrected to default `24` hours (`LUCENT_SESSION_TTL_HOURS`)
- `.env.example` daemon MCP API key placeholder updated from `mcp_...` to `hs_...`
- `docs/deployment-guide.md` corrected `LUCENT_SECURE_COOKIES` default to `true` (with local HTTP override guidance)
- Tool-calling robustness improved in MCP bridges/LLM integration paths
- Skills approval acceptance handling corrected in definitions web routes

### Removed
- Dead `LUCENT_DAEMON_RESEARCH_MODEL` entry removed from `.env.example`

### Security
- Protected built-in agent/skill/MCP definitions from daemon-driven modification paths

## [0.3.0] - 2026-03-27

### Added

#### Secret Storage — OpenBao Integration
- OpenBao (Vault-compatible) sidecar in `docker-compose.yml` for key-isolated secret encryption
- Transit secret provider (`transit`) — encrypts/decrypts via OpenBao's Transit engine; Lucent never sees the encryption key
- Vault KV v2 secret provider (`vault`) — full implementation for external Vault/OpenBao clusters
- Auto-detection of available secret providers at startup (`LUCENT_SECRET_PROVIDER=auto`)
- Migration utility (`scripts/migrate_secrets_to_transit.py`) for re-encrypting builtin (Fernet) secrets through Transit
- OpenBao init script (`docker/openbao-init.sh`) — configures KV v2, Transit engine, scoped policy, and token
- Secrets management Web UI and REST API (`/api/secrets`)

#### OpenTelemetry Observability
- Full OpenTelemetry integration: traces, metrics, and log correlation
- FastAPI and asyncpg auto-instrumentation for HTTP and database spans
- Central metrics registry (`lucent.metrics`) with HTTP request duration, error counters, and daemon session gauges
- Daemon instrumented with spans for cognitive cycles, dispatch, and LLM sessions
- Docker Compose observability profile: OpenTelemetry Collector, Prometheus, Jaeger, and Grafana with pre-built dashboards
- Opt-in via `OTEL_ENABLED=true` — zero overhead when disabled (no-op providers)

#### Integrations Framework
- Slack and Discord integration adapters with full test coverage
- Identity resolution: link external platform users to Lucent accounts via pairing challenges
- Signature verification middleware for webhook security
- Credential encryption for stored integration secrets
- Rate limiting per user, organization, webhook IP, and pairing challenge
- REST API (`/api/integrations`) for managing integrations, user links, and webhooks
- Database schema for integration configs, user links, events, and pairing challenges (migrations 028–030)

#### Request Review Lifecycle
- Post-completion review workflow: requests transition through `review` and `needs_rework` states
- Dedicated `request-review` agent definition for structured post-completion reviews (replaces generic `code-review`)
- Review count tracking, feedback storage, and configurable max review rounds
- API endpoints for review queue: `GET /api/requests/review`, `POST /api/requests/{id}/review-approve`, `POST /api/requests/{id}/review-reject`
- Database migration 044 for review lifecycle columns and indexes

#### Task Output Contracts
- Structured output validation for daemon tasks via JSON Schema contracts
- Tasks can declare an `output_contract` with a JSON Schema, failure policy (`fail`/`fallback`/`retry_then_fallback`), and max retries
- Agent results are extracted from `<task_output>` blocks, validated, and stored as structured JSONB
- Validation status tracking: `valid`, `invalid`, `extraction_failed`, `fallback_used`, `repair_succeeded`
- Database migration 045 for output contract and structured result columns

#### Groups and Resource Ownership
- Group management: create groups, add/remove members, assign group admin roles
- Resource ownership model: agents, skills, MCP servers, sandbox templates, and secrets have owner users and groups
- Access control service resolving visibility by built-in scope → owner → group → admin role
- REST API (`/api/groups`) and Web UI for group CRUD and membership management
- Database migrations 037–039 for groups, resource ownership columns, and requesting user tracking

#### Model Registry and Multi-Engine Support
- Database-backed model registry with 20+ seeded models across OpenAI, Anthropic, and Google
- Admin API (`/api/admin/models`) for enabling, disabling, adding, and removing models
- Model management Web UI page
- Multi-engine LLM support: configure different engines (Copilot, LangChain) per model
- `list_available_models` MCP tool for daemon task model selection
- Model validation with strict and lenient modes
- Database migrations 031 and 043 for model registry and engine overrides

#### Sandbox Enhancements
- MCP bridge server for sandbox containers — proxies memory tools back to Lucent's REST API
- Devcontainer.json detection and parsing: lifecycle commands, env vars, image overrides, forwarded ports
- Sandbox output capture modes for structured result extraction
- `sandbox_config` and `sandbox_template_id` parameters on `create_task` MCP tool
- `sandbox-orchestrator` and `sandbox` agent definitions

#### Chat Improvements
- Enhanced streaming chat endpoint (`/api/chat/stream-v2`) with granular SSE events: text deltas, tool calls, tool results, reasoning
- Agent-aware chat: select an agent definition to shape the chat system prompt and load its granted skills
- Web UI chat page with full conversation interface

#### Kubernetes Deployment
- Helm chart (`deploy/helm/lucent/`) with templates for deployment, services, ingress, HPA, PDB, network policies, and ServiceMonitor
- Environment-specific values files: dev, staging, production
- Kubernetes operator (`deploy/operator/`) with CRD for `LucentInstance` custom resources
- Operator handles reconciliation, backup CronJobs, and air-gapped deployment examples
- Comprehensive Kubernetes deployment documentation

#### Security Hardening
- Content Security Policy (CSP) and security headers middleware (X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy)
- Bundled Tailwind CSS, HTMX, and DOMPurify locally — eliminates CDN dependencies
- Memory content scanning for prompt injection patterns
- RBAC enforcement on web definition, sandbox, and auth routes
- Insecure default credential detection at startup with critical-level warnings in team mode
- Docker image base pinned with SHA256 digests for supply chain security
- Dockerfile `HEALTHCHECK` instruction added
- `docker-compose.prod.yml` for production deployment: no default credentials, docker-socket-proxy, production OpenBao mode
- Upper-bound version pins on security-sensitive dependencies (`cryptography`, `PyJWT`, `python-multipart`)
- Secret pattern redaction in daemon log output
- Restricted secrets API with scoped access
- Org list endpoint scoped to user's own organization
- 100K character limit on memory content

#### Agent and Skill System
- Built-in agent and skill definitions synced from `.github/` to database on startup
- `definition-engineer` agent and `definition-engineering` skill for crafting agent/skill definitions
- Consolidated deprecated specialized skills into their parent skills (removed `-specialized` variants)
- MCP tools for agent and skill definition management (full CRUD parity with REST API)
- Pagination support for definition listing endpoints
- Anti-pattern sections added to 6 critical skills
- Disambiguation sections added to 5 overlapping skills
- Numbered procedures and boundary sections across all skills

#### Daemon Improvements
- Graceful daemon restart: defers reload when LLM sessions are active (drain mode)
- Session idle timeout (`LUCENT_SESSION_IDLE_TIMEOUT`, default 300s) kills sessions with no LLM activity
- Request dependency policy: `strict` (default) blocks later tasks on predecessor failure; `permissive` allows continuation
- Sequence order validation with database CHECK constraint for task ordering
- Request deduplication via content fingerprinting (prevents duplicate requests in active states)
- Daemon memories automatically shared for org visibility
- `list_active_work` API for daemon to check existing work before creating duplicates

#### API Enhancements
- `/api/requests` endpoint accepts `source` query parameter for filtering by request origin
- `/api/requests/active` endpoint returns all non-completed requests with task status summaries
- Paginated responses across all list endpoints (definitions, tasks, requests, schedules)
- `list_active_work` MCP tool for checking in-progress work
- Force password change flag on user accounts (migration 026)
- HTML error pages for web UI
- MCP tool discovery and caching for server definitions (migration 042)
- `schedule` added as valid request source

#### Infrastructure
- DevContainer support for VS Code development environments
- Production Docker Compose (`docker-compose.prod.yml`) with hardened security defaults
- Comprehensive documentation: architecture guide, security model, configuration reference, troubleshooting, getting started, sandbox guide, observability guide, Kubernetes deployment, operator guide, Slack setup and security guides, migration guide for security changes

### Changed
- New `daemon` RBAC role for service accounts — scoped to memory operations (read all, update own, delete any, share) without admin privileges like user management or integration access
- Daemon service account uses `daemon` role instead of `member` for proper memory consolidation permissions (migration 046)
- Daemon API key provisioning no longer caches keys to `.daemon_api_key` file — keys are provisioned fresh each startup
- Request status lifecycle standardized: `planning` renamed to `planned` across all code paths
- Request source enum expanded from 4 to 5 values (`schedule` added) with shared constants module and DB CHECK constraint
- Session timeout increased from 600s to 3600s default (`LUCENT_SESSION_TIMEOUT`)
- Session cookie TTL reduced from 72 hours to 24 hours
- Task completion/failure endpoints now accept JSON body instead of query parameters (supports larger payloads)
- Definition listing endpoints return paginated response format (`{items, total_count, offset, limit, has_more}`)
- Removed team_mode gate from groups routes — groups available in all modes
- Lucent identity definition updated with conversation mode boundaries (no direct task creation from conversation)
- README streamlined; detailed content moved to dedicated docs
- Version bumped to 0.2.0 in package metadata

### Fixed
- Memory consolidation bloat: tightened daemon prompts to prevent memory creation during consolidation and learning extraction runs
- Learning extraction prompt hardened against creating duplicate memories
- Request deduplication prevents identical requests from being created while one is already active
- Pagination format mismatch in daemon definition and task lookups
- Cron expression validation: corrected DOM/DOW semantics for proper scheduling
- MCP URL port corrected from 8767 to 8766 in setup completion page
- API key prefix examples updated from `mcp_` to `hs_` in settings page
- `SECURE_COOKIES` default documentation corrected (true, not false)
- Stale task recovery and multiple lint fixes (unused imports, unsorted imports, undefined names)
- Test isolation: model registry and engine singletons properly reset between tests
- Definition table cleanup in test teardown to avoid FK violations
- Mock SecretRegistry in sandbox launch tests

### Security
- Admin and daemon roles granted `MEMORY_DELETE_ANY` permission — admins can manage org memories, daemon uses it for consolidation
- `MANAGE_INTEGRATIONS` permission added to admin and owner roles
- Docker socket access documented with security guidance; production config uses docker-socket-proxy
- Prompt injection scanning on memory content
- Secrets redacted from daemon log output using pattern matching
- API key file caching removed — eliminates credential persistence on disk

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

[Unreleased]: https://github.com/kahinton/lucent/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/kahinton/lucent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kahinton/lucent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kahinton/lucent/releases/tag/v0.1.0
