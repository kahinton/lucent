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

1. **Survey the knowledge base** — Start broad. Call `search_memories(query="<topic>", limit=50)` across the major domains you know about (architecture, bugs, preferences, projects, etc.). The goal is to see the full landscape, not just recent entries.
2. **Connect new to old** — For each recent memory (last 24-48 hours), search for older memories on the same topic. New observations should strengthen existing long-term knowledge, not sit in isolation. A fresh bug insight should merge into the established understanding of that system area.
3. **Identify stale memories** — Look for memories with outdated information (old versions, resolved goals, deprecated approaches). Check `created_at` dates and whether content still applies.
4. **Check tag consistency** — Call `get_existing_tags(limit=100)` and look for synonyms (`bug-fix` vs `bugs`), inconsistent formats, or missing standard tags. Normalize to the conventions in [Tag Conventions](#tag-conventions).
5. **Consolidate by topic** — Don't just deduplicate identical entries. Look for memories that are fragments of the same understanding — early notes, follow-up observations, corrections, refinements. Weave them into a single authoritative memory per topic. Follow [Consolidation Procedure](#consolidation-procedure).
6. **Recalibrate importance scores** — Review importance ratings against the [Importance Calibration](#importance-calibration) table. Memories that keep proving useful deserve higher importance. Memories that never get retrieved can be lowered.
7. **Report what was changed** — Log a summary: how many memories consolidated, tags normalized, importance scores adjusted, and any issues found that need human review.

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

Consolidation isn't just about removing duplicates. It's about building a long-term knowledge base where each memory represents the best current understanding of a topic, enriched over time by new observations.

### 1. Find Related Memories Across Time
```
search_memories(query="<topic>", limit=50)
```
Search broadly. A memory from a month ago and one from yesterday might be two pieces of the same puzzle.

### 2. Read Full Content
```
get_memories(memory_ids=["id1", "id2", "id3", ...])
```

### 3. Assess the Relationship

| Relationship | Action |
|-------------|--------|
| Same topic, same conclusion | **Consolidate** — merge into one authoritative entry |
| Same topic, evolved understanding | **Consolidate** — the newer insight updates the older knowledge |
| Same topic, genuinely different angles | **Keep both** — add `related_memory_ids` links |
| Old memory superseded by new evidence | **Update old** with new understanding, delete the fragment |
| New memory is just a detail of existing knowledge | **Absorb** into the broader memory, delete the fragment |

### 4. Choose the Keeper
Pick the memory that serves as the best foundation — usually the most comprehensive one, or the one with the broadest scope. Prefer memories with established structure over recent fragments.

### 5. Merge with Intent
```
update_memory(
  memory_id="<keeper_id>",
  content="<synthesized content — not just concatenated, but integrated>",
  importance=<reassessed importance>,
  expected_version=<current_version>
)
```
The merged content should read as if it was always a single well-written memory. Don't just concatenate — synthesize. New observations should be woven into the existing narrative.

### 6. Delete Fragments
```
delete_memory(memory_id="<absorbed_id>")
```

### 7. Verify
```
get_memories(memory_ids=["<keeper_id>"])
```

**The goal is fewer, richer memories** — not a growing pile of notes. Each memory should represent the current best understanding of its topic, built up from many observations over time.

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