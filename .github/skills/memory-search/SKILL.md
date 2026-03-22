---
name: memory-search
description: 'Find relevant past knowledge efficiently. Use when you need context about projects, decisions, or past work, when asked "what do I know about", "find previous", "recall", or when a topic feels familiar.'
---

# Memory Search — Tool Reference

## MCP Tools Used

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `memory-server-search_memories` | Content search with optional tag/type filters | `query` (optional), `tags` (list), `type`, `limit` (default 5, max 50) |
| `memory-server-search_memories_full` | Broad search across content + tags + metadata | `query` (required), `type`, `limit` |
| `memory-server-get_memories` | Get full content of specific memories (when search results were truncated) | `memory_ids` (list of UUIDs) |
| `memory-server-get_current_user_context` | Load user identity and preferences | (none) |

## Decision: Which Search Tool to Use

- IF you know specific tags → `search_memories(tags=["tag1","tag2"], limit=10)`
- ELIF searching for content about a topic → `search_memories(query="topic")`
- ELIF topic appears in metadata/tags AND content → `search_memories_full(query="topic")`
- ELIF you have memory IDs from truncated results → `get_memories(memory_ids=[...])`
- ALWAYS → `get_current_user_context()` at start of any conversation

## Search Strategies by Situation

### Starting work on a project
```
memory-server-search_memories(query="project-name", limit=10)
memory-server-search_memories(query="repo-name", tags=["architecture"], limit=5)
```
Look for: past decisions, known issues, architecture context, previous work sessions.

### Debugging a problem
```
memory-server-search_memories(query="error message or module name", limit=5)
memory-server-search_memories(query="module-name debugging", tags=["bugs"], limit=5)
```
Look for: past root causes, known failure modes, previous debugging sessions.

### Making a decision
```
memory-server-search_memories(query="topic", tags=["architecture", "decision"], limit=5)
memory-server-search_memories(query="previous-approach-name", limit=5)
```
Look for: past decisions on similar topics, rejected alternatives, lessons from previous approaches.

### Working with someone
```
memory-server-get_current_user_context()  # Always do this first
memory-server-search_memories(query="person-name", type="individual", limit=3)
```
Look for: their preferences, past interactions, working style.

### Checking what the daemon has done
```
memory-server-search_memories(tags=["daemon-message"], limit=10)
memory-server-search_memories(tags=["daemon-result", "needs-review"], limit=10)
```
Look for: unacknowledged messages, work needing review, task outcomes.

### Before creating a memory
```
memory-server-search_memories(query="topic of the memory you're about to create", limit=5)
```
Look for: existing memories to update instead of creating duplicates.

## Tips

1. **Start broad, then narrow.** `search_memories(query="auth")` first, then `search_memories(query="auth middleware cookie", tags=["bugs"])` if needed.
2. **Check for truncation.** If a memory's content ends with `...` in search results, call `get_memories(memory_ids=[id])` to get the full text.
3. **Combine text + tags.** `search_memories(query="rate limiting", tags=["architecture"])` is more precise than either alone.
4. **Use `search_memories_full` for broad discovery.** When you're not sure what tags to filter by, `search_memories_full` searches across content, tags, AND metadata.
5. **Set reasonable limits.** Default limit is 5. Use `limit=10` or `limit=15` when you need broader results. Don't go excessive.
6. **Search even when you think you know.** Past context often reveals details, caveats, or known issues that save significant time.

## When to Search

- **Always** before starting any substantive task
- **Always** before creating a new memory (avoid duplicates)
- When a topic feels familiar (trust that instinct — you probably have context)
- When debugging something that should have been solved before
- When a user references past work or decisions

## Example: Good Search Session

```
# Starting work on auth feature

1. memory-server-get_current_user_context()
   → User: Kyle, prefers concise responses

2. memory-server-search_memories(query="auth authentication lucent", limit=10)
   → Found: "Auth middleware cookie issue (2026-02-10)" — importance 8
   → Found: "RBAC implementation decisions" — importance 7

3. memory-server-get_memories(memory_ids=["abc123", "def456"])
   → Full content of both memories (they were truncated in search)

4. Now I have: known pitfalls, design decisions, specific file locations
   → Can implement without repeating past mistakes
```

## Example: Bad Search (Anti-Pattern)

```
❌ Skipping search because "it's a simple task"
❌ Using limit=50 for every search → floods context with noise
❌ Searching with overly specific query and getting 0 results → stop there
   Instead: start broad, then narrow
❌ Not reading truncated memories → missing critical details
```
