# Changelog

All notable changes to Lucent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

#### Chat, Handoffs, and Session Persistence
- Persisted LLM session tracking for chat/task runs, including message lineage, request links, tool call audit records, and session lifecycle state. **Migrations 075 and 079**.
- Proactive user Handoffs for daemon/workflow-to-human communication, with REST endpoints, web UI pages, MCP tools (`send_handoff`, `list_handoffs`, `get_handoff`, `resolve_handoff`), message threads, references, dedupe keys, attention counts, and response-required states. **Migration 084**.
- Chat page support for persisted conversation context, request creation/linking from chat sessions, and richer page-aware context for Activity and Handoff pages.

#### Workflows
- Workflow API and UI as the broader replacement for legacy schedules, with typed triggers (`schedule`, `manual`, `webhook`, `integration_event`), request templates, ordered actions, review instructions, webhook shared secrets, manual trigger support, and run history.
- Workflow action support for both task dispatch and `user_interaction` Handoff messages, including workflow output/clarification/decision flows that can require a user response. **Migrations 082 and 083**.
- Built-in server-function workflow action support for internal maintenance jobs that should record schedule-run results without dispatching agent tasks.

#### Agent Runtime Capabilities
- Agent hook definitions, approval workflow, agent grants, and runtime hook execution for model/tool call events. Hooks can inject context, block execution, or rewrite tool arguments/results. **Migration 076**.
- Managed Tool Builder for Lucent-hosted custom tools with proposal/approval flow, agent grants, sandboxed Python execution, JSON schemas, credential references, resource limits, and per-run audit records. **Migration 087**.
- Definition proposal evidence fields and org-shared ownership support for definition resources. **Migrations 077 and 080**.

#### Model and Runtime Settings
- Selectable model reasoning-effort metadata in the model registry, provider discovery, task/workflow creation, and LLM engine calls. **Migrations 073 and 074**.
- DB-backed Runtime Settings catalog and Settings UI for runtime-safe configuration, with environment/default fallback and locked/hidden visibility for bootstrap and credential settings. **Migration 086**.
- System-managed secret storage for values such as the signing secret, avoiding a hard requirement to persist sensitive runtime secrets in env vars for local compose. **Migration 085**.

#### Request and Activity Tracking
- Dedicated task outputs table and request output APIs for durable deliverables linked to tasks, requests, and external URLs. **Migration 078**.
- Request view helpers for activity/query performance. **Migration 081**.

### Changed
- Legacy schedules now read and write workflow-capable rows while preserving compatibility APIs and built-in schedule behavior.
- Built-in daemon maintenance cycles now prefer workflow/server-function execution and no-op skip records when there is no eligible work.
- MCP tool annotations classify read-only, create-only, and mutating tools for safer runtime/tool-call handling.
- Model validation now validates `reasoning_effort` against the selected model when provided.
- The Settings and Definitions UI now expose runtime settings, proposal evidence, hooks, custom tools, and external provider grants in the unified review/agent-composer flow.

### Fixed
- Fixed goal milestones not advancing when a request completed through the review/approval flow. Completing a goal-milestone request via `RequestRepository.update_request_status` correctly marked the milestone `completed` (which lets the planner move on to the next milestone), but the parallel review-completion path (`ReviewRepository.mark_request_completed`, used by the reviews API and the daemon's internal review loop) did a bare `UPDATE requests SET status='completed'` and skipped that side effect. As a result, on deployments where work flows through review, the first milestone's request would finish but its milestone stayed `active`, so the cognitive planner kept seeing the same milestone as the next target and never created a request for milestone 2. `mark_request_completed` now fires the same milestone-completion side effect at the single completion chokepoint, so every review-approval caller advances the goal. Regression test: `tests/test_db_requests.py::TestListPlanningTargets::test_milestone_advances_when_request_completed_via_review`.
- First-login/session setup flow now preserves the intended target path and handles authenticated redirects more consistently.
- Daemon dispatch and request-scoped planning now carry per-user/request context through task creation, LLM sessions, workflow runs, and handoff references.
- Secret resolution supports system-managed values and safer reference handling across providers.
- Shipped seed models are now disabled by default so the chat model picker no longer surfaces default offerings from providers that have not been configured. Completes the opt-in model policy (provider-discovered rows were already opt-in) — admins enable specific models explicitly. **Migration 088**.
- The daemon's restricted database role now reliably has `SELECT` on `runtime_settings`, so DB-managed runtime configuration loads instead of silently falling back to env/defaults. The original grant (migration 086) could be missed on instances where the role was created or restored after the migration was recorded as applied; this re-applies it idempotently. **Migration 089**.
- Fixed a clean-install failure where the daemon could not operate against a freshly provisioned database. The `lucent_daemon` role's grants had drifted: the daemon grew to read/write the whole data plane (schedules, tasks, requests, memories, sessions, the model registry, ...) but only minimal grants (users/orgs/api_keys) were ever codified — broader grants existed only on instances where they were applied by hand. On a clean volume the daemon hit `permission denied for schema public`, could not seed system schedules or load the model registry, and fell back to hardcoded engine/model defaults. Now: (1) the daemon no longer runs migrations (it passes `run_migrations=False` to `init_db` — migrations are the server's job), and (2) **migration 090** codifies the daemon role's working-set privileges (CRUD on all current and future tables, plus sequence usage) while deliberately withholding DDL, so the daemon can never alter the schema.
- Daemon organization binding is now explicit and single-tenant. Previously a single global `daemon-service` user was bound to the oldest organization; on a fresh instance that was the hidden `__lucent_system__` secret-storage org, so the daemon registered, heartbeated, and seeded its system schedules under an org the signed-in user could never see (no heartbeat or schedules in the UI). Now every real organization is provisioned with its own org-scoped `daemon-service` user at creation time, and a daemon binds to exactly one organization — resolved from `LUCENT_DAEMON_ORG` (org id or name) or, for the common local/compose case, auto-bound to the single real org. The daemon refuses to guess when multiple real orgs exist and skips the system org entirely. Daemon-service identity checks now recognize the per-org `daemon-service:{org_id}` scheme while remaining compatible with the legacy global user.
- Model selection no longer silently falls back to a hardcoded default model. When no model is enabled in the registry, `get_default_model_id`/`select_model_for_task` now raise `NoModelsAvailableError`, and the daemon fails loudly at startup instead of routing work to an arbitrary, possibly-unconfigured provider. Admins must enable at least one model (Settings → Models) before model-dependent work can run.
- The daemon's startup log now reports the engine that the resolved default model actually routes to (per-model routing) instead of the global default engine, which could differ and was misleading.
- Fixed daemon request decomposition (and all daemon MCP tool calls) failing from containers with `HTTP 421 Misdirected Request`, surfaced confusingly as `Attempted to exit cancel scope in a different task than it was entered in`. Recent MCP SDK versions auto-enable DNS-rebinding protection on the server and, by default, only allow loopback `Host` headers — so authenticated MCP requests arriving as `http://lucent:8766/mcp` (the in-network service name the daemon uses) were rejected, and the failed anyio transport teardown masked the real cause. The server now keeps DNS-rebinding protection enabled but builds its allowlist from the deployment's actual hostnames: loopback is always permitted, plus the host of `LUCENT_MCP_URL`/`LUCENT_PUBLIC_URL` and any hosts in the new `LUCENT_MCP_ALLOWED_HOSTS` setting (default `lucent` in local compose). Unknown hosts still receive 421.
- Fixed local (Ollama) models being unreachable from the docker-compose daemon workers. The `daemon-1`/`daemon-2` services were missing both `OLLAMA_HOST` and the `host.docker.internal` host-gateway mapping that the server already had, so the daemon's model discovery/sessions failed with `OLLAMA_HOST is not configured` (and would not resolve `host.docker.internal` on plain Docker Engine/Linux hosts). Both daemon services now mirror the server's Ollama networking.
- Fixed every Copilot-engine LLM session in the daemon failing with `Copilot engine requires the github-copilot-sdk package` even though the package was installed. The dependency was pinned `github-copilot-sdk>=0.3.0` with no upper bound, so fresh images pulled `1.0.2`, which removed the top-level `SubprocessConfig` export the copilot engine imports (`from copilot import CopilotClient, SubprocessConfig`); the import failed and `_ensure_sdk()` reported the SDK as missing. As a result cognitive/decomposition requests were created correctly (with the right per-user `created_by`) but could never decompose into tasks — they sat `pending` while the session logged "produced no output". Pinned to `github-copilot-sdk>=0.3.0,<1.0` until the engine is updated for the 1.x API.
- Fixed daemon-run Copilot sessions failing with `Session was not created with authentication info or custom provider` on container deployments, which left cognitive/decomposition requests stuck `pending` with zero tasks even after the SDK pin above. Three container-only gaps caused it: (1) the `daemon-1`/`daemon-2` compose services had no OpenBao/Vault configuration (`LUCENT_SECRET_PROVIDER`, `VAULT_ADDR`, `VAULT_TOKEN_FILE`, and the shared token volume), so the daemon fell back to the `builtin` secret provider and could not decrypt the org's transit-encrypted Copilot `github_token`; (2) the daemon never initialized the secret provider at startup (the API server did, but the daemon runs its own LLM sessions), leaving `SecretRegistry` empty; and (3) `run_session` did not pass the daemon's bound `organization_id` in the session `audit_context`, so the engine had no org to resolve the stored provider credential against. The host daemon was unaffected because its Copilot CLI is interactively logged in. All three are fixed: both daemon services now mirror the server's Vault config, the daemon initializes the secret provider on startup, and `run_session` injects the bound org id when a caller hasn't already supplied org scope.

### Documentation
- Added/updated API, architecture, configuration, security, troubleshooting, workflow, runtime settings, and managed tools documentation for the v5 surfaces.

## [0.4.0] - 2026-04-30

### Added

#### Two-Tier Connections Model
- **Settings → Connections** page split into two clearly labeled sections: **Workspace connections** (org-owned app/bot installs, admin-managed) and **Your connected accounts** (personal OAuth/PAT/env-token credentials, self-service).
- Six new feature flags for connection behavior, all with safe defaults that preserve the existing single-user/PAT workflow:
  - `LUCENT_CONNECTIONS_PAT_ENABLED` (default `true`)
  - `LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED` (default `true`)
  - `LUCENT_CONNECTIONS_OAUTH_ENABLED` (default `true`)
  - `LUCENT_WORKSPACE_INTEGRATIONS_ENABLED` (default `true`)
  - `LUCENT_GITHUB_APP_ENABLED` (default `false`)
  - `LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL` (default `false`)
- Centralized feature-flag layer (`src/lucent/integrations/connection_flags.py`) — sole reader of the `LUCENT_CONNECTIONS_*` / `LUCENT_GITHUB_APP_*` envs.
- CSRF protection added to all `/settings/connections/*` JSON mutation endpoints (PAT save, env-token claim, OAuth start, revoke).
- Workspace-integration mutations now require explicit `MANAGE_INTEGRATIONS` permission inside each handler (defense-in-depth on top of `AdminUser`).
- GitHub App groundwork: `integrations.type` widened to include `github_app` (plus `jira`, `linear`, `custom`); new `install_id`, `health_status`, `health_detail`, `health_checked_at` columns; partial unique index on `(org, type, install_id)` for active rows. **Migration 069**.
- New `RepoAccessDecision(allowed, reason, hint)` from `GitHubRepoAccessService.check_access_with_reason()`. Strict mode (`LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL=true`) returns `user_github_credential_required` with a "Connect your GitHub…" hint; compat mode keeps the existing single-user back-compat allowance.
- New `app_installation_can_see_repo()` API for app-level repo visibility, gated by `LUCENT_GITHUB_APP_ENABLED`. **Returns `None` ("unknown")** until App JWT signing / installation-token minting lands. **Never** substitutes for user ACL.
- Documentation: new [docs/connections.md](docs/connections.md) covering the model, every flag, three setup profiles (simple local, team, enterprise), role visibility, and the GitHub repo ACL rule.

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
- Root README repositioned Lucent as an enterprise AI agent / supervised digital teammate instead of a memory-server-first project
- Package metadata updated for the v0.4.0 release and new enterprise AI agent platform positioning
- Helm chart, Kubernetes examples, operator defaults, and release docs now use v0.4.0 image/version defaults
- The Connections page now shows two sections instead of one mixed list. Members see Workspace connections as read-only ("View only" badge) with no mutation controls
- Defaults are unchanged for existing single-user installs: PAT, env-token claim, OAuth, and workspace integrations are all on; GitHub App and strict repo ACL are off
- `_check_request_completion` now respects `LUCENT_REQUIRE_APPROVAL` — completed requests go straight to `completed` status by default instead of `review`
- `list_pending_tasks` query now filters out tasks from unapproved requests
- Learning extraction skill updated to search for `approval-rejected` tagged memories
- Rejected request lifecycle now uses an explicit `rejection_processing` phase before cancellation (migration 053)
- `LUCENT_REQUIRE_APPROVAL` documented in `docs/configuration.md`
- `GITHUB_TOKEN` guidance expanded in `.env.example` and `docs/getting-started.md` (Copilot scope/daemon requirements)
- Documentation refresh across `docs/configuration.md` and `docs/deployment-guide.md` for launch-readiness
- Python dependency constraints refreshed for current security and compatibility releases, including `python-multipart`, `cryptography`, LangChain packages, `langsmith`, and pytest

### Fixed
- Docker PostgreSQL initialization now sets the `lucent_daemon` role password from `DAEMON_DB_PASSWORD` instead of a hardcoded development password
- `.env.example` session TTL comment corrected to default `24` hours (`LUCENT_SESSION_TTL_HOURS`)
- `.env.example` daemon MCP API key placeholder updated from `mcp_...` to `hs_...`
- `docs/deployment-guide.md` corrected `LUCENT_SECURE_COOKIES` default to `true` (with local HTTP override guidance)
- Tool-calling robustness improved in MCP bridges/LLM integration paths
- Skills approval acceptance handling corrected in definitions web routes

### Removed
- Dead `LUCENT_DAEMON_RESEARCH_MODEL` entry removed from `.env.example`
- Local-only `check_session.py` debug helper removed from the release tree
- Stale `.bak` route/template files removed from tracked source

### Security
- Connection mutation endpoints now require CSRF validation, and workspace-integration mutations require explicit `MANAGE_INTEGRATIONS` checks inside each handler
- Production Compose continues to require explicit credentials and now passes `DAEMON_DB_PASSWORD` into the PostgreSQL init path for first-run daemon role setup
- Protected built-in agent/skill/MCP definitions from daemon-driven modification paths

### Deferred
- GitHub App webhook execution: the schema and flag are in place, but App JWT signing, installation-token minting, and the `/integrations/webhook/github` HMAC-verified receiver are not yet implemented. `app_installation_can_see_repo()` returns `None` until these land. Do not enable `LUCENT_GITHUB_APP_ENABLED=true` in production with the expectation of working webhook-driven behavior yet.

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

[Unreleased]: https://github.com/kahinton/lucent/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/kahinton/lucent/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/kahinton/lucent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kahinton/lucent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kahinton/lucent/releases/tag/v0.1.0
