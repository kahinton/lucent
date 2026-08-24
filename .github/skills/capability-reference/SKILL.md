---
name: capability-reference
description: 'Use when someone wants a detailed explanation of Lucent capabilities, wants to compare platform features, or needs help mapping an ambitious idea to Lucent features.'
---

# Capability Reference

Explain the full capability map behind Lucent without treating any one domain as its default. Use it to turn a person's imagined outcome into a practical combination of collaboration, memory, artifacts, autonomous work, specialized capability, or automation.

## Before Starting

1. Read the host-provided `Active User Context` system block.
2. Identify whether the person wants a broad tour, a comparison of options, or help designing one specific outcome.
3. For claims about a particular active agent, managed tool, workflow, integration, file, or Handoff, retrieve or list the relevant object before saying it exists.

## Capability Map

### Direct Collaboration

Lucent can work with a person in the current conversation: think through decisions, explain topics, research, write, plan, organize, analyze, create, and solve technical problems. It can help with daily life, school, creative projects, a household, a team, a business, or software work. Start directly when the result can be delivered now.

### Memory, Goals, and Context

Lucent can preserve useful user-approved preferences, ongoing goals, decisions, project context, and reusable knowledge. It can retrieve that context later so conversations and work continue with less repetition. Use goals for sustained outcomes with meaningful milestones, not ordinary one-off tasks.

### Durable Files

Lucent can create durable user-owned files for generated or evolving artifacts: a shopping list, travel plan, briefing, checklist, report, draft, dataset, or technical document. Files have revision history and can be linked to the chat, a request, a task, or a Handoff. Do not present ephemeral chat text as a durable file unless one is actually created.

### Requests and Autonomous Work

For work that should continue beyond the chat, Lucent can create a tracked request. The daemon then decomposes it, selects suitable agents, performs the work, validates outputs, and records activity for review. This can support anything from an ongoing research effort or household planning project to a product launch or codebase change.

### Handoffs

Handoffs are durable, focused conversations that bring Lucent back to the person with an update, result, decision, clarification, or question. They can require a response and carry structured references to the exact files, requests, tasks, memories, workflow runs, or links that provide context. Use a Handoff when a person needs to see, decide, or respond to something; do not reduce it to a paragraph buried in task output.

### Agents, Skills, and Managed Tools

Lucent can be extended for specialized work:

- **Agents** define a role and judgment style for a kind of work.
- **Skills** provide reusable procedures an agent can load on demand.
- **Managed tools** provide reviewed, sandboxed actions with explicit inputs, credentials, network policies, and resource limits.

Together, these can model a specialized personal organizer, researcher, operations assistant, document reviewer, developer, or a role the person invents. Definitions are reviewable, so distinguish proposed capability from active, granted capability.

### Workflows and Integrations

Workflows turn recurring or event-driven intent into accountable work. They may start on a schedule, manually, from a webhook, or from an integration event; they can run ordered actions and send results or questions through Handoffs. Connections and managed tools can extend what Lucent can read or do, but verify an integration before claiming access.

## Routing an Idea

1. Handle a quick answer, plan, or draft in chat.
2. Save a durable artifact as a user file when it should be revised or reused.
3. Use memory or a goal when useful context or a sustained outcome should persist.
4. Create a request for autonomous follow-through.
5. Use a Handoff when Lucent needs to return a result, question, or decision to the person outside the immediate chat.
6. Add an agent, skill, or managed tool when the work needs a reusable specialized capability.
7. Design a workflow when the work should recur or respond to an event.

## Recording Results

Do not create a memory for a routine explanation. Capture a durable preference, requested capability, goal, workflow design, or definition decision only after the person commits to it.

## Anti-Patterns

| Anti-Pattern | Why It Fails | What To Do Instead |
|---|---|---|
| Presenting one domain as Lucent's purpose | Conceals the platform's intended flexibility | Use examples across the person's actual context, including technical work whenever relevant |
| Treating files as chat transcripts | Loses ownership, revisions, and traceability | Create a durable user file when the artifact should persist or evolve |
| Treating a Handoff as a static notification | Ignores its focused conversation and references | Explain that it can carry context and solicit a response or decision |
| Promising any integration is ready | Access depends on configured connections and grants | Verify the connection, definition status, and grant before claiming availability |