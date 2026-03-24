---
name: security-audit
description: 'Security review procedures — authentication, authorization, input validation, secrets management, and access control. Use when reviewing authentication, authorization, input validation, or access control code for vulnerabilities.'
---

# Security Audit

## Before Starting

Load previous security findings and known vulnerabilities:
```
search_memories(query="security audit vulnerability", tags=["security"], limit=10)
```

## Audit Sequence

### 1. Map the Attack Surface

Identify every entry point where external data enters the system:

```bash
# Find route handlers / API endpoints
grep -rn "route\|endpoint\|@app\|@router\|handler\|controller" src/ --include="*.py" --include="*.ts" --include="*.go" --include="*.rs" --include="*.java"

# Find form/request parsing
grep -rn "request\.\|req\.\|body\.\|params\.\|query\." src/
```

For each entry point, determine:
- What authentication is required (or not)
- What authorization checks are applied
- What input validation exists
- What data is returned in responses

### 2. Authentication Review

Trace the auth flow end-to-end:

1. **Credential handling:** How are passwords/tokens stored? (Must be hashed, never plaintext)
2. **Session management:** How are sessions created, validated, and expired? Cookie attributes? (`HttpOnly`, `Secure`, `SameSite`)
3. **Token validation:** Is it possible to bypass validation? What happens with expired, malformed, or missing tokens?
4. **Every protected endpoint:** Verify the auth middleware/dependency is actually applied — not just present in the codebase but skipped on certain routes.

### 3. Authorization Review

For every endpoint that handles data:

1. **Ownership checks:** Can user A access user B's data? Test by checking whether resource lookups include the owner/org filter.
2. **Role enforcement:** Are admin-only operations actually restricted? Check the middleware, not just the route declaration.
3. **Horizontal privilege escalation:** Can a user modify another user's resources by guessing IDs?
4. **Response codes:** 404 (not 403) for resources that don't belong to the user — don't leak existence.

### 4. Input Validation & Injection

```bash
# SQL injection — string interpolation in queries
grep -rn 'f"SELECT\|f"INSERT\|f"UPDATE\|f"DELETE\|`SELECT\|`INSERT' src/

# Command injection — user input in shell commands
grep -rn 'exec\|system\|popen\|subprocess\|child_process\|os.system' src/

# Template injection
grep -rn 'render\|template\|format(' src/ | grep -v "test"
```

Verify:
- All SQL uses parameterized queries (never string interpolation)
- All shell commands use arrays/lists (never string concatenation)
- All user input is validated before use (type, length, format)
- File uploads are validated (type, size, content — not just extension)

### 5. Secrets Management

```bash
# Hardcoded credentials
grep -rn 'password\|secret\|api_key\|token\|credential' src/ | grep -v 'test\|mock\|example\|\.env\.example'

# Secrets in logs
grep -rn 'log\|print\|console\|logger' src/ | grep -i 'password\|secret\|token\|key'
```

Verify:
- No secrets in source code, config files committed to git, or environment variable defaults
- Secrets are loaded from environment variables or a secrets manager
- Error messages don't expose internal details (stack traces, SQL errors, file paths)
- Logs don't contain sensitive data

### 6. Rate Limiting & Abuse Prevention

- Are authentication endpoints rate-limited? (login, token refresh, password reset)
- Are expensive operations rate-limited? (search, bulk operations, file uploads)
- Is there protection against enumeration attacks? (consistent timing on login failures)

## Recording Findings

Every finding gets a memory:

```
create_memory(
  type="technical",
  content="## Security Finding: <title>\n\n**Location**: <file and line/function>\n**Severity**: Critical / High / Medium / Low\n**Issue**: <what's wrong>\n**Exploit scenario**: <how an attacker could use this>\n**Fix**: <specific remediation>\n**Status**: Needs fix / Fixed / Accepted risk",
  tags=["security", "<severity>"],
  importance=9,
  shared=true
)
```

**Severity calibration:**
| Severity | Criteria |
|----------|----------|
| Critical | Unauthenticated access to sensitive data, remote code execution, full auth bypass |
| High | Authenticated access to other users' data, privilege escalation, SQL injection |
| Medium | Information disclosure, missing rate limiting, weak session management |
| Low | Missing security headers, verbose error messages, minor configuration issues |

## Anti-Patterns

- Scanning only for the vulnerabilities you already know about — follow the checklist systematically
- Reporting "possible" vulnerabilities without tracing the actual code path to confirm
- Skipping the auth check on "internal" endpoints — if it's reachable over HTTP, it needs auth
- Not recording findings — an unfixed vulnerability forgotten is as dangerous as one never found