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

## Daemon Tasks

Base path: `/api/daemon/tasks`

Requires `daemon-tasks` scope or full `read`+`write` scopes. See the [Agent Integration Guide](agent-integration.md) for a complete walkthrough.

### Create Task

```
POST /api/daemon/tasks
```

```json
{
  "description": "Review auth module for security issues",
  "agent_type": "code",
  "priority": "medium",
  "context": "Focus on timing attacks",
  "tags": ["security"]
}
```

| Field | Values |
|-------|--------|
| `agent_type` | `research`, `code`, `memory`, `reflection`, `documentation`, `planning` |
| `priority` | `low`, `medium`, `high` |

### List Tasks

```
GET /api/daemon/tasks?status=pending&since=2026-03-07T20:00:00Z&limit=20
```

### Get Task

```
GET /api/daemon/tasks/{task_id}
```

### Get Task Result (Polling)

```
GET /api/daemon/tasks/{task_id}/result
```

Returns `200` with result if completed, `202` if still in progress, `404` if not found.

### Cancel Task

```
DELETE /api/daemon/tasks/{task_id}
```

Only pending tasks owned by the authenticated user can be cancelled.

---

## Daemon Messages

Base path: `/api/daemon/messages`

Requires `daemon-tasks` scope. Provides human-daemon communication.

### List Messages

```
GET /api/daemon/messages?pending_only=true&limit=50
```

### Send Message

```
POST /api/daemon/messages
```

```json
{
  "content": "Please prioritize the auth review",
  "in_reply_to": "optional-message-uuid"
}
```

### Acknowledge Message

```
POST /api/daemon/messages/{message_id}/acknowledge
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
