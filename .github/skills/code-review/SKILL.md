---
name: code-review
description: 'Structured code review process'
---

# Code Review

Code review skill for python/fastapi projects.

## When to Use

- Reviewing pull requests or code changes
- Evaluating code quality during development
- Performing security-focused code review
- Checking for python-specific anti-patterns

## MCP Tools Used

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `memory-server-search_memories` | Load past review findings for this module | `query="module-name code-review"`, `tags=["code-review"]` |
| `memory-server-create_memory` | Save significant review findings | `type="technical"`, `tags=["code-review"]`, `importance=7-8` |
| `memory-server-search_memories` | Check for known issues before reviewing | `query="security auth bugs [module]"`, `tags=["security","bugs"]` |

## Procedure: Full Code Review

### Step 1: Load Context

```
memory-server-search_memories(query="[module or PR area] code-review", tags=["code-review"], limit=5)
```

Check for: past review findings, known issues, patterns that have caused bugs before.

### Step 2: Understand the Change

1. Read the PR description or task context
2. Identify what files changed and why
3. Check if there are related tests

### Step 3: Check Correctness

1. Does the code do what it claims to do?
2. Are edge cases handled?
3. Are error paths covered?
4. Is the logic sound?

### Step 4: Check Style & Conventions

1. Run `ruff check` if available
2. Verify type hints are present and correct
3. Check docstrings for public APIs
4. Ensure imports are organized (stdlib → third-party → local)
5. Verify `pyproject.toml` conventions are followed

### Step 5: Check Security

1. No hardcoded secrets or credentials
2. Input validation on external data
3. Proper authentication/authorization checks
4. SQL injection, XSS, or injection vulnerabilities
5. Proper error messages (no internal details leaked)

### Step 6: Check Performance

1. No obvious O(n²) or worse algorithms where O(n) is possible
2. No unnecessary database queries or API calls
3. Proper resource management (connections, files, memory)
4. Caching where appropriate

### Step 7: Summarize and Save

Output a structured review:
- **Verdict**: approve / request-changes / needs-discussion
- **Critical issues**: Things that must be fixed
- **Suggestions**: Things that could be improved
- **Positive notes**: What was done well

If you found critical issues or patterns worth remembering:
```
memory-server-create_memory(
  type="technical",
  content="## Code Review Finding: [module]\n\n**Issue**: ...\n**Why it matters**: ...\n**Fix**: ...",
  tags=["code-review", "security"],  # or "bugs", "performance"
  importance=7,
  shared=true
)
```

## Decision: Verdict

- IF critical security vulnerability found → **request-changes**, add `security` tag to memory
- ELIF logic error that breaks core functionality → **request-changes**
- ELIF style violations only (no logic issues) → **approve** with suggestions, run `ruff` to verify
- ELIF needs architectural discussion → **needs-discussion**, explain tradeoff
- ELSE → **approve**

## Best Practices

- Focus on logic and correctness over style (linters handle style)
- Be specific: "this loop is O(n²) because X" not "performance concern"
- Suggest alternatives, don't just point out problems
- Acknowledge good patterns when you see them
- Check for test coverage on new code paths

## Example: Good Review Finding (Worth Saving)

```
# Critical issue found in auth.py — save as memory

memory-server-create_memory(
  type="technical",
  content="## Auth bypass risk in get_current_user\n\n**Issue**: Line 47 returns None instead of raising 401 when token is invalid. This allows unauthenticated access to any endpoint that checks `if user` instead of using the Depends().\n**Fix**: Raise HTTPException(401) instead of returning None.\n**Pattern**: Always use Depends(get_current_user) — never call it directly and check the return value.",
  tags=["code-review", "security", "auth"],
  importance=9,
  shared=true
)
```

## Example: Bad Review (Anti-Pattern)

```
❌ "This code could be improved" — not specific
❌ Rejecting for style issues that ruff would auto-fix
❌ Reviewing without loading past findings for this module
❌ Finding a critical issue but not saving it to memory
```
