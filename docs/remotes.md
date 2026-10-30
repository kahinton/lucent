# Remotes (Agent Control Protocol)

A **remote** is a non-local executor that Lucent can target with whole tasks —
another Lucent deployment, a laptop, a build box, or any machine running the
shipped agent client. Remotes are the out-of-process counterpart to
[sandboxes](sandboxes.md): where a sandbox is a container Lucent creates and
fully controls on the local host, a remote is an environment Lucent does not
control and cannot observe except through the protocol.

## Design

The control relationship is **pull-based and outbound-only**:

```mermaid
sequenceDiagram
    participant O as Owner
    participant C as Controller (Lucent)
    participant R as Remote agent
    O->>C: Register remote, receive lr_ token
    O->>C: Create task with remote_target_id
    R->>C: POST /api/remotes/{id}/claim
    C-->>R: Task payload (lease set)
    R->>R: Execute via configured executor
    R->>C: POST task events + heartbeat (lease renewal)
    R->>C: POST complete / fail with result
    Note over C: Dead remote → Stale Task Reaper requeues
```

A remote never opens an inbound port. It initiates every connection, which
means a machine behind NAT or a corporate firewall works identically to one on
the public internet.

### Coordination reuses the daemon model

Each remote registers as a `daemon_instances` row with the id `remote:{uuid}`
on first heartbeat. Because of that:

- lease renewal happens through the existing `heartbeat_instance` path;
- a dead remote's tasks are released by the existing **Stale Task Reaper**
  with no remote-specific logic;
- released tasks keep their `remote_target_id` pin, so they return to the same
  remote when it comes back rather than silently leaking to local dispatch.

### Dispatch rules

- Tasks pinned to a remote are invisible to local dispatch
  (`tasks.remote_target_id IS NULL` in the dispatch predicate).
- A remote only sees tasks pinned to **itself**.
- Stage gates (`sequence_order`, `dependency_policy`, approval status) apply
  unchanged — the remote predicate mirrors the local one deliberately.

## Security model

Remote execution is a trust boundary, so the controls are layered rather than
concentrated in one check.

| Threat | Control |
| --- | --- |
| Stolen token used against other endpoints | Dedicated `lr_` prefix; `remote-agent` scope allowlisted to five routes in `api/deps.py` |
| Token reused against another remote | Key name embeds the remote id; both `deps` and the route handler enforce path-vs-key binding |
| Remote touching tasks it never claimed | `_is_remote_task_actor` requires `tasks.claimed_by == remote:{id}`; request-level mutation rights are explicitly denied to remote keys |
| Privilege escalation via role | Remote identity users have role `daemon`, which grants blanket request mutation. `_can_mutate_request` returns `False` for any `remote-agent` key before the role check |
| Token exposure in transit | Client refuses plain `http://` to non-local hosts unless `--insecure` |
| Credential leakage to the remote | The claim payload strips `sandbox_config`; the remote never receives Lucent credentials |
| Token replay after revoke | Revoking a remote revokes its API key in the same transaction |
| Oversized payloads | Events capped at 64 KiB, results at 1 MiB, truncated with a visible marker |
| Shell injection via task text | Task text is delivered on **stdin**; argv is a fixed operator-configured template and never interpolates task content |
| Session key used as a REST credential | A `remote-session` key is rejected by every REST route; it only authenticates `/mcp` |
| Session key outliving its task | Minted per task with a TTL bounded by the task timeout plus a fixed grace period |
| Session key reaching other users' data | Bound to the requesting user's memory scope; the remote cannot widen it |
| Third-party credentials shipped to a remote | Granted MCP servers abort composition; only Lucent's own `/mcp` is wired |
| Remote choosing its own agent/model | The controller composes the whole session; the remote receives no choices |
| Unattended tool use | Off by default; requires both a remote-side executor choice and an owner-level controller setting |

### Authorization

- **Owners** register, rotate, disable, revoke, and requeue remotes.
- **Admins** may read the remote list.
- **Members** cannot see the Remotes tab.
- **Daemon** may target remotes when creating tasks; members may not.
- All web mutations go through the standard CSRF double-submit check.

## Setup

### 1. Register the remote

In **Sandboxes → Remotes**, enter a name and click **Register remote**. The
plaintext token is displayed exactly once — copy it immediately. Only a bcrypt
hash is stored.

Or via API:

```bash
curl -X POST https://lucent.example.com/api/remotes \
  -H "Authorization: Bearer $OWNER_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"build-box-01","description":"CI machine"}'
```

Note the returned `id` and `token`.

### 2. Run the agent on that machine

The client is `scripts/lucent_remote_agent.py` — single file, Python 3.9+,
**standard library only**. Copy it to the remote host; nothing to install.

```bash
export LUCENT_CONTROLLER_URL=https://lucent.example.com
export LUCENT_REMOTE_TOKEN=lr_...
export LUCENT_REMOTE_ID=<remote uuid>
export LUCENT_REMOTE_EXECUTOR=command
export LUCENT_REMOTE_COMMAND='["python3","/opt/agent/run_task.py"]'

python3 lucent_remote_agent.py
```

Use `--once` to claim at most one task and exit (useful for cron or smoke
tests). Use `--executor noop` to verify connectivity without running anything.

### Executors

| Executor | Behaviour |
| --- | --- |
| `noop` | Echoes the task back as the result. Connectivity smoke test. |
| `command` | Runs a fixed argv template, passing the rendered task text on stdin. Exit code 0 = success. |
| `shell` | Runs the commands the task itself carries, in a per-task directory. Opt-in. |
| `engine` | Runs a full model-driven agent session on the remote host. Opt-in, and the controller must also allow it. |

The remote reports its executor and the full executor list it can actually run
in each heartbeat, so task authors and the controller know what a given machine
supports. `engine` is only advertised when Lucent's Python package imports on
that host — a remote that cannot run a session never claims it can.

The task text includes the title, parent request title, task id, agent type,
priority, and description — enough context to act without a round trip.

#### The `command` executor

`command` is for a host that has one fixed entry point (an agent runner, a build
script). The task description travels on stdin, so it can never become shell
syntax on the remote.

#### The `shell` executor

`shell` is for hosts where Lucent should be able to run arbitrary work. Enable
it deliberately — it executes code chosen by whoever authored the task:

```bash
export LUCENT_REMOTE_EXECUTOR=shell
# optional, defaults to $SHELL then /bin/sh
export LUCENT_REMOTE_SHELL=/bin/zsh
```

The commands come from fenced code blocks in the task description, tagged `sh`,
`bash`, `shell`, `zsh`, or `console`:

    Report this host's identity.

    ```sh
    uname -a
    sw_vers 2>/dev/null || true
    ```

Every recognized block is concatenated in order and run with `bash -c` (or the
configured shell) in a fresh per-task directory. A task with **no** fenced
command block fails with exit code 2 instead of executing the prose — running a
description as a script is never the intent.

Guard rails on `shell`:

- per-task working directory (the cwd), cleaned up afterwards;
- `TMPDIR` points at the task directory so scratch files are cleaned up;
- `HOME` stays the operator's real home, so `git`, `ssh`, and `npm` read the
  configs they expect. The per-task dir is not a sandbox — this executor already
  runs task-authored code — so redirecting `HOME` would only have made commands
  report a throwaway path and broken those tools. Override with
  `LUCENT_REMOTE_HOME` if a dedicated home is wanted;
- the configured task timeout applies (`--task-timeout`, default 3600s);
- exit code and stderr are reported back, so failures are visible rather than
  silent.

A `lucent` executor (where the remote runs the task in its own sandboxes via a
local daemon) fits the same interface and is the natural next step.

#### The `engine` executor

`shell` runs commands the task already carries. `engine` is different: the
remote runs a **real agent session** — a model with tools — and decides for
itself which commands to run and which files to edit. That is what makes
open-ended work ("fix the flaky test", "add the endpoint") possible on a remote
machine instead of only pre-written scripts.

Two independent switches must agree before this happens:

1. **On the remote host** — run the agent with
   `LUCENT_REMOTE_EXECUTOR=engine`. The client refuses to start if Lucent's
   Python package (with the engine SDKs) is not importable, rather than
   advertising a capability it cannot deliver.
2. **On the controller** — an owner enables **Settings → Features → Remote
   engine sessions** (`features.remote_engine_session_enabled`, env
   `LUCENT_REMOTE_ENGINE_SESSION`). Off by default.

When a task pinned to that remote also carries
`execution_config={"executor": "engine"}`, the controller composes the session
and ships it in the claim response:

```json
{
  "execution_plan": {
    "kind": "task-session",
    "engine": "claude_code",
    "model": "…",
    "system_message_b64": "…",
    "backend": {"engine": "claude_code", "provider": "ollama", "config": {}},
    "mcp_config": {"memory-server": {"type": "http", "url": "…/mcp", "headers": {}}},
    "timeout": 3600
  }
}
```

**The controller composes, the remote executes.** That split is the security
model:

- The agent definition, skills, managed tools, model, and provider are resolved
  by the controller. The remote chooses none of them.
- The remote receives a **per-task `hs_` key scoped to `remote-session`**. It is
  valid **only on `/mcp`** — every REST route rejects it — and it expires with
  the task. It is not the daemon's key and it carries no request-mutation rights.
- The key is bound to the requesting user's memory scope, so an agent on the
  remote can read and write only what that person could.
- Third-party MCP servers granted to the agent are **not** sent: their
  credentials would have to be decrypted and shipped to a host Lucent does not
  control. A task that needs one runs locally or in a sandbox, and composition
  fails with that reason rather than silently dropping the server.

Sessions run unattended, so tool permissions are approved without a prompt
(`LUCENT_REMOTE_PERMISSION_MODE` overrides this on the remote host). This is
the same trust level the `shell` executor already carries — both run code on
the operator's machine — and it is why the feature is off by default and gated
behind an owner decision.

Requires `backend_id` and `model` on the task so the remote knows which engine
to run. `remote_target_id` remains mutually exclusive with
`sandbox_template_id` / `sandbox_overrides`.

Limits worth knowing:

- Only `copilot`, `claude_code`, and `codex` can run remotely. `langchain` owns
  no tool loop of its own, so a task pinned to it must use `shell`.
- Composition failures are reported in the claim payload as
  `execution_plan_error` and the task fails with that reason. It never falls back
  to shell execution or to local dispatch, because either would run the wrong
  thing in the wrong place.

```bash
# remote host
export LUCENT_REMOTE_EXECUTOR=engine
export LUCENT_REMOTE_PERMISSION_MODE=bypassPermissions   # default; unattended
```

```python
create_task(
    request_id="…",
    title="Add the missing endpoint",
    description="Implement GET /api/widgets and its test.",
    backend_id="<coding backend id>",
    model="<model id>",
    remote_target_id="<remote id>",
    execution_config={"executor": "engine"},
)
```

## Operations

### Targeting a remote

Set `remote_target_id` when creating a task:

```bash
curl -X POST https://lucent.example.com/api/requests/$REQUEST_ID/tasks \
  -H "Authorization: Bearer $OWNER_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Build release","description":"...","remote_target_id":"<uuid>"}'
```

`remote_target_id` cannot be combined with `sandbox_template_id` or
`sandbox_config`: a remote runs work in its own environment, so a local sandbox
would be silently ignored. This is rejected at authoring time rather than
pretended.

Tasks created by the daemon may also be pinned, which is how autonomous work
can be routed to remote capacity.

### From an agent (MCP)

Agents discover remotes with `list_remotes()` and pin work with
`remote_target_id`, either on `create_task` or inside a `create_request`
`task_plan` entry:

```python
list_remotes()                 # ids, names, status, last_seen_at
create_task(
    request_id="…",
    title="Collect host facts",
    description="Report this machine's OS and disk usage.\n\n```sh\nuname -a\ndf -h\n```",
    remote_target_id="<id from list_remotes>",
)
```

Only `active` remotes are targetable. An unknown, disabled, or revoked id is
rejected at authoring time with a list of usable remotes — never silently
dropped, because a dropped pin means a local daemon claims work that was meant
for another machine.

`remote_target_id` is mutually exclusive with `sandbox_template_id` /
`sandbox_overrides`, and applies to `agent` tasks only (decision and fusion
tasks execute in-process).

The shipped remote agent runs whichever executor its operator configured.

### Winding a remote down

- **Disable** — the remote stops being allowed to claim, but already-pinned
  tasks stay pinned.
- **Requeue tasks** — unpins `pending`/`planned` tasks so local dispatch can
  take them. Claimed/running work is left to the reaper rather than yanked from
  under an active agent.
- **Revoke** — terminal. The agent token is invalidated immediately.

### Recovery

If a remote dies mid-task, its `daemon_instances` row goes stale and the Stale
Task Reaper releases the claim within the normal stale window (default 30
minutes of inactivity). The task returns to `pending` **with its pin intact**,
so it is reclaimed when that remote reconnects, or you can requeue it manually.

## Testing

```bash
PYTHONPATH=src ./.venv/bin/pytest -q -m integration \
  tests/test_remotes_api.py tests/test_remote_claim_flow.py
PYTHONPATH=src ./.venv/bin/pytest -q tests/test_remote_agent_client.py
```

`tests/test_remote_claim_flow.py` covers the invariants that matter: dispatch
exclusion, cross-remote isolation, exclusive claiming under race, lease
renewal, reaper release, and the allowlist negatives.
