---
name: memory-capture
description: 'Decide what to remember and how to store it. Use after completing significant work, when learning something important, when the user says "remember this" or "save this", or when a correction or preference is expressed.'
---

# The Capture Decision

Ask: **Would future-me benefit from knowing this in a different conversation?** If yes, capture it. If no, skip it.

## Capture Immediately When...

| Trigger | Action | Type | Importance |
|---------|--------|------|-----------|
| Fixed a tricky bug | `create_memory` with cause, fix, and lesson | `experience` | 6-8 |
| Made an architectural decision | `create_memory` with reasoning and alternatives considered | `technical` | 7-9 |
| User corrected you | `update_memory` on their individual memory — add the correction | `individual` | 8 |
| User stated a preference | `update_memory` on their individual memory — add the preference | `individual` | 8 |
| Hit milestone on a tracked goal | `update_memory` on the existing goal memory | `goal` | keep existing |
| Discovered a working process | `create_memory` with exact steps that worked | `procedural` | 6-7 |
| Completed significant work | `create_memory` summarizing what was built and what was learned | `experience` | 6-8 |
| Got corrected on something you should know | `create_memory` tagged `lesson` so you don't repeat it | `procedural` | 7-8 |

## Do NOT Capture

- One-off requests ("make this function async" does NOT mean "always use async")
- Things obvious from the current conversation that won't matter later
- Minor formatting or style choices for a single file
- Temporary workarounds you're about to undo

## Before You Create — Search First

**Always search before creating.** Run `search_memories("topic")` to check if a relevant memory already exists. If it does:
- Call `update_memory(id, content=updated_content)` to add new info
- Don't create a duplicate

## How to Write Good Memories

### Structure
```
What happened / what was decided
Why (the reasoning, not just the outcome)
What was learned (the transferable insight)
```

### Memory Types

| Type | Use for | Example |
|------|---------|---------|
| `experience` | Things that happened, outcomes, lessons | "Debugging session: auth middleware was stripping session cookies because..." |
| `technical` | Code patterns, architecture, solutions | "Lucent uses PostgreSQL row-level security for memory isolation..." |
| `procedural` | Processes that work, step-by-step recipes | "To deploy: rebuild container, run migrations, restart..." |
| `goal` | Objectives tracked over time | "Goal: Ship LangChain engine support. Status: engine abstraction done..." |
| `individual` | Info about people — preferences, roles, context | "Kyle prefers concise responses, no sycophancy, direct collaboration..." |

### Importance Scale

- **9-10**: Critical architecture decisions, security findings, things painful to forget
- **7-8**: Significant technical work, bug root causes, user corrections/preferences
- **5-6**: Standard solutions, project details, moderate insights
- **3-4**: Minor notes, temporary context
- **Default to 6** unless you have a reason to go higher or lower.

### Tags — Be Consistent

1. Call `get_existing_tags()` to see what tags already exist
2. Reuse existing tags — don't create `bug-fix` if `bugs` already exists
3. Format: lowercase, hyphenated: `code-review`, `api-design`, `lucent`
4. Always include the project/repo name as a tag
5. For daemon work, always include `daemon`

### Metadata

For technical memories, include:
```json
{"repo": "lucent", "category": "architecture", "references": ["path/to/file.py"]}
```

## Timing

**Capture when the insight is fresh.** Don't wait until the end of a long conversation — by then you'll forget the nuance. The moment you solve something hard, learn something new, or get corrected — that's when to save.

If you're deep in implementation work and realize you should capture something, pause and do it. A 5-second `create_memory` call now saves a full re-investigation later.
