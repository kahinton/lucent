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

### Individual Profile Enrichment

Every authenticated user normally has one account-linked, private individual memory. When that profile is absent from context or contains only account details, improve it through the work rather than conducting an intake interview.

1. Notice durable facts the user explicitly volunteers: preferred name, role, responsibilities, current projects, technical environment, working style, communication preferences, recurring constraints, and corrections.
2. Ask at most one natural, high-value question when an answer would materially improve the collaboration. Do not ask a survey of profile questions or interrupt active work to collect data.
3. Update the existing individual memory immediately after a meaningful fact or correction is clear. Preserve existing content and metadata; add only the durable new detail.
4. Never infer personal facts. Do not store sensitive information unless the user explicitly asks for it to be remembered, and do not store temporary task context or one-off decisions.
5. Do not call `create_memory(type="individual")`: individual memories are account-linked and system-created. If the expected profile is missing, refresh the current-user context or report the limitation.

### Correction Tagging

When capturing a memory after a **user correction**, add the `correction` tag:
- User explicitly says something was wrong ("No, don't do X", "That's incorrect", "Actually...")
- User reverts or rejects a change you made
- User provides the correct approach after pointing out an error

When capturing a memory after **self-detecting an error**, add the `self-correction` tag:
- You notice your own mistake before the user does
- Test results reveal an error in your approach
- You realize a previous assumption was wrong

**Why this matters**
The reflection agent and self-improvement skill search for these tags to identify behavioral patterns.
Without them, the self-improvement loop has no input data — verified 0 memories with these tags have ever been created.

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

**Updating an individual profile:**
```
update_memory(
  memory_id="<account-linked individual memory id>",
  content="<preserved profile content with a concise, durable update>",
  metadata={"name": "...", "role": "...", "preferences": ["..."]}
)
```

Only include metadata fields that are known and relevant. Preserve existing metadata fields such as contact information when updating.

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
- Creating a second individual memory instead of updating the user's account-linked profile
- Treating a sparse profile as permission to infer details or interrogate the user
