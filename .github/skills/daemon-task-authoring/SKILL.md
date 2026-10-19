---
name: daemon-task-authoring
description: 'Turn a user's durable-work intent into a complete request in chat, or decompose approved requests into tasks when running as the daemon planner. Use for queued work, goal-linked work, task authoring, priority calibration, or task validation failures.'
---

# Durable Work and Daemon Task Authoring

This skill has two operating paths. Choose the path from the runtime context before using any work-creation tool.

| Runtime context | Responsibility | Allowed work creation |
|---|---|---|
| Direct conversation | Recognize intent, track goals, and hand complete work to the daemon | `create_request` only |
| Daemon cognitive planner | Turn a request into executable, validated tasks | `create_task` only after receiving a request |

## Conversation Path: Activate Durable Work

Use this path whenever a person is talking to Lucent directly. The user does not need to know whether requests, goals, or tasks exist.

### 1. Classify the Intent

- **Direct help now:** answer, investigate, edit, or otherwise work in the conversation. Do not queue a duplicate of work that can be completed now.
- **Durable work:** create a request when the user wants follow-through, background progress, later investigation, a fix, or an outcome that should survive the chat. Phrases such as "look into," "fix," "follow up," "keep working on," or "make sure" are sufficient when their target is clear.
- **Sustained outcome:** also track a goal when the user describes ongoing progress, multiple milestones, a deadline, or an objective that will outlive one request.
- **Recurring automation:** design a workflow or schedule and obtain confirmation before activating it, unless the user explicitly asks for immediate creation.

Tentative brainstorming and questions about what is possible are not authorization to create a request. Ask one focused question only when the necessary outcome or scope is genuinely ambiguous.

### 2. Reuse Existing Work and Goals

1. Call `list_active_work()` before creating a request.
2. Search relevant memories, including goal memories, before creating a goal or request.
3. If an active request already covers the intent, report it and link or update context rather than duplicating work.
4. If a matching goal exists, update or link it. If not, create a goal memory only when the user described an enduring outcome.

### 3. Create a Complete Request

Write a clear title (under 80 characters), a self-contained description, and calibrated priority. Include the desired outcome, available context, constraints, and the evidence that will establish completion. For code work, set `target_repo` and narrow with `target_paths` when known.

When a request advances an active structured goal milestone, pass that goal and milestone to `create_request`. Otherwise link the request to the relevant goal after creation.

A request may also carry `backend_id` (coding-engine backend) and `requested_model` (model). For normal tasks these request-level pins are authoritative: every task uses them even if the planner supplied a different default, so set them at request level when the whole request must run one way. Decision-step tasks ignore both. Choose the pair from `list_available_models`' `execution_targets`: pass a row's `model` and `backend_id`. Naming a model alone uses its `default_engine`; the model is not restricted to one engine. For a local-provider backend (Ollama / LM Studio), also choose one of that backend's models — set `requested_model`, or rely on the backend's `config.model`; the request is rejected without one.

```
create_request(
  title="Short title for the work",
  description="Full instructions — everything the daemon needs to do.",
  source="user",
  priority="medium",
  target_repo="owner/repo",
  target_paths=["relevant/path"]
)
```

### 4. Hand Off to the Daemon

After creating or linking the request, tell the user what is being tracked or queued and its expected next state. **Stop there. Do not call `create_task`, choose an agent type, choose a model, or decompose the work in conversation mode.** The daemon picks up the request, creates tasks, and dispatches them.

## Daemon Path: Decompose an Accepted Request

Use this path only as the daemon cognitive planner after a request exists. Do not ask the chat agent to perform it.

1. **Read the request and context** — Confirm the request is self-contained, inspect linked goals and memories, and identify its completion evidence.
2. **Break into tasks** — Decompose only when multiple independently accountable steps are needed. Keep each task completable within a single 3600-second session (see [Task Size](#task-size)).
3. **Verify task descriptions** — Each task description must be self-contained. An agent reading only the description should understand what to do without external context. Run every description through the [Description Checklist](#description-checklist).
4. **Submit tasks** — Call `create_task()` for each task, setting `request_id` to the request already received. Follow the [Per-Task Authoring](#per-task-authoring) steps below.

## Per-Task Authoring

For each task within a request, follow these steps:

1. **Define the goal** — State the single, specific outcome this task must produce. One task = one deliverable.
2. **Write the description** — Follow the [Description Checklist](#description-checklist). The description must be self-contained — an agent reading only this should know exactly what to do.
3. **Choose agent_type** — Pick a name returned by `list_agent_definitions(status='active')` (the [Agent Type Selection](#agent-type-selection) table gives examples). If it edits files → `code`. If it reads and synthesizes → `research`.
4. **Set priority and size** — Use [Priority Calibration](#priority-calibration) for priority. Ensure the task fits within a single 3600-second session (see [Task Size](#task-size)); decompose if too large.
5. **Add context** — Include specific file paths, search terms, constraints, and references to prior task results stored in memory. Omit nothing the agent would need.
6. **Submit** — Call `create_task(request_id=..., title=..., description=..., agent_type=..., model=..., priority=..., sequence_order=..., backend_id=...)`. `model` is optional: omit it and the daemon selects a default. Set it only when the task clearly needs a specialized capability (see the model-selection skill). Normal tasks use the parent request's `backend_id` and `requested_model`; request-level pins are authoritative and cannot be replaced by a task-level default (decision steps ignore them). To run work on a coding engine, choose an `execution_targets` row from `list_available_models` and set both its `model` and `backend_id`; naming the model alone uses its `default_engine` instead. Do not assume a model is pinned to one engine — the model's `engine` is its default, and any compatible engine is selectable through that engine's backend.

### Target Repository

Every request that involves code work MUST set `target_repo` in owner/repo format. This enables automatic injection of technical memories into the working agent's context. Optionally set `target_paths` to narrow which areas' conventions are loaded.

Without `target_repo`, the working agent starts with no codebase context and must discover conventions through manual memory searches — which is slow and unreliable.

## Writing Descriptions That Work

The description is the sub-agent's **entire understanding of what to do** (combined with its agent definition). Write it as instructions for a competent engineer who has never seen the codebase.

**Good:**
> Review the test files in `tests/` and identify which core modules in the database layer lack test coverage. List specific functions that have no corresponding tests. Focus on memory operations, search, and API key management.

**Bad:**
> Improve test coverage.

### Description Checklist

- [ ] States the objective clearly (what to produce, not just what area)
- [ ] Names specific files or directories when relevant
- [ ] Defines "done" (what does the output look like?)
- [ ] Includes constraints (don't modify X, only look at Y)
- [ ] Self-contained — no references to "the thing we discussed"

## Task Kinds

Task execution is described by two separate things:

- `agent_type` / `agent_definition_id` — **who** does the work (persona, skills,
  tools). Only used by agent-family kinds.
- `task_kind` + `execution_config` — **how** it executes (agent session,
  decision call, fusion, and future command/webhook kinds).

Call `list_task_kinds()` before creating a non-standard task. It returns each
kind's config schema, examples, model/agent/backend requirements, and capability
flags. Pass `task_kind` and `execution_config` to `create_task`; `decision_config`
and `fusion_config` remain accepted aliases for the `decision` and `fusion` kinds.
`agent` is the default kind. Do not send fields from one kind in another kind's
payload.

## Agent Type Selection

Agent types are dynamic: available definitions depend on the deployment and the
request owner's access. Call `list_agent_definitions(status='active')` and use a
name it returns — it is the source of truth. The table below is examples only,
not an exhaustive list.

| Agent type | Use when | Examples |
|-----------|----------|---------|
| `code` | Task edits files, runs tests, builds, or lints | Fix a bug, write tests, refactor a module |
| `research` | Task investigates, reads, and synthesizes | Compare approaches, audit a dependency, analyze patterns |
| `memory` | Task reads/writes/consolidates memories | Deduplication, tag cleanup, knowledge synthesis |
| `reflection` | Task analyzes behavior and proposes improvements | Review task outcomes, check for recurring failures |
| `documentation` | Task creates or updates documentation | Write a guide, update a README, document an API |
| `planning` | Task decomposes goals into actionable steps | Break down a feature, create a roadmap |
| `assessment` | Task discovers and profiles an environment | New workspace analysis, tool inventory |
| `definition-engineer` | Task creates or improves agent definitions or skills | Build a new agent for a domain, improve an existing skill, extract capability from a pattern |
| `decision-step` | Task is a System One choice/noul/score decision; requires `decision_config` | Classify an input, route a ticket, score a candidate |

**Rule of thumb:** If it edits files → `code`. If it reads and synthesizes → `research`. If it touches memories → `memory`.

### Decision Steps

Use `agent_type="decision-step"` when the deliverable is a typed answer from a
System One decision model, not prose or code. One task = one decision call.

- Call `list_available_models(category="decision")` and choose an id from its
  `decision_models` list. You may omit the model entirely and the daemon will
  select an available decision model at dispatch.
- `decision_config` requires a non-empty `questions` list (1–64). Each question
  has `name`, `type` (`noul`, `choice`, or `score`), `instructions`, and
  `criteria` for choice/score questions.
- `state` is optional named text fields. The executor also provides the parent
  request and completed sibling task results, so a step can score upstream work.
- `threshold_policy` is optional:
  `{questions: {name: {min: 0.0–1.0}}, on_fail: "fail_task" | "needs_review"}`.
- Name the decision model once — via `model=<decision id>` or
  `decision_config.model`, never both. `model="auto"`, `backend_id`,
  `output_contract`, and `fusion_config` do not apply to decision steps.

```json
create_task(
  request_id="<request-id>",
  title="Decide whether the release is ready",
  agent_type="decision-step",
  model="nimble:latest",
  decision_config={
    "questions": [
      {"name": "ready", "type": "noul",
       "instructions": "The release evidence shows every blocking check passes."}
    ],
    "state": {"release": "2026.10"},
    "threshold_policy": {
      "questions": {"ready": {"min": 0.8}},
      "on_fail": "needs_review"
    }
  }
)
```

Decision models are never valid for chat or normal code/research tasks. They
also do not appear in `execution_targets`; `decision_models` is their dedicated
selection list.

## Priority Calibration

| Priority | When to use |
|----------|------------|
| `urgent` | Critical and time-sensitive; dispatches ahead of `high` |
| `high` | Blocking other work, user-requested, or a bug fix |
| `medium` | Normal development work (default) |
| `low` | Cleanup, exploration, nice-to-have |

The daemon dispatches up to 2 tasks per cycle, highest priority first.

## Task Size

Tasks must complete within a single 3600-second session. If a task is too large, decompose it:

### Sequential Pattern (each builds on the last)
```
Task 1 (research): "Analyze test coverage gaps in the database layer"
  → Result stored in memory
Task 2 (code): "Write tests for the gaps identified in Task 1. Search memory for the analysis results."
  → References Task 1 via memory
Task 3 (code): "Run the new tests and fix any failures."
```

### Parallel Pattern (independent tasks, same cycle)
```
Task A (code, high): "Fix the SQL injection vulnerability in the search module"
Task B (documentation, low): "Update README with the new API endpoints"
Task C (research, medium): "Investigate connection pool sizing best practices"
```

## Validation

After completion, the daemon validates task results:
- Result must be non-empty (>=100 characters)
- Must not contain only error messages
- Must reference the task objective

**Common validation failures and their causes:**
| Failure | Root cause | Fix |
|---------|-----------|-----|
| Generic output | Description too vague | Be more specific about what to produce |
| Timeout | Scope too large | Decompose into smaller tasks |
| Error-only output | Wrong agent_type or missing tools | Match agent to required capabilities |
| Empty result | Agent couldn't find what was described | Verify file paths and search terms exist |

## Anti-Patterns

- **Demanding internal vocabulary:** Waiting for a user to say "create a request" or "make a goal" → classify the outcome and take the appropriate conversation-path action.
- **Chat-side decomposition:** Creating a request and immediately calling `create_task()` from direct chat → stop at the request handoff; the daemon owns decomposition.
- **Goal inflation:** Creating a goal for every task → use a goal only for an enduring outcome with meaningful progress to track.
- **Circular tasks:** "Review the last task's output and create a new task" → infinite loop
- **Approval-dependent chains:** Task B needs Task A approved, but approval is async → B stalls
- **Overly ambitious scope:** "Refactor the entire auth system" → timeout, partial results, validation failure
- **Vague instructions:** "Make it better" → agent has no way to determine success