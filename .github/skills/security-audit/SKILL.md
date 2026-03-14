---
name: security-audit
description: 'Security review procedures for auth, API keys, rate limiting, RBAC, and memory access control — pairs with the existing security agent'
---

# Security Audit — Lucent Project

## Lucent Security Architecture

| Layer | Implementation | Key Files |
|-------|---------------|-----------|
| Authentication | Session cookies (bcrypt + PyNaCl), API keys, OAuth providers | `auth.py`, `auth_providers.py` |
| Authorization | RBAC (owner/admin/member) + org-scoped isolation | `rbac.py`, `api/deps.py` |
| Rate limiting | Token bucket per-user, configurable per-endpoint | `rate_limit.py` |
| Memory isolation | Organization-scoped — users only see their org's memories | `db/` repositories, SQL queries |
| MCP auth | Session token or API key via Bearer auth in MCP middleware | `server.py` middleware |
| CSRF | Double-submit cookie pattern on state-changing endpoints | `api/app.py` |

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

## How to Run an Audit

1. **Search memories** for past audit results: `search_memories(tags=["security", "code-review"])`
2. **Read the auth chain**: `auth.py` → `auth_providers.py` → `api/deps.py` → router endpoints
3. **Check every route** in `api/routers/` — verify auth dependency is present and RBAC is correct
4. **Grep for red flags**: `f"SELECT`, `f"INSERT`, `f"UPDATE`, `f"DELETE` (SQL injection risk), `password` in logs, hardcoded secrets
5. **Test boundary conditions**: What happens with expired sessions? Revoked API keys? Wrong org ID?
6. **Save findings** as a memory tagged `security`, `code-review` with importance 8-9
