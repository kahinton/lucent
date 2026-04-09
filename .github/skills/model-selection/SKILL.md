---
name: model-selection
description: 'Choose the right LLM model for a task based on complexity, cost, speed, and capability requirements. Use when creating tasks, dispatching sub-agents, or when the daemon needs to decide which model to assign.'
---

# Model Selection

How to pick the right model for a given task. The goal is matching task demands to model strengths — not always using the most powerful option.

## When to Use

- Creating a task via `create_task` and choosing the `model` field
- Daemon dispatch deciding which model to assign to a sub-agent
- Reviewing task failures that might be caused by model mismatch
- Optimizing cost by downgrading tasks that don't need premium models

## Model Comparison Table

| Model | Category | Strengths | Cost Tier | Recommended agent_types |
|-------|----------|-----------|-----------|------------------------|
| `claude-haiku-4.5` | Fast | Speed, simple tasks, low cost | Cheap | memory, fast |
| `gemini-3-flash` | Fast | Speed, lightweight inference | Cheap | memory, fast |
| `gpt-5-mini` | Fast | Cheap, decent quality | Cheap | memory, fast |
| `gpt-4.1` | General | Balanced quality/cost, tool use | Standard | code, documentation |
| `claude-sonnet-4.6` | General | Strong coding, vision, tools | Standard | code, documentation, review |
| `claude-sonnet-4.5` | General | Coding, tool use | Standard | code, documentation |
| `gemini-3-pro` | Reasoning | Long context, research synthesis | Premium | research, planning |
| `claude-opus-4.6` | Reasoning | Deep reasoning, nuanced judgment | Premium | reflection, planning, review |
| `gpt-5.3-codex` | Agentic | Multi-step execution, edit-test loops | Premium | code (agentic), refactoring |
| `gpt-5.2-codex` | Agentic | Sustained tool-calling workflows | Premium | code (agentic) |

## Procedure

### Step 1: Assess Task Complexity

Classify the task into one of these tiers:

| Complexity | Signals | Examples |
|------------|---------|----------|
| **Simple** | Single-step, mechanical, no reasoning | Tagging, formatting, status checks, boilerplate |
| **Standard** | Multi-step but well-defined, moderate reasoning | Feature implementation, bug fixes, test writing, docs |
| **Complex** | Multi-step reasoning, trade-off analysis, judgment | Architecture decisions, security review, planning |
| **Agentic** | Sustained autonomous execution, edit-test loops | Large refactors, autonomous coding sessions |

### Step 2: Check agent_type Recommendation

Call `list_available_models(agent_type="<type>")` to get the system's recommended model for the agent. Use the Model Comparison Table above to verify the recommendation fits the task's actual complexity — the default may over- or under-shoot.

### Step 3: Consider Cost/Speed Tradeoff

Apply this decision rule:

- **Must be fast, accuracy non-critical** → Cheap tier (Haiku, Flash, GPT-5-mini)
- **Must be correct, speed acceptable** → Standard tier (Sonnet, GPT-4.1)
- **Must be deeply correct, cost acceptable** → Premium tier (Opus, Gemini Pro)
- **Must run autonomously for many steps** → Agentic tier (Codex models)

If rate limits are being hit, downgrade non-critical tasks one tier.

### Step 4: Select Model

Pick the model that matches the intersection of complexity tier and cost tolerance. If the system recommendation from Step 2 aligns, use it. If not, override with your selection and note the reason.

```
create_task(request_id=..., title=..., model="<selected-model>", ...)
```

## Reference: Task Type → Model Mapping

### Fast / Lightweight
Tasks: memory tagging, simple lookups, formatting, status checks, boilerplate generation

**Pick:** `claude-haiku-4.5` or `gemini-3-flash`

These are cheap and fast. Use them for anything that doesn't require multi-step reasoning. If a task is mechanical — extracting tags, reformatting text, simple CRUD — don't waste a premium model on it.

### General Coding
Tasks: feature implementation, bug fixes, test writing, refactoring, documentation

**Pick:** `claude-sonnet-4.6` (default) or `gpt-4.1`

The workhorse tier. Sonnet 4.6 is the default for a reason — it handles most coding tasks well, supports vision and tools, and balances quality with cost. GPT-4.1 is a solid alternative.

### Deep Reasoning / Analysis
Tasks: architecture decisions, complex debugging, security review, code analysis, planning

**Pick:** `claude-opus-4.6`, `gpt-5.4`, or `gemini-3-pro`

When the task requires thinking through multiple steps, weighing trade-offs, or analyzing a large codebase. Opus is strongest here. GPT-5.4 and Gemini 3 Pro are alternatives if you need provider diversity.

### Agentic / Multi-step Execution
Tasks: autonomous coding sessions, edit-test loops, complex refactors across many files

**Pick:** `gpt-5.3-codex` or `gpt-5.2-codex`

OpenAI's Codex models are optimized for agentic workflows — they're better at sustained multi-step execution with tool calls. Use these when the task involves iterating through edit-test cycles.

### Research / Long Context
Tasks: reading large documents, cross-referencing multiple sources, literature review

**Pick:** `gemini-3-pro` or `gemini-2.5-pro`

Google's models handle long contexts well and are strong at research-style synthesis across multiple sources.

### Reflection / Self-Assessment
Tasks: reviewing own output quality, extracting lessons, meta-analysis

**Pick:** `claude-opus-4.6`

Reflection benefits from the highest reasoning capability available. This is where you want the model that's best at nuanced judgment.

## Cost Awareness

Models have different premium request costs. In rough order from cheapest to most expensive:

1. **Cheapest:** Haiku 4.5, Gemini 3 Flash, GPT-5 mini
2. **Standard:** Sonnet 4.5/4.6, GPT-4.1, Qwen2.5
3. **Premium:** Opus 4.5/4.6, GPT-5.1+, Gemini 3 Pro
4. **Most expensive:** GPT-5.1-codex-max, GPT-5.3-codex

Don't use premium models for tasks that a standard model handles fine. Save the expensive ones for where they make a real difference.

## When to Override Defaults

The `get_recommended_model()` function in `src/lucent/model_registry.py` provides baseline recommendations. Override when:

- A task failed with the default model and needs more reasoning power
- Rate limits are being hit — downgrade non-critical tasks to cheaper models
- A task specifically needs vision capabilities (check `supports_vision`)
- The task involves extremely long input — prefer models with large context windows
- Provider diversity matters (don't put all tasks on one provider)

## Anti-Patterns

- **Always using Opus** — It's the most powerful but also the most expensive. Most tasks don't need it.
- **Ignoring task failures** — If a model consistently fails on a task type, try a different one rather than retrying with the same model.
- **Not specifying a model** — Letting everything fall to the default means you're not thinking about what the task actually needs.
- **Chasing new models** — A model being newer doesn't make it better for your specific task. Stick with what works until you have evidence otherwise.
