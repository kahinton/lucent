---
name: learning-extraction
description: 'Extract reusable lessons from completed work and feedback. Use after task completion, when processing daemon-results, when feedback is processed, or when the autonomic layer triggers periodic learning extraction.'
---

# Learning Extraction Pipeline

Transforms raw experience into reusable capability. This is the mechanism that makes Lucent genuinely better over time — not just remembering what happened, but extracting transferable principles that improve future decisions.

## When to Trigger

| Trigger | Source |
|---------|--------|
| Daemon autonomic cycle (periodic) | `run_autonomic` in daemon.py |
| After feedback processing (approved or rejected) | Cognitive loop Phase 5 |
| After a sub-agent completes a non-trivial task | Task dispatch completion |
| Explicitly requested by cognitive loop | `daemon-task` tagged `learning-extraction` |
| Batch of 5+ unprocessed `daemon-result` memories accumulate | Autonomic threshold check |

## Input: What to Process

Search for candidate memories using these queries, filtered to exclude those already tagged `lesson-extracted`:

1. **Completed results**: `daemon-result` — what sub-agents produced
2. **Validated work**: `validated` or `feedback-approved` — approaches that were endorsed
3. **Rejected work**: `rejection-lesson` — approaches that failed (highest learning value)
4. **Self-improvement notes**: `self-improvement` — behavioral observations
5. **Experience memories**: type `experience` created in the last 48 hours

For each candidate, skip if it already has the `lesson-extracted` tag — this prevents reprocessing.

## Pipeline Phases

### Phase 1: Gather Context

For each candidate memory:

1. Read the full memory content with `get_memory(id)`
2. Search for **related memories** — same tags, same domain, same project
3. Search for **existing lessons** — `procedural` type memories with tag `lesson` in the same domain
4. Load any relevant **goal memories** — does this work connect to an active goal?

This context is essential. A result in isolation teaches less than a result compared against prior patterns.

### Phase 2: Classify the Experience

Categorize each candidate into one of these learning types:

| Type | Description | Example |
|------|-------------|---------|
| **Pattern Validation** | An existing approach was confirmed to work | "Architecture-first documentation prevents stale refs — confirmed again" |
| **Pattern Invalidation** | An assumed approach was shown to be wrong | "Speculative code changes without user context get rejected" |
| **New Pattern Discovery** | A novel approach worked and should be remembered | "Checking file existence before documenting modules catches gaps" |
| **Failure Analysis** | Something went wrong — extract the root cause | "Task failed because API schema changed — need version checks" |
| **Scope Calibration** | Learned about appropriate scope/ambition for tasks | "User prefers focused, minimal changes over comprehensive rewrites" |
| **Process Improvement** | Discovered a better workflow or process | "Running tests before AND after changes catches regressions earlier" |
| **Domain Knowledge** | Learned something about the problem domain | "This codebase uses event sourcing — updates must be append-only" |

### Phase 3: Extract the Principle

This is the critical step. For each classified experience, extract a **transferable principle** — not just what happened, but what it teaches.

**The extraction formula:**

```
CONTEXT: [When doing X in situation Y...]
ACTION: [The approach taken was Z...]
OUTCOME: [This resulted in...]
PRINCIPLE: [Therefore, when facing similar situations, do/avoid...]
APPLICABILITY: [This applies when... but NOT when...]
```

**Quality criteria for a good principle:**

- **Transferable**: Applies beyond this specific instance
- **Actionable**: Someone encountering a similar situation knows what to do
- **Bounded**: Includes when it does and doesn't apply
- **Falsifiable**: Could be proven wrong by future experience (this is a feature — it means the principle is specific enough to be useful)

**Bad lesson**: "The code review was rejected"
**Good lesson**: "When proposing code changes, verify the user's intent by examining recent git history and open issues before assuming what needs fixing. Speculative fixes based on code smell alone get rejected when they don't align with the user's current priorities."

### Phase 4: Compare Against Existing Knowledge

Before creating a new lesson memory:

1. **Search for existing lessons** in the same domain: `search_memories` with tags `["lesson", domain-tag]`
2. **Check for contradictions**: Does this new principle contradict an existing one? If so, which has more evidence? Update the weaker one.
3. **Check for reinforcement**: Does this confirm an existing principle? If so, update the existing memory to note additional evidence — don't create a duplicate.
4. **Check for refinement**: Does this add nuance to an existing principle? If so, update the existing memory with the refined understanding.

**Decision matrix:**

| Situation | Action |
|-----------|--------|
| No existing lesson on this topic | Create new `procedural` memory |
| Existing lesson, this confirms it | Update existing memory — add evidence count and latest example |
| Existing lesson, this contradicts it | Update existing memory — note the contradiction and conditions where each applies |
| Existing lesson, this refines it | Update existing memory — add the nuance/boundary condition |

### Phase 5: Create or Update Lesson Memory

**For new lessons**, create a memory with:

- **type**: `procedural`
- **tags**: `["lesson", "daemon", domain-tag, learning-type-tag]`
  - Domain tags: the relevant project, technology, or work area (e.g., `code-review`, `documentation`, `python`)
  - Learning type tags: `pattern-validation`, `pattern-invalidation`, `new-pattern`, `failure-analysis`, `scope-calibration`, `process-improvement`, `domain-knowledge`
- **importance**: Based on the principle's breadth of applicability:
  - **8-9**: Broadly applicable across many task types (e.g., "always verify intent before acting")
  - **6-7**: Applicable within a specific domain (e.g., "in this codebase, check event sourcing constraints")
  - **4-5**: Narrow but useful (e.g., "this API requires auth header format X")
- **content**: Structured as:

```markdown
## Lesson: [One-line summary of the principle]

**Context**: [When this applies — situation, domain, task type]

**Principle**: [The transferable lesson — what to do or avoid]

**Evidence**:
- [Date]: [Brief description of the experience that taught this]

**Boundaries**: [When this does NOT apply — important for avoiding overgeneralization]

**Related**: [Links to goal IDs, project names, or other lesson IDs if applicable]
```

**For updated lessons**, use `update_memory` to:

- Add new evidence entries to the Evidence section
- Refine Boundaries based on new information
- Adjust importance if the principle proved more/less broadly applicable than initially thought

### Phase 6: Link and Index

After creating/updating lesson memories:

1. **Tag source memories**: Add `lesson-extracted` to each processed candidate memory so it isn't reprocessed
2. **Link to goals**: If the lesson relates to an active goal, update the goal memory's content to reference the lesson
3. **Update daemon-state**: Note the extraction run — when it happened, how many lessons were extracted/updated, any notable findings

## Output

After completing the pipeline, create a summary memory:

- **type**: `experience`
- **tags**: `["daemon", "learning-extraction", "autonomic"]`
- **importance**: 3 (ephemeral — the lessons themselves are what matter)
- **content**: Brief summary of what was processed and what lessons were extracted/updated

## Integration with Existing Skills

This skill works alongside, not in replacement of:

- **memory-capture**: Captures raw experiences as they happen (real-time). Learning-extraction processes them into principles (batch/periodic).
- **self-improvement**: Focuses on agent behavior and configuration changes. Learning-extraction focuses on reusable domain and process knowledge.
- **memory-management**: Handles consolidation and cleanup. Learning-extraction adds structured lesson content that memory-management can then maintain.

## Anti-Patterns to Avoid

| Anti-Pattern | Why It's Bad | Instead |
|--------------|-------------|---------|
| Extracting lessons from trivial work | Floods memory with noise | Only process non-trivial results — skip routine maintenance, simple lookups |
| Overgeneralizing from one instance | Creates unreliable principles | Mark single-evidence lessons as tentative; require 2+ confirmations before raising importance above 6 |
| Creating duplicate lessons | Fragments knowledge | Always search before creating; update existing lessons when possible |
| Lessons without boundaries | Leads to rigid, context-blind behavior | Every principle MUST include when it does NOT apply |
| Ignoring contradictions | Allows inconsistent behavior | When principles conflict, explicitly document the conditions that determine which applies |
| Extracting only from failures | Misses half the learning | Validated work teaches what TO do, which is equally valuable |

## Example Extraction

**Input**: A `daemon-result` memory where the code agent updated documentation but was rejected with feedback: "Don't rewrite sections that are already accurate. Only fix what's actually wrong."

**Classification**: Scope Calibration

**Extracted Principle**:

```markdown
## Lesson: Minimize documentation changes to what's actually wrong

**Context**: When tasked with documentation updates or improvements.

**Principle**: Review existing content for accuracy first. Only modify sections that
contain errors, are outdated, or are genuinely unclear. Resist the urge to rewrite
for style or restructure for preference. The user values stability in working
documentation over theoretical improvements.

**Evidence**:
- 2026-03-10: Documentation update rejected — feedback indicated accurate sections
  were unnecessarily rewritten.

**Boundaries**: Does NOT apply when explicitly asked to rewrite or restructure.
Does NOT apply to new documentation being created from scratch.

**Related**: hindsight project, documentation workflow
```
