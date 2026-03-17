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

## Decision Framework

Consider three factors in order:

1. **What does the task actually need?** — Reasoning depth, tool use, speed, vision, context length
2. **What's the cost tolerance?** — Premium models burn through rate limits faster
3. **Does it need to be right, or does it need to be fast?** — Not every task needs Opus

## Task Type → Model Mapping

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
