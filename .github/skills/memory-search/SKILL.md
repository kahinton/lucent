---
name: memory-search
description: 'Find relevant past knowledge efficiently. Use when you need context about projects, decisions, or past work, when asked "what do I know about", "find previous", "recall", or when a topic feels familiar.'
---

# Memory Search Tools

| Tool | Use When |
|------|----------|
| `search_memories` | Content search with optional filters (tags, date range, memory_ids) — query is optional |
| `search_memories_full` | Broad search across content, tags, and metadata — query is required |
| `get_memory` | Search results were truncated, need full content |

# Search Strategies

## By Project
```
search_memories("project-name") or search_memories("repo-name")
```

## By Problem Domain
```
search_memories("authentication bug")
search_memories("rate limiting")
```

## By Decision Type
```
search_memories with tags: ["architecture", "decision"]
search_memories with tags: ["api-design"]
```

## By Person (for team context)
```
search_memories_full with type: "individual"
```

# Tips

1. **Start broad, narrow down** - Generic search first, then add specificity
2. **Check truncation** - If content ends with `...`, call `get_memory(id)` for full text
3. **Use tags for precision** - `search_memories_full` with specific tags beats broad text search
4. **Combine approaches** - Text search + tag filter for best results

# When to Search

- Starting work on a project you've touched before
- Debugging something that feels familiar
- Making a decision that might have precedent
- Before creating a memory (avoid duplicates)
