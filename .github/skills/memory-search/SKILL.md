---
name: memory-search
description: 'Find relevant past knowledge efficiently. Use when you need context about projects, decisions, or past work.'
---

# Memory Search

## Disambiguation

This skill is for **finding relevant past knowledge** — search strategies, tool selection, and query patterns. Use it when you need to retrieve context from memory.

- To decide **what to save** and how to structure new memories → use **memory-capture**
- To **clean up, deduplicate, or reorganize** existing memories → use **memory-management**

## Which Tool to Use

| Situation | Tool |
|-----------|------|
| Know specific tags to filter by | `search_memories(tags=["tag1", "tag2"], limit=10)` |
| Searching for content about a topic | `search_memories(query="topic")` |
| Topic might be in metadata or tags, not just content | `search_memories_full(query="topic")` |
| Need full text of truncated search results | `get_memories(memory_ids=["id1", "id2"])` |
| Start of conversation — always | `get_current_user_context()` |

## Search Strategies

### Starting work on a project
```
search_memories(query="<project-name>", limit=10)
search_memories(query="<project>", tags=["architecture"], limit=5)
```
Look for: past decisions, known issues, architecture context.

### Debugging a problem
```
search_memories(query="<error message or module name>", limit=5)
search_memories(query="<module> debugging", tags=["bugs"], limit=5)
```
Look for: past root causes, known failure modes.

### Making a decision
```
search_memories(query="<topic>", tags=["architecture"], limit=5)
```
Look for: past decisions on similar topics, rejected alternatives, lessons from previous approaches.

### Checking daemon activity
```
search_memories(tags=["daemon-message"], limit=10)
search_memories(tags=["daemon-result", "needs-review"], limit=10)
```

### Before creating a memory
```
search_memories(query="<topic of upcoming memory>", limit=5)
```
Always — to avoid creating duplicates.

## Rules

1. **Start broad, then narrow.** `search_memories(query="auth")` first, then `search_memories(query="auth middleware", tags=["bugs"])` if needed.
2. **Check for truncation.** If content ends with `...`, call `get_memories(memory_ids=[id])` for the full text.
3. **Combine text + tags** for precision. `search_memories(query="rate limiting", tags=["architecture"])` beats either alone.
4. **Use `search_memories_full`** for broad discovery when you're not sure what tags to filter by.
5. **Set reasonable limits.** Default is 5. Use 10-15 for broader results. Don't go excessive.
6. **Search even when you think you know.** Past context often reveals caveats and shortcuts.

## When to Search

- **Always** before starting any substantive task
- **Always** before creating a new memory
- When a topic feels familiar — trust that instinct
- When debugging something that should have been solved before
- When someone references past work or decisions

## Anti-Patterns

- Skipping search because "it's a simple task"
- Using `limit=50` on every search — floods context with noise
- Getting 0 results with an overly specific query and stopping — start broader
- Not reading truncated memories — missing critical details