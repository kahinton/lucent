---
name: daemon-task-authoring-specialized
description: 'DEPRECATED — content merged into daemon-task-authoring. Use daemon-task-authoring instead.'
---

> **Deprecated**: This skill has been merged into `daemon-task-authoring`. All content below is preserved for reference but `daemon-task-authoring` is the canonical version.

# Daemon Task Authoring — Specialized

Advanced patterns for authoring daemon tasks in the Lucent architecture. Builds on the base `daemon-task-authoring` skill with Lucent-specific knowledge.

## When to Use

- Tasks that failed validation and need redesign
- Multi-step workflows that need decomposition into sequential tasks
- Tuning task descriptions for higher sub-agent success rates
- Creating tasks that interact with the memory system

## Lucent Task Lifecycle

```
Request created → Daemon picks up → Tasks created → Agent dispatched → Validated → Completed/Failed
```

### How It Works

1. **`create_request` MCP tool** — single call creates a request in the pending queue
2. **Daemon cognitive cycle** — picks up pending requests, creates tasks, assigns agents
3. **Agent dispatch** — sub-agent runs with the agent definition + task description as prompt
4. **Validation** — daemon checks result quality, marks completed or failed
5. **Review** — results appear in Review Queue for human approval

## Sub-Agent System Prompts

Each agent type gets its prompt from the agent definition stored in the database (manageable via the Agents & Skills page). The task description is appended to this prompt.

Agent types are dynamic — they match the `name` field of active agent definitions. Check currently available agents at Agents & Skills in the web UI or `GET /api/definitions/agents?status=active`.

Common built-in agent types include: `code-review`, `testing`, `security`, `documentation`, `deployment`, `monitoring`, `triage`, `data-analysis`, `knowledge-base`, and many more.

When creating a request, you don't need to specify an agent_type — the daemon's cognitive cycle will analyze the request and assign appropriate agents automatically.

## Multi-Step Task Decomposition

When a task is too large for a single 720s session:

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

## Validation Tuning

### Why Tasks Fail Validation

| Failure Mode | Root Cause | Fix |
|-------------|-----------|-----|
| Result too short (<50 chars) | Agent couldn't do the work | Add more context, simplify scope |
| Only error messages | Wrong agent_type or missing tools | Check agent capabilities |
| No objective reference | Vague description | Be specific about expected output format |
| Timeout (720s) | Task too large | Decompose into subtasks |

### Writing Validation-Friendly Descriptions

Include explicit output format expectations:

> **Good**: "List each function lacking tests in a markdown table with columns: function name, file, reason it needs tests."

> **Bad**: "Check what needs testing."

The validation function looks for substantive content — formatted output with specifics passes more reliably than prose summaries.

## Memory-Interacting Tasks

Tasks that read/write memories need special care:

### Reading Memories
```
"Search for memories tagged 'architecture' and synthesize a summary of the current system design. Create a new memory tagged 'architecture-summary' with the result."
```

**Important**: The sub-agent uses MCP tools, not the REST API. Reference memory operations by their tool names: `search_memories`, `create_memory`, `get_memory`, etc.

### Writing Memories
```
"After completing the analysis, save the findings as a new memory with type 'technical', tags ['test-coverage', 'analysis', 'daemon'], and importance 0.7."
```

Be explicit about memory type, tags, and importance — the sub-agent won't infer good values.

## Anti-Patterns

1. **Circular tasks**: "Review the last task's output and create a new task" → infinite loop
2. **Approval-dependent chains**: Task B needs Task A approved first, but approval is async → B sits pending indefinitely
3. **Environment-dependent tasks**: "Read the .env file" → the sub-agent runs in a Docker container, not the host
4. **Overly ambitious scope**: "Refactor the entire auth system" → will timeout, produce partial results that fail validation
