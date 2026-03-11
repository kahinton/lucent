---
name: test-coverage-analysis
description: 'Identify untested code paths, prioritize test writing, track coverage metrics across the 27K-line codebase'
---

# Test Coverage Analysis

Code review skill for python/fastapi projects.

## When to Use

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
