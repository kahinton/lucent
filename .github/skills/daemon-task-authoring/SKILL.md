---
name: daemon-task-authoring
description: 'Guide creation of well-structured daemon tasks — clear descriptions, appropriate agent_type, priority calibration, and context fields that lead to high validation rates'
---

# Daemon Task Authoring

How to create daemon tasks that get picked up, executed successfully, and pass validation.

## When to Use

- Creating daemon tasks via API (`POST /api/daemon/tasks`) or memory
- Reviewing why tasks failed validation or produced poor results
- Calibrating task priority for the cognitive cycle queue

## Task Structure

A daemon task is a memory with tags `["daemon-task", "pending", "<agent_type>"]` and these fields:

| Field | Required | Notes |
|-------|----------|-------|
| `description` | Yes | The full instructions for the sub-agent. Must be self-contained. |
| `agent_type` | Yes | One of: `research`, `code`, `memory`, `reflection`, `documentation`, `planning` |
| `priority` | Yes | `low`, `medium`, or `high` — affects dispatch order |
| `context` | No | Additional context (file paths, error messages, prior results) |
| `tags` | No | Extra tags for categorization beyond the auto-added ones |

## Writing Good Descriptions

The description is the sub-agent's **entire prompt context** (plus its agent definition). Write it like instructions for a competent engineer who has never seen the codebase.

**Good description:**
> Review the test files in `tests/` and identify which core modules in `src/lucent/db/` lack test coverage. List specific functions/methods that have no corresponding tests. Focus on `memory.py`, `search.py`, and `api_key.py`.

**Bad description:**
> Improve test coverage.

### Description Checklist

- [ ] States the objective clearly (what to produce, not just what area)
- [ ] Names specific files or directories when relevant
- [ ] Defines success criteria (what does "done" look like?)
- [ ] Includes constraints (don't modify X, only look at Y)
- [ ] Self-contained — no references to "the thing we discussed"

## Agent Type Selection

| Agent Type | Use For | Tools Available |
|-----------|---------|-----------------|
| `code` | File editing, testing, building, linting | All CLI + file tools |
| `research` | Investigation, web lookups, synthesis | Web + search tools |
| `memory` | Memory cleanup, consolidation, tagging | Memory tools |
| `reflection` | Self-analysis, behavioral review, planning | Memory + search |
| `documentation` | Docs, guides, READMEs | File + search tools |
| `planning` | Goal decomposition, roadmaps, task breakdown | Memory + search |

**Rule of thumb**: If the task edits files, it's `code`. If it reads and synthesizes, it's `research`. If it touches memories, it's `memory`.

## Priority Calibration

| Priority | When to Use | Dispatch Behavior |
|----------|------------|-------------------|
| `high` | Blocking other work, user-requested, bug fixes | Dispatched first |
| `medium` | Normal development work, improvements | Default queue order |
| `low` | Nice-to-have, cleanup, exploration | Dispatched when queue is empty |

The cognitive cycle dispatches up to 2 tasks per cycle, highest priority first.

## Validation

After a sub-agent completes, the daemon validates the result (`_validate_task_result()`):

- Result must be non-empty (>50 chars)
- Must not contain only error messages
- Must reference the task objective

**Tasks fail validation when:**
1. Description was too vague → agent produced generic output
2. Task required tools the agent_type doesn't have
3. Task was too large for a single session (>720s timeout)

## Examples

### Good Task: Code Analysis
```json
{
  "description": "Run `ruff check src/lucent/` and fix any auto-fixable lint errors. Then run `python -m pytest tests/ -x` to verify nothing breaks. Report the number of fixes applied and test results.",
  "agent_type": "code",
  "priority": "medium"
}
```

### Good Task: Research
```json
{
  "description": "Search the codebase for all places where `asyncpg` pool connections are acquired but not properly released. Check for missing `async with` patterns in `src/lucent/db/`. List any connection leak risks found.",
  "agent_type": "research",
  "priority": "high",
  "context": "We've seen occasional 'too many connections' errors in production logs."
}
```

### Good Task: Memory Maintenance
```json
{
  "description": "Search for memories tagged 'daemon-heartbeat' older than 24 hours and delete them. Then search for duplicate memories with the same content (>90% similarity) and consolidate them. Report what was cleaned up.",
  "agent_type": "memory",
  "priority": "low"
}
```
