---
name: daemon-task-authoring
description: 'Guide creation of well-structured daemon tasks — clear descriptions, appropriate agent_type, priority calibration, and context fields that lead to high validation rates'
---

# Daemon Task Authoring

How to create daemon tasks that get picked up, executed successfully, and pass validation.

## When to Use

- User asks to create work for the daemon
- Scheduling recurring tasks
- Submitting requests for agent creation, code changes, research, etc.

## How to Create a Request

**Use the `create_request` MCP tool.** This is a single call:

```
create_request(
  title="Short title for the work",
  description="Full instructions for the daemon — everything it needs to do.",
  source="user",
  priority="medium"
)
```

That's it. The daemon picks it up, creates tasks, and dispatches to the appropriate agent.

## Key Fields

| Field | Required | Notes |
|-------|----------|-------|
| `title` | Yes | Short label (1-256 chars) |
| `description` | Yes | Full instructions. Must be self-contained. This is what the daemon reads. |
| `source` | No | `"user"` (default), `"cognitive"`, `"api"`, `"schedule"` |
| `priority` | No | `"low"`, `"medium"` (default), `"high"`, `"urgent"` |

## Writing Good Descriptions

The description is the sub-agent's **entire prompt context** (plus its agent definition). Write it like instructions for a competent engineer who has never seen the codebase.

**Good description:**
> Review the test files in `tests/` and identify which core modules in the database layer lack test coverage. List specific functions/methods that have no corresponding tests. Focus on memory operations, search, and API key management.

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

## Multi-Step Task Decomposition

When a task is too large for a single 720s session, decompose it:

### Pattern: Sequential Tasks with Dependencies

Create tasks that build on each other's results:

```
Task 1 (research): "Analyze test coverage gaps in the database layer"
  → Result stored in memory
Task 2 (code): "Write tests for the gaps identified in memory [ID]. Focus on memory CRUD operations."
  → References Task 1's result
Task 3 (code): "Run the new tests from Task 2 and fix any failures."
```

Each task should be independently executable — reference prior results by memory ID or by describing what to search for.

### Pattern: Parallel Independent Tasks

For work that doesn't depend on ordering:

```
Task A (code, high): "Fix the SQL injection vulnerability in search.py"
Task B (documentation, low): "Update README.md with the new API endpoints"
Task C (research, medium): "Investigate asyncpg connection pool sizing best practices"
```

These can be dispatched in the same cycle (up to `MAX_CONCURRENT_SESSIONS`).

## Memory-Interacting Tasks

Tasks that read/write memories need special care:

### Reading Memories
```
"Search for memories tagged 'architecture' and synthesize a summary of the current system design. Create a new memory tagged 'architecture-summary' with the result."
```

**Important**: The sub-agent uses MCP tools, not the REST API. Reference memory operations by their tool names: `search_memories`, `create_memory`, `get_memory`, etc.

### Writing Memories
```
"After completing the analysis, save the findings as a new memory with type 'technical', tags ['test-coverage', 'analysis', 'daemon'], importance 7, and shared=true."
```

Be explicit about memory type, tags, importance, and shared — the sub-agent won't infer good values.

## Additional Anti-Patterns

1. **Circular tasks**: "Review the last task's output and create a new task" → infinite loop
2. **Approval-dependent chains**: Task B needs Task A approved first, but approval is async → B sits pending indefinitely
3. **Environment-dependent tasks**: "Read the .env file" → the sub-agent runs in a Docker container, not the host
4. **Overly ambitious scope**: "Refactor the entire auth system" → will timeout, produce partial results that fail validation
```json
{
  "description": "Run `ruff check src/` and fix any auto-fixable lint errors. Then run `python -m pytest tests/ -x` to verify nothing breaks. Report the number of fixes applied and test results.",
  "agent_type": "code",
  "priority": "medium"
}
```

### Good Task: Research
```json
{
  "description": "Search the codebase for all places where `asyncpg` pool connections are acquired but not properly released. Check for missing `async with` patterns in the database layer. List any connection leak risks found.",
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
