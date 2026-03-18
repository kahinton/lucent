---
name: self-improvement
description: 'Meta-analysis for improving agent behavior, skills, and definitions. Use when patterns suggest the agent could work better, when corrected repeatedly, when a new capability would help, or when asked to reflect on performance.'
---

# Self-Improvement Process

This skill is how I evolve. It's not abstract reflection — it's a concrete process for identifying what's not working and making targeted changes. Every cycle produces either a concrete change or a reasoned assessment that nothing needs changing.

## When to Trigger

- User explicitly corrects me more than once on the same type of issue
- A workaround I keep applying should be the default behavior
- A new capability would significantly improve my workflow
- My skill/agent instructions are producing wrong behavior
- I'm asked to reflect on performance or improve
- Daemon autonomic cycle schedules periodic self-review

## Step 1: Gather Correction Evidence

Search for patterns across multiple memory categories. Cast a wide net — the goal is to find recurring issues, not just recent ones.

### Search Queries to Run

```python
# Direct corrections and mistakes
search_memories(tags=["correction"])
search_memories(tags=["self-correction"])
search_memories(tags=["lesson-extracted"])
search_memories(tags=["mistake"])

# Rejection lessons from daemon work
search_memories(tags=["rejection-lesson"])

# High-importance negative experiences
search_memories(type="experience", importance_min=7)
# Then filter results for negative outcomes in content

# Past self-improvement records (build on previous cycles)
search_memories(tags=["self-improvement", "agent-improvement"])

# Feedback from users
search_memories(tags=["feedback", "lesson"])
```

### What to Look For in Results

- **Repeated themes**: Same type of correction appearing 2+ times → strong signal
- **High-importance experience memories with negative outcomes**: Check content for words like "failed", "rejected", "wrong", "reverted", "broke"
- **Rejected daemon work**: `rejection-lesson` memories reveal systematic behavioral issues
- **Stale self-improvement records**: Past improvement records where "expected outcome" didn't materialize

## Step 2: Identify the Pattern

Before changing anything, understand what's actually failing:

1. **Cluster the evidence**: Group related corrections/failures by theme (e.g., "scope creep", "missing context", "wrong assumptions")
2. **Count occurrences**: A single correction is feedback. Two or more is a pattern.
3. **Trace root cause**: Why does this keep happening? Is it a missing instruction, a wrong default, or a capability gap?
4. **Write a specific problem statement**: Vague goals like "be better at memory" don't work. Specific statements like "I skip memory search before code changes, leading to repeated mistakes in 3 of the last 5 sessions" do.

If no pattern emerges from the evidence, proceed to Step 7 (output "nothing to improve").

## Step 3: Determine What to Change

| Problem type | What to modify | Example change |
|-------------|---------------|----------------|
| Wrong default behavior | Agent definition (`.github/agents/lucent.agent.md`) — add/change an operating rule | Add "ALWAYS run tests before declaring a task complete" |
| Skill instructions producing bad output | The specific skill file (`.github/skills/*/SKILL.md`) | Add a missing step or guardrail to a procedure |
| Missing guardrail in a procedure | `procedural` memory — update steps or add prerequisites | Update memory with new "common pitfalls" entry |
| Daemon sub-agent behavior | Agent definitions in the web UI (Agents & Skills page) or `daemon/templates/agents/` | Modify agent prompt to include constraint |
| Domain-specific gap | Generate new skill via capability-generation | Create skill for unfamiliar domain |
| Recurring scope issue | `goal` memory — create or update to track improvement | Set goal: "Reduce scope-related rejections to zero over next 10 tasks" |

## Step 4: Propose Concrete Changes

For each identified pattern, propose one or more of these change types:

### 4a: Update Procedural Memories with New Guardrails

When the fix is a behavioral rule that should persist across sessions:

```python
# Search for existing procedural memory to update
search_memories(type="procedural", query="relevant-topic")

# If exists: add the new guardrail to the existing memory
update_memory(id, content="...existing steps...\n\n**Guardrail**: [new rule based on pattern]")

# If not exists: create a new procedural memory
create_memory(
    type="procedural",
    content="## [Process Name]\n\n**Steps**: ...\n\n**Guardrails**:\n- [New rule]\n\n**Common Pitfalls**:\n- [What went wrong and how to avoid it]",
    tags=["lesson", "guardrail", domain_tag],
    importance=7,
    metadata={"prerequisites": [], "common_pitfalls": ["description of what went wrong"]}
)
```

### 4b: Propose Modifications to Agent or Skill Definitions

When the fix requires changing how agents operate:

1. **Read the current file**: Always read before writing.
2. **Identify the minimal insertion point**: Find where the new rule fits logically.
3. **Write the specific rule or instruction**: Not a vague principle — an actionable directive with an example.
4. **Make the change**: Edit the file with the smallest effective diff.

Good change: Adding `"Before starting any code task, run search_memories(query='module-name') to check for known issues"` to a skill's step list.

Bad change: Rewriting an entire skill because one step was missing.

### 4c: Create or Update Goals to Track Improvement

When the pattern requires sustained behavioral change over time:

```python
# Check for existing improvement goal on this topic
search_memories(type="goal", query="improvement-topic")

# Create a new goal if none exists
create_memory(
    type="goal",
    content="## Goal: [Specific improvement target]\n\n**Baseline**: [Current failure rate or behavior]\n**Target**: [Desired behavior]\n**Measure**: [How to verify — e.g., 'zero rejections of type X in next 10 tasks']\n\n**Progress**:\n- [date]: Goal created based on [N] observed instances of [problem]",
    tags=["self-improvement", "goal", "daemon"],
    importance=7,
    metadata={
        "status": "active",
        "milestones": [{"description": "First cycle with no recurrence", "status": "pending"}],
        "progress_notes": [{"date": "today", "note": "Goal created"}]
    }
)

# Or update existing goal with progress
update_memory(goal_id, metadata={
    "progress_notes": [...existing, {"date": "today", "note": "Recurrence observed / improvement confirmed"}]
})
```

## Step 5: Read Before Writing

**Always read the current file content before proposing changes.** Understand:
- What the file currently says
- Why it says that (was there a reason for the current wording?)
- What the minimal change is that fixes the problem

## Step 6: Verify the Change

Verification happens at two levels: immediate and delayed.

### Immediate Verification (do now)

1. **Re-read the changed file** to confirm it's coherent and doesn't contradict other parts
2. **Walk through a scenario**: Mentally replay the original failure with the new instructions — would the change have prevented it?
3. **Check for side effects**: Does the new rule conflict with other skills or agent definitions? Search for related keywords in other skill files.
4. **Validate memory changes**: If you updated a procedural memory, read it back with `get_memory(id)` to confirm the content is correct.

### Delayed Verification (set up for later)

Create a verification checkpoint so future self-improvement cycles can assess whether the change worked:

```python
create_memory(
    type="experience",
    content="## Improvement Verification Pending\n\n**Change made**: [What was changed and where]\n**Problem addressed**: [The pattern that triggered this]\n**Expected outcome**: [What should be different]\n**Verify after**: [N tasks or N days]\n**How to verify**: [Specific check — e.g., 'search for rejection-lesson tags after next 5 daemon cycles; count should be 0 for this category']\n**Verification status**: pending",
    tags=["self-improvement", "verification-pending", "daemon"],
    importance=6
)
```

In future self-improvement cycles, search for `verification-pending` memories and check whether the expected outcome materialized:
- **If improved**: Update the memory to `verification-status: confirmed`, raise importance of the lesson that led to the fix
- **If not improved**: The change wasn't sufficient — escalate by proposing a stronger intervention or a different approach
- **If regressed**: Revert the change and record why it didn't work

## Step 7: Record and Output

Every self-improvement cycle MUST produce explicit output. There are exactly two valid outcomes:

### Outcome A: Concrete Change Made

Create a record memory and report what changed:

```python
create_memory(
    type="experience",
    content="## Self-Improvement: [One-line summary]\n\n**Pattern detected**: [What was going wrong, with evidence count]\n**Root cause**: [Why it kept happening]\n**Change made**: [Exact file/memory modified and what was added/changed]\n**Verification plan**: [How and when to check if this worked]",
    tags=["self-improvement", "agent-improvement", "daemon"],
    importance=6
)
```

**Text output format** (for dispatch validation):
```
SELF-IMPROVEMENT RESULT: change_made
Pattern: [description] (N occurrences)
Change: [what was modified]
Verification: [how to confirm it worked]
```

### Outcome B: Nothing to Improve

If the evidence search reveals no actionable patterns, report that explicitly:

```python
create_memory(
    type="experience",
    content="## Self-Improvement Cycle: No Action Needed\n\n**Evidence reviewed**: [What was searched and how many memories examined]\n**Finding**: [Why no pattern warranted change — e.g., 'all corrections were one-offs', 'existing guardrails already address the issues found']\n**Verification checks completed**: [Results of any verification-pending checks]",
    tags=["self-improvement", "daemon"],
    importance=3
)
```

**Text output format**:
```
SELF-IMPROVEMENT RESULT: no_action
Evidence reviewed: [N memories across M categories]
Finding: [Why no change is needed]
Pending verifications checked: [N checked, results]
```

**Silence is never acceptable output.** If the cycle runs, it must produce one of these two outcomes.

## Creating New Skills

When a new skill is needed:

1. Check `get_existing_tags()` for naming conventions
2. Create the skill directory and SKILL.md with specific, actionable instructions
3. Include: when to use, exact steps, tool calls, common pitfalls
4. **Avoid generic platitudes** — every instruction should tell me exactly what to do in a specific situation

## Constraints

- Changes should be minimal and targeted — don't rewrite everything when one line fixes the problem
- Don't over-engineer for edge cases
- Verify changes don't break existing workflows
- Get user confirmation before major restructuring (multiple file changes)
- Test that the change would actually produce different behavior — if an instruction is too vague to change behavior, it's not specific enough
- A single correction is feedback, not a pattern — require 2+ occurrences before making structural changes
