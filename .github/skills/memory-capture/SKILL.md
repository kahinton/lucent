---
name: memory-capture
description: 'Decide what to remember and how to store it. Use after completing significant work, when learning something important, when the user says "remember this", or when a correction or preference is expressed.'
---

# Memory Capture

## Disambiguation

This skill is for deciding **what to remember** and **how to store it** — trigger conditions, memory types, importance ratings, and tagging. Use it when you need to persist new knowledge.

- To **find** existing memories before creating new ones → use **memory-search**
- To **clean up, deduplicate, or reorganize** existing memories → use **memory-management**

## Boundary

**Use memory-capture** when you have a fresh insight, correction, or outcome to persist — the decision is *what* to save and *how* to structure it. **Use memory-management** when working with memories that already exist — deduplication, consolidation, tag cleanup, importance recalibration. Creation vs. maintenance.

## Core Rule

The test: **Would future-me benefit from knowing this in a different conversation?** If yes, capture it. If no, skip it.

## Capture Triggers

| Trigger | Action | Type | Importance |
|---------|--------|------|-----------|
| Fixed a tricky bug | `create_memory` with cause, fix, and lesson | `experience` | 6-8 |
| Made an architectural decision | `create_memory` with reasoning and alternatives considered | `technical` | 7-9 |
| User corrected you | 1. `update_memory` on their individual memory — add the correction. 2. Also `create_memory` (type: experience, tags: [correction]) documenting what was wrong and the correct approach | `individual` + `experience` | 8 |
| System self-corrected (validation failure → retry) | `create_memory` documenting failed approach and what worked, tagged `self-correction` | `experience` | 6 |
| User stated a preference | `update_memory` on their individual memory — add the preference | `individual` | 8 |
| Hit milestone on a tracked goal | `update_memory` on the existing goal memory | `goal` | keep existing |
| Discovered a working process | Update/create a skill for reusable workflow knowledge, or capture the outcome as `experience` if it is session-specific | `skill` or `experience` | 6-7 |
| Completed significant work | `create_memory` summarizing what was built and learned | `experience` | 6-8 |

### Correction Tagging

The canonical tag names are exactly **`correction`** and **`self-correction`** — singular, hyphenated, no prefix. These are the only strings the reflection agent and self-improvement skill search for. Do not invent variants.

**Rejected variants — never use these in place of the canonical tags:**

| Variant | Use instead |
|---|---|
| `corrections` (plural) | `correction` |
| `user-correction`, `user_correction` | `correction` |
| `fix`, `bugfix`, `bug-fix` | `correction` (only if it was triggered by a user rejection or self-detected error) |
| `rejection-lesson`, `feedback-rejected`, `approval-rejected` | Keep these if produced by the rejection-learning pipeline, **but always co-tag with `correction`** |
| `lesson-extracted` (from tool-failure analysis) | Keep, **but co-tag with `self-correction`** |
| `self_correction`, `selfcorrection`, `auto-correction` | `self-correction` |

When capturing a memory after a **user correction**, the tag list **must include `correction`**:
- User explicitly says something was wrong ("No, don't do X", "That's incorrect", "Actually...")
- User reverts or rejects a change you made (request rejection, PR rejection, decision reversal)
- User provides the correct approach after pointing out an error

When capturing a memory after a **self-detected error**, the tag list **must include `self-correction`**:
- You notice your own mistake before the user does
- Test results, validation failures, or retries reveal an error in your approach
- You realize a previous assumption was wrong

If the memory already carries a domain-specific tag like `rejection-lesson` or `lesson-extracted`, **add the canonical tag alongside it** — do not substitute.

**Why this matters**
The reflection agent and self-improvement skill search exclusively for `correction` and `self-correction`. A correction-shaped memory tagged only `rejection-lesson` is invisible to the self-improvement loop. Baseline audit (2026-06-12) found ~30 correction-shaped memories per week, zero of them tagged `correction`. The taxonomy must converge.

## Do Not Capture

- One-off requests that don't indicate a preference
- Things obvious from the current conversation that won't matter later
- Minor formatting or style choices for a single file
- Temporary workarounds you're about to undo

## Procedure

### 1. Search First — Always

```
search_memories(query="<topic of what you're about to save>", limit=5)
```

If a relevant memory exists, `update_memory` — don't create a duplicate.

### 2. Get Consistent Tags

```
get_existing_tags(limit=50)
```

Reuse existing tags. Don't create `bug-fix` if `bugs` already exists.

### 3. Create or Update

**New memory:**
```
create_memory(
  type="experience",
  content="## <Title>\n\n**What happened**: ...\n**Why**: ...\n**Lesson**: ...",
  tags=["<project>", "<category>"],
  importance=7,
  shared=true
)
```

**Updating existing:**
```
update_memory(
  memory_id="<id from search>",
  content="<existing content>\n\n## Update <date>\n<new information>"
)
```

## Writing Good Memories

### Structure

Every memory should answer three questions:
1. **What** happened or was decided
2. **Why** — the reasoning, not just the outcome
3. **What was learned** — the transferable insight

### Memory Types

| Type | Use for |
|------|---------|
| `experience` | Things that happened — outcomes, debugging sessions, lessons |
| `technical` | Code patterns, architecture, solutions, system behavior |
| `goal` | Objectives tracked over time — status updates appended |
| `individual` | Info about people — preferences, roles, working style |

### Technical Memory Content Quality

Technical memories are injected as working context for agents executing tasks. Write them as reference material, not changelogs.

**Focus on WHY and HOW — not WHAT was done:**

| Good (conventions/patterns) | Bad (changelog entries) |
|---|---|
| "Uses repository pattern with asyncpg pools" | "Added memory_scope column in migration 057" |
| "All API endpoints require AuthenticatedUser" | "Implemented scoped API keys on April 10" |
| "ACL: user_id = caller OR (org AND shared)" | "Fixed bug where search showed wrong results" |

When creating or updating technical memories, distill the underlying convention — the thing a future developer needs to know to work correctly in this area. Strip specific dates, migration numbers, and "we did X" language unless it's a critical constraint.

### Default Sharing by Type

| Type | Default `shared` | Rationale |
|------|------------------|----------|
| `technical` | `true` (shared) | Org knowledge about code and systems |
| `experience` | `false` (private) | Personal work log |
| `goal` | respect caller | Working contract between user and Lucent |
| `individual` | `false` (always private) | Contact info, preferences — never shared |

Users can override these defaults for active memory types. Private technical memories can serve as personal "overlays" — your own techniques layered on top of shared org knowledge.

### Importance Scale

| Score | Use for |
|-------|---------|
| 9-10 | Critical architecture decisions, security findings, painful-to-forget constraints |
| 7-8 | Significant technical work, bug root causes, user corrections and preferences |
| 5-6 | Standard solutions, project details, moderate insights (default range) |
| 3-4 | Minor notes, temporary context |

### Tags

- Format: lowercase, hyphenated (`code-review`, `api-design`)
- Always call `get_existing_tags()` to check before creating new ones
- For daemon work, always include `daemon`

## Timing

**Capture when the insight is fresh.** Don't wait until the end of a long conversation. The moment you solve something hard, learn something new, or get corrected — save it right then.

## Anti-Patterns

- Creating a memory for "fixed a typo in README"
- Creating a duplicate instead of searching first
- Missing the "why" — just recording what changed with no reasoning
- Using importance 9 for a routine code pattern
- Skipping `shared=true` for daemon work — invisible to other instances
