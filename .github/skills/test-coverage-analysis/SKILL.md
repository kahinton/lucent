---
name: test-coverage-analysis
description: 'Identify untested code paths, prioritize test writing, and track coverage gaps.'
---

# Test Coverage Analysis

## Before Starting

Check for previous coverage assessments:
```
search_memories(query="test coverage gaps", tags=["code-review"], limit=10)
```

## Assessment Procedure

### 1. Run Coverage

Use the project's test runner with coverage reporting:

```bash
# Identify the test runner from project config, then run with coverage
# Examples by ecosystem:
#   pytest --cov=src/ --cov-report=term-missing tests/
#   npx jest --coverage
#   go test ./... -coverprofile=coverage.out && go tool cover -func=coverage.out
#   cargo tarpaulin --out Stdout
```

If no coverage tool is configured, start with a manual audit — read the test directory and compare against the source directory.

### 2. Identify Gaps

From the coverage report, extract:
- Files with zero or near-zero coverage (completely untested)
- Functions with partial coverage (some branches untested)
- New code added without corresponding tests

Cross-reference against the source tree — look especially at:
- **API route handlers** — each endpoint should have at least a happy-path test
- **Database operations** — CRUD functions need coverage
- **Authentication/authorization paths** — every auth check must be tested (both allow and deny)
- **Error handling paths** — don't just test the happy path
- **Business logic** — core algorithms and decision functions

### 3. Prioritize

Not all untested code is equally important. Prioritize by risk:

| Priority | What to test | Why |
|----------|-------------|-----|
| **Critical** | Auth flows, access control, input validation | Security-sensitive — bugs here are exploits |
| **High** | Core business logic, data mutations, API endpoints | Correctness-sensitive — bugs here break users |
| **Medium** | Error handling, edge cases, concurrent access | Reliability-sensitive — bugs here cause incidents |
| **Lower** | Logging, metrics, admin utilities | Operational — bugs here are annoying, not dangerous |

### 4. Write Tests That Matter

**Good test characteristics:**
- Tests one specific behavior (not a grab-bag of assertions)
- Has a descriptive name that explains what's being verified
- Uses the project's existing test fixtures and patterns
- Tests both success and failure paths
- Tests boundary conditions (empty input, maximum sizes, concurrent access)
- Is fast — mocks external services, avoids unnecessary I/O

**Test the boundaries, not the internals:**
- Prefer testing public interfaces over private implementation details
- If a refactor breaks your tests but the behavior hasn't changed, your tests were too tightly coupled

### 5. Record Findings

```
create_memory(
  type="technical",
  content="## Coverage Assessment: <date>\n\n**Overall**: X% line coverage\n**Critical gaps**:\n- <module>: <what's untested and why it matters>\n**Tests added**: <count and areas>\n**Remaining gaps**: <what still needs coverage>",
  tags=["code-review", "testing"],
  importance=6,
  shared=true
)
```