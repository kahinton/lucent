---
name: test-coverage-analysis
description: 'Identify untested code paths, prioritize test writing, and track coverage gaps. Use when assessing test coverage gaps, prioritizing test writing, or evaluating test suite health.'
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

## Anti-Patterns

| Anti-Pattern | Why It Fails | What To Do Instead |
|---|---|---|
| **Chasing line coverage percentage as a goal** | 90% line coverage with meaningless assertions is worse than 60% with tests that catch real regressions — high numbers create false confidence that masks untested critical paths. | Treat coverage as a diagnostic signal, not a target. Focus on whether critical paths (auth, data mutation, business logic) have meaningful assertions. |
| **Ignoring branch coverage in favor of line coverage** | A function can show 100% line coverage with only the happy path tested — untested `if/else` branches and error handlers hide the bugs that actually reach production. | Check branch coverage alongside line coverage. Every conditional and error path should have at least one test exercising it. |
| **Testing implementation details instead of behavior** | Tests coupled to internal structure (private methods, specific call sequences) break on every refactor even when behavior is unchanged, creating maintenance burden and noisy CI. | Test public interfaces and observable behavior. If a refactor breaks your test but not the functionality, the test was wrong. |
| **Ignoring error and exception paths** | Happy-path-only testing means the first time your error handling runs is in production — when it fails, you get cascading failures instead of graceful degradation. | For every operation that can fail (I/O, network, parsing, auth), write a test that triggers the failure and asserts the error is handled correctly. |
| **Writing assertion-free "smoke" tests** | A test that calls code without asserting on the result only verifies "it didn't throw" — it catches almost nothing and inflates coverage numbers with false confidence. | Every test must assert a specific expected outcome. If you can't state what the test verifies, it isn't a test. |

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

## Anti-Patterns

| Anti-Pattern | Why It Fails | What To Do Instead |
|---|---|---|
| **Chasing line coverage percentage over branch coverage** | 95% line coverage can still miss every error path and every `else` branch. Line coverage rewards touching code, not testing it. | Measure branch coverage (`--cov-branch`). Prioritize covering conditional branches, especially error handling and edge cases. |
| **Writing tests that mirror implementation instead of behavior** | Tests break on every refactor even when behavior is unchanged, creating maintenance burden and false negatives. | Test public interfaces and observable behavior. Ask "what should happen?" not "what does the code do?" |
| **Ignoring error paths and exception handling** | Happy-path-only tests miss the code that runs during failures — exactly when correctness matters most. | For every function, write at least one test for each documented exception and one for invalid input. |
| **Adding tests without checking what's already covered** | Duplicate tests inflate count without improving coverage. New tests may cover the same paths as existing ones. | Run coverage *before* writing tests. Identify the specific uncovered lines/branches, then target those. |
| **Treating coverage as a gate instead of a guide** | Teams game coverage metrics with trivial assertions that technically cover lines but verify nothing meaningful. | Review test *quality* alongside coverage numbers. A test that asserts nothing is worse than no test — it gives false confidence. |