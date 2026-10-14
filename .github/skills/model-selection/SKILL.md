---
name: model-selection
description: 'Choose the right LLM model for a task based on complexity, cost, speed, and capability requirements. Use when creating tasks, dispatching sub-agents, or when the daemon needs to decide which model to assign.'
---

# Model Selection

How to pick the right model for a given task. The goal is matching task demands
to model strengths — not always using the most powerful option, and not
hardcoding model IDs that may not be enabled in a deployment.

## When to Use

- Creating a task via `create_task` and deciding whether to set the `model` field
- Deciding whether a task should run on a **coding engine** (Copilot / Claude
  Code / Codex) and, if so, which backend — see [Coding Engines](#coding-engines)
- Daemon dispatch deciding which model to assign to a sub-agent
- Reviewing task failures that might be caused by model mismatch
- Optimizing cost by avoiding premium models where the default is sufficient

## Selection Principle

Default first. Use the configured/enabled default model whenever there is no
clear reason to specialize. Model catalogs are provider- and user-specific, so
do not hardcode model IDs in plans, prompts, or task decomposition. Ask for the
available models and choose by capability category.

## Coding Engines

Most work runs through the general LLM layer and needs only a `model`. Separately,
Lucent can run a task on a **coding engine** — GitHub Copilot, Claude Code, or
OpenAI Codex — by setting the task's `backend_id`. Each engine runs either its own
provider's models or a local model server (e.g. Ollama).

Call `list_engines` to see the engines, their availability, and the backends
available to you. Every backend lists the models it can run and may declare a
`default_model`.

Choose a coding engine when the task is genuine code work that benefits from the
engine's own agent loop and tooling (file edits, refactors, multi-file
implementation, sustained edit-test cycles). Otherwise leave `backend_id` unset
and just pick a `model` as above.

```text
list_engines()                       # engines + backends (+ each backend's models)
create_task(request_id=..., title=..., description=..., agent_type="code",
            model="<a model that backend lists>", backend_id="<backend id>")
```

**Be consistent within a request.** Decide once whether this request's work runs
on a coding engine, and set the same `backend_id` on *every* task that does
comparable work. Setting it on some tasks but not others yields mixed execution —
part of the work on the engine, part on the general layer — and is almost always a
mistake. If a backend declares a `default_model`, you may omit `model` and the
backend's default is used.

## Capability Categories

| Category | Strengths | Cost tendency | Use when |
|----------|-----------|---------------|----------|
| `fast` | Speed, simple tasks, low cost | Cheap | Memory tagging, simple lookups, formatting, status checks |
| `general` | Balanced quality/cost, tool use | Standard | Most code, docs, review, planning, and research tasks |
| `reasoning` | Deep analysis, trade-offs, synthesis | Premium | Architecture, security, root-cause analysis, complex planning |
| `agentic` | Sustained execution and edit-test loops | Premium | Large refactors, multi-file implementation, autonomous coding sessions |
| `visual` | Image/input understanding | Varies | Tasks with explicit image or visual context |

## Procedure

### Step 1: Assess Task Complexity

Classify the task into one of these tiers:

| Complexity | Signals | Examples |
|------------|---------|----------|
| **Simple** | Single-step, mechanical, no reasoning | Tagging, formatting, status checks, boilerplate |
| **Standard** | Multi-step but well-defined, moderate reasoning | Feature implementation, bug fixes, test writing, docs |
| **Complex** | Multi-step reasoning, trade-off analysis, judgment | Architecture decisions, security review, planning |
| **Agentic** | Sustained autonomous execution, edit-test loops | Large refactors, autonomous coding sessions |

### Step 2: Check Available Models and Default

Call `list_available_models(agent_type="<type>")` to get the enabled model list,
the configured `default_model`, and the selector's recommendation. Use the
default unless task requirements clearly justify a specialized category.

### Step 3: Consider Cost/Speed Tradeoff

Apply this decision rule:

- **No clear specialized need** → default model
- **Must be fast, accuracy non-critical** → enabled `fast` model
- **Must be deeply correct, cost acceptable** → enabled `reasoning` model
- **Must run autonomously for many steps** → enabled `agentic` model

If rate limits are being hit, downgrade non-critical tasks one tier.

### Step 4: Select Model

Pick the default model unless the task's requirements match a specialized
category. If overriding the default, note the reason in the task description or
planning summary.

```text
create_task(request_id=..., title=..., model="<selected-model only if needed>", ...)
```

## Reference: Task Type → Category Mapping

### Fast / Lightweight
Tasks: memory tagging, simple lookups, formatting, status checks, boilerplate generation

Pick an enabled `fast` model when one exists. If no fast model is enabled, use
the default model.

### General Coding
Tasks: feature implementation, bug fixes, test writing, refactoring, documentation

Use the default model. Only override for multi-file, sustained, or high-risk
work where a specialized category is clearly justified.

### Deep Reasoning / Analysis
Tasks: architecture decisions, complex debugging, security review, code analysis, planning

Pick an enabled `reasoning` model when the task requires thinking through
multiple steps, weighing trade-offs, or analyzing a large codebase. Otherwise,
use the default model.

### Agentic / Multi-step Execution
Tasks: autonomous coding sessions, edit-test loops, complex refactors across many files

Pick an enabled `agentic` model when the task involves sustained edit-test
cycles or a large autonomous refactor. Otherwise, use the default model.

### Research / Long Context
Tasks: reading large documents, cross-referencing multiple sources, literature review

Prefer the default model for ordinary research. Pick an enabled long-context or
`reasoning` model only when the source volume or synthesis complexity requires it.

### Reflection / Self-Assessment
Tasks: reviewing own output quality, extracting lessons, meta-analysis

Use the default model for ordinary review. Pick an enabled `reasoning` model for
high-stakes reflection, complex failure analysis, or nuanced judgment.

## Cost Awareness

Models have different premium request costs. In rough order from cheapest to most expensive:

1. **Cheapest:** `fast` models
2. **Standard:** `general` models
3. **Premium:** `reasoning` models
4. **Most expensive:** large-context or `agentic` models

Don't use premium models for tasks that a standard/default model handles fine.
Save the expensive ones for where they make a real difference.

## When to Override Defaults

The `select_model_for_task()` function in `src/lucent/model_registry.py` provides
baseline recommendations. Override when:

- A task failed with the default model and needs more reasoning power
- Rate limits are being hit — downgrade non-critical tasks to cheaper models
- A task specifically needs vision capabilities (check `supports_vision`)
- The task involves extremely long input — prefer models with large context windows
- Provider diversity matters (don't put all tasks on one provider)

## Anti-Patterns

- **Always using premium reasoning models** — Most tasks don't need them.
- **Ignoring task failures** — If a model consistently fails on a task type, try a different one rather than retrying with the same model.
- **Specifying a model without a reason** — Let the default selector handle standard tasks.
- **Chasing new models** — A model being newer doesn't make it better for your specific task. Stick with what works until you have evidence otherwise.

