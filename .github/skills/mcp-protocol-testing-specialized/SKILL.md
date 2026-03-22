---
name: mcp-protocol-testing-specialized
description: 'DEPRECATED — content merged into mcp-protocol-testing. Use mcp-protocol-testing instead.'
---

> **Deprecated**: This skill has been merged into `mcp-protocol-testing`. All content below is preserved for reference but `mcp-protocol-testing` is the canonical version.

# MCP Protocol Testing

Procedures for validating Lucent's MCP tool implementations, testing edge cases, and verifying search behavior. Tools are defined in `src/lucent/tools/memories.py` and registered via FastMCP in `src/lucent/server.py`.

## When to Use

- After modifying any tool in `src/lucent/tools/memories.py`
- After changing validation logic in `src/lucent/models/validation.py`
- After modifying search/database queries in `src/lucent/db/memory.py`
- When adding new MCP tools
- When debugging tool behavior reported by LLM clients
- Before releasing a new version

## Key Files

| File | Purpose |
|------|---------|
| `src/lucent/tools/memories.py` | 16 MCP tool implementations via `@mcp.tool()` |
| `src/lucent/server.py` | FastMCP server init, auth middleware (`MCPAuthMiddleware`) |
| `src/lucent/models/validation.py` | Pydantic metadata validation schemas |
| `src/lucent/models/memory.py` | `MemoryType` enum and memory model |
| `src/lucent/db/memory.py` | PostgreSQL CRUD + fuzzy search |
| `tests/test_mcp_tools.py` | Integration test suite |

## Tool Inventory

### CRUD Tools
| Tool | Key Parameters | Validation Points |
|------|---------------|-------------------|
| `create_memory` | `type`, `content`, `tags`, `importance` (1-10), `metadata` | Type enum check, metadata schema validation, `individual` type blocked |
| `get_memory` | `memory_id` | UUID format validation |
| `get_memories` | `memory_ids` (list) | Batch UUID validation, returns `found` + `not_found` lists |
| `update_memory` | `memory_id`, `expected_version` | UUID check, ownership check, optimistic locking via `VersionConflictError` |
| `delete_memory` | `memory_id` | UUID check, ownership check, `individual` type blocked |

### Search Tools
| Tool | Key Parameters | Validation Points |
|------|---------------|-------------------|
| `search_memories` | `query` (optional), `tags`, `type`, `importance_min/max`, `created_after/before`, `limit` (max 50) | Datetime ISO parsing, limit capping |
| `search_memories_full` | `query` (required), `type`, `importance_min/max`, `limit` (max 50) | Empty query check, limit capping |

### Versioning Tools
| Tool | Key Parameters | Validation Points |
|------|---------------|-------------------|
| `get_memory_versions` | `memory_id`, `limit` (max 50) | UUID check, limit capping |
| `restore_memory_version` | `memory_id`, `version` | UUID check, version existence |

### Utility Tools
| Tool | Key Parameters | Validation Points |
|------|---------------|-------------------|
| `get_existing_tags` | `limit` (max 100) | Limit capping |
| `get_tag_suggestions` | `query`, `limit` (max 25) | Partial match behavior |
| `get_current_user_context` | (none) | Auth context extraction |
| `share_memory` / `unshare_memory` | `memory_id` | Team mode only (conditional registration) |
| `create_daemon_task` | `description`, `agent_type`, `priority` | Creates a task in the tasks table (not a memory) |
| `create_request` | `title`, `description`, `source`, `priority` | Creates a request in the pending queue |

## Testing Procedures

### 1. Input Validation Testing

Test each tool with invalid inputs to verify error handling returns JSON `{"error": "..."}` via `_error_response()`:

**UUID validation** (affects `get_memory`, `update_memory`, `delete_memory`, `get_memory_versions`, `restore_memory_version`):
- Empty string → should return error
- Non-UUID string (e.g., `"not-a-uuid"`) → `ValueError` caught, returns error JSON
- Valid UUID that doesn't exist → "Memory not found or not accessible"

**Type validation** (`create_memory`):
- Invalid type string (e.g., `"invalid"`) → `MemoryType(type)` raises error
- `"individual"` type → explicitly blocked, returns error
- Valid types: `"experience"`, `"technical"`, `"procedural"`, `"goal"`

**Limit capping**:
- `search_memories(limit=100)` → silently capped to 50 via `min(limit, 50)`
- `get_existing_tags(limit=200)` → capped to 100
- `get_tag_suggestions(limit=50)` → capped to 25
- `get_memory_versions(limit=100)` → capped to 50

**Datetime validation** (`search_memories`):
- Invalid ISO format for `created_after`/`created_before` → `datetime.fromisoformat()` fails, caught and returns error

### 2. Search Behavior Testing

**`search_memories` (content-only fuzzy search)**:
- Uses PostgreSQL trigram similarity on content field
- `query` is optional — can search by tags/type/dates alone
- Returns results truncated to 1000 chars via `_serialize_truncated_memory()`
- Response format: `{"memories": [...], "total_count": N, "offset": 0, "limit": 5, "has_more": bool}`

**`search_memories_full` (broad search)**:
- `query` is required — empty/whitespace returns `{"error": "..."}`
- Searches across content, tags, AND metadata
- Same truncation and pagination as `search_memories`

**Pagination**:
- Test `offset` + `limit` combinations
- Verify `has_more` is accurate
- Verify `total_count` reflects all matches, not just the page

**Edge cases**:
- Search with no results → `{"memories": [], "total_count": 0, ...}`
- Search with special characters in query (SQL injection safety)
- Very long query strings
- Combining multiple filters: `tags` + `type` + `importance_min` + `created_after`

### 3. Optimistic Locking Testing

Test the `expected_version` parameter on `update_memory`:

1. Create a memory → version 1
2. Update with `expected_version=1` → succeeds, version becomes 2
3. Update with `expected_version=1` again → `VersionConflictError` with expected vs actual version
4. Update without `expected_version` → succeeds (no locking enforced)

### 4. Access Control Testing

**Ownership checks**:
- Create memory as user A → update as user B → should fail with "not accessible"
- Create memory as user A → delete as user B → should fail

**Team mode** (`is_team_mode()` in `server.py`):
- `share_memory` and `unshare_memory` tools only registered when team mode is active
- Shared memories accessible by other org members
- Unshared memories only accessible by owner

**Auth middleware** (`MCPAuthMiddleware` in `server.py`):
- Missing `Authorization` header → 401
- Invalid token format (not `hs_*` prefix) → 401
- Rate limited → 429 with `Retry-After` header

### 5. Batch Operation Testing

Test `get_memories` with mixed valid/invalid IDs:
- All valid → all in `found` list
- All invalid → all in `not_found` list
- Mix → split correctly between lists
- Empty list → verify behavior

### 6. Memory Versioning Testing

1. Create memory → `get_memory_versions` → should show version 1
2. Update memory 3 times → versions list should show 4 entries
3. `restore_memory_version(version=2)` → content should match version 2
4. Version count should increment (restore creates a new version)

### 7. Request/Task Creation Testing

`create_request` creates a request in the pending queue:
- Verify request appears in `GET /api/requests?status=pending`
- Verify `source` field is preserved (`user`, `cognitive`, `api`, `schedule`)
- Verify `priority` is preserved
- Test with all source types

`create_daemon_task` creates a task in the tasks table:
- Verify task appears linked to its parent request
- Verify `agent_type` matches an active agent definition
- Verify `priority` is stored
- Test with optional `context` and `tags` parameters

## Running the Test Suite

```bash
# Ensure PostgreSQL is running
docker compose up -d postgres

# Set test database URL
export TEST_DATABASE_URL="postgresql://lucent:lucent_dev_password@localhost:5433/lucent_test"

# Run MCP tool tests
pytest tests/test_mcp_tools.py -v

# Run all tests
pytest
```

### Test Pattern

Tests use a mock auth helper and call tools directly:
```python
set_current_user({"id": user_id, "organization_id": org_id, "role": "member"})
result = await _call(mcp_tools, "tool_name", {"param": value})
parsed = json.loads(result)  # All tool responses are JSON strings
```

## Common Issues

| Issue | Cause | Check |
|-------|-------|-------|
| Tool returns `{"error": "..."}` unexpectedly | Validation failure | Check parameter types and formats |
| Search returns empty for known content | Trigram similarity threshold too high | Check `src/lucent/db/memory.py` similarity config |
| Version conflict on update | Concurrent modification | Use `expected_version` or retry |
| Tool not found by client | Tool not registered | Check `register_tools(mcp)` in `memories.py` |
| Auth failures on `/mcp` | Token invalid or rate limited | Check `MCPAuthMiddleware` logs |
