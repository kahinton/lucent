# API Reference

Lucent exposes a REST API at `/api/*` alongside its MCP endpoint. All API routes require authentication via API key unless noted otherwise.

Interactive API docs are available at `/api/docs` (Swagger UI) and `/api/redoc` when the server is running.

## Authentication

Include your API key in every request:

```
Authorization: Bearer hs_your_api_key_here
```

API keys are created during first-run setup or via the Settings page at `/settings`. Keys prefixed with `hs_` are the only accepted format.

### Scoped Keys

Keys can be created with restricted scopes:

| Scopes | Access |
|--------|--------|
| `["read", "write"]` | Full access to all endpoints (default) |
| `["daemon-tasks"]` | Only daemon task and message endpoints |

## Rate Limiting

Default: 100 requests/minute per API key (configurable via `LUCENT_RATE_LIMIT_PER_MINUTE`). Daemon-scoped keys get 300 requests/minute.

Rate limit headers are included in every response:

```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 95
X-RateLimit-Reset: 1709845260
```

A `429` response with `Retry-After` header is returned when the limit is exceeded.

## Common Response Models

**ErrorResponse** — returned on 4xx/5xx errors:

```json
{"error": "string", "detail": "string or null"}
```

**SuccessResponse** — returned on successful delete/action operations:

```json
{"success": true, "message": "string"}
```

---

## Health Check

```
GET /api/health
```

No authentication required. Returns `{"status": "healthy"}`.

---

## Memories

Base path: `/api/memories`

### Create Memory

```
POST /api/memories
```

**Request Body:**

```json
{
  "type": "experience",
  "content": "Discovered that connection pooling improves latency by 40%",
  "username": "optional-override",
  "tags": ["performance", "database"],
  "importance": 7,
  "related_memory_ids": ["uuid-1", "uuid-2"],
  "metadata": {"context": "load testing", "outcome": "positive"}
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | `experience`, `technical`, `procedural`, or `goal` |
| `content` | string | yes | Main content of the memory |
| `username` | string | no | Defaults to authenticated user's display name |
| `tags` | string[] | no | Categorization tags |
| `importance` | int (1-10) | no | Default: 5 |
| `related_memory_ids` | UUID[] | no | Links to related memories |
| `metadata` | object | no | Type-specific metadata (validated per type) |

> **Note:** `individual` type memories cannot be created via API — they are auto-created with user accounts.

**Response:** `201` with the full memory object.

### Get Memory

```
GET /api/memories/{memory_id}
```

Returns the full memory object. Access is scoped to memories you own or that are shared within your organization.

### Update Memory

```
PATCH /api/memories/{memory_id}
```

Only the memory owner can update. All fields are optional — only provided fields are changed.

```json
{
  "content": "Updated content",
  "tags": ["new-tag"],
  "importance": 8,
  "metadata": {"updated": true}
}
```

All changes are versioned automatically.

### Delete Memory (Soft Delete)

```
DELETE /api/memories/{memory_id}
```

Soft deletes the memory. Individual memories cannot be deleted via API.

### Share Memory (Team Mode)

```
POST /api/memories/{memory_id}/share
```

Shares a memory with your organization. Only the memory owner can share.

### Unshare Memory (Team Mode)

```
POST /api/memories/{memory_id}/unshare
```

Removes a memory from organization sharing.

---

## Tags

Base path: `/api/memories/tags`

### List Tags

```
GET /api/memories/tags/list?username=alice&type=technical&limit=50
```

Returns all tags with usage counts. All parameters are optional.

**Response:**

```json
{
  "tags": [{"tag": "python", "count": 12}, {"tag": "api-design", "count": 8}],
  "total_count": 2
}
```

### Suggest Tags

```
GET /api/memories/tags/suggest?query=pyth&limit=10
```

Fuzzy-matches existing tags against a query string. Useful for autocomplete.

**Response:**

```json
{
  "suggestions": [{"tag": "python", "count": 12, "similarity": 0.85}],
  "query": "pyth"
}
```

---

## Search

Base path: `/api/search`

### Search Memories (Content Only)

```
POST /api/search
```

Fuzzy search on the content field. All fields are optional — omit `query` to browse/filter.

```json
{
  "query": "database optimization",
  "type": "technical",
  "tags": ["performance"],
  "importance_min": 5,
  "importance_max": 10,
  "created_after": "2026-01-01T00:00:00Z",
  "created_before": "2026-12-31T23:59:59Z",
  "offset": 0,
  "limit": 20
}
```

A `GET` version with query parameters is also available:

```
GET /api/search?query=database+optimization&type=technical&limit=20
```

**Response:**

```json
{
  "memories": [
    {
      "id": "uuid",
      "username": "alice",
      "type": "technical",
      "content": "...",
      "content_truncated": false,
      "tags": ["performance"],
      "importance": 7,
      "similarity_score": 0.82,
      "created_at": "2026-01-15T10:30:00Z",
      "updated_at": "2026-01-15T10:30:00Z",
      "..."
    }
  ],
  "total_count": 1,
  "offset": 0,
  "limit": 20,
  "has_more": false
}
```

### Search Full (All Fields)

```
POST /api/search/full
```

Searches across content, tags, and metadata. The `query` field is required.

Same request/response format as content search.

---

## Export & Import

Base path: `/api/memories/export`

### Export Memories

```
GET /api/memories/export?type=technical&format=json
```

| Parameter | Description |
|-----------|-------------|
| `type` | Filter by memory type |
| `tags` | Filter by tags (any match) |
| `importance_min` / `importance_max` | Filter by importance range |
| `created_after` / `created_before` | Filter by date range |
| `format` | `json` (default) or `jsonl` (streaming) |

The `jsonl` format streams one JSON object per line — first line is metadata, subsequent lines are memories.

### Import Memories

```
POST /api/memories/export/import
```

```json
{
  "memories": [
    {
      "type": "technical",
      "content": "Memory content",
      "tags": ["imported"],
      "importance": 5,
      "metadata": {}
    }
  ]
}
```

Deduplicates by content hash — memories with identical content, type, and username are skipped. All imported memories are owned by the authenticated user.

**Response:**

```json
{
  "imported": 5,
  "skipped": 2,
  "errors": [],
  "total": 7
}
```

---

## Requests & Tasks

Base path: `/api/requests`

The request/task system provides structured work tracking with full event timelines.

### Create Request

```
POST /api/requests
```

```json
{
  "title": "Audit authentication module",
  "description": "Review for timing attacks and session fixation",
  "priority": "high",
  "source": "user"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | yes | Short description of the work |
| `description` | string | no | Detailed instructions |
| `priority` | string | no | `low`, `medium` (default), `high`, `urgent` |
| `source` | string | no | `user` (default), `cognitive`, `api`, `daemon`, `schedule` |
| `dependency_policy` | string | no | `strict` (default) or `permissive` — controls whether later tasks are blocked when a predecessor fails |

### List Requests

```
GET /api/requests?status=pending&source=user&limit=20&offset=0
```

### Get Request Details

```
GET /api/requests/{request_id}
```

Returns the request with all tasks and their event timelines.

### Active Work

```
GET /api/requests/active
```

Returns non-completed requests with task status summaries (counts by status). Used by the daemon to understand what's already being worked on.

### Request Summary

```
GET /api/requests/summary
```

Returns aggregate stats for active requests.

### Recent Events

```
GET /api/requests/events?limit=50
```

Returns recent task events across all requests.

### Update Request Status

```
PATCH /api/requests/{request_id}/status
```

```json
{
  "status": "in_progress"
}
```

Valid statuses: `pending`, `planned`, `in_progress`, `review`, `needs_rework`, `completed`, `failed`, `cancelled`.

### Request Review

When `LUCENT_REQUIRE_APPROVAL=true`, completed requests transition to `review` status instead of `completed`. Admins can then approve or reject them.

#### List Requests in Review

```
GET /api/requests/review?limit=50&offset=0
```

Returns requests with status `review` or `needs_rework`.

#### Approve Request

```
POST /api/requests/{request_id}/review/approve
```

Transitions a request from `review` → `completed`. Returns `409` if the request is not in `review` status.

#### Reject Request

```
POST /api/requests/{request_id}/review/reject
```

```json
{
  "feedback": "The security audit missed the session fixation vulnerability in auth.py"
}
```

Transitions from `review` → `needs_rework`. The `feedback` field is required (min 1 char). Increments `review_count` and sets `reviewed_at`.

### Create Task (under a Request)

```
POST /api/requests/{request_id}/tasks
```

```json
{
  "title": "Security review",
  "description": "Check auth.py for timing attacks",
  "agent_type": "security",
  "priority": "high",
  "model": "claude-opus-4.6",
  "sandbox_template_id": "uuid-of-template",
  "output_contract": {
    "json_schema": {
      "type": "object",
      "properties": {
        "vulnerabilities": {"type": "array"},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]}
      },
      "required": ["vulnerabilities", "risk_level"]
    },
    "on_failure": "retry_then_fallback",
    "max_retries": 2
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | yes | Task name (1–256 chars) |
| `description` | string | no | Instructions for the agent |
| `agent_type` | string | no | Agent definition to use (default: `code`) |
| `model` | string | no | LLM model override |
| `priority` | string | no | `low`, `medium`, `high`, `urgent` |
| `sequence_order` | int | no | Execution order (0-based, lower runs first) |
| `parent_task_id` | UUID | no | Parent task ID for sub-tasks |
| `sandbox_template_id` | UUID | no | Sandbox template for isolated execution |
| `sandbox_config` | object | no | Inline sandbox configuration (template takes precedence) |
| `output_contract` | object | no | JSON Schema validation for task results (see below) |

#### Output Contracts

Output contracts let you require structured results from tasks. The daemon validates agent output against a JSON Schema before completing the task.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `json_schema` | object | yes | JSON Schema that the task result must validate against |
| `on_failure` | string | no | `fail`, `fallback` (default), or `retry_then_fallback` |
| `max_retries` | int | no | Max repair attempts when using retry modes (default: 1) |

**Failure modes:**

- `fail` — Task fails if output doesn't match the schema
- `fallback` — Task completes with unstructured result and `validation_status: "fallback_used"`
- `retry_then_fallback` — Attempts to repair the output up to `max_retries` times, then falls back

**Result fields** added to the completion payload:

| Field | Description |
|-------|-------------|
| `result_structured` | Parsed JSON matching the schema (when valid) |
| `result_summary` | Human-readable summary |
| `validation_status` | `valid`, `invalid`, `fallback_used`, `repair_succeeded`, `extraction_failed`, `not_applicable` |
| `validation_errors` | Array of validation error messages (when invalid) |

### Task Queue (Pending Tasks)

```
GET /api/requests/queue/pending
```

Returns tasks with `status = 'pending'`, ordered by priority and creation time.

### Queue Management

```
POST /api/requests/queue/release-stale?stale_minutes=30
```

Releases tasks claimed longer than `stale_minutes` without a heartbeat. Returns `{"released": count}`.

```
POST /api/requests/queue/reconcile
```

Reconciles parent request statuses based on their task states. Returns `{"reconciled": count}`.

### Task Lifecycle

```
POST /api/requests/tasks/{task_id}/claim
POST /api/requests/tasks/{task_id}/start
POST /api/requests/tasks/{task_id}/complete
POST /api/requests/tasks/{task_id}/fail
POST /api/requests/tasks/{task_id}/release
POST /api/requests/tasks/{task_id}/retry
POST /api/requests/tasks/{task_id}/retry-with-feedback
POST /api/requests/tasks/{task_id}/model
```

**Claim** — Body: `{"instance_id": "my-daemon-instance"}`. Locks the task for processing.

**Start** — Marks the task as actively running.

**Complete** — Body: `{"result": "...", "result_structured": {...}}`. Marks as done.

**Fail** — Body: `{"error": "..."}`. Marks as failed.

**Release** — Releases a claimed task back to pending.

**Retry** — Resets a failed task to pending for re-execution.

**Retry with feedback** — Body: `{"feedback": "..."}`. Retries with additional context from review.

**Model** — Body: `{"model": "claude-sonnet-4.5"}`. Updates the model assigned to a task.

### Task Events

```
POST /api/requests/tasks/{task_id}/events
GET /api/requests/tasks/{task_id}/events
```

```json
{
  "event_type": "sandbox_created",
  "detail": "Sandbox abc123 created for task",
  "metadata": {"sandbox_id": "abc123"}
}
```

Events are appended to the task timeline and visible on the Activity page.

### Task Memory Links

```
POST /api/requests/tasks/{task_id}/memories
GET /api/requests/tasks/{task_id}/memories
```

Link memories to tasks for lineage tracking. POST body: `{"memory_id": "uuid", "relation": "created"}`. Relations: `created`, `read`, `updated`.

---

## Schedules

Base path: `/api/schedules`

### Create Schedule

```
POST /api/schedules
```

```json
{
  "title": "Daily weather check",
  "description": "Fetch weather and recommend outfit",
  "schedule_type": "cron",
  "cron_expression": "30 5 * * *",
  "timezone": "US/Eastern",
  "agent_type": "weather-advisor",
  "priority": "low",
  "description": "Fetch weather for West Bloomfield, MI and recommend outfit",
  "sandbox_template_id": "uuid-of-template"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | yes | Schedule name |
| `schedule_type` | string | yes | `once`, `interval`, or `cron` |
| `cron_expression` | string | cron only | 5-field cron (min hour dom month dow) |
| `interval_seconds` | int | interval only | Repeat interval (minimum 60) |
| `timezone` | string | no | IANA timezone (default: `UTC`) |
| `agent_type` | string | no | Agent for task dispatch |
| `model` | string | no | LLM model override |
| `sandbox_template_id` | UUID | no | Sandbox template for each run |
| `sandbox_config` | object | no | Inline sandbox config (template takes precedence) |
| `task_template` | object | no | Reusable task template |
| `priority` | string | no | `low`, `medium`, `high`, `urgent` |
| `max_runs` | int | no | Stop after N runs (≥ 1) |
| `expires_at` | datetime | no | Schedule expiration time |

### List Schedules

```
GET /api/schedules?status=active&enabled=true
```

### Schedule Summary

```
GET /api/schedules/summary
```

Returns aggregate stats for schedules (counts by status).

### Get Due Schedules

```
GET /api/schedules/due
```

Returns schedules where `next_run_at <= now()` and `status = 'active'` and `enabled = true`.

### Get Schedule Details

```
GET /api/schedules/{schedule_id}
```

Returns the schedule with its run history.

### Update Schedule

```
PUT /api/schedules/{schedule_id}
```

All fields from Create Schedule are accepted (all optional). Only provided fields are updated.

### Toggle Schedule

```
POST /api/schedules/{schedule_id}/toggle
```

Body: `{"enabled": true}` or `{"enabled": false}`.

### Trigger Schedule

```
POST /api/schedules/{schedule_id}/trigger?force=false
```

Fires the schedule immediately. Pass `force=true` to bypass the time guard (for manual "Run Now" actions). Returns the created request and run record.

### Schedule Runs

```
GET /api/schedules/{schedule_id}/runs
```

Returns the execution history for a schedule.

### Delete Schedule

```
DELETE /api/schedules/{schedule_id}
```

---

## Agent Definitions

Base path: `/api/definitions`

### Agents

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/agents` | Create an agent definition (status: `proposed`) |
| `GET` | `/agents` | List agents (filter by `?status=active`) |
| `GET` | `/agents/{id}` | Get agent with linked skills and MCP servers |
| `PATCH` | `/agents/{id}` | Update an agent definition |
| `DELETE` | `/agents/{id}` | Delete an agent definition |
| `POST` | `/agents/{id}/approve` | Approve a proposed agent |
| `POST` | `/agents/{id}/reject` | Reject a proposed agent |

### Skills

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/skills` | Create a skill definition |
| `GET` | `/skills` | List skills |
| `GET` | `/skills/{id}` | Get skill details |
| `DELETE` | `/skills/{id}` | Delete a skill |
| `POST` | `/skills/{id}/approve` | Approve a proposed skill |
| `POST` | `/skills/{id}/reject` | Reject a proposed skill |

### MCP Servers

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/mcp-servers` | Register an MCP server |
| `GET` | `/mcp-servers` | List MCP servers |
| `PATCH` | `/mcp-servers/{id}` | Update an MCP server |
| `POST` | `/mcp-servers/{id}/approve` | Approve a proposed MCP server |
| `POST` | `/mcp-servers/{id}/reject` | Reject a proposed MCP server |
| `GET` | `/mcp-servers/{id}/tools` | Discover available tools (`?refresh=true` to force rediscovery) |

### Proposals

```
GET /api/definitions/proposals
```

Returns all pending proposals (agents, skills, and MCP servers awaiting approval) in a single response.

### Agent Access Grants

Grant or revoke skills and MCP servers for agent definitions:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/agents/{id}/skills` | Grant a skill to an agent (body: `{"target_id": "skill-uuid"}`) |
| `DELETE` | `/agents/{id}/skills/{skill_id}` | Revoke a skill from an agent |
| `POST` | `/agents/{id}/mcp-servers` | Grant an MCP server (body: `{"target_id": "server-uuid"}`) |
| `DELETE` | `/agents/{id}/mcp-servers/{server_id}` | Revoke an MCP server from an agent |

---

## Secrets

Base path: `/api/secrets`

The secrets API provides secure storage for sensitive values (API keys, tokens, passwords). Secret values are encrypted at rest and access-controlled via ownership. All secret operations are audit-logged.

### Create Secret

```
POST /api/secrets
```

**Request Body:**

```json
{
  "key": "my-api-key",
  "value": "sk_live_abc123...",
  "owner_group_id": "optional-group-uuid"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `key` | string | yes | Secret key name (1–256 chars) |
| `value` | string | yes | Secret value (never returned after creation) |
| `owner_group_id` | string | no | Group to own the secret (defaults to current user) |

**Response:** `201`

```json
{
  "key": "my-api-key",
  "owner_user_id": "uuid",
  "owner_group_id": null,
  "created_at": null,
  "updated_at": null
}
```

### List Secrets

```
GET /api/secrets?owner_group_id=optional-group-uuid
```

Lists secret key names for the current user or a specified group. **Values are never included.**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `owner_group_id` | string | no | List secrets owned by this group instead of the current user |

**Response:** `200`

```json
{
  "keys": [
    {
      "key": "my-api-key",
      "owner_user_id": "uuid",
      "owner_group_id": null,
      "created_at": "2026-03-15T10:00:00Z",
      "updated_at": "2026-03-15T10:00:00Z"
    }
  ]
}
```

### Get Secret Value

```
GET /api/secrets/{key}?owner_group_id=optional-group-uuid
```

Retrieves the decrypted value of a secret. Requires ACL access permission on the secret. Every read is audit-logged.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `key` | path | yes | Secret key name |
| `owner_group_id` | query | no | Scope to a group-owned secret |

**Response:** `200`

```json
{
  "key": "my-api-key",
  "value": "sk_live_abc123..."
}
```

**Errors:** `403` if ACL denies access, `404` if the secret does not exist.

### Delete Secret

```
DELETE /api/secrets/{key}?owner_group_id=optional-group-uuid
```

Deletes a secret. Requires ACL modify permission on the secret.

**Response:** `200`

```json
{
  "deleted": true,
  "key": "my-api-key"
}
```

**Errors:** `403` if ACL denies modify, `404` if the secret does not exist.

### Migrate Plaintext Configs

```
POST /api/secrets/migrate-plaintext-configs
```

Scans MCP server configs, sandbox templates, and integrations for plaintext sensitive values (tokens, passwords, API keys) and migrates them to encrypted secret storage. Original values are replaced with `secret://` references.

**Requires:** admin role or higher.

**Response:** `200`

```json
{
  "migrated_mcp_env_vars": 3,
  "migrated_sandbox_env_vars": 1,
  "migrated_integration_values": 2
}
```

---

## Legacy Daemon Endpoints

> **Note:** These endpoints are from the earlier daemon task system and are still functional but superseded by the Request/Task API above.

## Integrations

See the full [Integrations API Reference](integrations-api-reference.md) for detailed request/response schemas.

Base paths: `/integrations/webhook/*` (webhooks), `/api/v1/integrations` (admin CRUD, linking)

### Webhooks

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/integrations/webhook/{provider}` | Signature | Receive platform webhook (Slack, Discord) |

### Admin (admin+ role)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/integrations` | Create integration |
| `GET` | `/api/v1/integrations` | List integrations |
| `GET` | `/api/v1/integrations/{id}` | Get integration |
| `PATCH` | `/api/v1/integrations/{id}` | Update integration (config, channels, status) |
| `DELETE` | `/api/v1/integrations/{id}` | Soft-delete integration |
| `GET` | `/api/v1/integrations/links` | List user links |
| `POST` | `/api/v1/integrations/links` | Admin-create user link |
| `DELETE` | `/api/v1/integrations/links/{id}` | Revoke user link |

### Pairing (any authenticated user)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/integrations/link` | Generate pairing code |
| `POST` | `/api/v1/integrations/verify` | Verify pairing code and activate link |

---

## Legacy Daemon Endpoints

> **Note:** These endpoints are from the earlier daemon task system and are still functional but superseded by the Request/Task API above.

Base path: `/api/daemon`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/tasks` | Create a legacy daemon task |
| `GET` | `/tasks` | List legacy tasks |
| `GET` | `/tasks/{id}` | Get a legacy task |
| `GET` | `/tasks/{id}/result` | Poll for task result |
| `DELETE` | `/tasks/{id}` | Cancel a pending task |
| `GET` | `/messages` | List daemon messages |
| `POST` | `/messages` | Send a message to the daemon |
| `POST` | `/messages/{id}/acknowledge` | Acknowledge a message |

---

## Groups

Base path: `/api/groups`

Groups enable shared ownership of resources (agents, skills, MCP servers, secrets) and team-based access control. Groups are scoped to an organization — all group operations require the user to belong to one.

### Create Group

```
POST /api/groups
```

**Requires:** `USERS_MANAGE` permission (admin+ role).

**Request Body:**

```json
{
  "name": "platform-team",
  "description": "Core platform engineering team"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Group name (1–128 chars, unique within org) |
| `description` | string | no | Group description |

**Response:** `201`

```json
{
  "id": "uuid",
  "name": "platform-team",
  "description": "Core platform engineering team",
  "org_id": "uuid",
  "member_count": 0,
  "created_at": "2026-03-15T10:00:00Z",
  "updated_at": "2026-03-15T10:00:00Z"
}
```

**Errors:** `409` if a group with the same name already exists.

### List Groups

```
GET /api/groups?limit=25&offset=0
```

Lists all groups in the caller's organization with pagination.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `limit` | int | no | Results per page (1–100, default: 25) |
| `offset` | int | no | Pagination offset (default: 0) |

**Response:** `200`

```json
{
  "items": [
    {
      "id": "uuid",
      "name": "platform-team",
      "description": "Core platform engineering team",
      "org_id": "uuid",
      "member_count": 3,
      "created_at": "2026-03-15T10:00:00Z",
      "updated_at": "2026-03-15T10:00:00Z"
    }
  ],
  "total_count": 1,
  "offset": 0,
  "limit": 25,
  "has_more": false
}
```

### Get Group

```
GET /api/groups/{group_id}
```

Returns group details and its full member list.

**Response:** `200`

```json
{
  "group": {
    "id": "uuid",
    "name": "platform-team",
    "description": "Core platform engineering team",
    "org_id": "uuid",
    "member_count": 2,
    "created_at": "2026-03-15T10:00:00Z",
    "updated_at": "2026-03-15T10:00:00Z"
  },
  "members": [
    {
      "user_id": "uuid",
      "display_name": "Alice",
      "email": "alice@example.com",
      "role": "admin",
      "joined_at": "2026-03-15T10:00:00Z"
    }
  ]
}
```

### Update Group

```
PUT /api/groups/{group_id}
```

**Requires:** group admin or org admin+ role.

**Request Body:**

```json
{
  "name": "new-name",
  "description": "Updated description"
}
```

Both fields are optional, but at least one must be provided.

**Response:** `200` with the updated group object (same schema as Create Group response).

**Errors:** `409` if the new name conflicts with an existing group, `422` if no fields are provided.

### Delete Group

```
DELETE /api/groups/{group_id}
```

**Requires:** org admin+ role.

**Response:** `200`

```json
{"success": true}
```

### Add Group Member

```
POST /api/groups/{group_id}/members
```

**Requires:** group admin or org admin+ role. The target user must belong to the same organization.

**Request Body:**

```json
{
  "user_id": "uuid-of-user",
  "role": "member"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | UUID | yes | User to add |
| `role` | string | no | `member` (default) or `admin` |

**Response:** `201`

```json
{
  "user_id": "uuid",
  "display_name": "Bob",
  "email": "bob@example.com",
  "role": "member",
  "joined_at": "2026-03-15T10:00:00Z"
}
```

**Errors:** `409` if the user is already a member.

### Remove Group Member

```
DELETE /api/groups/{group_id}/members/{user_id}
```

**Requires:** group admin or org admin+ role.

**Response:** `200`

```json
{"success": true}
```

### List Group Members

```
GET /api/groups/{group_id}/members
```

Returns all members of a group.

**Response:** `200`

```json
{
  "group_id": "uuid",
  "members": [
    {
      "user_id": "uuid",
      "display_name": "Alice",
      "email": "alice@example.com",
      "role": "admin",
      "joined_at": "2026-03-15T10:00:00Z"
    }
  ],
  "total_count": 1
}
```

---

## Team Mode Endpoints

The following endpoints are only available when `LUCENT_MODE=team`. They require appropriate roles (admin or owner).

### Users

Base path: `/api/users`

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `GET` | `/me` | any | Get your own profile |
| `PATCH` | `/me` | any | Update your profile (email, display_name, avatar_url) |
| `GET` | `/` | member+ | List organization users (optionally filter by `?role=admin`) |
| `GET` | `/{user_id}` | member+ | Get a user by ID (same org only) |
| `POST` | `/` | admin+ | Create a user in your organization |
| `PATCH` | `/{user_id}` | admin+ | Update a user's profile or deactivate them |
| `PATCH` | `/{user_id}/role` | admin+ | Change a user's role (`member`, `admin`, `owner`) |
| `POST` | `/{user_id}/reset-password` | admin+ | Reset a user's password |
| `DELETE` | `/{user_id}` | admin+ | Delete a user and all their memories |

### Organizations

Base path: `/api/organizations`

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `GET` | `/current` | member+ | Get your organization |
| `PATCH` | `/current` | owner | Update organization name |
| `POST` | `/` | owner | Create a new organization |
| `GET` | `/{organization_id}` | member+ | Get organization by ID (own org only) |
| `GET` | `/` | owner | List all organizations |
| `DELETE` | `/current` | owner | Delete organization (irreversible) |
| `POST` | `/current/transfer` | owner | Transfer ownership to another user |

### Audit Logs

Base path: `/api/audit`

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `GET` | `/memory/{memory_id}` | any | Audit log for a memory (own actions, or org-wide for admins) |
| `GET` | `/user/{user_id}` | any/admin | Audit log for a user's actions |
| `GET` | `/organization` | admin+ | Full organization audit log |
| `GET` | `/recent` | admin+ | Recent audit entries for monitoring |

### Access Logs

Base path: `/api/access`

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `GET` | `/memory/{memory_id}` | any | Access history for a memory |
| `GET` | `/memory/{memory_id}/searches` | any | Search queries that surfaced a memory |
| `GET` | `/user/{user_id}` | any/admin | A user's memory access activity |
| `GET` | `/most-accessed` | any | Most frequently accessed memories (`?organization_wide=true` for admins) |

### Sandboxes

Base path: `/api/sandboxes`

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `POST` | `/` | any | Create a new sandbox instance |
| `GET` | `/` | any | List sandboxes (filtered by organization) |
| `GET` | `/{sandbox_id}` | any | Get sandbox details (org-scoped) |
| `POST` | `/{sandbox_id}/exec` | any | Execute a command in a sandbox |
| `GET` | `/{sandbox_id}/files` | any | List files in sandbox (default `/workspace`) |
| `GET` | `/{sandbox_id}/files/{path}` | any | Read a file from sandbox |
| `PUT` | `/{sandbox_id}/files/{path}` | any | Write a file to sandbox |
| `POST` | `/{sandbox_id}/stop` | any | Stop a running sandbox |
| `DELETE` | `/{sandbox_id}` | any | Permanently destroy a sandbox |

All instance endpoints verify the caller's organization matches the sandbox. Sandbox IDs from other organizations return `404`.

### Sandbox Templates

Base path: `/api/sandboxes/templates`

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `POST` | `/` | any | Create a reusable sandbox template |
| `GET` | `/` | any | List templates for the organization |
| `GET` | `/{template_id}` | any | Get a template by ID |
| `PATCH` | `/{template_id}` | any | Update a template |
| `DELETE` | `/{template_id}` | any | Delete a template |
| `POST` | `/{template_id}/launch` | any | Launch a sandbox instance from a template |

Templates define reusable environment configurations (image, setup commands, resource limits, etc.) that can be referenced by tasks and schedules via `sandbox_template_id`.

---

## Chat

Base path: `/api/chat`

The chat API provides streaming LLM responses with MCP tool access.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/stream` | Stream a chat response via Server-Sent Events |
| `POST` | `/stream-v2` | Enhanced streaming with agent-scoped SSE events |
| `GET` | `/models` | List available chat models |
| `GET` | `/status` | Check chat availability and configured model |
