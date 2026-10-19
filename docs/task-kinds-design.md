# Task Kinds: Executable Task Architecture

Lucent tasks currently run in one of three ways: an approved **agent session**,
a **decision-model call**, or a **fusion** run. Those kinds grew as conventions:
`decision_config` present meant decision, `fusion_config` present meant fusion,
and everything else was an agent. This document defines the explicit model and
the path for adding deterministic executors such as `run_code`, `webhook`, or
`shell` without editing every creation surface.

## Two orthogonal axes

| Axis | Field | Meaning |
|---|---|---|
| Who does the work | `agent_type` / `agent_definition_id` | Persona, skills, tools, prompt. Only meaningful for agent-family kinds. |
| How the work executes | `task_kind` + `execution_config` | Executor selection and its typed payload. |

`decision-step` is a legacy sentinel that conflated the two: it is an agent
definition name *and* the decision executor discriminator. New kinds must not
be encoded as `agent_type` values.

## Registered kinds

| Kind | Aliases | Config | Model | Agent definition | Sandbox | Output contract |
|---|---|---|---|---|---|---|
| `agent` | — | none | chat, tools required | required | optional | supported |
| `decision` | `decision-step` | `decision_config` | decision-class | required (`decision-step`) | forbidden | forbidden |
| `fusion` | — | `fusion_config` | chat, tools required | required | forbidden | supported |

Registry: `src/lucent/task_kinds.py`. Each `TaskKindSpec` declares its config
field/schema, examples, model/agent/backend requirements, sandbox and
output-contract capabilities. `list_task_kinds()` (MCP, read-only) projects the
registry to JSON for agents, including the live decision-model availability for
the `decision` kind.

## Storage and API

Migration `141_task_kinds.sql` adds:

- `tasks.task_kind VARCHAR(32) NOT NULL DEFAULT 'agent'`
- `tasks.execution_config JSONB`

Existing rows are backfilled (`decision_config` → `decision`,
`fusion_config` → `fusion`, otherwise `agent`). The legacy columns remain and
are mirrored during the compatibility window: `decision_config` and
`fusion_config` are still written and read, so older code, in-flight rows, and
existing prompts keep working.

`create_task` (MCP and REST) and `RequestRepository.create_task` accept:

- `task_kind: str | None` — explicit kind; inferred when omitted
- `execution_config: dict | None` — generic per-kind payload
- `decision_config` / `fusion_config` — accepted aliases for the matching kind

Inference order: explicit `task_kind` → `decision_config` or
`agent_type='decision-step'` → `fusion_config` → `agent`. Supplying
`execution_config` together with a legacy config is rejected.

The repository writes `task_kind`/`execution_config` and mirrors the config into
the legacy column. The dispatcher reads `task_kind` first and falls back to the
legacy columns for rows written before migration 141. Unknown kinds fail
closed with a `dispatch_denied` event rather than silently running as an agent.

## Execution lifecycle

The daemon keeps these invariants centralized, regardless of kind:

- queue fetch and sequence gating
- `max_tasks`, drain checks, `_claim_operations` accounting
- atomic claim, bound-org check, `start_task`/`release`
- model-access denial before claim
- persisting the resolved model before execution
- one terminal surface: `complete` / `fail` / `needs_review`
- generic output-contract validation and the completion tail
- task events and observability

An executor supplies only the kind-specific phases:

1. **validate** — config and capability checks
2. **plan model** — `None` for non-LLM kinds; chat/decision model selection for
   LLM-backed kinds
3. **prepare** — agent context, prompt, MCP/hooks, sandbox config, secrets
4. **execute** — the actual work (agent session, decision call, command, HTTP)
5. **finalize** — kind-specific result shape, then the shared completion tail

The current implementation still has the three built-in executors inline in
`LucentDaemon._dispatch_tracked_tasks`. The seam is intentionally phased:

1. explicit kind/config storage and discovery (done)
2. extract the decision and standard branches into daemon methods without
   behavior change
3. introduce `TaskExecutor`/`ExecutionIO` and an executor registry; built-ins
   wrap the extracted methods
4. add the first non-LLM executor behind the registry (webhook is the cheapest)
5. move preparation/model planning into the executor contract for `run_code` /
   `shell`

## Adding a kind

1. Add a `TaskKindSpec` registration in `src/lucent/task_kinds.py` (or a module
   that registers on import) with:
   - canonical `kind`, display name, description, aliases
   - `config_schema` and examples
   - `requirements`: agent definition, model class, tools, backend,
     output-contract, sandbox
   - a `validate` callable if the schema is not enough
2. Extend the dispatcher with an executor for the kind. Non-agent kinds should
   execute through the kind registry before the agent context is assembled; the
   orchestrator must still own claim/start/events/finalize.
3. Add the kind to `list_task_kinds()` automatically (registry projection).
4. Add a conformance test: catalog entry, config rejection, creation
   round-trip, dispatch/finalize, and terminal event.
5. Document the kind in `docs/api-reference.md` and
   `.github/skills/daemon-task-authoring/SKILL.md`.

No new `tasks` column is required for a new kind: `execution_config` is the
generic payload. New shared capabilities should be added to `TaskKindSpec`, not
as another nullable column.

## Sketch: deterministic executors (not implemented)

### `run_code` / `shell`

```json
{
  "task_kind": "run_code",
  "execution_config": {
    "command": "pytest",
    "args": ["-q"],
    "cwd": ".",
    "timeout_seconds": 900,
    "expected_exit_codes": [0]
  },
  "sandbox_template_id": "<approved-template-id>",
  "output_contract": {"json_schema": {"type": "object"}}
}
```

Requirements: no agent definition, no model, no backend; sandbox required;
secrets via the existing `secret://` / `credential://` sandbox env references;
result `{exit_code, stdout, stderr, duration_ms, artifacts}`; validation is
exit-code based, not the prose heuristics used for agent output.

### `webhook`

```json
{
  "task_kind": "webhook",
  "execution_config": {
    "url": "https://example.test/hook",
    "method": "POST",
    "headers": {"Authorization": "secret://..."},
    "body": {"event": "release.ready"},
    "timeout_seconds": 30,
    "expected_status": [200, 202],
    "max_retries": 2,
    "idempotency_key": "..."
  }
}
```

Requirements: no agent/model/sandbox; org egress allowlist and SSRF validation
(reuse `lucent/url_validation.py`); secret references resolved at dispatch;
response mapped to a structured result; retry/backoff and idempotency declared
by the kind. This is distinct from the existing **workflow trigger** named
`webhook`, which starts a workflow rather than executing as a task.

## Compatibility and rollback

- New columns are additive; legacy columns remain authoritative for old rows.
- Unknown kind rows fail closed.
- The down migration drops the two columns and index; the legacy config
  columns are untouched.
- Once all readers use `task_kind`/`execution_config` and no in-flight rows rely
  on the legacy columns, a later migration can deprecate the mirrors.
