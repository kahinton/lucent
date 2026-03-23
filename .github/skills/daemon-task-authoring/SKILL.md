---
name: daemon-task-authoring
description: 'Guide creation of well-structured daemon tasks — clear descriptions, appropriate agent_type, priority calibration, and context that leads to high validation rates.'
---

# Daemon Task Authoring

## Creating a Request

Use the `create_request` MCP tool:

```
create_request(
  title="Short title for the work",
  description="Full instructions — everything the daemon needs to do.",
  source="user",
  priority="medium"
)
```

The daemon picks it up, creates tasks, and dispatches to the appropriate agent.

## Writing Descriptions That Work

The description is the sub-agent's **entire understanding of what to do** (combined with its agent definition). Write it as instructions for a competent engineer who has never seen the codebase.

**Good:**
> Review the test files in `tests/` and identify which core modules in the database layer lack test coverage. List specific functions that have no corresponding tests. Focus on memory operations, search, and API key management.

**Bad:**
> Improve test coverage.

### Description Checklist

- [ ] States the objective clearly (what to produce, not just what area)
- [ ] Names specific files or directories when relevant
- [ ] Defines "done" (what does the output look like?)
- [ ] Includes constraints (don't modify X, only look at Y)
- [ ] Self-contained — no references to "the thing we discussed"

## Agent Type Selection

| Agent type | Use when | Examples |
|-----------|----------|---------|
| `code` | Task edits files, runs tests, builds, or lints | Fix a bug, write tests, refactor a module |
| `research` | Task investigates, reads, and synthesizes | Compare approaches, audit a dependency, analyze patterns |
| `memory` | Task reads/writes/consolidates memories | Deduplication, tag cleanup, knowledge synthesis |
| `reflection` | Task analyzes behavior and proposes improvements | Review task outcomes, check for recurring failures |
| `documentation` | Task creates or updates documentation | Write a guide, update a README, document an API |
| `planning` | Task decomposes goals into actionable steps | Break down a feature, create a roadmap |
| `assessment` | Task discovers and profiles an environment | New workspace analysis, tool inventory |

**Rule of thumb:** If it edits files → `code`. If it reads and synthesizes → `research`. If it touches memories → `memory`.

## Priority Calibration

| Priority | When to use |
|----------|------------|
| `high` | Blocking other work, user-requested, or a bug fix |
| `medium` | Normal development work (default) |
| `low` | Cleanup, exploration, nice-to-have |

The daemon dispatches up to 2 tasks per cycle, highest priority first.

## Task Size

Tasks must complete within a single 720-second session. If a task is too large, decompose it:

### Sequential Pattern (each builds on the last)
```
Task 1 (research): "Analyze test coverage gaps in the database layer"
  → Result stored in memory
Task 2 (code): "Write tests for the gaps identified in Task 1. Search memory for the analysis results."
  → References Task 1 via memory
Task 3 (code): "Run the new tests and fix any failures."
```

### Parallel Pattern (independent tasks, same cycle)
```
Task A (code, high): "Fix the SQL injection vulnerability in the search module"
Task B (documentation, low): "Update README with the new API endpoints"
Task C (research, medium): "Investigate connection pool sizing best practices"
```

## Validation

After completion, the daemon validates task results:
- Result must be non-empty (>50 characters)
- Must not contain only error messages
- Must reference the task objective

**Common validation failures and their causes:**
| Failure | Root cause | Fix |
|---------|-----------|-----|
| Generic output | Description too vague | Be more specific about what to produce |
| Timeout | Scope too large | Decompose into smaller tasks |
| Error-only output | Wrong agent_type or missing tools | Match agent to required capabilities |
| Empty result | Agent couldn't find what was described | Verify file paths and search terms exist |

## Anti-Patterns

- **Circular tasks:** "Review the last task's output and create a new task" → infinite loop
- **Approval-dependent chains:** Task B needs Task A approved, but approval is async → B stalls
- **Overly ambitious scope:** "Refactor the entire auth system" → timeout, partial results, validation failure
- **Vague instructions:** "Make it better" → agent has no way to determine success