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

| Anti-Pattern | Why It Fails | What To Do Instead |
|---|---|---|
| **Committing without running tests** | "Small changes" cause large regressions — untested commits break CI, block other developers, and erode trust in the main branch. | Run the project's full test suite before every commit. If tests are slow, at minimum run the tests for the affected module. |
| **Skipping code review for "small" changes** | Small changes have a disproportionate error rate because reviewers (and authors) let their guard down — a one-line auth bypass is small but catastrophic. | Every change gets reviewed regardless of size. Small changes are fast to review, so there's no valid reason to skip. |
| **Not searching memory before starting work** | Without checking for past attempts, documented decisions, and known pitfalls, you repeat mistakes that are already recorded and waste time rediscovering what's known. | Run `search_memories` for the module, feature area, and related past bugs before writing any code. Prior context shapes better solutions. |
| **Bundling multiple concerns in one commit** | Mixing a bug fix with a refactor in one commit makes it impossible to isolate which change caused a regression, and makes code review ineffective. | One concern per commit. If you discover a refactoring opportunity while fixing a bug, commit the fix first, then refactor separately. |
| **Deferring memory capture to "later"** | Context evaporates rapidly — the root cause you understood perfectly while debugging will be a vague recollection by tomorrow, and the memory never gets created. | Create the memory immediately after the insight, while you're still in context. If it takes more than 30 seconds, you're over-thinking it. |