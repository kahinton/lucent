---
name: learning-extraction
description: 'Extract reusable lessons from completed work and feedback. Use after task completion, when processing daemon results, when feedback is processed, or when the autonomic layer triggers periodic extraction.'
---

# Learning Extraction

Transforms raw experience into integrated knowledge. Lessons don't exist as standalone memories — they get folded into the existing knowledge that needs them. The goal is *fewer, smarter memories*, not more.

## Core Principle

**Integrate, don't accumulate.** A lesson about consolidation timing should update the memory about the consolidation system. A lesson about RBAC should update the memory about the RBAC module. If there's no existing memory to update, that's a knowledge gap — fill it with one well-scoped memory, not a floating "lesson."

## Triggers

| Trigger | Source |
|---------|--------|
| Daemon autonomic cycle (periodic) | `run_autonomic` |
| After feedback processing (approved or rejected) | Cognitive loop |
| After a request is rejected at the approval gate | Tags: `approval-rejected` |
| After a sub-agent completes a non-trivial task | Task dispatch completion |
| Correction memories created | Tags: `correction`, `self-correction` |

## Step 1: Find Unprocessed Experiences

```
search_memories(tags=["daemon-result"], limit=20)
search_memories(tags=["rejection-lesson"], limit=10)
search_memories(tags=["approval-rejected"], limit=10)
search_memories(tags=["correction"], limit=10)
search_memories(tags=["feedback-processed"], limit=10)
```

Filter to memories that do NOT have the `lesson-extracted` tag.

Cap at 10 per run. Skip anything tagged `daemon-heartbeat`.

## Step 2: Classify Each Experience

| Classification | Criteria | Action |
|---------------|----------|--------|
| **Success pattern** | Task completed, validated, produced good output | Find the related technical/procedural memory and add what worked |
| **Failure pattern** | Task failed, rejected, or produced poor output | Find the related memory and add the gotcha/pitfall |
| **Correction** | User or system corrected a behavior | Find the related memory and update it with the correct approach |
| **Discovery** | New information about the domain or tools | Find the related memory and add the new knowledge |
| **Routine** | Normal completion, nothing notable | Mark as extracted, move on |

## Step 3: Find the Memory to Update

This is the critical step. For each non-routine experience:

```
search_memories(query="<the topic/module/system this lesson is about>", limit=10)
```

Look for:
- Technical memories about the relevant file, module, or system
- Procedural memories about the relevant workflow or process
- Any memory whose scope covers this lesson's domain

**If a matching memory exists**: Update it with the new knowledge using `update_memory`. Append the insight to the existing content — don't rewrite the whole thing, just add what's new.

**If no matching memory exists**: This reveals a genuine knowledge gap. Create ONE technical or procedural memory scoped to the right level (file, module, or system). Include the lesson as part of its content, not as a standalone "Lesson:" entry.

**For correction-tagged memories**: When integrating a correction, note the correction source in the updated memory. User corrections (tagged `correction`): note "Corrected by user feedback" with date. Self-corrections (tagged `self-correction`): note "Self-corrected" with date. This creates traceable lineage from correction event to knowledge update.

## Step 4: Mark Sources as Processed

```
update_memory(
  memory_id="<source_id>",
  tags=[...existing_tags, "lesson-extracted"]
)
```

## Step 5: Clean Up

After integration, check if any source experience memories are now fully redundant (their knowledge has been absorbed into a better-scoped memory). If so, delete them.

The memory count should go DOWN or stay the same after extraction. Never up.

## Output

Brief text summary only. Do NOT create a summary memory.

```
EXTRACTION RESULT:
Processed: N experiences
Updated: K existing memories with new knowledge
Created: M new memories (for genuine gaps only)
Deleted: D redundant source memories
Skipped: J routine experiences
```

## Anti-Patterns

- **Creating standalone "Lesson:" memories** — lessons should be integrated into the memory they're about, not floating independently
- **Creating "Learning Extraction Run" summary memories** — the output goes to text, not to memory
- **Extracting from a single occurrence** — wait for 2+ confirming instances before treating something as a pattern
- **Writing lessons too vaguely to be actionable** — "be careful with X" is not a lesson
- **Not marking sources as `lesson-extracted`** — leads to re-processing loops
- **Increasing total memory count** — extraction should consolidate, not expand