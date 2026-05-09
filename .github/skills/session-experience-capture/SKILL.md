---
name: session-experience-capture
description: 'Create high-signal experience memory summaries from meaningful LLM/chat sessions. Use when summarizing a completed or ongoing session into durable memory, especially when the session created requests, changed memories, used mutating tools, or produced user-visible work.'
---

# Session Experience Capture

This skill produces durable experience memories from meaningful sessions. The goal is not to archive a transcript; it is to capture what happened, why it mattered, and what future Lucent or the user should know.

## Before Starting

Use the provided session context only. Do not invent actions, outputs, decisions, or results that are not present in the transcript, request links, tool events, or page/work context.

Confirm the session is worth capturing. Good signals include:

- A request/task/goal was created, changed, reviewed, or discussed in depth
- Memories, definitions, skills, hooks, schedules, sandboxes, integrations, or project files were materially changed
- The user made a decision, gave a correction, or clarified a durable preference
- The session produced an output the user can use later
- The session explains why a larger workstream moved in a particular direction

Skip or produce `NO_EXPERIENCE_NEEDED` for:

- Jokes, greetings, thanks, or quick status checks with no durable decision
- Pure lookup/recall where nothing was learned or changed
- Short conversations that only restate visible UI content

## Procedure

### 1. Identify the session arc

Summarize the session as a short narrative:

- What the user wanted
- What Lucent did
- Important decisions or corrections
- Requests/tasks/outputs/memories that were created or changed
- What remains to follow up, if anything

### 2. Preserve useful specifics

Include durable identifiers only when they help reconnect work later:

- request IDs or titles
- task/output types
- memory IDs only when they are important anchors
- repo/path/module names when code was involved
- links or external IDs only when they are central to the outcome

Do not dump every metadata field. Metadata belongs in metadata; the experience content should read like a useful work note.

### 3. Write a good experience memory

Use this structure exactly:

```markdown
## Session Summary

<2-5 sentences describing the actual work and outcome.>

## What Happened

- <specific event/action/result>
- <specific event/action/result>

## Why It Matters

<1-3 sentences explaining the durable context or lesson.>

## Follow-up

- <remaining action, open question, or "None identified.">
```

Keep it concise. Prefer 250-700 words. If the session was large, summarize themes and key outcomes instead of reproducing the transcript.

## Recording Results

Return only the memory content. Do not wrap it in JSON unless the caller explicitly asks. If no experience should be created, return exactly:

`NO_EXPERIENCE_NEEDED`

## Anti-Patterns

- Writing a metadata report instead of a narrative summary
- Capturing trivial chat because a transcript exists
- Treating every tool call as important
- Inventing unstated outcomes or follow-up items
- Creating multiple memories for the same ongoing session instead of updating the session experience
