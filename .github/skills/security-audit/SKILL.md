---
name: security-audit
description: 'Security review procedures for auth, API keys, rate limiting, RBAC, and memory access control — pairs with the existing security agent'
---

# Security Audit — Lucent Project

## MCP Tools Used

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `memory-server-search_memories` | Load past audit findings | `tags=["security", "code-review"]`, `limit=10` |
| `memory-server-create_memory` | Save audit findings | `type="technical"`, `tags=["security"]`, `importance=8-9` |
| `memory-server-search_memories` | Find known vulnerabilities | `query="vulnerability [area]"`, `tags=["security"]` |

## Lucent Security Architecture

| Layer | Implementation | Key Files |
|-------|---------------|-----------|
| Authentication | Session cookies (bcrypt + PyNaCl), API keys, OAuth providers | `auth.py`, `auth_providers.py` |
| Authorization | RBAC (owner/admin/member) + org-scoped isolation | `rbac.py`, `api/deps.py` |
| Rate limiting | Token bucket per-user, configurable per-endpoint | `rate_limit.py` |
| Memory isolation | Organization-scoped — users only see their org's memories | `db/` repositories, SQL queries |
| MCP auth | Session token or API key via Bearer auth in MCP middleware | `server.py` middleware |
| CSRF | Double-submit cookie pattern on state-changing endpoints | `api/app.py` |

## Procedure: Full Security Audit

### Step 1: Load Past Findings

```
memory-server-search_memories(tags=["security", "code-review"], limit=10)
```

Check for: known vulnerabilities, past findings, areas previously flagged.

### Step 2: Read the Auth Chain

Trace the full authentication path:
```
auth.py → auth_providers.py → api/deps.py → router endpoints
```

Verify at each step:
- Session cookie attributes: `HttpOnly`, `Secure`, `SameSite=Lax`
- Token validation is not bypassable
- `get_current_user` dependency is used on every protected endpoint

### Step 3: Check Every Route

```bash
# Find all router files (adapt path to the project's structure)
find src/ -path "*/routers/*.py" -o -path "*/routes/*.py"

# Check for routes without auth
grep -rn "def " src/ --include="*.py" | grep -i "route\|endpoint" | grep -v "get_current_user\|Depends"
```

Verify auth dependency is present and RBAC is correct for each endpoint.

### Step 4: Grep for Red Flags

```bash
# SQL injection risk — look for f-string SQL
grep -rn 'f"SELECT\|f"INSERT\|f"UPDATE\|f"DELETE' src/

# Password logging
grep -rn 'password' src/ | grep -i 'log\|print'

# Hardcoded secrets
grep -rn 'secret_key\|api_key\s*=' src/ | grep -v 'os.env\|config\|test'
```

### Step 5: Test Boundary Conditions

- Expired sessions → should get 401, not 500
- Revoked API keys → should get 401
- Wrong org ID in request → should get 404 (not 403, to prevent IDOR)
- Admin-only endpoints accessed by member → should get 403

### Step 6: Save Findings

```
memory-server-create_memory(
  type="technical",
  content="## Security Audit: [area] — [date]\n\n**Scope**: ...\n**Findings**: ...\n**Status**: [clean|findings]\n**Recommendations**: ...",
  tags=["security", "code-review"],
  importance=8,
  shared=true
)
```

## Audit Checklist

### 1. Authentication
- [ ] Session cookies: `HttpOnly`, `Secure`, `SameSite=Lax`, proper expiry
- [ ] Password hashing: bcrypt with adequate work factor
- [ ] Session tokens: cryptographically random, not guessable
- [ ] API keys: hashed in database (never stored plaintext)
- [ ] No auth bypass on sensitive endpoints (check `get_current_user` dependency)

### 2. Authorization (RBAC)
- [ ] Destructive operations (DELETE) require `admin` or `owner` role
- [ ] Write operations require `member` or above
- [ ] Organization isolation: queries always filter by `organization_id`
- [ ] No IDOR — users can't access resources from other orgs by guessing IDs
- [ ] Daemon API key has minimal permissions (read/write, not admin)

### 3. Input Validation
- [ ] All API inputs validated via Pydantic models
- [ ] SQL queries use parameterized queries (asyncpg `$1` placeholders, never f-strings)
- [ ] File paths validated and sandboxed (sandbox module)
- [ ] Search queries sanitized (no SQL injection via search)

### 4. Rate Limiting
- [ ] Auth endpoints (login, register) have strict rate limits
- [ ] API endpoints have per-user rate limits
- [ ] Rate limit headers returned in responses
- [ ] Failed auth attempts tracked and limited

### 5. Memory Access Control
- [ ] Memories scoped to organization — cross-org access impossible
- [ ] Shared memories only visible within the same org
- [ ] Memory operations validate user.organization_id against memory.organization_id
- [ ] Daemon operations use the daemon's own identity, not impersonation

### 6. MCP Protocol Security
- [ ] MCP middleware validates session token or API key on every request
- [ ] Tool operations respect the authenticated user's permissions
- [ ] No tools expose internal server state or other users' data

## Decision: Severity of Finding

- IF auth bypass or privilege escalation possible → **critical**, importance=10, tag `security-critical`
- ELIF SQL injection or data exfiltration risk → **high**, importance=9
- ELIF IDOR or cross-org data leak → **high**, importance=9
- ELIF missing rate limiting on auth endpoints → **medium**, importance=7
- ELIF information leakage in error messages → **low**, importance=6

## Example: Good Security Finding

```
memory-server-create_memory(
  type="technical",
  content="## Security Finding: IDOR in memory update endpoint\n\n**File**: <router_file> line 84\n**Issue**: update endpoint checks UUID format but does not verify the record belongs to the current user's org before updating. An attacker could update any record if they know its UUID.\n**Exploit**: PUT /api/<resource>/{any_uuid} with modified content\n**Fix**: Add org check: WHERE id = $1 AND organization_id = $2 in the SQL query\n**Status**: Needs fix",
  tags=["security", "security-critical"],
  importance=9,
  shared=true
)
```
