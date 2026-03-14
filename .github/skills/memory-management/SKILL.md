---
name: memory-management
description: 'Maintain memory quality by updating, consolidating, and organizing. Use when memories need cleanup, when duplicates are noticed, when consolidating related memories, or when reviewing tag consistency.'
---

# Memory Maintenance

## Core Rule: Update Before Create

**Always search before creating a new memory.** Run `search_memories(query="topic")` first. If a relevant memory exists, call `update_memory(id=..., content=...)` to add new information instead of creating a duplicate.

## When to Update vs Create

| Situation | Action |
|-----------|--------|
| New info about an existing topic | `update_memory` — append or revise the existing memory |
| Correction to what was previously stored | `update_memory` — fix the content |
| Progress on a tracked goal | `update_memory` — update status in the goal memory |
| Refined understanding of a user | `update_memory` — update their individual memory |
| Completely new topic with no existing memory | `create_memory` — new memory |
| Different angle on existing topic (genuinely distinct) | `create_memory` — both memories have value |

## Tag Conventions

### Before Creating Tags
Call `get_existing_tags()` to see what tags exist. **Reuse existing tags.** Don't create `bug-fix` if `bugs` already exists. Don't create `code-reviews` if `code-review` exists.

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

## Consolidation

When multiple memories cover the same ground and the redundancy is clear:

1. **Identify candidates**: `search_memories(query="overlapping-topic")` — look for memories that say the same thing
2. **Choose the keeper**: Pick the most comprehensive memory
3. **Merge content**: `update_memory(id=keeper_id, content=merged_content)` — fold unique details from others into the keeper
4. **Update metadata**: Ensure tags, importance, and metadata are correct on the merged memory
5. **Delete redundant ones**: `delete_memory(id=redundant_id)` — clean up after merging
6. **Verify**: `get_memory(memory_ids=[keeper_id])` — read back to confirm nothing was lost

**Only consolidate when there's clear redundancy.** Two memories about the same topic from different angles both have value — leave them.
