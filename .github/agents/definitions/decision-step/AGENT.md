name: decision-step
description: Decision-step marker — tasks with decision_config are executed by a System One decision model through the daemon dispatcher, never through an agent session.
skill_names: []

# Decision Step

This definition marks **decision-step tasks**: tasks carrying a
`decision_config` payload (created via the REST API, MCP `create_task`, or a
workflow action). Tasks with this agent type are NOT run as agent sessions —
there is no chat model, no tools, no sandbox. The daemon dispatcher claims
such a task directly and executes it through the decision API
(`lucent.llm.decision`, Ollama 0.35+ `/v1/systemone`), then completes it with
typed answers as the task result.

The body below is documentation of that machinery (the dispatcher never reads
this prompt), so authors and reviewers understand what a decision step does.

## What Runs

1. **State** is built for the configured questions, in this order:
   - `request` — parent request title + description (2,000-char budget).
   - `task` — this task's title + description (3,000-char budget); this is
     the primary thing the questions judge.
   - `context` — completed sibling task results from the same request,
     oldest→newest, with each sibling's `result_structured` included when
     present (that is how a decision step scores upstream step outputs).
     Aggregate budget 8,000 characters.
   - `state` literals from `decision_config.state` — merged last, so authors
     can pin or override exact field labels.
2. **Questions** from `decision_config.questions` are answered in ONE
   System One call:
   - `noul` — probability a condition holds. The thresholdable type.
   - `score` — probability-weighted position on an ordered rubric
     (`criteria` = level labels low→high); answers are level-INDEX values,
     not percentages. Also thresholdable, against index values.
   - `choice` — picks one option; classification output, recorded only
     (never thresholded).
3. **Completion**:
   - No `threshold_policy` → the task completes with the answers as output
     (`validation_status="valid"`), always.
   - With `threshold_policy`, any `noul`/`score` question scoring below its
     `min` triggers the policy: `on_fail: fail_task` fails the task (a
     preventative gate — downstream `strict`-policy stages see the failure),
     or `on_fail: needs_review` routes the task to review with the answers
     attached.

## Authoring Rules

- `decision_config` always pairs with `agent_type: decision-step`; anything
  else is rejected at creation.
- `output_contract` cannot be combined with `decision_config` — the questions
  themselves are the schema.
- A decision model (`model_class="decision"`, e.g. local System One models
  like `nimble`) must be available to the request owner; set it via
  `decision_config.model` or the task's `model` field (not both). Omitted =
  the first enabled decision model the owner can use, resolved at dispatch.
- Question names must be unique (≤128 chars); 1–64 questions per step.