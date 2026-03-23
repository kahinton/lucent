---
name: code-review
description: 'Structured code review process — correctness, security, performance, and conventions.'
---

# Code Review

## Before Reviewing

Load context for the area being changed:
```
search_memories(query="<module or feature area> code-review", tags=["code-review"], limit=5)
search_memories(query="<module> bugs security", limit=5)
```

Past review findings, known issues, and previous bug patterns are critical context. Don't review blind.

## Review Sequence

Execute these checks in order. Each is a distinct pass — don't try to check everything at once.

### Pass 1: Understand the Change

1. Read the description — what is this change supposed to do?
2. Identify every file changed and categorize: new feature, bug fix, refactor, config change
3. Check if corresponding tests exist for new or changed behavior
4. If the change touches an interface boundary (API, public function signature, schema), flag it for extra scrutiny

### Pass 2: Correctness

For each changed function or block:

1. **Does it do what the description claims?** Trace the logic path.
2. **Edge cases:** What happens with null/empty input? Boundary values? Concurrent access?
3. **Error handling:** Are errors caught, logged, and propagated appropriately? Are resources cleaned up on failure?
4. **State management:** Does the change leave the system in a consistent state if interrupted? Are transactions used where needed?

### Pass 3: Security

1. **Input validation:** Is all external input validated before use? (HTTP params, user input, file contents, environment variables)
2. **Injection:** Any string interpolation in SQL, shell commands, or template rendering? (Must use parameterized queries/commands)
3. **Secrets:** Any hardcoded credentials, API keys, or tokens? Any secrets logged or returned in error messages?
4. **Auth/authz:** Are endpoints properly protected? Does the change respect existing access control patterns?
5. **Data exposure:** Could internal details leak through error messages, logs, or API responses?

### Pass 4: Performance

1. **Algorithmic complexity:** Any O(n²) or worse where O(n) is possible? Any unbounded loops?
2. **Resource management:** Are connections, file handles, and locks properly acquired and released? Any potential for leaks?
3. **Query efficiency:** N+1 queries? Missing indexes on new columns? Full table scans?
4. **Unnecessary work:** Repeated computations? Data fetched but not used?

### Pass 5: Conventions

1. Run the project's linter if available — don't manually check what a tool can catch
2. Does the change follow the project's existing patterns? (naming, error handling, file organization)
3. Are new public interfaces documented?
4. Are imports organized per project convention?

## Verdict

| Condition | Verdict |
|-----------|---------|
| Security vulnerability found | **Request changes** — block merge until fixed |
| Logic error that breaks functionality | **Request changes** |
| Missing tests for new behavior | **Request changes** — unless explicitly out of scope |
| Style issues only (no logic/security problems) | **Approve** with suggestions |
| Design tradeoff with no clear right answer | **Needs discussion** — explain the tradeoff |
| Clean change, well-tested | **Approve** |

## Recording Findings

If you find something worth remembering — a security pattern, a recurring mistake, a non-obvious invariant — save it:

```
create_memory(
  type="technical",
  content="## Review Finding: <module>\n\n**Issue**: <what was wrong>\n**Why it matters**: <impact>\n**Fix**: <what should be done>\n**Pattern**: <reusable lesson>",
  tags=["code-review", "<category>"],
  importance=7,
  shared=true
)
```

Categories: `security`, `performance`, `correctness`, `architecture`

## Anti-Patterns

- "LGTM" without reading the code
- Rejecting for style issues a linter would auto-fix
- Reviewing without checking memory for past findings in this area
- Finding a critical issue but not saving it — the same mistake will happen again
- Being vague: "this could be improved" — say specifically what and how