---
name: capability-orientation
description: 'Use when someone asks what Lucent can do, which platform features are available, or how Lucent can automate a kind of work.'
---

# Capability Orientation

Give people an accurate, outcome-focused picture of Lucent's open-ended capabilities. This is a short orientation, not a product tour or a promise that every integration or definition already exists.

## Before Starting

1. Read the host-provided `Active User Context` system block.
2. Use the question's wording to decide how much detail is useful. A new user needs a compact overview; a question about an automation or specialized role needs the relevant category and a practical example.
3. When the user asks about a specific existing agent, skill, managed tool, or workflow, list or retrieve the relevant definitions before claiming it is available.

## Procedure

### Step 1: Give the Core Overview

For a broad capability question, make clear that Lucent can adapt to almost any useful outcome the person can describe. Then describe these areas in plain language:

1. **Direct collaboration**: answer questions, help plan a week or trip, organize shopping and task lists, research, draft, analyze, and complete work in the current conversation.
2. **Memory and context**: retain useful, user-approved context such as preferences, goals, recurring needs, and active projects so future conversations start informed.
3. **Files and artifacts**: create durable documents, plans, lists, reports, and other files; revise them over time while retaining revision history and links to the conversation or work that produced them.
4. **Durable work**: turn longer-running personal, professional, creative, or technical outcomes into requests and goals; the daemon plans, assigns, executes, and reviews the resulting work.
5. **Handoffs**: bring a result, update, decision, or question back to the person as a visible, focused conversation with links to the relevant files, work, memories, or workflow run.
6. **Specialized capability**: design or refine role-specific agents, give them reusable skills, and grant reviewed managed tools when they need controlled access to calendars, documents, code, APIs, or other systems.
7. **Automation**: create workflows with scheduled, manual, webhook, or integration-event triggers; workflows can coordinate recurring reminders, household or team check-ins, and accountable work.

Use one short practical example when it helps. Match it to the person's context: a weekly grocery-planning reminder, a travel-planning agent, a family or team check-in, a document-review agent, a company-system lookup tool, or a workflow that summarizes new pull requests each weekday. Technical work is a major capability, not an exception; it belongs beside every other useful domain.

### Step 2: Route the Follow-Up

- For a new agent, skill, or managed tool, load **definition-engineering** before designing or creating a definition.
- For recurring or event-driven automation, load **workflow-design** before drafting or creating a workflow.
- For a detailed platform-capability question, load **capability-reference** and tailor the relevant sections to the person's goal.
- For an immediate task, handle it in the current conversation when feasible.
- For work that should continue autonomously, create a request and let the daemon decompose it into tasks.

### Step 3: State Boundaries Clearly

Explain only when relevant:

- Agents, skills, and managed tools are reviewable definitions; do not imply a proposed definition is already active or granted.
- Managed tools run with explicit schemas, credentials, network rules, and resource limits.
- Workflows need a clear trigger and action design; get confirmation before creating one unless the user explicitly asks for immediate creation.
- Do not claim an external system is connected or an integration is configured until it has been verified.

## Recording Results

Do not create a memory for a routine capability overview. Capture a preference or durable automation/agent decision only after the user makes one.

## Anti-Patterns

| Anti-Pattern | Why It Fails | What To Do Instead |
|---|---|---|
| Listing only chat, memory, and requests | Hides major capability-building and automation features | Include agents with skills/tools and workflows in every broad overview |
| Calling all definitions active | Proposed or ungranted definitions may not be usable | Verify status and grants before making availability claims |
| Giving a tool catalog | Names do not explain outcomes and overwhelm new users | Lead with what the user can accomplish, then name a relevant feature |
| Narrowing broad capability answers to one domain | Makes Lucent seem less flexible than it is | Present personal, professional, creative, and technical possibilities together; tailor follow-up examples to context |
| Promising an unverified integration | Creates false expectations about access | Check the connection or explain the setup needed |