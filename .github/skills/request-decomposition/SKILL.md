---
name: request-decomposition
description: 'Decompose durable work into a request and tasks from conversation, or hand decomposition to the daemon. Use when creating a request, deciding whether to author a task plan, sequencing or sizing tasks for a request, or when a request exists but its tasks do not match what the person asked for.'
---

# Request and Task Decomposition

Durable work in Lucent is a **request** (the outcome) containing **tasks** (the
individually accountable steps). Someone has to decide the steps. Two parties
can, and exactly one owns a given request:

| Owner | Who decomposes | How to claim it |
|---|---|---|
| `planner` (default) | The daemon's background composer, from the request text | Create the request without `task_plan` and leave `decomposition_mode` unset |
| `author` | You, in this conversation | Pass `task_plan` to `create_request`, or add a task with `create_task` |

Ownership is stored on the request and is **sticky**. Once you author a task,
the composer will never add tasks to that request, because a second breakdown
on top of yours is the failure mode this design exists to prevent.

## Before Starting

1. Call `list_active_work()` and search memories for related goals. Reuse or
   extend existing work rather than duplicating it.
2. Call `list_agent_definitions(status='active')` — agent types are deployment
   specific and only the names it returns are dispatchable.
3. Call `list_available_models()` only if model choice matters for this work.
   The request-level `backend_id`/`requested_model` pins are inherited by every
   task, so set them at request level rather than per task.
4. Call `list_sandbox_templates()` before assigning a sandbox to any task.
5. Call `list_remotes()` when the work should run somewhere other than the
   local environment — a person's own machine, a build box, or a named remote.
   Only `active` remotes can be targeted. Never invent a remote id.

## Step 1: Decide Whether This Is Durable Work

- **Do it now** — the result is wanted in this conversation and is small enough
  to finish here. Do not create a request to narrate or defer.
- **Durable work** — the person wants follow-through, background progress, or an
  outcome that should outlive the chat. "Look into", "fix", "follow up", "keep
  working on", "make sure" are sufficient when the target is clear.
- **Sustained outcome** — also track a goal when the work spans milestones, a
  deadline, or ongoing progress.
- **Recurring automation** — design a workflow or schedule instead; confirm
  before activating unless explicitly asked to create it now.

Tentative brainstorming is not authorization. Ask one focused question only when
the outcome or scope genuinely cannot be inferred.

## Step 2: Choose the Decomposition Path

This is the decision the rest of the skill depends on. Pick **one** path.

### Path A — Hand off to the daemon (`planner`)

Choose this when:

- you do not know the shape of the work well enough to write honest task
  descriptions, or
- the work is exploratory and the right breakdown will only become clear after
  someone investigates, or
- the person is describing an outcome in their own terms and has no view on the
  steps.

```python
create_request(
    title="Short title for the work",
    description="Full instructions — everything needed to do it.",
    source="user",
    priority="medium",
    target_repo="owner/repo",       # for code work
    target_paths=["relevant/path"],
)
```

Then tell the person what is queued and that Lucent will plan the steps. Stop.
Do not add tasks afterward — that would split ownership mid-flight.

### Path B — Author the plan (`author`)

Choose this when **any** of the following is true:

- the person named the specific asks, so the steps are already decided,
- you discovered the shape of the work while talking with them,
- a person asked for a specific sequence, scope, or set of deliverables,
- you know which agents or models the work needs.

This is the path that keeps a request's stated asks from drifting away from
what actually gets scheduled. Authoring a plan is a normal, expected action in
conversation — not an escalation.

## Step 3: Author the Plan

### Hard rule: one call for the whole plan

| Situation | Tool |
|---|---|
| **More than one task** | `create_request(task_plan=[...])` |
| **Exactly one task, added to an existing request later** | `create_task(...)` |

Never create a request with `create_request` and then add several tasks one at a
time. The atomic form validates every task before writing anything, so a bad
entry produces one clear error instead of a half-authored request.

```python
create_request(
    title="Add request decomposition control",
    description="Explain the desired outcome, constraints, and evidence of completion.",
    source="user",
    priority="high",
    target_repo="owner/repo",
    task_plan=[
        {
            "title": "Add decomposition ownership column and gate the composer",
            "description": (
                "Add requests.decomposition_mode … Gate the composer predicates … "
                "Done when: the composer ignores author-owned requests and a test "
                "proves it."
            ),
            "agent_type": "code",
            "sequence_order": 0,
        },
        {
            "title": "Expose task_plan on create_request",
            "description": "…",
            "agent_type": "code",
            "sequence_order": 1,
        },
        {
            "title": "Document the workflow in the chat UI",
            "description": "…",
            "agent_type": "documentation",
            "sequence_order": 2,
        },
    ],
)
```

### Plan entry fields

Every entry accepts the same fields as `create_task`:

`title`, `description`, `agent_type`, `sequence_order`, `priority`, `model`,
`reasoning_effort`, `sandbox_template_id`, `sandbox_overrides`,
`output_contract`, `task_kind`, `execution_config`, `decision_config`,
`fusion_config`, `backend_id`, `remote_target_id`

Plus `parent_task_index` — the 0-based position of the parent **within this same
plan** — when a step is genuinely a sub-task. Parents must appear earlier in the
list than their children.

## Step 3b: Choose Where the Work Runs

Three execution placements exist and they are mutually exclusive:

| Placement | Mechanism | Use when |
|---|---|---|
| Local (default) | Neither field set | Ordinary work; the local daemon runs it |
| Sandbox | `sandbox_template_id` | Isolated disposable container on this host |
| Remote | `remote_target_id` | Work must happen on another machine — the person's laptop, a build box, a peer Lucent deployment |

A remote is a registered executor that polls this instance, claims the tasks
pinned to it, and reports back. It runs in **its own** environment, so a task
that is remote-pinned must not also carry `sandbox_template_id` /
`sandbox_overrides` — the combination is rejected rather than silently dropping
one of them.

```python
list_remotes()      # discover ids; only status='active' is targetable
```

Then set it on the task:

```python
create_task(..., remote_target_id="<id from list_remotes>")
# or, inside a plan:
task_plan=[{..., "remote_target_id": "<id>"}]
```

Operational consequences to state plainly, because they change what the person
observes:

- A remote-pinned task is **invisible to local dispatch** — the local daemon
  will never pick it up. It stays queued until that remote polls.
- If the remote is offline, nothing runs. There is no automatic fallback to
  local execution; that is deliberate, so work never silently runs on a machine
  it was not meant for.
- Only `active` remotes can claim. A `disabled` remote keeps its pinned tasks
  queued; a `revoked` one is terminal.
- `remote_target_id` applies to `agent` tasks only. Decision and fusion tasks
  execute in-process.
- A remote receives the task title/description and runs its configured
  executor. It does not get sandbox credentials or Lucent secrets.

If `list_remotes()` returns no active remote, do **not** invent an id. Either
create the task without a remote pin so local execution handles it, or tell the
person that a remote must be enabled first.

When you report back, read the task's `result` (via `get_request_details`) and
show the actual output. Saying "I got the details" without them makes the
person ask a second time, which defeats the point of running it.

## Step 4: Sequence the Work

`sequence_order` is the only dependency mechanism. A task at order *N* becomes
dispatchable only when every task at a lower order satisfies the request's
`dependency_policy` (`strict` = all completed; `permissive` = completed, failed
or cancelled all unblock).

- **Same `sequence_order`** → independent, may run in parallel.
- **Increasing `sequence_order`** → later steps wait for earlier ones.
- Keep the plan to **1–5 tasks** unless the work genuinely needs more. A long
  chain is a planning smell: it usually means the request should be two requests.
- Each task must fit one session (roughly an hour of agent work). Split work that
  cannot finish in one session.

## Step 5: Write Descriptions That Stand Alone

A task description is the sub-agent's entire understanding of the job, combined
with its agent definition. Write it for a competent engineer who has never seen
this conversation.

**Good:**

> Add a `requests.decomposition_mode` column (migration after 142) with values
> `planner`/`author`. Gate both composer predicates in `src/lucent/db/daemon.py`
> on `decomposition_mode = 'planner'`. Add tests proving an `author` request with
> no tasks is never decomposed. Done when `pytest tests/test_decomposition_mode.py`
> passes.

**Bad:**

> Implement the decomposition change discussed above.

### Description checklist

- [ ] States the objective as a concrete deliverable
- [ ] Names files, modules, or directories when relevant
- [ ] Defines "done" — what exists or passes when it is finished
- [ ] Carries any constraint from the conversation the agent cannot otherwise know
- [ ] Stands alone — no "the thing we discussed", no implicit prior context
- [ ] Names the target repo and paths for code work

## Step 6: Tell the Person What Will Happen

After creating the plan, state three things in the person's own language:

- what is now queued,
- in what order it will run, and
- that **the result comes back here on its own** — they do not need to ask, and
  they do not need to open Activity.

Do not enumerate internal statuses or tool names. Do not ask them to approve the
task list unless the request itself needs approval — that is what the approval
gate is for.

Do **not** end the turn by watching the request. The daemon is now the executor;
your job in this turn is finished.

## Step 7: Understand What Comes Back (and When)

This is the part agents get wrong, so read it before creating any request. The
conversation stays the **supervisor** of every request it created, and results
are pushed back **into this thread**. Three things happen, none of which you
trigger:

| What | When | Where you see it |
|---|---|---|
| Report card | ~8s after a task or request settles | A card in this transcript |
| Reply turn | With a final outcome, or after the reply interval | An assistant message in this thread |
| Context digest | Every later turn | The `--- DELEGATED WORK ---` block in your context |

**`create_request` tells you this in its response.** When the call is made from
a chat session the result carries a `reporting` block. Read it rather than
inferring the behaviour.

### The request lifecycle you will actually observe

```
pending → planned → in_progress → review → completed
                                      │
                                      └── automatic, resolves in seconds
```

`review` is **Lucent's own post-completion quality check**, performed by an
internal `request-review` task. It is not a person's approval, it is not a
decision anyone must make, and it does not need to be reported to the person as
"awaiting review". It normally resolves within a minute. Treat it as an internal
step that is about to become `completed`.

### Rules that follow from this

- **Never say you cannot know what happened to work you started.** The digest in
  your context describes it, and the result will arrive in the thread. Polling
  `get_request_details` mid-flight to narrate progress is noise.
- **Never tell the person to check the Requests/Activity UI** for a result that
  arrives here.
- **Never claim work is still running after a `completed` card** or after the
  digest says all delegated work has finished.
- **Do not wait for the result in the same turn.** Say what you queued and stop.
  The reply arrives on its own; you will see it in a later turn.
- **Do not re-create the request because a result has not appeared yet.** Absence
  of a card is not failure — it may be inside the ~8s debounce, or the reply may
  be waiting on the reply interval.
- **Report the real result, not that there is one.** When the card or reply
  arrives, quote the actual output. "The report came back empty" is nearly always
  wrong — read the card body before saying so.

### If a report looks thin

Read the card body, which carries up to five task result excerpts. If it truly
lacks the output, the correct move is `get_request_details(request_id)` and then
quoting the task `result` field — not asking the person to re-run the work, and
not describing the report as missing.

## Adding to a Plan Later

A request you authored stays yours. To extend it, use `create_task` with the
existing `request_id`. The request keeps `decomposition_mode='author'`, so the
composer still will not add anything.

Use `update_request_decomposition(request_id, 'author')` to protect a request you
are about to extend. Switching a request that already has tasks back to
`'planner'` is refused — the composer would add a competing plan.

## Anti-Patterns

- **Two authorities.** Creating a request without a plan and then adding tasks
  yourself. The composer may already have started; you now have two breakdowns.
  Pick one path in Step 2.
- **Task-per-sentence.** Turning every clause of a request into a task. Group
  into accountable deliverables.
- **Narrating instead of delegating.** Creating a request for work you could have
  finished in the conversation.
- **Chat-only deliverables.** Asking for research or a report but not requiring a
  durable artifact (`record_task_output` or a file) — the result then exists only
  in a transcript nobody will find later.
- **Invented agent or model names.** Names not returned by
  `list_agent_definitions` / `list_available_models` are rejected at dispatch.
- **Skipping discovery.** Naming an agent type, sandbox template, or remote you
  did not look up.
- **Inventing a remote.** A made-up `remote_target_id` is rejected at authoring
  time; an omitted one silently sends the work local instead of remote.
- **Plans that cannot finish.** A single task asking for a multi-day refactor.
