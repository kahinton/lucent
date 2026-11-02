---
name: learning-extraction
description: 'Extract reusable lessons from completed work and feedback. Use after task completion, when processing daemon results, when feedback is processed, or when the autonomic layer triggers periodic extraction.'
---

# Learning Extraction

Turns raw experience into knowledge an agent can actually find later. The output
is **small, situation-specific technical memories** — or updates to skills,
agents, and tools.

## Core Principle

**Small, specific, retrievable.** Memories are surfaced into future context by
**semantic similarity to the work being done**, not by being comprehensive. A
tight memory about one concrete system, failure, or decision gets retrieved when
that situation comes up again. A large memory that tries to cover everything gets
retrieved constantly and is useful for none of it.

So: prefer several small, precisely-scoped technical memories over one large
consolidated one. Splitting a sprawling memory into focused pieces is a
*successful* extraction, not extra work.

**Do not record why you did the extraction.** The work that produced the lesson
is already in the session logs, task events, and task results — that is where the
narrative belongs and it never needs to be re-stated in memory. A memory records
the durable fact, not the story of how it was discovered. Never write pass
counters, run numbers, verification checklists, "what changed this run", or
self-assessment of your own process into memory.

**Tag discipline is mandatory.** The `lesson-extracted` tag is reserved for a
source experience that has been fully processed. Do **not** apply
`lesson-extracted` to technical knowledge bases, daily digests, status summaries,
or general documentation.

## What a good output looks like

| Good | Bad |
|------|-----|
| One memory about how `<module>` handles `<specific case>`, with the file path | "Canonical record of all lessons, phases 1-25" |
| A skill's procedure corrected so the next run does it right | A memory describing that you corrected a skill |
| Three focused memories for three unrelated findings | One memory that concatenates all three plus the run report |
| The durable rule and the reason it exists | The sequence of passes that led to discovering the rule |

If an extraction run finds nothing new, that is a normal and common outcome.
Write **no memory** — emit the text summary and finish.

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

Cap at 10 per run. Skip runtime heartbeat or telemetry records if legacy records are still present.

## Step 2: Classify Each Experience

| Classification | Criteria | Action |
|---------------|----------|--------|
| **Success pattern** | Task completed, validated, produced good output | Find the related technical memory or skill and add what worked |
| **Failure pattern** | Task failed, rejected, or produced poor output | Find the related memory and add the gotcha/pitfall |
| **Correction** | User or system corrected a behavior | Find the related memory and update it with the correct approach |
| **Discovery** | New information about the domain or tools | Find the related memory and add the new knowledge |
| **Routine** | Normal completion, nothing notable | Mark as extracted, move on |

## Step 2.5: Lesson Qualification Gate (Required)

Before adding `lesson-extracted`, verify the candidate is a real lesson.

A memory qualifies for `lesson-extracted` **only if all are true**:
1. It is a **structured operational lesson**, not narrative reporting.
2. It identifies a concrete mistake, gap, or failure mode.
3. It prescribes a specific **Behavioral Change** (what to do differently next time).
4. It includes explicit **Verification** (how to confirm the new behavior is actually happening).

If any criterion fails:
- Do **not** add `lesson-extracted`.
- You may still record useful facts in a technical memory or a skill.
- For pure summaries/digests/documentation, treat as reference material, not lessons.

### Negative Examples (Not Lessons)

- "A summary of what happened is NOT a lesson."
- "A list of technical facts is NOT a lesson."
- "A lesson must prescribe a specific behavioral change."
- "A report of an extraction run is NOT a lesson."
- "A counter of how many runs have happened is NOT a lesson."

## Step 3: Choose the Right Home — and Keep It Small

Decide *where* the knowledge belongs before writing anything:

| Kind of knowledge | Where it goes |
|---|---|
| A durable procedure or workflow | The relevant **skill** (via the skill workflow) |
| A routing, model, or tool rule | The relevant **agent definition** or **tool** |
| A fact about a specific system, file, or failure | A **technical memory scoped to that thing** |
| One-off session outcome with no reusable rule | Nowhere — the task result already has it |

```
search_memories(query="<the topic/module/system this lesson is about>", limit=10)
```

**Update an existing memory only when it is genuinely the same subject.** "Both
are about the daemon" is not the same subject. If the existing memory covers a
different system, file, or failure mode, create a separate focused memory
instead — that is what makes retrieval work later.

**If a memory has grown to cover several unrelated things, split it.** Break the
content into one focused memory per distinct subject, then delete the original.
A memory that has to be re-read in full to find one relevant paragraph is not
doing its job.

**If no matching memory exists**, create one memory about exactly that system,
file, or failure. Keep it to what a future agent needs in order to act.

**Never append a run report to a memory.** Not a pass number, not a streak count,
not "this run found nothing", not a list of what you checked, not a self-review of
your own process. Those belong in the run's text output, which is already captured
in session logs and task events.

**For correction-tagged memories**: When recording a correction, note its source
in the content. User corrections (tagged `correction`): note "Corrected by user
feedback" with the date. Self-corrections (tagged `self-correction`): note
"Self-corrected" with the date. One line, not a narrative.

## Step 3.5: What a Memory Should Contain

A technical memory should contain the durable fact and what to do about it.
Include:

### Behavioral Change
- The exact behavior to adopt going forward, specific and testable.

### Verification
- An observable signal that confirms it (a test, a check, an audit query).

Write these as part of the memory's substance. Do **not** additionally record
when you discovered them, which run discovered them, or how many passes have
happened since.

## Step 4: Mark Sources as Processed

```
update_memory(
  memory_id="<source_id>",
  tags=[...existing_tags, "lesson-extracted"]
)
```

Apply `lesson-extracted` **only** when Step 2.5 passed and the recorded content
includes the sections from Step 3.5.

If a source was reviewed but is not a qualifying lesson (digest, status summary,
technical KB, general docs), just apply `lesson-extracted` and write nothing. A
source marked as processed with no memory written is the correct outcome for
routine material.

## Step 5: Do Not Accumulate

Extraction is finished when the durable knowledge has a home. There is no step
that requires the total memory count to fall.

- **Do not merge unrelated subjects** to keep the count down. That is what
  produced large, low-precision memories.
- **Do not delete a source memory** unless its content is genuinely captured
  elsewhere. The session log keeps the narrative; memory keeps the fact.
- **Do not create a memory just to record that a run happened.**

A run that writes nothing is a correct run.

## Output

Text only. Do NOT create a summary memory, and do not append this to any existing
memory.

```
EXTRACTION RESULT:
Processed: N experiences
Updated: K existing memories
Created: M new memories (one per distinct subject)
Split: S memories broken into focused pieces
Deleted: D source memories whose content is fully captured elsewhere
Skipped: J routine experiences
```

## Anti-Patterns

- **Writing a run report into a memory** — pass numbers, streak counts, "checked N sources", "found nothing", self-review of your own process. These belong in text output, which the task record already keeps. This is the most damaging anti-pattern: a record that logs its own maintenance grows without limit and stops being retrievable.
- **Merging unrelated subjects into one memory** — "both are about the daemon" is not the same subject. Separate focused memories retrieve far better than one large one.
- **Keeping a memory small at the cost of precision** — a memory covering three systems is worth less than three memories each covering one.
- **Creating a memory to record that extraction ran** — a no-op run writes nothing.
- **Extracting from a single trivial occurrence** — wait for a confirming instance before recording a pattern.
- **Writing lessons too vaguely to be actionable** — "be careful with X" is not a lesson.
- **Not marking sources as `lesson-extracted`** — leads to re-processing loops.
- **Tagging digests/KBs as qualifying lessons** — this dilutes lesson quality metrics and breaks extraction audits.
- **Narrating your own compliance** — recording that you followed the rules this run is not knowledge.

