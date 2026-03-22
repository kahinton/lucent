---
name: memory-management
description: 'Maintain memory quality by updating, consolidating, and organizing. Use when memories need cleanup, when duplicates are noticed, when consolidating related memories, or when reviewing tag consistency.'
---

# Memory Maintenance

## MCP Tools Used

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `memory-server-search_memories` | Find memories to consolidate or update | `query="topic"`, `limit=20` |
| `memory-server-get_memories` | Get full content of multiple memories at once | `memory_ids` (list) |
| `memory-server-get_existing_tags` | Audit tag consistency | `limit=100` |
| `memory-server-update_memory` | Add new info to existing memory | `memory_id`, `content`, `tags`, `importance` |
| `memory-server-delete_memory` | Remove redundant memories after merging | `memory_id` |
| `memory-server-get_memory_versions` | Check history before modifying | `memory_id`, `limit=5` |

## Core Rule: Update Before Create

**Always search before creating a new memory.** Run:
```
memory-server-search_memories(query="topic", limit=10)
```

If a relevant memory exists, call `update_memory` to add new information instead of creating a duplicate.

## When to Update vs Create

| Situation | Action |
|-----------|--------|
| New info about an existing topic | `update_memory` — append or revise the existing memory |
| Correction to what was previously stored | `update_memory` — fix the content |
| Progress on a tracked goal | `update_memory` — update status in the goal memory |
| Refined understanding of a user | `update_memory` — update their individual memory |
| Completely new topic with no existing memory | `create_memory` — new memory |
| Different angle on existing topic (genuinely distinct) | `create_memory` — both memories have value |

## Procedure: Consolidation

When multiple memories cover the same ground:

1. **Identify candidates**:
   ```
   memory-server-search_memories(query="overlapping-topic", limit=20)
   ```
   Look for memories that say the same thing.

2. **Get full content**:
   ```
   memory-server-get_memories(memory_ids=["id1", "id2", "id3"])
   ```

3. **Choose the keeper**: Pick the most comprehensive or highest-importance memory.

4. **Merge content**:
   ```
   memory-server-update_memory(
     memory_id="keeper_id",
     content="<merged content with unique details from all>",
     importance=<reassessed importance>
   )
   ```

5. **Delete redundant ones**:
   ```
   memory-server-delete_memory(memory_id="redundant_id_1")
   memory-server-delete_memory(memory_id="redundant_id_2")
   ```

6. **Verify**: Read back to confirm nothing was lost:
   ```
   memory-server-get_memories(memory_ids=["keeper_id"])
   ```

**Only consolidate when there's clear redundancy.** Two memories about the same topic from different angles both have value — leave them.

## Tag Conventions

### Before Creating Tags
Call `memory-server-get_existing_tags(limit=100)` to see what tags exist. **Reuse existing tags.** Don't create `bug-fix` if `bugs` already exists. Don't create `code-reviews` if `code-review` exists.

### Tag Format
- Lowercase, hyphenated: `code-review`, `api-design`, `lucent`
- Project tags: `lucent`, `<repo-name>`
- Type tags: `bugs`, `architecture`, `security`, `lesson`
- Source tags: `daemon`, `daemon-result`, `needs-review`
- Workflow tags: `self-improvement`, `feedback-processed`, `validated`

## Metadata

### Technical memories
```json
{"repo": "lucent", "category": "architecture", "references": ["src/lucent/llm/engine.py"]}
```

### Experience memories
```json
{"repo": "lucent", "date": "2026-03-14", "context": "LLM engine abstraction implementation"}
```

## Importance Calibration

| Score | Use for |
|-------|---------|
| 9-10 | Architecture decisions, security findings, core constraints (rarely — most things aren't this critical) |
| 7-8 | Significant technical work, bug root causes, user preferences, lessons learned |
| 5-6 | Standard solutions, project context, moderate insights (this is the default range) |
| 3-4 | Minor notes, temporary context |

**When updating, reassess importance.** Did this turn out more or less critical than expected? Adjust accordingly.

## Example: Good Consolidation

```
1. memory-server-search_memories(query="asyncpg connection", limit=15)
   → Found 3 memories about asyncpg connection pool issues
   → Two are similar, one is older and superseded

2. memory-server-get_memories(memory_ids=["abc", "def", "ghi"])
   → Full content of all three

3. memory-server-update_memory(
     memory_id="abc",  # most comprehensive one
     content="<merged content combining all three unique details>",
     importance=8
   )

4. memory-server-delete_memory(memory_id="def")  # redundant
5. memory-server-delete_memory(memory_id="ghi")  # superseded

6. memory-server-get_memories(memory_ids=["abc"])
   → Verify merged content looks correct
```
