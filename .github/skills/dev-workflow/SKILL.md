---
name: dev-workflow
description: 'Standard development workflow — code, test, review cycle with memory integration at every step. Use when implementing code changes, running test/review cycles, or following the code-test-commit workflow.'
---

# Development Workflow

The discipline of making changes that work, are tested, and leave a trail of knowledge for next time.

## Before Starting Any Task

```
search_memories(query="<module or feature area>", limit=10)
search_memories(query="<related past bugs or decisions>", tags=["validated"], limit=5)
```

If a previous attempt at this task exists in memory, read the failure reason before repeating the approach.

## The Cycle

### 1. Understand

Read the code you're about to change. Not just the function — the callers, the tests, the imports. Map the blast radius of your change before you make it.

Identify:
- The project's language, framework, and conventions (check build config, linter config, existing code style)
- The test runner and how tests are organized
- Whether the change affects runtime behavior (needs container restart) or is purely structural

### 2. Implement

One concern per commit. Match the project's conventions exactly — naming, formatting, error handling patterns, import ordering. If you're unsure of a convention, look at three existing files for consensus.

**Rules that apply regardless of language:**
- Don't change code you didn't need to change
- Don't add comments to functions you didn't modify
- Handle errors explicitly — never silently swallow them
- If adding a dependency, check whether an existing one already covers the need

### 3. Validate

Run tests — always. Use the project's test runner (not a guess):

```bash
# Check the project root for how tests are run
# Look for: Makefile, package.json scripts, pyproject.toml, Cargo.toml, go.mod
# Then run accordingly
```

Run the linter if the project has one configured. Fix what it flags before moving on.

If the change affects runtime behavior, verify in the running environment:
```bash
# Restart the relevant service
# Check logs for errors
# Hit the affected endpoint or trigger the affected behavior
```

### 4. Capture

If you learned something non-obvious, fixed a tricky bug, or made a design decision — save it now, while the context is fresh:

```
create_memory(
  type="experience",
  content="## <What happened>\n\n**Root cause**: ...\n**Fix**: ...\n**Lesson**: ...",
  tags=["<relevant-tags>"],
  importance=7,
  shared=true
)
```

**What to capture:** bug root causes, architectural decisions, surprising behavior, gotchas.
**What to skip:** routine changes, obvious fixes, one-off formatting.

## Common Gotchas

- **Volume-mounted source:** If code changes aren't reflected in a container, the volume cache may be stale — restart the service.
- **Test isolation:** Tests that share state can pass individually but fail in suite. Run both ways.
- **Dependency changes:** Adding or updating dependencies usually requires a container rebuild, not just a restart.
- **Import context:** If the project runs from a specific working directory or module path, your imports must work from that context.

## Anti-Patterns

| Pattern | Problem |
|---------|---------|
| Skip memory search before starting | You repeat a mistake that's already documented |
| Touch 5 unrelated modules in one change | Impossible to review, easy to introduce regressions |
| "I'll save the memory later" | Context is lost, the memory never gets created |
| Skip tests because "it's a small change" | Small changes cause large regressions |
| Refactor while fixing a bug | Two concerns in one change — if the refactor breaks something, you can't tell which change caused it |