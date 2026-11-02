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
| Missing guardrail in a reusable workflow | Skill — update steps, prerequisites, or pitfalls |
| Domain-specific gap | Generate a new skill via capability-generation |
| Recurring scope issue | Goal memory — create or update to track improvement |

## Step 4: Make the Change

**Always read the target file before modifying it.** Understand what's there and why before changing anything.

**For skill/agent files:**
Find the minimal insertion point. Write a specific, actionable directive — not a vague principle. Make the smallest effective diff.

Good change: Adding "Before starting any code task, search memory for known issues in the affected module" to a skill's step list.

Bad change: Rewriting an entire skill because one step was missing.

## Step 5: Set Up Verification

State the verification plan in your **text output**. Do not create a memory for
it.

Verification state is process bookkeeping: it exists only until the check
resolves, is read once, and is never durable knowledge. Writing a memory per
pending check means every cycle leaves behind an artifact that grows the store
forever and helps no future retrieval. The task result already records this run.

If a check needs to be findable later, name it clearly in the run's output
(`Verify after: <N days> — <specific check>`); the session log and task record
are the place to look, not memory.

## Step 6: Check Pending Verifications

Before finishing, review any improvements whose verification window has elapsed
and judge whether the expected outcome materialized. Report findings in text.

There is no `verification-pending` memory to search for. Judge from the evidence
you gather in Step 1 and state the result.

## Step 7: Record and Output

The output is **text only**. Do **not** create a `scan-result` memory for the
cycle.

A memory recording that a scan happened — what it reviewed, what it decided, and
whether it acted — is a log entry, not knowledge. Writing one every cycle is how
a store fills with unreadable process narrative. The task result already keeps
the full record.

Write a memory only when the cycle produced a genuinely reusable fact about a
specific system, file, or failure — small and scoped to that one subject.

### Outcome A: Change Made
```
SELF-IMPROVEMENT RESULT: change_made
Pattern: <description> (N occurrences)
Change: <what was modified>
Verification: <how to confirm>
```

### Outcome B: No Action Needed
```
SELF-IMPROVEMENT RESULT: no_action
Evidence reviewed: <N memories across M categories>
Finding: <why no change needed>
```

SELF-IMPROVEMENT RESULT: no_action
Evidence reviewed: <N memories across M categories>
Finding: <why no change needed>
```

## Anti-Patterns

- Don't make structural changes based on a single correction because one data point is noise, not a pattern — require 2+ occurrences before modifying a definition or skill.
- Don't make speculative changes without evidence because improvements without corroborating signals are as likely to break working behavior as to fix broken behavior.
- Don't over-engineer changes for edge cases because a targeted one-line fix that addresses the root cause is always better than a sweeping rewrite that introduces new failure modes.
- Don't modify definitions during active task execution because changes to agent behavior mid-task create inconsistencies between what was planned and what executes.
- Don't write improvement instructions that are too vague to act on because a directive like "be better at memory" changes nothing — every instruction must be specific enough to change observable behavior.
- Don't save a memory about the cycle itself because a record of "what I scanned and what I decided" is a log entry, not knowledge; the task result already holds it, and one per cycle grows the store without ever being read again.
- Don't write a verification-checkpoint memory because pending-check state resolves once and is never durable knowledge — state it in the run output instead.