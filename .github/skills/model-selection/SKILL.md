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
  Code / Codex) and, if so, which backend — see [Models, Providers, and
  Engines](#models-providers-and-engines)
- Daemon dispatch deciding which model to assign to a sub-agent
- Reviewing task failures that might be caused by model mismatch
- Optimizing cost by avoiding premium models where the default is sufficient

## Selection Principle

Default first. Use the configured/enabled default model whenever there is no
clear reason to specialize. Model catalogs are provider- and user-specific, so
do not hardcode model IDs in plans, prompts, or task decomposition. Ask for the
available models and choose by capability category.

## Models, Providers, and Engines

Every model has a `provider` and a derived list of compatible `engines`. The
`engine` stored on a model is only its **default engine** — it does not restrict
where that model can run.

Call `list_available_models(agent_type="<type>")` and select from its
`execution_targets` list. Each row is a concrete model + engine + backend
pairing:

```json
{"model": "...", "provider": "anthropic", "engine": "copilot",
 "engine_display": "GitHub Copilot", "backend_id": "...",
 "backend_name": "...", "available": true, "default": false}
```

The controlled provider→engine map is:

| Provider | Compatible engines |
|---|---|
| `anthropic` | `langchain`, `copilot`, `claude_code` |
| `copilot` | `copilot` |
| `google` | `langchain`, `copilot` |
| `lmstudio` | `copilot`, `codex` |
| `ollama` | `langchain`, `copilot`, `claude_code`, `codex` |
| `openai` | `langchain`, `copilot`, `codex` |
| `xai` | `copilot` |

`custom` has no engine yet and is omitted from the map.

**Default engine.** Naming a model with no `backend_id` runs it on that model's
`default_engine`. The default is `langchain` for Ollama; for other providers it
is the deployment's configured default engine (`LUCENT_LLM_ENGINE`, currently
`copilot`) when compatible, otherwise `langchain`, otherwise the first compatible
engine. A model's own stored `engine` (when set) is its default engine. The
`default` flag on an execution target marks the default target.

**Choosing a target.**

- General work: pick an `available` execution target, or omit `model` and
  `backend_id` to use default routing.
- Different engine: pass both `model` and that row's `backend_id` to
  `create_task`. The backend pins the engine; naming the model alone uses its
  default engine instead.
- Validity: a pairing is valid only when `model.provider == backend.provider`.
  Backend creation, task validation (`validate_backend_selection`), and engine
  overrides (`validate_engine_override`) enforce the map.
- Local providers need a configured backend to run on a coding engine. A local
  model named without `backend_id` runs on its default engine (for Ollama,
  `langchain`).
- Decision models (`decision_model: true`) have no `execution_targets` — never
  choose one for normal task work.

Call `list_engines` for backend detail: each engine carries
`supported_providers`; each backend carries `provider`, `provider_label`,
`provider_supported`, `enabled`, `default_model`, and the `models` it can run.
Only use backends whose `enabled` flag is true. If a backend declares a
`default_model`, you may omit `model` and the backend's default is used.

```text
list_available_models(agent_type="code")   # catalog + execution_targets
create_task(request_id=..., title=..., description=..., agent_type="code",
            model="<target model>", backend_id="<target backend_id>")
```

Choose a coding engine when the task is genuine code work that benefits from the
engine's own agent loop and tooling (file edits, refactors, multi-file
implementation, sustained edit-test cycles). Otherwise use the general layer.

**Be consistent within a request.** Decide once whether this request's work runs
on a coding engine, and use the same execution target on *every* task doing
comparable work. Setting `backend_id` on some tasks but not others yields mixed
execution — part of the work on the engine, part on the general layer — and is
almost always a mistake. A request can carry `backend_id` and `requested_model`
itself; tasks created from it inherit both unless they override them.

### Local providers

Coding engines reach local models through a provider backend:

- **Ollama** (>= 0.14) exposes OpenAI-, Anthropic-, and Codex-OSS-compatible
  surfaces, so it works with all three coding engines.
- **LM Studio** exposes OpenAI- and Codex-OSS-compatible surfaces (Copilot and
  Codex).

Claude Code speaks only the Anthropic Messages API. Its backend supplies the
gateway URL from `config.base_url`, falling back to `OLLAMA_HOST`, then the
provider default, along with `ANTHROPIC_AUTH_TOKEN` when the gateway requires
auth. Choose the backend from `list_engines`; do not assemble these settings in
a task description.

**Local-provider pins need a model.** When pinning an Ollama or LM Studio
backend, also name one of that backend's `models` (as `model` on a task, or
`requested_model` on a request) unless the backend declares `config.model`.
Request creation is rejected otherwise: there is no safe global default, and an
auto-selected cloud model would be handed to the local engine.

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
the configured `default_model`, the `provider_engine_map`, each model's
`provider`, `default_engine`, and compatible `engines`, the flat
`execution_targets` list, and the selector's recommendation. Use the default
unless task requirements clearly justify a specialized category.

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

For a plain model choice, set only `model` and use its default engine. To run it
on a different engine, choose a matching row from `execution_targets` and pass
that row's `backend_id` alongside the model.

```text
create_task(request_id=..., title=..., model="<selected-model>",
            backend_id="<target backend_id only when overriding the default engine>", ...)
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
- **Treating a model's engine as a restriction** — The stored `engine` is only the default. Any compatible engine is valid when you pass that target's `backend_id`.
- **Chasing new models** — A model being newer doesn't make it better for your specific task. Stick with what works until you have evidence otherwise.

