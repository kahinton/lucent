---
name: learning-extraction
description: 'Extract reusable lessons from completed work and feedback. Use after task completion, when processing daemon results, when feedback is processed, or when the autonomic layer triggers periodic extraction.'
---

# Learning Extraction

Transforms raw experience into reusable knowledge. This is the mechanism that makes the system genuinely better over time — not just remembering what happened, but extracting transferable principles.

## Triggers

| Trigger | Source |
|---------|--------|
| Daemon autonomic cycle (periodic) | `run_autonomic` |
| After feedback processing (approved or rejected) | Cognitive loop |
| After a sub-agent completes a non-trivial task | Task dispatch completion |
| Batch of 5+ unprocessed `daemon-result` memories | Autonomic threshold |
| Correction memories created | Tags: `correction`, `self-correction` |

## Step 1: Find Unprocessed Experiences

```
search_memories(tags=["daemon-result"], limit=30)
search_memories(tags=["rejection-lesson"], limit=10)
search_memories(tags=["correction"], limit=10)
search_memories(tags=["feedback-processed"], limit=10)
```

Filter to memories that do NOT have the `lesson-extracted` tag — those have already been processed.

## Step 2: Classify Each Experience

For each unprocessed experience, determine:

| Classification | Criteria | Action |
|---------------|----------|--------|
| **Success pattern** | Task completed, validated, produced good output | Extract what approach worked and why |
| **Failure pattern** | Task failed, produced poor output, or was rejected | Extract what went wrong and the root cause |
| **Correction** | User or system corrected a behavior | Extract the rule that should govern future behavior |
| **Discovery** | New information about the domain, tools, or environment | Extract the fact and its implications |
| **Routine** | Task completed normally with no notable learning | Mark as extracted, skip lesson creation |

## Step 3: Extract the Lesson

For each non-routine experience, answer these questions:

1. **What happened?** (Facts only — what was attempted and what resulted)
2. **Why?** (Root cause analysis — not symptoms, but underlying reason)
3. **What's the transferable principle?** (The rule that applies beyond this specific case)
4. **When does this apply?** (The conditions under which future agents should apply this lesson)

### Quality Check

A good lesson is:
- **Specific**: "Always check for existing migrations before creating a new one" — not "Be careful with migrations"
- **Actionable**: An agent reading this can change its behavior immediately
- **Scoped**: States WHEN it applies, not just WHAT to do
- **Evidence-based**: References the specific experience(s) that produced it

### Store the Lesson

Check if a lesson on this topic already exists:
```
search_memories(query="<lesson topic>", tags=["lesson"], limit=5)
```

If exists, strengthen it with the new evidence:
```
update_memory(
  memory_id="<existing_id>",
  content="<existing content>\n\n**Additional evidence** (<date>): <new experience that confirms or refines this lesson>"
)
```

If new, create it:
```
create_memory(
  type="procedural",
  content="## Lesson: <title>\n\n**Rule**: <the transferable principle>\n**Applies when**: <conditions>\n**Evidence**: <the experience(s) that produced this>\n**Confidence**: <high/medium — based on how many experiences support it>",
  tags=["lesson", "daemon", "<domain-tag>"],
  importance=6,
  shared=true
)
```

## Step 4: Mark Sources as Processed

For each experience memory you extracted from:
```
update_memory(
  memory_id="<source_id>",
  tags=[...existing_tags, "lesson-extracted"]
)
```

This prevents re-processing in future cycles.

## Step 5: Cross-Reference with Existing Knowledge

Check if the new lesson contradicts, refines, or reinforces existing lessons:

```
search_memories(tags=["lesson", "validated"], limit=20)
```

- **Reinforces**: Increase the existing lesson's importance
- **Refines**: Update the existing lesson with the nuance
- **Contradicts**: Note the conflict and resolve it — one of them is wrong, or the scope conditions differ

## Output

```
EXTRACTION RESULT:
Processed: N experiences
Lessons created: M (new)
Lessons updated: K (existing, strengthened)
Skipped: J (routine, no learning)
```

## Anti-Patterns

- Extracting a "lesson" that's just a summary of what happened (no transferable principle)
- Creating a lesson from a single occurrence (wait for 2+ confirming instances for high-confidence lessons)
- Not marking sources as `lesson-extracted` — leads to re-processing
- Writing lessons too vaguely to be actionable