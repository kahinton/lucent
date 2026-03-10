# External Agent Integration Guide

Lucent exposes a REST API that allows external AI agents to submit tasks to the daemon, poll for completion, and retrieve results. This guide covers authentication, endpoints, and the integration flow.

## Authentication

External agents authenticate using **scoped API keys**. A key with the `daemon-tasks` scope can only interact with daemon task endpoints — it cannot read or modify general memories.

### Creating a Scoped Key

Create an API key with restricted scopes via the Lucent API:

```bash
# Full-access key (existing behavior)
POST /api/keys
{"name": "my-agent", "scopes": ["read", "write"]}

# Task-only key (recommended for external agents)
POST /api/keys
{"name": "my-agent", "scopes": ["daemon-tasks"]}
```

All requests must include the key in the `Authorization` header:

```
Authorization: Bearer hs_your_key_here
```

### Scope Behavior

| Scopes | Access |
|--------|--------|
| `["read", "write"]` | Full access to all API endpoints (default) |
| `["daemon-tasks"]` | Only `/api/daemon/tasks/*` endpoints |

## Daemon Task Schema

Daemon tasks are stored as Lucent memories with specific tags. When you create a task via the API, the following structure is used:

```json
{
  "id": "uuid",
  "description": "Task instructions for the agent",
  "agent_type": "code",
  "priority": "medium",
  "status": "pending",
  "tags": ["optional", "extra", "tags"],
  "created_at": "2026-03-07T21:00:00Z",
  "updated_at": "2026-03-07T21:00:00Z",
  "result": null,
  "claimed_by": null
}
```

### Agent Types

| Type | Description |
|------|-------------|
| `research` | Information gathering and analysis |
| `code` | Code review, bug fixes, implementation |
| `memory` | Memory maintenance and organization |
| `reflection` | Self-analysis and improvement |
| `documentation` | Documentation creation and updates |
| `planning` | Project planning and task breakdown |

### Task Lifecycle

```
pending → claimed → completed
                  ↘ pending (on failure, re-queued)
```

## API Endpoints

### Submit a Task

```
POST /api/daemon/tasks
```

**Request:**
```json
{
  "description": "Review auth module for security issues",
  "agent_type": "code",
  "priority": "medium",
  "context": "Focus on timing attacks and session handling",
  "tags": ["security", "auth"]
}
```

**Response:** `201 Created`
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "description": "Review auth module for security issues",
  "agent_type": "code",
  "priority": "medium",
  "status": "pending",
  "tags": ["security", "auth"],
  "created_at": "2026-03-07T21:00:00Z",
  "updated_at": "2026-03-07T21:00:00Z",
  "result": null,
  "claimed_by": null
}
```

### List Tasks

```
GET /api/daemon/tasks?status=pending&since=2026-03-07T20:00:00Z&limit=20
```

All query parameters are optional:
- `status`: Filter by `pending`, `claimed`, or `completed`
- `since`: Only tasks updated after this ISO timestamp (for efficient polling)
- `limit`: Max results (1-100, default 20)

### Get Task Status

```
GET /api/daemon/tasks/{task_id}
```

Returns the full task object.

### Get Task Result

```
GET /api/daemon/tasks/{task_id}/result
```

Returns:
- **200** with result if task is completed
- **202** if task is still in progress (body contains current task state)
- **404** if task not found

This endpoint is designed for polling — check the HTTP status code to determine completion.

### Cancel a Task

```
DELETE /api/daemon/tasks/{task_id}
```

Only pending tasks owned by the authenticated user can be cancelled.

## Rate Limiting

API keys with `daemon-tasks` scope get a higher rate limit (300 requests/minute vs the default 100) to support agent polling patterns.

Rate limit headers are included in every response:
```
X-RateLimit-Limit: 300
X-RateLimit-Remaining: 295
X-RateLimit-Reset: 1709845260
```

When rate limited, you'll receive a `429` response with a `Retry-After` header.

## Integration Flow

```
┌──────────────┐     POST /tasks      ┌───────────┐
│              │ ──────────────────── → │           │
│   External   │                       │   Lucent  │
│    Agent     │  GET /tasks/{id}/result│   API     │
│              │ ──────────────────── → │           │
│              │   202 (in progress)   │           │
│              │ ← ─────────────────── │           │
│              │                       │           │
│   (wait)     │  GET /tasks/{id}/result│           │
│              │ ──────────────────── → │           │
│              │   200 (completed)     │           │
│              │ ← ─────────────────── │           │
└──────────────┘                       └───────────┘
                                            │
                                            │ (daemon picks up task)
                                            ▼
                                       ┌───────────┐
                                       │  Lucent   │
                                       │  Daemon   │
                                       └───────────┘
```

## Example (Python)

See [`examples/agent_integration.py`](../examples/agent_integration.py) for a complete working example.

Quick version:

```python
import httpx, time

API = "http://localhost:8766/api/daemon/tasks"
HEADERS = {"Authorization": "Bearer hs_your_key"}

# Submit
task = httpx.post(API, headers=HEADERS, json={
    "description": "Analyze error handling in src/api/",
    "agent_type": "code",
}).json()

# Poll
while True:
    r = httpx.get(f"{API}/{task['id']}/result", headers=HEADERS)
    if r.status_code == 200:
        print("Result:", r.json()["result"])
        break
    time.sleep(10)
```
