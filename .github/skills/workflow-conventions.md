---
name: workflow-conventions
description: Canonical conventions for the Lucent workflow — tags, statuses, review routing, and memory sharing.
---

# Workflow Conventions

This skill defines the canonical standards for all Lucent workflow operations. All agents (conversational, daemon, and sub-agents) must adhere to these conventions.

## 1. Tagging Conventions

Tags are the primary mechanism for routing, visibility, and state tracking. Use these exact tag names.

### Canonical Tags (USE THESE)

| Tag | Purpose |
|-----|---------|
| `daemon` | Applied to **all** memories created by the daemon or sub-agents. |
| `needs-review` | Applied to any work that requires human attention or approval. |
| `feedback-approved` | Applied by the user to indicate work is accepted. |
| `feedback-rejected` | Applied by the user to indicate work needs revision. |
| `feedback-processed` | Applied by the daemon after handling approval/rejection. |
| `validated` | Applied to patterns/lessons that have been proven to work. |
| `rejection-lesson` | Applied to self-improvement memories derived from rejected work. |
| `phase-N` | Used for multi-phase tasks (e.g., `phase-1`, `phase-2`). |
| `planning` | Used for memories related to task decomposition or roadmapping. |
| `technical` | Used for technical implementation details or code knowledge. |
| `experience` | Used for retrospective or learning memories. |

### Prohibited Tags (DO NOT USE)

| Wrong Tag | Correct Replacement |
|-----------|---------------------|
| `awaiting-approval` | `needs-review` |
| `pending-review` | `needs-review` |
| `from-daemon` | `daemon` |
| `daemon-service` | `daemon` |
| `user-approved` | `feedback-approved` |

## 2. Request & Task Status

Lucent uses a strict state machine for requests and tasks.

### Request Statuses
- **pending**: Created but not yet planned or started.
- **planned**: Broken down into tasks, ready for execution.
- **in_progress**: At least one task is running or completed.
- **completed**: All tasks finished successfully.
- **failed**: One or more tasks failed (and retries exhausted).

### Task Statuses
- **pending**: Created but not yet claimed.
- **planned**: Assigned to a sequence but not yet runnable.
- **running**: Currently being executed by an agent.
- **completed**: Finished successfully.
- **failed**: Terminated with error.

## 3. Review Routing & Visibility

### Review Queue
- The **Requests UI** and **Review Queue** filter by the `needs-review` tag.
- Any artifact (plan, code, memory) that needs human eyes **MUST** have `needs-review`.
- **Who reviews what?**
  - `technical` + `needs-review` → Engineering review
  - `planning` + `needs-review` → Product/Approach review
  - `experience` + `needs-review` → Learning verification

### Memory Sharing
- **Default Rule**: All memories created by the daemon **MUST** be shared.
- **Why?** The daemon runs as a service user (`daemon-service`). If `shared=False`, the memories are invisible to organization members.
- **Mechanism**:
  - When calling `create_memory`, explicitly set `shared=True` (or `shared: true`).
  - *Exception*: Private internal scratchpad memories (rare) may be unshared.

## 4. Model Selection Guidelines

When creating tasks (`create_task`), choose the model based on complexity:

| Task Type | Recommended Model | Rationale |
|-----------|-------------------|-----------|
| **Complex Logic / Architecture** | `claude-opus-4.6` | Highest reasoning capability, best for subtle bugs and design. |
| **Code Generation / Refactoring** | `gpt-5.3-codex` | Excellent code fluency and standard library knowledge. |
| **Documentation / summarization** | `gemini-3.1-pro` | Strong context window and natural language generation. |
| **Routine / Simple Tasks** | `claude-sonnet-4.6` | Cost-effective for well-defined, lower-risk tasks. |

**Note**: Do not use "preview" or "legacy" models for critical daemon workflows unless explicitly requested.
