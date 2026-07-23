---
name: workflow-design
description: 'Use when helping a user design, draft, validate, or create Lucent workflows with schedule, manual, webhook, or integration-event triggers and ordered task/server actions.'
---

# Workflow Design

This skill turns a plain-language automation idea into a safe Lucent workflow that fits the request/task/review model.

## Before Starting

1. Read the host-provided `Active User Context` system block.
2. Inspect active agents with `list_agent_definitions(status="active")` before naming any `agent_type`.
3. Inspect existing workflows with `list_workflows` when the user may be duplicating or changing an existing automation.
4. Inspect available models with `list_available_models` before recommending model overrides.
5. Ask at most three clarifying questions if the trigger, action output, or review criteria are ambiguous.

## Procedure

### Step 1: Classify the Trigger

Map the user's description to exactly one trigger type:

- `schedule` — time-based repeat or one-time run.
- `manual` — user intentionally starts it from the workflow detail page.
- `webhook` — an external service sends an HTTP event to Lucent.
- `integration_event` — a named provider event such as `github_app.pull_request.opened`.

For scheduled workflows:
- Use `cron` when the user names calendar/time semantics such as weekday, business day, or 9 AM.
- Use `interval` when the user says every N minutes/hours/days.
- Use `once` only for a one-shot run.

### Step 2: Design the Request Template

Create a request template that a human can understand in Activity:

- `title_prefix`: `[Scheduled]` for schedule triggers, `[Workflow]` otherwise.
- `title`: short, specific, and stable. Use `{workflow_title}` or `{event_summary}` only when they improve clarity.
- `description`: explain why the run exists, what inputs it uses, and what success means.
- `dependency_policy`: default to `strict`; use `permissive` only when independent actions can partially succeed.

### Step 3: Design Ordered Actions

Create one to five actions. Each action must include:

- `action_type: "task"` for agent-dispatched work.
- `title`: verb phrase with a concrete deliverable.
- `description`: exact task instructions including what outputs to record.
- `agent_type`: choose an active agent returned by `list_agent_definitions`; never invent labels such as `general-purpose`.
- `priority`: inherit the workflow priority unless one action is clearly urgent.
- `sequence_order`: zero-based ordering.

Use multiple actions only when order or accountability matters. Do not create multiple actions just to look thorough. If you cannot verify an agent type is active, use `code` for implementation/verification work, `documentation` for docs/changelog work, `research` for investigation, `memory` for memory maintenance, `api-testing` for endpoint checks, or `lucent` for coordination — but only when those names appear in the active-agent list.

### Step 4: Define Review Criteria

Write `review_instructions` as a checklist. Include:

- Required durable outputs: links, files, PRs, docs, memories, deployment records, or run summaries.
- Rejection criteria: missing outputs, unverifiable claims, unsafe changes, or incomplete evidence.
- Any domain-specific constraints from the user's description.

### Step 5: Present a Draft Before Creating

Unless the user explicitly says to create it immediately:

1. Show a concise draft with trigger, request, actions, review checklist, and any secret/setup needs.
2. Ask for confirmation or missing values.
3. Only call `create_workflow` after the user confirms or clearly asks you to create it.

### Step 6: Create the Workflow

When creating:

1. Convert `actions` to compact JSON and pass it as `actions_json`.
2. Convert `request_template` to compact JSON and pass it as `request_template_json`.
3. For webhook workflows, require the user to provide or generate a shared secret; never invent a weak secret.
4. Call `create_workflow`.
5. Report the workflow name, trigger, workflow ID if returned, webhook endpoint if relevant, and what to test next.

## Recording Results

After creating a meaningful workflow, create or update a shared technical memory only if the workflow encodes reusable platform behavior or an important operational convention. Do not create memory for every ordinary user automation.

## Anti-Patterns

| Anti-Pattern | Why It Fails | What To Do Instead |
|---|---|---|
| Creating before the user confirms | Workflows are active runtime objects, not harmless drafts | Present a draft first unless creation was explicit |
| Vague action descriptions | Agents produce unverifiable work | Specify inputs, outputs, and evidence |
| Overusing model overrides | Makes workflows brittle and costly | Prefer daemon defaults unless a model capability is required |
| Webhook without a secret | External trigger is unauthenticated | Require a strong shared secret |
| Duplicate workflows | Causes repeated work and noisy requests | Check existing workflows before creating similar automation |
