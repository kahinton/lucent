# Configuration

Lucent uses **Settings → Runtime Settings** as the canonical settings catalog. Runtime-safe values can be saved in the database by an admin or owner, while bootstrap and credential values are shown for visibility but remain locked/masked when they must be supplied before the database is available or through secret storage.

For DB-backed runtime settings, precedence is:

1. Database value saved for the workspace
2. Environment variable fallback
3. Built-in default

Resetting a runtime setting in the UI deletes the database value and returns it to environment/default fallback. Secrets are intentionally not stored in `runtime_settings`; use **Settings → Secrets**, **Settings → Connections**, or the configured secret provider for credentials. Copy `.env.example` to `.env` and customize bootstrap values as needed.

## Runtime Settings UI

Admins and owners can manage allowlisted settings in **Settings → Runtime Settings**. Editable saved values are stored in PostgreSQL, included in normal database backups, and loaded by the API/daemon at startup. The catalog includes model defaults, chat behavior, daemon timing, request/review gates, memory lifecycle/search rollout flags, server rate limits, and read-only visibility for bootstrap/credential settings.

Controls are generated from the setting catalog rather than treated as arbitrary strings. Boolean settings use explicit on/off switches, bounded numbers use numeric inputs, fixed choices use selectors, and URL settings use URL inputs. Model settings list the enabled models visible to the workspace, display their names and providers, and save the stable model ID. A configured model that is no longer available is shown as unavailable so it can be repaired or reset; it cannot be selected as a new value.

Validation is enforced in layers: the web route checks source-backed choices such as model IDs, the repository verifies each key and primitive type against the code-owned catalog, and PostgreSQL verifies that the JSON value shape matches its declared type. Environment variables remain supported as fallbacks and are coerced through the same primitive type definitions.

The UI shows the active source for each value:

- **Saved in DB** — workspace value takes precedence and is backed up with the database.
- **From env** — no database value exists, so the matching environment variable is used.
- **Default** — neither a database value nor environment variable is set.
- **Locked** — visible for audit/diagnostics, but managed outside the DB-backed runtime settings table.
- **Hidden** — secret material is masked and not persisted in `runtime_settings`.
- **Restart may be required** — the setting is loaded into process-level state; restart affected services after changing it.

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
| `LUCENT_FILE_STORAGE_PATH` | `./data/files` | Root directory for the built-in local user-file provider. Compose and Helm mount durable storage at `/var/lib/lucent/files`. |
| `LUCENT_MAX_USER_FILE_BYTES` | `10485760` | Maximum size of one stored user file in bytes (10 MiB by default). |

User-file identity, ownership, provenance, and provider keys are stored in
PostgreSQL. File content uses the local provider by default. The provider
boundary is independent of request outputs and handoff references so external
backends such as Azure Blob Storage or Google Drive can be added without
changing user-facing file IDs or authorization rules.

## Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_AUTH_PROVIDER` | `basic` | Auth backend (`basic` or `api_key`) |
| `LUCENT_SESSION_TTL_HOURS` | `24` | Web session cookie lifetime in hours |
| `LUCENT_SECURE_COOKIES` | `true` | Cookie `Secure` flag. Set to `false` for local HTTP development without HTTPS |
| `LUCENT_SIGNING_SECRET` | *(secret storage)* | Optional override for the cookie/impersonation HMAC key. By default Lucent creates and reloads a system-managed secret via the configured secret provider (OpenBao Transit in local compose). |

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
| `LUCENT_SECRET_PROVIDER` | `auto` | Secret storage backend (`auto`, `builtin`, `transit`, `vault`, `aws`, or `azure`) |
| `LUCENT_SECRET_KEY` | *(required for builtin)* | Fernet encryption key for the builtin provider |
| `LUCENT_CREDENTIAL_KEY` | *(none)* | Fernet key for encrypting integration credentials (Slack/Discord tokens). Separate from `LUCENT_SECRET_KEY` |
| `VAULT_ADDR` | `http://openbao:8200` | OpenBao/Vault server address |
| `VAULT_TOKEN_FILE` | `/shared/vault-token` in Docker | Preferred local OpenBao token source; avoids putting the token in process env |
| `VAULT_TOKEN` | *(none)* | OpenBao/Vault authentication token override when a token file is not available |

For detailed secret storage configuration, see [Secret Storage](secret-storage.md).

## LLM Engine

Lucent engines come in two kinds:

- **`llm_access`** — `langchain`, the general LLM layer for plain
  text/reasoning models (Ollama, OpenAI, Anthropic, Google, and any
  OpenAI-compatible endpoint). Lucent owns the tool loop.
- **`coding_agent`** — product-specific agent harnesses that bring their own
  agent loop, tools, and auth: `copilot` (GitHub Copilot SDK) today, with
  `claude_code` and `codex` declared in the registry for future releases.

Engine identity, capabilities, and config schemas live in one data-driven
registry (`lucent.llm.engine_registry`); every other surface derives from it.

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_LLM_ENGINE` | `copilot` | Default engine when a model or task does not override it (`copilot` or `langchain`) |
| `LUCENT_ENGINE_AUTO_ROUTE` | `false` | When enabled, code/agentic work prefers a `coding_agent` engine and plain chat uses the general `llm_access` engine. Off keeps the configured default engine for every model. |
| `LUCENT_CHAT_MODEL` | *(model registry default)* | Optional override for chat sessions |
| `LUCENT_MODEL_VALIDATION` | `strict` | Model validation mode: `strict` rejects unknown models, `lenient` allows them |
| `LUCENT_CHAT_MCP_URL` | `http://localhost:8766/mcp` | MCP URL for chat-initiated tool calls |
| `LUCENT_CHAT_TIMEOUT` | `300` | Chat session timeout in seconds |
| `LUCENT_SESSION_EXPERIENCE_SUMMARY_ENABLED` | `true` | Enable an extra skill-guided model call to write narrative experience memories for meaningful chat sessions |
| `LUCENT_SESSION_EXPERIENCE_MODEL` | *(model registry default)* | Optional model override for session experience summaries |
| `LUCENT_SESSION_EXPERIENCE_TIMEOUT` | `180` | Timeout in seconds for the session experience summary model call |
| `ANTHROPIC_API_KEY` | — | Anthropic API key (only when `LUCENT_LLM_ENGINE=langchain`) |
| `OPENAI_API_KEY` | — | OpenAI API key (only when `LUCENT_LLM_ENGINE=langchain`) |
| `GOOGLE_API_KEY` | — | Google API key (only when `LUCENT_LLM_ENGINE=langchain`) |
| `OLLAMA_HOST` | `http://host.docker.internal:11434` | Ollama host URL for local models |
| `GITHUB_TOKEN` | — | GitHub token for Copilot SDK authentication |

### Engine Backends

A **backend** binds one engine to one provider family with its own config and
credentials (**Settings → Models → Engine Backends**, or
`/api/admin/backends`). Models reference a backend; tasks/workflows can pin one;
otherwise a model resolves through its pinned `(engine, provider)` match or the
configured default engine. Backends are additive — a model with no backend
resolves exactly as before.

Non-secret config (base URL, wire protocol, local provider, gateway URL) is
stored on the backend; secret fields are stored in the secret provider and only
their key names appear on the backend. Each field has an environment-variable
fallback (for example the Copilot BYOK fields `COPILOT_PROVIDER_BASE_URL`,
`COPILOT_PROVIDER_TYPE`, `COPILOT_PROVIDER_API_KEY`, `COPILOT_MODEL`).

### Coding Engines

Three product harnesses run alongside the general LangChain engine. Each is
backed by an official Python SDK that bundles (Claude Code) or auto-installs
(Codex) its own CLI, so no Node/npm or separate CLI install is required. These
ship in the base install — the engines are available out of the box:

| Engine | SDK | Notes |
|--------|-----|-------|
| `copilot` | `github-copilot-sdk` | GitHub Copilot harness |
| `claude_code` | `claude-agent-sdk` | Bundles the Claude Code CLI |
| `codex` | `openai-codex` | Pulls the Codex CLI runtime |

Each coding engine can run either **its own provider-native models** (Anthropic
for Claude Code, OpenAI/ChatGPT for Codex, GitHub-hosted for Copilot) or models
from a **local provider**. Local providers are described non-specifically — a
provider declares which API surfaces it exposes, and each engine consumes the
surface it supports:

| Local provider | Surfaces | Claude Code | Copilot | Codex |
|---|---|---|---|---|
| `ollama` | `openai`, `anthropic`, `codex-oss` | ✅ `ANTHROPIC_BASE_URL` | ✅ BYOK | ✅ `model_provider` |
| `lmstudio` | `openai`, `codex-oss` | — | ✅ BYOK | ✅ `model_provider` |
| `openai-compatible` | `openai`, `anthropic` | ✅ | ✅ | — |

To use a local provider with a coding engine, create an **Engine Backend** with
that `provider` (e.g. `engine=codex, provider=ollama`) and pin it to a model or
task. The base URL resolves config → env (`OLLAMA_HOST`) → provider default. Adding
a new local provider is one spec in `src/lucent/llm/local_providers.py` — no engine
changes. Anthropic-native and OpenAI-native models work the same way with a
`provider=anthropic` / `provider=openai` backend.

### Model Reasoning Effort

Admins can configure selectable reasoning/thinking levels per model in
**Settings → Models**. Provider sync stores provider-reported levels as
`reasoning_efforts` on the model registry record. Lucent does not maintain a
global static enum of possible levels; if a provider catalog does not report
exact selectable values for a model, Lucent leaves the list blank and uses the
provider default.

Runtime mappings:

| Backend | Parameter sent |
|---------|----------------|
| GitHub Copilot SDK | `reasoning_effort` |
| LangChain OpenAI | `reasoning_effort` |
| LangChain Anthropic | `effort` |
| LangChain Google GenAI | `thinking_level` |

Leave `reasoning_effort` unset to use the provider default. Choose higher
levels only for complex analysis where extra latency/cost is justified.

### Model Classification (decision models)

Every model carries an explicit classification, `model_class`, kept separate
from the task-flavored `category`:

| `model_class` | Meaning |
|---------------|---------|
| `language` | Standard chat LLM. Default for every model. |
| `decision` | System One decision model (Jev-style `choice`/`noul`/`score`, served locally by Ollama 0.35+ via `POST {OLLAMA_HOST}/v1/systemone`). |

Decision models never participate in chat-model selection, daemon task
dispatch, or the LangChain/Copilot chat engines — `select_model_for_task`
excludes them and chat validation rejects them with a clear message. Admins
set (or override) the classification in **Settings → Models**; provider sync
detects local decision models via a `decision`/`systemone` capability marker
or the known model families (`nimble`, `tev1`, `clef`) and classifies them
automatically.

Lucent exposes decision models through `lucent.llm.decision`:

```python
from lucent.llm import DecisionQuestion, DecisionRequest, decide

response = await decide(
    DecisionRequest(
        model_id="nimble",
        state={"ticket": "I was charged twice. Please refund the extra payment."},
        questions={
            "team": DecisionQuestion(
                name="team", type="choice",
                instructions="Which team should handle this ticket?",
                criteria={"billing": "Payments and refunds",
                          "technical": "Bugs and integrations",
                          "other": "None of the above"},
            ),
            "urgency": DecisionQuestion(
                name="urgency", type="score",
                instructions="How urgent is this ticket?",
                criteria=["Routine", "Soon", "Urgent"],
            ),
        },
    )
)
response.answers["team"].choice         # "billing"
response.answers["team"].probabilities  # {"billing": 0.985, ...}
```

`decide()` sends one HTTP request to `/v1/systemone` and answers every
question at once; answers are typed (`choice` → option plus per-option
probabilities, `noul` → true-probability, `score` → weighted rubric position
with its legend). Request limits mirror the server (1–64 questions, 2–255
choice options, 2–26 score levels, 64 KiB body) and are validated client-side
first. For local testing, start Ollama 0.35+, `ollama pull nimble`, and keep
`OLLAMA_HOST` pointed at it — provider discovery picks the decision model up
on the next sync.

### Proactive Memory Injection

The message hook can look up relevant accessible memories before the first
model call of a user turn. Its defaults favor a bounded search plan: it starts
with the latest meaningful sentence, then uses quoted/code/path/name anchors
and samples the middle and opening of a long message. The shared memory search
API is unchanged; these settings only control the hook's retrieval plan and
final injection filter.

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_INJECT_MIN_SIMILARITY` | `0.30` | Absolute minimum search similarity score for injection. |
| `LUCENT_INJECT_MAX_SCORE_DROP` | `0.15` | Maximum score gap from a query window's best hit. Lower values favor precision; higher values favor recall. |
| `LUCENT_INJECT_MAX_TERMS` | `64` | Maximum meaningful terms retained from each selected message span. |
| `LUCENT_INJECT_MAX_QUERIES` | `5` | Maximum bounded searches for one message. Each extra search adds memory-server work. |
| `LUCENT_INJECT_FOLLOWUP_QUERY_TERMS` | `4` | Terms in each follow-up window after the initial eight-term lookup. |
| `LUCENT_INJECT_MAX_MEMORIES` | `3` | Maximum memories inserted into the model context per user message. |
| `LUCENT_INJECT_DECISION_GATE_ENABLED` | `false` | Env fallback for **Settings → Runtime Settings → Message injection decision gate**. Screens surviving injected memories with a decision-class model and keeps only those judged relevant to the conversation. |
| `LUCENT_INJECT_DECISION_GATE_MODEL` | *(first enabled decision model)* | Decision-class model for the injection screen. Leave blank to use the first enabled decision-class model (ordered by provider, name). |
| `LUCENT_INJECT_DECISION_GATE_THRESHOLD` | `0.7` | Minimum relevance probability for an injected memory to survive the decision-model screen. |

When the decision gate is enabled, the injection hook makes ONE extra
`/v1/systemone` call per user message after the similarity filters and
before the injection cap: the state carries the current message, the last
few turns of conversation, the chat project name, and one field per
surviving candidate memory (its ~1000-char search preview); the decision
model answers one `noul` relevance question per candidate, and only
memories scoring at or above the threshold are injected. The injection cap
still applies to the survivors. The memory chip in chat shows when the gate
ran (which model, how many candidates it gated out) and each injected
memory's relevance score, both live and on session replay.

The gate is fail-open — any gate error (model unavailable, endpoint
failure, timeout) leaves the pre-gate candidate set untouched, and a
rejected memory is never marked injected, so it may still be picked on a
later turn. This adds at most ~15s of bounded pre-model latency (warm
inference is tens of milliseconds; the budget covers one cold model load)
for a locally hosted decision model; the independent `LUCENT_DECISION_GATE_*`
settings above control the request pre-review gate, not this one.

## Daemon Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_MAX_SESSIONS` | `3` | Max concurrent sub-agent sessions |
| `LUCENT_DAEMON_INTERVAL` | `15` | Minutes between cognitive cycles |
| `LUCENT_DAEMON_MODEL` | *(model registry default)* | Optional override for daemon sessions |
| `LUCENT_DAEMON_ROLES` | `all` | Enable specific loops: `cognitive`, `dispatcher`, `scheduler`, `autonomic` (comma-separated, or `all`) |
| `LUCENT_DAEMON_SOURCE_RELOAD` | `false` | Restart the daemon after watched source changes; enable only for local development, then restart the daemon |
| `LUCENT_MCP_URL` | `http://localhost:8766/mcp` | MCP server URL for memory access |
| `LUCENT_MCP_API_KEY` | — | API key for MCP authentication |
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
| `LUCENT_LANGCHAIN_MAX_TOOL_ROUNDS` | `0` | Maximum consecutive LangChain tool-call rounds per session. `0` is unlimited; a positive value from `1–1000` interrupts and requeues the task on exhaustion rather than marking it complete. |
| `LUCENT_WATCHDOG_TIMEOUT` | `900` | Watchdog timeout for stuck sessions (15 minutes) |
| `LUCENT_MAX_RESULT_LENGTH` | `8000` | Maximum characters stored from sub-agent results |

### Daemon Review & Approval

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_AUTO_APPROVE` | `false` | Env fallback for **Settings → Runtime Settings → Auto-approve daemon-created requests**. When `false`, daemon/cognitive requests require human approval via the review queue before work begins. User/API/scheduled requests are always auto-approved. |
| `LUCENT_SKIP_POST_REVIEW` | `false` | Env fallback for **Settings → Runtime Settings → Skip automatic post-completion review**. When `true`, bypasses the daemon's automatic post-completion quality review. By default, finished work goes through an internal review task that auto-approves or sends back for rework. |
| `LUCENT_REQUIRE_APPROVAL` | `false` | When `true`, completed requests transition to `review` status for human sign-off instead of going directly to `completed`. |
| `LUCENT_REQUEST_REVIEW_MODEL` | *(daemon default)* | Model for request-level post-completion review |
| `LUCENT_REQUEST_REVIEW_AGENT_TYPE` | `request-review` | Agent type for post-completion review |
| `LUCENT_REQUEST_REVIEW_FALLBACK_AGENT_TYPE` | `code` | Fallback agent type if review agent not found |
| `LUCENT_DECISION_GATE_ENABLED` | `false` | Env fallback for **Settings → Runtime Settings → Decision-model pre-review gate**. Runs a cheap decision-model screening of the completed work before spawning the LLM review task. |
| `LUCENT_DECISION_GATE_MODEL` | *(first enabled decision model)* | Decision-class model for the pre-screen. Leave blank to use the first enabled decision-class model (ordered by provider, name). |
| `LUCENT_DECISION_GATE_THRESHOLD` | `0.7` | Minimum probability per pre-screen question to count as passing. |
| `LUCENT_DECISION_GATE_STATE_CHAR_BUDGET` | `16000` | Character budget for the state sent to the decision endpoint (roughly 4 chars/token; the state is re-sent per question). |
| `LUCENT_DECISION_GATE_TASK_RESULT_CHAR_BUDGET` | `2000` | Per-task result excerpt budget inside the pre-screen state. |

#### Decision-model pre-review gate

When **Settings → Runtime Settings → Decision-model pre-review gate** is enabled, the request review lifecycle asks a decision model (Ollama 0.35+ System One: `model_class="decision"`, e.g. `nimble`) one cheap `noul` question per concern in a single `/v1/systemone` call:

- `request_fulfilled` / `not_blocked` — did the completed outputs accomplish the request, free of unresolved blockers?
- `contracts_satisfied` — when any task carries an `output_contract`: does every structured payload satisfy the contract *in intent and meaning*, not merely schema-valid it? One additional question per contracted task verifies each payload individually.
- `deliverables_durable` — when a `target_repo` or recorded outputs exist: are the deliverables durably captured?

If every asked question scores at or above the threshold, the request is **auto-completed without an LLM review session**. If any question scores below it, the ordinary (deeper, LLM-based) review task is created with a `DECISION PRE-SCREEN FINDINGS` block listing the failed items — the reviewer verifies them and directs rework explicitly.

The gate is fail-open: disabled flag, no available decision model, or an endpoint error falls back to the standard review. Requests with failed/`needs_review` tasks, linked memories requiring review-agent updates, or a `target_repo` never auto-approve — the pre-screen findings still surface, but the full review runs.

#### Decision steps as tasks

Decision-class models can also run as ordinary dispatchable tasks (`agent_type="decision-step"`) instead of a chat session, both created directly on a request and as workflow actions. Availability is not feature-flagged: a step is addable when at least one decision-class model is accessible to the requesting principal, and the daemon re-checks access at dispatch.

The step is configured by a `decision_config` JSON document (validated at every creation surface — REST `POST /api/requests/{id}/tasks`, MCP `create_task`, workflow actions, and the workflow wizard):

```json
{
  "questions": [
    {"name": "report_complete", "type": "noul", "instructions": "Did the prior step finish the audit?"}
  ],
  "state": {"extra_hint": "focus on the security section"},
  "model": "nimble",
  "threshold_policy": {"questions": {"report_complete": {"min": 0.6}}, "on_fail": "fail_task"}
}
```

- `questions` — 1–64 ordered questions; `noul` (probability), `score` (2–26 legend levels), or `choice` (2–255 options, classification — never thresholded). Names ≤128 chars, unique.
- `state` — optional literal string fields merged last, over the generated state.
- `threshold_policy` — optional per-question minimums (`{"min": 0.6}` or a bare float) plus `on_fail`: `"fail_task"` (default) fails the task on a miss and blocks downstream stages, or `"needs_review"` routes the step to review. Without a policy, answers are always recorded as output regardless of score.
- `output_contract` and `decision_config` are mutually exclusive (schema-shaped output vs. decision answers).

In the workflow wizard and the workflow edit page, every step card carries a visible two-way kind toggle — **Agent task** vs. **Decision check** — that reshapes the card: Decision check reveals a structured Decision editor (per-question rows: name, type with an explanation of what the type means, instructions, per-type options, optional minimum), a failure-action select for when a minimum is missed, and a collapsed Advanced section holding extra state fields and the raw `decision_config` JSON (which still overrides the fields above when non-empty), while the instructions, agent select, sandbox, and structured-output controls hide and the step's model list swaps to decision-class models — or "Daemon decision default (resolves at dispatch)". Agent task keeps the ordinary agent form and full chat-model list; the underlying form fields are unchanged either way (the toggle only drives the step's agent type), saving a decision step with the wrong kind still fails with a step-named validation message, and toggling back does not discard typed questions.

At dispatch the daemon claims the task, builds state from the request, the task itself, and completed sibling tasks' results (a decision step can score an upstream step's output against it), then answers all questions in a single `/v1/systemone` call. The answers land as the task's human-readable result plus a structured payload (`result_structured`: answers, scores, choices, per-question thresholds) that downstream task context and the reviewer can read. Decision-model token usage is recorded under `engine="decision"`. A decision step never inherits a chat model default.

#### Approval Flow

Requests go through a two-stage gate:

1. **Pre-work approval** (`LUCENT_AUTO_APPROVE`): Controls whether daemon-initiated requests need human approval before any tasks are dispatched. When disabled (`false`), these requests appear in the Review Queue with an "Approve — Start Work" button. Rejected requests are cancelled and generate a learning memory.

2. **Post-completion review** (`LUCENT_REQUIRE_APPROVAL`): Controls whether finished requests need human sign-off. When enabled (`true`), requests that complete successfully go to `review` status and appear in the Review Queue's "Completed Work Review" section.

### Daemon Fusion Dispatch

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_FUSION_BEST_OF_N_ENABLED` | `false` | Env fallback for **Settings → Runtime Settings → Fusion best-of-N**. Lets tasks configured with fusion mode `best_of_n` run against several candidate chat models scored by a decision-model arbiter. |
| `LUCENT_FUSION_SYNTHESIS_ENABLED` | `false` | Env fallback for **Settings → Runtime Settings → Fusion synthesis**. Lets tasks configured with fusion mode `synthesize` merge the candidate responses into one answer in an extra chat session. |
| `LUCENT_FUSION_REFINE_LOOP_ENABLED` | `false` | Env fallback for **Settings → Runtime Settings → Fusion refine loop**. Allows best-of-N dispatches one bounded refine pass (re-run with critique, re-score; kept only if strictly better). |
| `LUCENT_FUSION_MAX_CANDIDATES` | `4` | Maximum candidate models allowed in a `fusion_config` (per-config values clamp to this, 2–8). |
| `LUCENT_FUSION_ARBITER_MODEL` | *(blank / first enabled decision model)* | Decision-class model used as the arbiter when the task's `fusion_config` does not name one. Leave blank to use the first enabled decision-class model (ordered by provider, name). |
| `LUCENT_FUSION_ARBITER_TIMEOUT_SECONDS` | `60` | Timeout for each arbiter `/v1/systemone` call. One retry is attempted on error before scoring degrades. |

#### Fusion dispatch (best-of-N, synthesis, refine)

Fusion runs **inside a single task's dispatch cycle**: instead of one chat session, the daemon fans out N candidate chat sessions (named `…-cand-1`, `…-cand-2`, …), asks a decision-class model (Ollama 0.35+ System One, e.g. `nimble`) to score every candidate against the task's authored questions, and ships the winner through the ordinary completion path. It is entirely opt-in per task — a task carries a `fusion_config` JSON document (authored via REST `POST /api/requests/{id}/tasks`, MCP `create_task`, or workflow actions, each rejecting configs while the mode's flag is off) and the daemon checks the flag again at dispatch:

- **Flag off**: the task dispatches as a normal single-model task with a `fusion_disabled` event — flipping a flag mid-flight never strands a queued task.
- **Unsupported shape** (sandboxed task, `decision-step` agent, agent-required tools present): `fusion_unsupported` event → normal single-model path.
- **Enabled + supported**: each candidate runs sequentially in its own session; `fusion_candidate_done` fires per candidate and a final `fusion_completed` event carries the full comparison metadata.

The `fusion_config` shape (validated at every authoring surface; `arbiter.questions` uses the same question schema as decision steps — see [*Decision steps as tasks*](#decision-steps-as-tasks)):

```json
{
  "mode": "best_of_n",
  "candidates": {"models": ["m1", "m2"]} | {"count": 3},
  "arbiter": {
    "model": "nimble",
    "questions": [{"name": "fulfilled", "type": "noul", "instructions": "Is the request fulfilled?"}],
    "threshold_policy": {"questions": {"fulfilled": {"min": 0.7}}, "on_fail": "needs_review"}
  },
  "refine": {"model": "m1", "max_refine_passes": 1}
}
```

- `mode` — `best_of_n` (pick the top-scoring candidate) or `synthesize` (merge the candidates in one extra chat session, then re-score the merged answer); `candidates.models` (2+ unique chat-class models) and `candidates.count` (filled from the task's model first, then accessible models round-robin) are mutually exclusive.
- `arbiter` — required questions (1–64, `noul`/`score`/`choice`), optional explicit `model` (falls back to `LUCENT_FUSION_ARBITER_MODEL`, then the first enabled decision model), optional `threshold_policy` on the winner's aggregate.
- `refine` — best-of-N only: the best candidate text plus the failing arbiter questions feed a bounded critique-and-rescore loop (1–2 passes); the refined result ships only when it scores **strictly** better, and a refine failure never fails the task (`fusion.refine_loop_enabled` gates it).
- `synthesis` (with `mode: "synthesize"`) — names the chat model that merges the candidates, each excerpted to 3k characters, steered by the per-candidate score table.

Scoring: the arbiter is asked the authored questions **once per candidate** (prefixed `cand0_fulfilled`, `cand1_fulfilled`, …) in one `/v1/systemone` call per batch, so aggregates are directly comparable; ties and threshold failures break deterministically (highest normalized aggregate, then fewer failing thresholds, then earlier candidate). Candidates run sequentially inside one dispatch, so an enabled fusion task spends several full chat sessions' worth of tokens up front — the `fusion_completed` event's `totals` object (candidate/arbiter/synthesis/refine input- and output-token sums) plus `engine="decision"` arbiter ledger rows exist so admins can compare against a single-model baseline before keeping the flags on. Fusion tasks cannot use sandboxes, required tools, or `decision_config`; their `output_contract` still applies to the shipped result. Full candidate texts are not persisted — only the winner's result plus per-candidate excerpts in the event metadata.

### Auto Model Selection

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_MODEL_AUTO_SELECTION_ENABLED` | `false` | Env fallback for **Settings → Runtime Settings → Auto model selection**. Enables the `model="auto"` sentinel and, while on, routes model-unset daemon tasks (and model-unset chat messages) through the selection arbiter. |
| `LUCENT_MODEL_AUTO_SELECTION_MODEL` | *(blank / first enabled decision model)* | Decision-class model used as the selection arbiter. Leave blank to use the first enabled decision-class model via the ordinary decision-model resolution. |
| `LUCENT_MODEL_AUTO_SELECTION_CONFIDENCE` | `0.6` (`0.05`–`0.99`) | Confidence floor for adopting the arbiter's pick; lower results fall back to the caller's default chain. |
| `LUCENT_MODEL_AUTO_TIMEOUT_SECONDS` | `60` | Timeout for each arbiter `/v1/systemone` call. One retry is attempted on error before resolution degrades. |

#### How auto selection works

Every surface keeps its pre-auto default chain; auto selection **only** runs while the flag is on, and any resolution failure behaves exactly like the flag being off (fail-open):

- **Triggers** — daemon tasks: the reserved sentinel `model="auto"` (authorable via REST, MCP, the web edit form, workflow actions, and schedule overrides; refused with `422 Auto model selection is disabled` while the flag is off) or, while the flag is on, **no model set at all**. Chat: `model="auto"` from the picker (or an unset model while the flag is on). Decision-step tasks never take the sentinel.
- **Rule filters run first** — candidates must be enabled (and decision-class models are never chat candidates); an image-bearing chat message requires `supports_vision`; and when a model declares a context window (`> 0`), it must fit the estimated prompt size plus 8k headroom. Models with an unknown window (0) are never filtered on suspicion. Candidates are capped at 32 options, keeping the default chain's pick in the pool.
- **One `choice` question** — a decision-class model answers `best_model` over the surviving model ids, given task title/description/agent type (or the chat message excerpt) and each model's metadata line (name, category, tags, context window, notes). Models tagged with a parameter count (`763b`, `31b`, …) surface it in both the metadata line and the choice criteria (`… (fast, 763B params)`), and the instructions ask for size-matching — simple, short requests go to small models, not heavyweights.
- **Answer rule** — unknown option or probability below the confidence floor falls back; a tie (top-two within 0.10 probability) goes to the default chain's pick when it is the runner-up; a single surviving model is chosen directly without consulting the arbiter.
- **Observability** — daemon dispatch emits `model_auto_selected` (chosen source, probabilities, survivors, dropped reasons, arbiter usage) or `model_auto_disabled` (sentinel encountered with the flag off); the arbiter's own token spend lands in the token-usage ledger with `engine="decision"`, and chat sessions stash an `auto_selection` summary in session metadata; in chat the streaming `session` event also carries a live `model_selected` payload, and the rendered turn shows a quiet note (picked model + confidence, or the fallback reason when the arbiter degraded), persisted on the assistant message so it survives replays.
- **One-shot on tasks** — dispatch persists the resolved concrete model back onto the task (`tasks.model`), so retries never re-consult the arbiter. Chat consults per message but persists the resolved model on the session row.

Background daemon passes (memory maintenance, rejection processing, decomposition backfill) deliberately skip auto selection — internal hygiene stays on the cheap default chain.

### Daemon Git Operations


| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_ALLOW_GIT_COMMIT` | `false` | Allow the daemon to create git commits |
| `LUCENT_ALLOW_GIT_PUSH` | `false` | Allow the daemon to push to remote (requires `LUCENT_ALLOW_GIT_COMMIT=true`) |

## Connections & Feature Flags

Controls the **Settings → Connections** page and per-feature gating of personal access tokens, env-token claim, OAuth, workspace integrations, and the GitHub App. Defaults are tuned for friendly local/open-source use.

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_CONNECTIONS_PAT_ENABLED` | `true` | Show the PAT entry form and accept `POST /settings/connections/pat`. When `false`, UI hides the form and the backend rejects with `403 feature_disabled`. |
| `LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED` | `true` | Show the env-token detection card and accept `POST /settings/connections/env/claim`. Also gates the `GITHUB_TOKEN` fallback used in repo-existence checks (never user ACL). |
| `LUCENT_CONNECTIONS_OAUTH_ENABLED` | `true` | Show OAuth "Connect with …" buttons. Per-provider visibility additionally requires `LUCENT_OAUTH_<PROVIDER>_CLIENT_ID` to be set. |
| `LUCENT_WORKSPACE_INTEGRATIONS_ENABLED` | `true` | Show the **Workspace connections** section and load workspace rows. When `false`, the section is hidden and the read model short-circuits. |
| `LUCENT_GITHUB_APP_ENABLED` | `false` | Enable GitHub App install rows and `app_installation_can_see_repo()` queries. Does **not** affect user ACL. |
| `LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL` | `false` | Strict mode: deny repo access for users without a personal GitHub credential. When `false`, no-credential users are allowed for back-compat. |

Per-provider OAuth client configuration (read by `LUCENT_CONNECTIONS_OAUTH_ENABLED`):

| Variable | Description |
|----------|-------------|
| `LUCENT_OAUTH_GITHUB_CLIENT_ID` / `_CLIENT_SECRET` | GitHub OAuth app credentials |
| `LUCENT_OAUTH_SLACK_CLIENT_ID` / `_CLIENT_SECRET` | Slack OAuth app credentials |
| `LUCENT_OAUTH_JIRA_CLIENT_ID` / `_CLIENT_SECRET` | Jira OAuth app credentials |

For the full two-tier model, recommended setup profiles (simple / team / enterprise), and the GitHub repo ACL rule, see [Connections](connections.md).

## Sandbox Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LUCENT_SANDBOX_BACKEND` | `docker` | Sandbox backend: `docker` or `kubernetes` |
| `LUCENT_SANDBOX_BRIDGE_API_URL` | `http://host.docker.internal:8766/api` | API URL accessible from inside sandboxes |
| `DOCKER_HOST` | *(system default)* | Docker daemon socket URL for sandbox creation |
| `LUCENT_SANDBOX_K8S_NAMESPACE` | `lucent-sandboxes` | Namespace for Kubernetes sandbox pods and NetworkPolicies |
| `LUCENT_SANDBOX_KUBECONFIG` | *(in-cluster or default config)* | Optional kubeconfig path used outside a cluster |

Templates share image, repository, setup, environment, working directory,
resource, network, and lifecycle settings across both backends. Docker bind
mounts are local-runtime settings and only appear in the UI when the Docker
backend is active. Kubernetes uses an `emptyDir` workspace bounded by the
template disk limit and requires a CNI plugin that enforces NetworkPolicy for
isolated or allowlisted egress.

## Docker Compose

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_PASSWORD` | `change-me-insecure-dev-password` | PostgreSQL password for local Docker Compose only |
| `LUCENT_DB_PORT` | `5433` | Host port for the PostgreSQL container |
| `DAEMON_DB_PASSWORD` | `change-me-insecure-daemon-password` | Password for the restricted `lucent_daemon` database role in local Docker Compose |

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

Plain `docker compose up` starts `daemon-1` by default. Enable the multi-daemon profile to add `daemon-2`:

```bash
docker compose --profile multi-daemon up
```

Uses the same daemon environment variables listed above. See `docker-compose.yml` for the `daemon-1` and `daemon-2` service definitions.

## Docker Deployment

### Full Stack

Run PostgreSQL, OpenBao, the Lucent server, and one daemon worker:

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
- [Connections](connections.md) — two-tier connections model and feature-flag profiles
- [Secret Storage](secret-storage.md) — secret storage providers in detail
- [Observability](observability.md) — metrics, traces, dashboards
- [Security Model](security-model.md) — authentication and access control
