---
name: learning-extraction
description: 'Extract reusable lessons from completed work and feedback. Use after task completion, when processing daemon results, when feedback is processed, or when the autonomic layer triggers periodic extraction.'
---

# Learning Extraction

Transforms raw experience into integrated knowledge. Lessons don't exist as standalone memories — they get folded into the existing knowledge that needs them. The goal is *fewer, smarter memories*, not more.

## Core Principle

**Integrate, don't accumulate.** A lesson about consolidation timing should update the memory about the consolidation system. A lesson about RBAC should update the memory about the RBAC module. If there's no existing memory to update, that's a knowledge gap — fill it with one well-scoped memory, not a floating "lesson."

**Activate, don't merely record.** If the lesson says an agent, skill, hook, request, schedule, or tool behavior must change, the extraction is incomplete until a system object changes or a concrete follow-up request exists. Memory updates preserve context; they are not the deliverable for capability or behavior changes.

**Tag discipline is mandatory.** The `lesson-extracted` tag is reserved for structured procedural lessons that prescribe a specific behavioral change. Do **not** apply `lesson-extracted` to technical knowledge bases, daily digests, status summaries, or general documentation.

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
- You may still integrate useful facts into existing technical memories or skills.
- For pure summaries/digests/documentation, treat as reference material, not lessons.

### Negative Examples (Not Lessons)

- "A summary of what happened is NOT a lesson."
- "A list of technical facts is NOT a lesson."
- "A lesson must prescribe a specific behavioral change."

## Step 2.6: Tool Failure Pattern Review

Tool-call audit data is operational telemetry, **not memory content**. Use it to
find repeated failures and propose capability improvements, but do not store raw
audit rows as memories.

Run:

```
analyze_tool_failure_patterns(since_days=14, min_failures=3, limit=20)
```

For each returned pattern:

1. Confirm it is repeated evidence, not a one-off failure.
2. Classify the likely improvement target:
  - **Agent**: one agent consistently misuses a tool or ignores required workflow.
  - **Skill**: a loaded skill lacks concrete instructions for the failing tool/workflow.
  - **Tool/hook**: many agents fail the same tool in the same way, suggesting generic guidance, validation, or a hook.
3. Prefer proposing a focused skill when a reusable procedure would prevent the mistake.
4. Queue the smallest concrete human-reviewed change that your tools permit:
  - For an existing instance skill/agent, create a proposed replacement or create a follow-up request that asks a human to review and apply the update.
  - Create a proposed skill/agent/hook with `create_skill_definition`, `create_agent_definition`, or `create_hook_definition` when the capability does not exist.
  - When the missing link is a runtime binding, create a follow-up request that asks a human to grant the active skill/hook/server to the affected agent after review.
  - Create a `create_request` follow-up when the change belongs in a built-in on-disk definition, source code, or a protected object your tools cannot mutate.
5. Call `propose_definition_improvement` with:
  - `definition_type="skill"`, `"agent"`, or `"hook"`
  - a concrete `proposal_reason`
  - the pattern's `proposal_evidence`
  - `recommended_agent_id` or `recommended_agent_type` when a skill should later be granted to an agent

Do not approve your own proposal. The definition approval workflow is the human
review gate. A proposal by itself is evidence for review, not an activated
runtime change; pair it with a proposed definition or follow-up request unless blocked.

## Step 2.7: Capability Activation Gate (Required)

Before marking any source as processed, answer this question:

> Does this lesson imply the system should be able to do something differently next time?

If **yes**, take one of these actions before continuing:

| Needed change | Required action |
|---|---|
| Existing instance skill is incomplete | `get_skill_definition`, draft revised content, then create a proposed replacement or follow-up request for human review |
| Existing instance agent has wrong behavior | `get_agent_definition`, draft revised content, then create a proposed replacement or follow-up request for human review |
| Missing reusable workflow | `create_skill_definition` with evidence-backed `proposal_reason` and `proposal_evidence` |
| Missing role/agent | Create needed skills first, then `create_agent_definition`; include `skill_names` in frontmatter |
| Existing active agent lacks an active skill | `create_request` asking a human to review and grant the skill |
| Built-in definition/source file must change | `create_request` targeted at `kahinton/lucent` and the relevant `.github/` or source path |
| Missing work item to exercise the new capability | `create_request` and, where appropriate, `create_task` |

If no action is possible because of permissions or missing tooling, log that
blocker with `log_task_event` and create a follow-up request. Do not silently
fall back to a memory note.

Never grant yourself access to skills, hooks, MCP servers, or other runtime powers. Never approve your own proposals. Human review is the activation boundary.

## Step 3: Find the Memory to Update

This is the critical step. For each non-routine experience:

```
search_memories(query="<the topic/module/system this lesson is about>", limit=10)
```

Look for:
- Technical memories about the relevant file, module, or system
- Skills about the relevant workflow or process
- Any memory whose scope covers this lesson's domain

**If a matching memory exists**: Update it with the new knowledge using `update_memory` only after the Capability Activation Gate is satisfied. Append the insight to the existing content — don't rewrite the whole thing, just add what's new.

**If no matching memory exists**: This reveals a genuine knowledge gap. Create ONE technical or experience memory scoped to the right level (file, module, system, or work session). Include the lesson as part of its content, not as a standalone "Lesson:" entry. If the gap is a reusable workflow or role, update/create the corresponding skill/agent/request first; the memory is only the context trail.

**For correction-tagged memories**: When integrating a correction, note the correction source in the updated memory. User corrections (tagged `correction`): note "Corrected by user feedback" with date. Self-corrections (tagged `self-correction`): note "Self-corrected" with date. This creates traceable lineage from correction event to knowledge update.

## Step 3.5: Required Lesson Format

Every extracted lesson (the content being integrated) must include both sections below:

### Behavioral Change
- State the exact behavior to adopt going forward.
- Must be specific and testable (who does what, when).

### Verification
- State how to confirm the behavior is being applied.
- Include an observable signal (checklist item, metric, test, audit query, or review criterion).

## Step 4: Mark Sources as Processed

```
update_memory(
  memory_id="<source_id>",
  tags=[...existing_tags, "lesson-extracted"]
)
```

Apply `lesson-extracted` **only** when Step 2.5 passed and the lesson includes both required sections from Step 3.5.

If a source was reviewed but is not a qualifying lesson (digest, status summary, technical KB, general docs), do not apply `lesson-extracted`.

Tool-audit patterns are not source memories and should not receive memory tags.
If a pattern produces a definition proposal, the proposal's `proposal_evidence`
is the traceable source of why the change was suggested.

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
- **Stopping at memory updates for capability gaps** — if the lesson says a role, skill, hook, request, or tool must change, make or request that change
- **Extracting from a single occurrence** — wait for 2+ confirming instances before treating something as a pattern
- **Storing raw audit data as memory** — audit rows belong in `tool_call_audit_log`; use definition proposals with evidence for improvements
- **Proposing vague definition changes** — every audit-driven proposal must identify the repeated failure, target agent/skill/tool, and verification behavior
- **Writing lessons too vaguely to be actionable** — "be careful with X" is not a lesson
- **Not marking sources as `lesson-extracted`** — leads to re-processing loops
- **Tagging digests/KBs as `lesson-extracted`** — this dilutes lesson quality metrics and breaks extraction audits
- **Missing Behavioral Change or Verification sections** — incomplete lessons are not valid lessons
- **Increasing total memory count** — extraction should consolidate, not expand
