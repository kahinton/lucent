---
name: test-coverage-analysis
description: 'Identify untested code paths, prioritize test writing, track coverage metrics across the Lucent codebase'
---

# Test Coverage Analysis — Lucent Project

## Test Infrastructure

| Tool | Command | Config |
|------|---------|--------|
| pytest | `.venv/bin/pytest tests/ -q --tb=short` | `pyproject.toml` `[tool.pytest.ini_options]` |
| pytest-asyncio | Async test support | `asyncio_mode = "auto"` |
| ruff | `.venv/bin/ruff check .` | `pyproject.toml` `[tool.ruff]` |
| coverage | `.venv/bin/pytest --cov=src/lucent tests/` | Optional — install `pytest-cov` |

## Test File Layout

Tests live in `tests/` and mirror the source structure:
```
tests/
  conftest.py          # Shared fixtures (db_pool, test users, etc.)
  test_auth.py         # Authentication tests
  test_rbac.py         # RBAC and role checks
  test_server.py       # MCP server and tools
  test_memories_api.py # Memory CRUD API
  test_search_api.py   # Search endpoint tests
  test_llm_engine.py   # LLM engine abstraction
  test_daemon_api.py   # Daemon API endpoints
  ...
```

## How to Assess Coverage

### Step 1: Run tests and check current count
```bash
pytest tests/ -q --tb=short  # Quick pass/fail count
```

### Step 2: Identify coverage gaps
Run with coverage report:
```bash
pytest --cov=src/ --cov-report=term-missing tests/
```

Look for files with low coverage, especially:
- API route handlers — each router should have a corresponding test file
- Database repositories — CRUD operations need coverage
- Tool implementations — public-facing tools and integrations
- Core business logic — the abstraction and service layers

### Step 3: Prioritize what to test

**High priority** (security/correctness critical):
1. Auth flows — login, session validation, API key auth, token expiry
2. RBAC — role checks on every endpoint, org isolation
3. Memory operations — CRUD, search, access control, org scoping
4. Input validation — malformed requests, injection attempts

**Medium priority** (functional correctness):
5. MCP tools — each tool's happy path and error cases
6. Database repositories — edge cases, concurrent access
7. LLM engine — factory selection, error handling, timeout behavior

**Lower priority** (operational):
8. Rate limiting behavior
9. Logging and audit trails
10. Mode/license checks

### Step 4: Write tests that matter

Good tests for any project:
- **Use the shared fixtures** in `conftest.py` — reuse database pools, test users, async clients
- **Test auth boundaries** — verify endpoints reject unauthenticated requests
- **Test org/tenant isolation** — verify user A can't see user B's data
- **Test error paths** — not just happy paths
- **Keep tests fast** — mock external services, use in-memory where possible

## Current Test State

Check `search_memories(tags=["code-review", "test-coverage"])` for previous coverage assessments and known gaps.

- Reviewing pull requests or code changes
- Evaluating code quality during development
- Performing security-focused code review
- Checking for python-specific anti-patterns

## Review Process

### Step 1: Understand the Change

1. Read the PR description or task context
2. Identify what files changed and why
3. Check if there are related tests

### Step 2: Check Correctness

1. Does the code do what it claims to do?
2. Are edge cases handled?
3. Are error paths covered?
4. Is the logic sound?

### Step 3: Check Style & Conventions

1. Run `ruff check` if available
2. Verify type hints are present and correct
3. Check docstrings for public APIs
4. Ensure imports are organized (stdlib → third-party → local)
5. Verify `pyproject.toml` conventions are followed

### Step 4: Check Security

1. No hardcoded secrets or credentials
2. Input validation on external data
3. Proper authentication/authorization checks
4. SQL injection, XSS, or injection vulnerabilities
5. Proper error messages (no internal details leaked)

### Step 5: Check Performance

1. No obvious O(n²) or worse algorithms where O(n) is possible
2. No unnecessary database queries or API calls
3. Proper resource management (connections, files, memory)
4. Caching where appropriate

### Step 6: Summarize

Output a structured review:
- **Verdict**: approve / request-changes / needs-discussion
- **Critical issues**: Things that must be fixed
- **Suggestions**: Things that could be improved
- **Positive notes**: What was done well

## Best Practices

- Focus on logic and correctness over style (linters handle style)
- Be specific: "this loop is O(n²) because X" not "performance concern"
- Suggest alternatives, don't just point out problems
- Acknowledge good patterns when you see them
- Check for test coverage on new code paths
