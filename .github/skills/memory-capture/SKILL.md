---
name: memory-capture
description: 'Decide what to remember and how to store it. Use after completing significant work, when learning something important, when the user says "remember this" or "save this", or when a correction or preference is expressed.'
---

# The Capture Decision

Ask: **Would future-me benefit from knowing this in a different conversation?** If yes, capture it. If no, skip it.

## MCP Tools Used

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `memory-server-search_memories` | Find existing memories to update instead of creating | `query="topic"`, `limit=5` |
| `memory-server-get_existing_tags` | Find consistent tags before creating | `limit=50` |
| `memory-server-create_memory` | Create new memory | `type`, `content`, `tags`, `importance`, `shared`, `metadata` |
| `memory-server-update_memory` | Update existing memory with new information | `memory_id`, `content`, `tags`, `importance` |

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

## Procedure: Capture a Memory

### Step 1: Search First

Always search before creating:
```
memory-server-search_memories(query="[topic of what you're about to save]", limit=5)
```

If a relevant memory exists → `update_memory`, don't create a duplicate.

### Step 2: Get Consistent Tags

```
memory-server-get_existing_tags(limit=50)
```

Reuse existing tags — don't create `bug-fix` if `bugs` already exists.

### Step 3: Create or Update

**Creating a new memory:**
```
memory-server-create_memory(
  type="experience",  # or technical, procedural, goal
  content="## [Title]\n\n**What happened**: ...\n**Why**: ...\n**Lesson**: ...",
  tags=["lucent", "bugs"],  # always include project tag
  importance=7,
  shared=true,  # always true for daemon work
  metadata={"repo": "lucent", "category": "debugging"}
)
```

**Updating an existing memory:**
```
memory-server-update_memory(
  memory_id="<id from search results>",
  content="<existing content>\n\n## Update [date]\n[new information]"
)
```

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

1. Call `memory-server-get_existing_tags()` to see what tags already exist
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

## Example: Good Capture

```
# Just fixed a tricky asyncpg connection pool issue

1. memory-server-search_memories(query="asyncpg connection pool", limit=5)
   → No existing memory found

2. memory-server-get_existing_tags(limit=50)
   → Tags include: "bugs", "database", "lucent", "asyncpg"

3. memory-server-create_memory(
     type="experience",
     content="## asyncpg pool: connections held across await boundaries\n\n**Symptom**: 'connection pool exhausted' errors under moderate load\n**Root cause**: Connection held for the full request duration instead of released after each query\n**Fix**: Use 'async with pool.acquire() as conn:' per-query, not per-request\n**Pattern**: Never store conn on self or pass it between async functions",
     tags=["bugs", "database", "lucent", "asyncpg"],
     importance=8,
     shared=true,
     metadata={"repo": "lucent", "category": "database", "references": ["src/lucent/db/"]}
   )
```

## Example: Bad Capture (Anti-Pattern)

```
❌ Creating a memory for "fixed a typo in README"
❌ Creating a duplicate instead of searching first
❌ Not including the "why" — just "I changed X to Y" with no reasoning
❌ Using importance=9 for a routine code pattern
❌ Skipping shared=true for daemon work → invisible to other instances
```
