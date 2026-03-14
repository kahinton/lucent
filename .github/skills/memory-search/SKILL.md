---
name: memory-search
description: 'Find relevant past knowledge efficiently. Use when you need context about projects, decisions, or past work, when asked "what do I know about", "find previous", "recall", or when a topic feels familiar.'
---

# Memory Search — Tool Reference

| Tool | When to Use | Query Required? |
|------|------------|----------------|
| `search_memories(query, tags, type, limit)` | Most searches — content search with optional tag/type filters | No (but recommended) |
| `search_memories_full(query, tags, type, limit)` | Broad search across content + tags + metadata | Yes |
| `get_memory(memory_ids)` | Get full content of specific memories (when search results were truncated) | N/A — pass IDs |

# Search Strategies by Situation

## Starting work on a project
```
search_memories(query="project-name")
search_memories(query="repo-name", tags=["architecture"])
```
Look for: past decisions, known issues, architecture context, previous work sessions.

## Debugging a problem
```
search_memories(query="error message or module name")
search_memories(query="module-name debugging", tags=["bugs"])
```
Look for: past root causes, known failure modes, previous debugging sessions.

## Making a decision
```
search_memories(query="topic", tags=["architecture", "decision"])
search_memories(query="previous-approach-name")
```
Look for: past decisions on similar topics, rejected alternatives, lessons from previous approaches.

## Working with someone
```
get_current_user_context()  # Always do this first
search_memories(query="person-name", type="individual")
```
Look for: their preferences, past interactions, working style.

## Checking what the daemon has done
```
search_memories(tags=["daemon-message"])
search_memories(tags=["daemon-result", "needs-review"])
```
Look for: unacknowledged messages, work needing review, task outcomes.

## Before creating a memory
```
search_memories(query="topic of the memory you're about to create")
```
Look for: existing memories to update instead of creating duplicates.

# Tips

1. **Start broad, then narrow.** `search_memories(query="auth")` first, then `search_memories(query="auth middleware cookie", tags=["bugs"])` if needed.
2. **Check for truncation.** If a memory's content ends with `...` in search results, call `get_memory(memory_ids=[id])` to get the full text.
3. **Combine text + tags.** `search_memories(query="rate limiting", tags=["architecture"])` is more precise than either alone.
4. **Use `search_memories_full` for broad discovery.** When you're not sure what tags to filter by, `search_memories_full` searches across content, tags, AND metadata.
5. **Set reasonable limits.** Default limit is 5. Use `limit=10` or `limit=15` when you need broader results. Don't go excessive.
6. **Search even when you think you know.** Past context often reveals details, caveats, or known issues that save significant time.

# When to Search

- **Always** before starting any substantive task
- **Always** before creating a new memory (avoid duplicates)
- When a topic feels familiar (trust that instinct — you probably have context)
- When debugging something that should have been solved before
- When a user references past work or decisions
