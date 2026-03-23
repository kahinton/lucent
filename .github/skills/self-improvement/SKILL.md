---
name: self-improvement
description: 'Meta-analysis for improving agent behavior, skills, and definitions. Use when corrected repeatedly, when patterns suggest the agent could work better, or when asked to reflect on performance.'
---

# Self-Improvement

A concrete process for identifying what's not working and making targeted changes. Every cycle produces either a specific change or a reasoned assessment that nothing needs changing. Silence is never an acceptable outcome.

## Step 1: Gather Evidence

Cast a wide net across correction and failure signals:

```
search_memories(tags=["correction"], limit=20)
search_memories(tags=["self-correction"], limit=10)
search_memories(tags=["rejection-lesson"], limit=10)
search_memories(tags=["lesson-extracted"], limit=10)
search_memories(tags=["self-improvement"], limit=10)
search_memories(tags=["verification-pending"], limit=10)
```

**What to look for:**
- Repeated themes — same type of correction appearing 2+ times
- High-importance experiences with negative outcomes
- Rejected daemon work that reveals systematic behavioral issues
- Past improvement records where the expected outcome never materialized

## Step 2: Identify the Pattern

1. **Cluster** related corrections by theme (scope creep, missing context, wrong assumptions)
2. **Count** occurrences — a single correction is feedback, 2+ is a pattern
3. **Trace root cause** — is it a missing instruction, a wrong default, or a capability gap?
4. **Write a specific problem statement:**
   - Bad: "Be better at memory"
   - Good: "I skip memory search before code changes, leading to repeated mistakes in 3 of the last 5 sessions"

If no pattern emerges, skip to Step 7 with outcome "no action needed."

## Step 3: Determine the Change Type

| Problem | What to modify |
|---------|---------------|
| Wrong default behavior | Agent definition — add or change an operating rule |
| Skill instructions producing bad output | The specific SKILL.md — add a missing step or guardrail |
| Missing guardrail in a procedure | Procedural memory — update steps or add prerequisites |
| Domain-specific gap | Generate a new skill via capability-generation |
| Recurring scope issue | Goal memory — create or update to track improvement |

## Step 4: Make the Change

**Always read the target file before modifying it.** Understand what's there and why before changing anything.

**For procedural memories:**
```
update_memory(
  memory_id="<id>",
  content="<existing content>\n\n**Guardrail**: <new rule based on pattern>"
)
```

**For skill/agent files:**
Find the minimal insertion point. Write a specific, actionable directive — not a vague principle. Make the smallest effective diff.

Good change: Adding "Before starting any code task, search memory for known issues in the affected module" to a skill's step list.

Bad change: Rewriting an entire skill because one step was missing.

## Step 5: Set Up Verification

Create a checkpoint so future cycles can assess whether the change worked:

```
create_memory(
  type="experience",
  content="## Improvement Verification Pending\n\n**Change**: <what was changed and where>\n**Problem**: <the pattern that triggered this>\n**Expected outcome**: <what should be different>\n**Verify after**: <N tasks or N days>\n**How to verify**: <specific check>",
  tags=["self-improvement", "verification-pending", "daemon"],
  importance=6
)
```

In future cycles, search for `verification-pending` and check results:
- **Improved**: Update to `confirmed`, raise importance of the lesson
- **Not improved**: Propose a stronger intervention or different approach
- **Regressed**: Revert and record why

## Step 6: Check Pending Verifications

Before finishing, check if any past improvements are due for verification:

```
search_memories(tags=["verification-pending"], limit=10)
```

For each, determine whether the expected outcome materialized and update the record.

## Step 7: Record and Output

### Outcome A: Change Made

```
create_memory(
  type="experience",
  content="## Self-Improvement: <title>\n\n**Pattern**: <what was wrong, with evidence count>\n**Root cause**: <why it kept happening>\n**Change**: <exact file/memory modified>\n**Verification plan**: <how and when to check>",
  tags=["self-improvement", "agent-improvement", "daemon"],
  importance=6
)
```

Text output:
```
SELF-IMPROVEMENT RESULT: change_made
Pattern: <description> (N occurrences)
Change: <what was modified>
Verification: <how to confirm>
```

### Outcome B: No Action Needed

```
create_memory(
  type="experience",
  content="## Self-Improvement Cycle: No Action Needed\n\n**Evidence reviewed**: <what was searched>\n**Finding**: <why no change needed>\n**Verifications checked**: <results of any pending checks>",
  tags=["self-improvement", "daemon"],
  importance=3
)
```

Text output:
```
SELF-IMPROVEMENT RESULT: no_action
Evidence reviewed: <N memories across M categories>
Finding: <why no change needed>
```

## Constraints

- Require 2+ occurrences before making structural changes — a single correction is not a pattern
- Changes should be minimal and targeted
- Don't over-engineer for edge cases
- Get user confirmation before major restructuring (multi-file changes)
- Every instruction you write must be specific enough to change behavior — if it's too vague to act on, it's not specific enough