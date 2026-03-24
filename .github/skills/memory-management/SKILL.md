---
name: memory-management
description: 'Maintain memory quality — update, consolidate, deduplicate, and organize. Use when memories need cleanup or tag consistency review.'
---

# Memory Maintenance

## Disambiguation

This skill is for **maintaining memory quality** — deduplication, consolidation, tag cleanup, and reorganization of existing memories. Use it when the memory store needs housekeeping.

- To decide **what to save** and how to structure new memories → use **memory-capture**
- To **find** existing memories efficiently → use **memory-search**

## Boundary

**Use memory-management** when maintaining existing memories — deduplication, consolidation, tag normalization, importance recalibration, and staleness cleanup. **Use memory-capture** when deciding what new knowledge to persist and how to structure it. Maintenance vs. creation.

## Core Rule

**Update before create.** Always search before making a new memory:
```
search_memories(query="<topic>", limit=10)
```
If a relevant memory exists, `update_memory` instead of creating a duplicate.

## Maintenance Procedure

Execute these steps in order when performing a maintenance pass:

1. **Search for duplicates** — Query overlapping topics with `search_memories(query="<topic>", limit=20)`. Flag memories with substantially similar content for consolidation.
2. **Identify stale memories** — Look for memories with outdated information (old versions, resolved goals, deprecated approaches). Check `created_at` dates and whether content still applies.
3. **Check tag consistency** — Call `get_existing_tags(limit=100)` and look for synonyms (`bug-fix` vs `bugs`), inconsistent formats, or missing standard tags. Normalize to the conventions in [Tag Conventions](#tag-conventions).
4. **Consolidate related memories** — For each group of duplicates, follow the [Consolidation Procedure](#consolidation-procedure) below: choose a keeper, merge unique details, delete redundants.
5. **Recalibrate importance scores** — Review importance ratings against the [Importance Calibration](#importance-calibration) table. Adjust scores that no longer reflect current relevance.
6. **Report what was changed** — Log a summary: how many memories consolidated, tags normalized, importance scores adjusted, and any issues found that need human review.

## When to Update vs. Create

| Situation | Action |
|-----------|--------|
| New info about an existing topic | `update_memory` — append or revise |
| Correction to previously stored content | `update_memory` — fix it |
| Progress on a tracked goal | `update_memory` — update the status |
| Refined understanding of a user | `update_memory` — update their individual memory |
| Completely new topic with no existing memory | `create_memory` |
| Different angle on existing topic (genuinely distinct) | `create_memory` — both have value |

## Consolidation Procedure

When multiple memories cover the same ground:

### 1. Identify Candidates
```
search_memories(query="<overlapping topic>", limit=20)
```

### 2. Read Full Content
```
get_memories(memory_ids=["id1", "id2", "id3"])
```

### 3. Choose the Keeper
Pick the most comprehensive or highest-importance memory.

### 4. Merge
```
update_memory(
  memory_id="<keeper_id>",
  content="<merged content with unique details from all sources>",
  importance=<reassessed importance>
)
```

### 5. Delete Redundants
```
delete_memory(memory_id="<redundant_id>")
```

### 6. Verify
```
get_memories(memory_ids=["<keeper_id>"])
```

**Only consolidate when there's clear redundancy.** Two memories about the same topic from different angles both have value — leave them.

## Tag Conventions

Before creating any tag, call `get_existing_tags(limit=100)` and reuse what exists.

**Format:** lowercase, hyphenated (`code-review`, `api-design`)

**Common tag categories:**
- Project/repo name
- Domain: `bugs`, `architecture`, `security`, `database`, `performance`
- Source: `daemon`, `daemon-result`, `needs-review`
- Workflow: `self-improvement`, `feedback-processed`, `validated`, `rejection-lesson`

## Importance Calibration

| Score | Use for |
|-------|---------|
| 9-10 | Architecture decisions, security findings, core constraints |
| 7-8 | Bug root causes, user preferences, significant technical work |
| 5-6 | Standard solutions, project context, moderate insights (default) |
| 3-4 | Minor notes, temporary context |

When updating, reassess importance. Did this turn out more or less critical than expected?

## Anti-Patterns

- Bulk-deleting without reading each memory first
- Consolidating memories that are related but cover genuinely different aspects
- Creating a new tag when a synonym already exists (`bug-fix` when `bugs` exists)
- Changing the meaning of a memory during consolidation — preserve intent