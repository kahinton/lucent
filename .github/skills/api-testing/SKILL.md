---
name: api-testing
description: 'Procedures for testing REST API endpoints and MCP protocol compliance — pairs with the existing api-testing agent'
---

# API Testing — Lucent Project

## MCP Tools Used

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `memory-server-search_memories` | Load past test findings and known issues | `query="api test [endpoint area]"`, `tags=["api-testing"]` |
| `memory-server-create_memory` | Save test findings and bug discoveries | `type="technical"`, `tags=["api-testing", "lucent"]`, `importance=7` |

## Endpoints Reference

### REST API (FastAPI at `:8766/api`)

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/api/health` | GET | None | Liveness check |
| `/api/memories` | GET | Required | List memories (paginated, org-scoped) |
| `/api/memories/{id}` | GET | Required | Get single memory |
| `/api/memories/search` | POST | Required | Fuzzy search with tag/type filters |
| `/api/definitions/agents` | GET | Required | List agent definitions |
| `/api/definitions/skills` | GET | Required | List skill definitions |
| `/api/users` | GET | Admin | List org users |
| `/api/daemon/tasks` | POST | Required | Create daemon task |
| `/api/daemon/tasks` | GET | Required | List daemon tasks |

### MCP Protocol (via `/mcp` SSE/StreamableHTTP)

| Tool | Key Parameters | Validations |
|------|---------------|-------------|
| `create_memory` | type, content, tags, importance, metadata | Type enum, `individual` blocked, metadata schema |
| `search_memories` | query (optional), tags, type, limit (max 50) | Limit capping, datetime parsing |
| `search_memories_full` | query (required), type, limit | Empty query check |
| `update_memory` | memory_id, expected_version | UUID check, ownership, optimistic locking |
| `delete_memory` | memory_id | UUID check, ownership, `individual` blocked |
| `get_current_user_context` | (none) | Auth context extraction |
| `get_existing_tags` | limit (max 100) | Limit capping |

## Procedure: Full API Test Run

### Step 1: Load Context

```
memory-server-search_memories(query="api test failures known issues", tags=["api-testing"], limit=5)
```

### Step 2: Health Check

```bash
curl -s http://localhost:8766/api/health
```

Expected: `{"status": "ok"}` with 200. If this fails, no other tests will pass — diagnose server first.

### Step 3: Auth Flow Testing

```bash
# Login
curl -s -X POST http://localhost:8766/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"password"}' \
  -c cookies.txt

# Access with session
curl -s http://localhost:8766/api/memories \
  -b cookies.txt

# Access without auth → expect 401
curl -s http://localhost:8766/api/memories

# API key auth
curl -s http://localhost:8766/api/memories \
  -H "Authorization: Bearer hs_your_api_key"
```

### Step 4: Run Test Suite

```bash
pytest tests/test_mcp_tools.py -v          # MCP tool tests
pytest tests/test_memories_api.py -v       # Memory API tests
pytest tests/test_auth.py -v               # Auth tests
pytest tests/test_search_api.py -v         # Search tests
pytest tests/test_rate_limit.py -v         # Rate limit tests
```

### Step 5: Save Findings

```
memory-server-create_memory(
  type="technical",
  content="## API Test Run: [date]\n\n**Scope**: ...\n**Results**: [pass/fail counts]\n**Issues Found**: ...",
  tags=["api-testing", "lucent"],
  importance=6,
  shared=true
)
```

## Test Procedures by Area

### 1. Auth Flow Testing
- Login with valid creds → session cookie set (HttpOnly, Secure, SameSite=Lax)
- Access without auth → 401
- Access with valid session → 200
- Expired session → 401
- API key auth (`Authorization: Bearer hs_...`) → same access as session

### 2. Org Isolation Testing
- Create memory as Org A user
- Search/GET as Org B user → must NOT see it (return 404, not 403)

### 3. RBAC Testing
- Member: can read/write memories
- Admin: can manage users
- Owner: can do everything
- Wrong role → 403

### 4. MCP Protocol CRUD Lifecycle
```
create_memory → get_memory → update_memory → search_memories → delete_memory
```
Verify each step returns expected data. Test optimistic locking: update with wrong `expected_version` → version conflict error.

### 5. Rate Limiting
- Normal rate → 200
- Exceed limit → 429 with `Retry-After`
- Wait for refill → 200 again

### 6. Input Validation
- Invalid UUID → appropriate error
- Invalid memory type → rejected
- Missing required fields → 400/422
- Oversized content → handled gracefully

## Decision: What to Test First

- IF server just restarted → run health check + auth flow first
- ELIF code change to auth/RBAC → run test_auth.py and org isolation tests
- ELIF code change to memory CRUD → run test_mcp_tools.py and test_memories_api.py
- ELIF code change to search → run test_search_api.py with edge case queries
- ELIF full regression before release → run full test suite in order above

## Key Test Files
- `tests/conftest.py` — shared fixtures (db_pool, test users, async client)
- `tests/test_mcp_tools.py` — MCP tool integration tests
- `tests/test_auth.py`, `test_auth_providers.py` — auth flow tests
- `tests/test_memories_api.py` — REST memory CRUD tests
