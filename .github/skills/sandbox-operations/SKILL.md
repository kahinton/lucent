---
name: sandbox-operations
description: 'Create, configure, and manage sandbox environments — SandboxManager API, DockerBackend specifics, MCP bridge setup, known bugs, and operational procedures. Use when creating sandboxes, configuring Docker backends, setting up MCP bridges, debugging sandbox lifecycle issues, sandboxes fail to provision, or configuring resource limits and timeout policies.'
---

# Sandbox Operations

Reusable knowledge for creating, operating, and cleaning up Lucent sandbox environments. Read this before writing any sandbox code — the API has critical non-obvious behavior and known bugs with mandatory workarounds.

## Key Files

| File | Purpose |
|------|---------|
| `src/lucent/sandbox/manager.py` | `SandboxManager` class + `get_sandbox_manager()` singleton |
| `src/lucent/sandbox/docker_backend.py` | `DockerBackend` — container lifecycle, networking, MCP bridge injection |
| `src/lucent/sandbox/models.py` | `SandboxConfig`, `SandboxInfo`, `ExecResult`, `OutputResult` |
| `src/lucent/sandbox/mcp_bridge.py` | MCP bridge server that runs inside the container |
| `daemon/daemon.py` (~line 2309) | `_create_task_sandbox()` / `_destroy_task_sandbox()` daemon integration |

---

## 1. SandboxManager API Reference

### Getting the Manager (Singleton)

```python
from lucent.sandbox.manager import get_sandbox_manager

manager = get_sandbox_manager()  # Always use this — never instantiate directly
```

### `create(config) → SandboxInfo`

Creates a container, persists a DB record, schedules auto-destroy, and provisions a scoped API key when `task_id` is set.

```python
from lucent.sandbox.models import SandboxConfig

config = SandboxConfig(
    image="lucent-sandbox:base",
    task_id="task-uuid-here",
)
info = await manager.create(config)
# info.sandbox_id, info.status ("ready" | "error"), info.container_id
```

**Important**: Always check `info.status == "ready"` before proceeding. A non-ready status means the container failed to start.

### `exec(sandbox_id, command, *, cwd=None, env=None, timeout=300) → ExecResult`

Runs a shell command inside the container.

```python
result = await manager.exec(
    sandbox_id=info.sandbox_id,
    command="git clone --depth=1 https://token@github.com/org/repo /workspace/repo",
    cwd="/workspace",
    timeout=120,
)
# result.exit_code (int), result.stdout (str), result.stderr (str)
# result.duration_ms (int), result.timed_out (bool)

if result.timed_out:
    raise RuntimeError(f"Command timed out after {timeout}s")
if result.exit_code != 0:
    raise RuntimeError(f"Command failed: {result.stderr}")
```

### `read_file(sandbox_id, path) → bytes`

Reads a file from the container via `cat` exec internally.

```python
content = await manager.read_file(info.sandbox_id, "/workspace/result.txt")
text = content.decode("utf-8")
```

### `write_file(sandbox_id, path, content: bytes)`

Writes a file using Docker's `tar put_archive`.

```python
await manager.write_file(info.sandbox_id, "/workspace/script.sh", b"#!/bin/bash\necho hello")
```

### `list_files(sandbox_id, path='/workspace') → list[dict]`

Lists files using `find -maxdepth 1`.

```python
files = await manager.list_files(info.sandbox_id, "/workspace")
# [{"name": "repo", "type": "directory", ...}, ...]
```

### `get(sandbox_id) → dict | None`

Returns DB record only (not live Docker state). Fast but may be stale.

### `get_live(sandbox_id) → SandboxInfo | None`

Returns live Docker container state. Use this to verify actual health.

```python
live = await manager.get_live(info.sandbox_id)
if live is None or live.status != "ready":
    raise RuntimeError("Container is not running")
```

### `stop(sandbox_id)`

Stops the container but preserves state and updates DB to `"stopped"`. Container can be restarted.

### `destroy(sandbox_id)`

Full cleanup: stops + removes container, removes workspace volume (`lucent-sandbox-{id}-workspace`), revokes the bridge API key, updates DB to `"destroyed"`. **Always call this when done.**

```python
await manager.destroy(info.sandbox_id)
```

### `process_output(*, sandbox_id, task_id, task_description, config, request_api, memory_api, log) → OutputResult | None`

Only runs if `config.output_mode` is set. Extracts git diff and handles `diff | pr | review | commit` modes. Call this before `destroy`.

### `list_all(organization_id=None, limit=25, offset=0) → dict`
### `list_active(organization_id=None, limit=25, offset=0) → dict`
### `cleanup_all() → int`

Destroys all live containers. Use with caution.

---

## 2. SandboxConfig Fields Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `str \| None` | auto | Auto-generated as `lucent-sandbox-{id[:12]}` |
| `image` | `str` | `"lucent-sandbox:base"` | Container image. **Daemon overrides this to `python:3.12-slim` — always set explicitly to `lucent-sandbox:base`** |
| `dockerfile` | `str \| None` | `None` | Path to a Dockerfile to build instead of pulling an image |
| `repo_url` | `str \| None` | `None` | Git repo to clone into `/workspace` |
| `branch` | `str \| None` | `None` | Branch to checkout (defaults to repo default) |
| `git_credentials` | `str \| None` | `None` | Injected as `https://token@host/...` in the clone URL |
| `git_credentials_ttl` | `int` | `3600` | Seconds before credentials expire. `0` = no expiry |
| `setup_commands` | `list[str]` | `[]` | Shell commands to run after container start. Best-effort — failures do **not** abort creation |
| `env_vars` | `dict[str, str]` | `{}` | Environment variables injected into the container |
| `working_dir` | `str` | `"/workspace"` | Default working directory for exec calls |
| `memory_limit` | `str` | `"2g"` | Docker memory limit |
| `cpu_limit` | `float` | `2.0` | CPU cores (converted to `nano_cpus = cpu_limit * 1e9`) |
| `disk_limit` | `str` | `"10g"` | Storage quota via `storage_opt`. Falls back gracefully on unsupported drivers |
| `network_mode` | `str` | `"none"` | `none` \| `bridge` \| `allowlist` |
| `allowed_hosts` | `list[str]` | `[]` | Hostnames resolved to IPs for iptables `OUTPUT` chain (requires `allowlist` mode) |
| `timeout_seconds` | `int` | `1800` | Auto-destroy after this many seconds |
| `idle_timeout_seconds` | `int` | `300` | Destroy after idle (each operation calls `touch()`) |
| `mcp_bridge_port` | `int` | `8765` | Port the MCP bridge listens on inside the container |
| `output_mode` | `str \| None` | `None` | `diff` \| `pr` \| `review` \| `commit` |
| `commit_approved` | `bool` | `False` | Must be `True` when `output_mode="commit"` |
| `task_id` | `str \| None` | `None` | Links sandbox to a task; triggers scoped API key provisioning |
| `request_id` | `str \| None` | `None` | Links sandbox to a request for tracking |
| `organization_id` | `str \| None` | `None` | Org isolation |

### Configuration Examples

**Basic sandbox (no repo, no network):**
```python
config = SandboxConfig(
    image="lucent-sandbox:base",
    task_id=task_id,
)
```

**Sandbox with repo clone:**
```python
config = SandboxConfig(
    image="lucent-sandbox:base",
    repo_url="https://github.com/org/repo",
    branch="main",
    git_credentials="ghp_tokenhere",
    git_credentials_ttl=3600,
    task_id=task_id,
    request_id=request_id,
)
```

**Sandbox with network access (allowlist mode):**
```python
config = SandboxConfig(
    image="lucent-sandbox:base",
    network_mode="allowlist",
    allowed_hosts=["pypi.org", "files.pythonhosted.org", "github.com"],
    task_id=task_id,
)
# NOTE: allowlist mode requires NET_ADMIN cap and iptables binary in the image
```

**Sandbox with custom image:**
```python
config = SandboxConfig(
    image="my-custom-image:latest",
    memory_limit="4g",
    cpu_limit=4.0,
    timeout_seconds=3600,
    task_id=task_id,
)
```

---

## 3. DockerBackend Specifics

Source: `src/lucent/sandbox/docker_backend.py`

### Image Requirements

`lucent-sandbox:base` provides:
- Python 3.12 runtime
- Git
- Common build tools
- `iptables` binary (required for `allowlist` network mode)
- MCP bridge dependencies pre-installed

Using `python:3.12-slim` (the daemon's incorrect default) **lacks iptables**, breaking `allowlist` mode. Always specify `image="lucent-sandbox:base"` explicitly.

### Networking Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| `none` | No network access | Default. Code execution with no external calls |
| `bridge` | Full outbound access via Docker bridge | Development/testing only — not recommended for untrusted code |
| `allowlist` | Outbound restricted to specific IPs via iptables | Package installs, GitHub access while isolating other traffic |

### DNS Configuration (Bug Workaround #3)

Custom bridge networks on Docker Desktop and Colima don't configure DNS automatically. **Always set explicit DNS for `bridge` and `allowlist` modes:**

```python
# This is handled inside DockerBackend — but verify it's in place (lines 322-324)
# containers.run(..., dns=['8.8.8.8', '1.1.1.1'], ...)
```

Without this, `git clone` and `pip install` will fail with name resolution errors even when the network mode should allow access.

### Docker Context Detection

`DockerBackend` auto-detects Colima and Rancher Desktop contexts and adjusts socket paths accordingly. This runs automatically in `DockerBackend.create()` — no manual configuration needed.

`devcontainer.json` auto-detection also runs automatically if present in the repo root being cloned.

### Known Bugs and Mandatory Workarounds

#### Bug 1: Docker SDK 7.x Networking (`docker_backend.py` lines 349–351)

**Problem**: Docker SDK 7.x requires *both* `network=` and `networking_config=` in `containers.run()`. Passing only `networking_config=` silently fails to attach the container to the network.

**Workaround** (already in codebase, verify it's present):
```python
network=self._network_name if networking_config is not None else None,
networking_config=networking_config,
```

If you see containers that appear to start but have no network access despite `bridge` mode, this bug is the cause.

#### Bug 2: Storage Quota Fallback (`docker_backend.py` lines 361–377)

**Problem**: `storage_opt={'size': '10g'}` fails on Colima, Rancher Desktop (macOS), and any storage driver without quota support (overlay2 without d_type, btrfs with certain configs).

**Workaround** (already in codebase, verify it's present):
```python
try:
    container = client.containers.run(..., storage_opt={"size": config.disk_limit}, ...)
except docker.errors.APIError as exc:
    if "storage opt" in str(exc).lower() or "quota" in str(exc).lower():
        # Retry without storage_opt
        container = client.containers.run(..., storage_opt=None, ...)
    else:
        raise
```

#### Bug 3: DNS on Custom Bridge (`docker_backend.py` lines 322–324)

Already described above. The fix is explicit `dns=['8.8.8.8', '1.1.1.1']` in `containers.run()` for non-`none` network modes.

---

## 4. MCP Bridge Setup

The MCP bridge is a JSON-RPC 2.0 server that runs inside the container and connects the sandbox agent back to the Lucent API.

### Architecture

```
[Sandbox Container]                    [Lucent API]
  Agent process
      ↓ POST /mcp
  127.0.0.1:8765 (MCP bridge)  →→→  host.docker.internal:8766/api
      ↑
  LUCENT_SANDBOX_MCP_API_KEY env var
```

### Port Configuration

- Bridge listens on `127.0.0.1:8765` inside the container (localhost only)
- Connects outbound to `http://host.docker.internal:8766/api`
- Port configurable via `SandboxConfig.mcp_bridge_port` (default `8765`)

### Startup Sequence

`DockerBackend` handles this automatically:
1. Copies `mcp_bridge.py` to `/tmp/lucent_mcp_bridge.py` inside the container
2. Starts it as a background process
3. Health-checks `GET /health` up to 5 times with 1-second delays
4. Only proceeds if `{"status": "ok"}` is returned

### Authentication Flow

1. When `task_id` is set in `SandboxConfig`, `manager.create()` provisions a **short-lived, task-scoped API key**
2. The key is bcrypt-hashed in the DB
3. Injected as `LUCENT_SANDBOX_MCP_API_KEY` env var in the container
4. Scopes: `sandbox-memory`, `sandbox-task-events`
5. Key is **revoked on `destroy()`**

### Available MCP Tools Inside Sandbox

Agents running inside the sandbox can call these tools via `POST http://127.0.0.1:8765/mcp`:

| Tool | Description |
|------|-------------|
| `create_memory` | Create a memory (type, content, tags?, importance?, ...) |
| `search_memories` | Search memories (query?, type?, tags?, ...) |
| `update_memory` | Update an existing memory |
| `log_task_event` | Log a progress event on the linked task |
| `link_task_memory` | Link a memory to the linked task |

**Note**: The `complete_task` tool referenced in some documentation does **not exist** in the bridge spec — this is a documentation bug. Do not attempt to call it.

---

## 5. Step-by-Step Procedures

### Creating a Sandbox

```python
from lucent.sandbox.manager import get_sandbox_manager
from lucent.sandbox.models import SandboxConfig

manager = get_sandbox_manager()

# 1. Build config
config = SandboxConfig(
    image="lucent-sandbox:base",   # Always set explicitly
    task_id=task_id,
    organization_id=org_id,
    timeout_seconds=1800,
    idle_timeout_seconds=300,
)

# 2. Create container
info = await manager.create(config)

# 3. Verify healthy (critical — do not skip)
if info.status != "ready":
    raise RuntimeError(f"Sandbox failed to start: {info.status}")

live = await manager.get_live(info.sandbox_id)
if live is None or live.status != "ready":
    raise RuntimeError("Container not running after create()")

sandbox_id = info.sandbox_id
```

### Cloning a Repository

```python
# Credentials are injected automatically when git_credentials is set in config.
# For manual clone (e.g., using exec):

clone_url = f"https://{git_token}@github.com/org/repo"
result = await manager.exec(
    sandbox_id,
    f"git clone --depth=1 --branch main {clone_url} /workspace/repo",
    timeout=120,
)
if result.exit_code != 0:
    raise RuntimeError(f"Clone failed: {result.stderr}")

# Verify
files = await manager.list_files(sandbox_id, "/workspace/repo")
if not files:
    raise RuntimeError("Clone succeeded but workspace is empty")
```

**If DNS resolution fails during clone**: Verify `network_mode` is `bridge` or `allowlist` (not `none`), and that the DNS workaround is applied (Bug #3).

**If auth fails**: Token may have expired. Check `git_credentials_ttl` — set to `0` for no expiry during long tasks.

### Running Commands

```python
# Standard exec
result = await manager.exec(
    sandbox_id,
    "pytest tests/ -q --tb=short",
    cwd="/workspace/repo",
    timeout=300,
)

# Handle timeout
if result.timed_out:
    # Container is still alive — you can continue or destroy
    await manager.destroy(sandbox_id)
    raise RuntimeError("Tests timed out")

# Check exit code
if result.exit_code != 0:
    # Log stderr for debugging; decide whether to retry or fail
    print(f"STDOUT: {result.stdout}")
    print(f"STDERR: {result.stderr}")
```

### Extracting Diffs

```python
# Get the git diff directly
result = await manager.exec(
    sandbox_id,
    "git diff HEAD",
    cwd="/workspace/repo",
)
diff_text = result.stdout

# Or use process_output() for structured output handling
output = await manager.process_output(
    sandbox_id=sandbox_id,
    task_id=task_id,
    task_description="Fix the authentication bug",
    config=config,
    request_api=request_api,
    memory_api=memory_api,
    log=logger,
)
# output.diff, output.pr_url, output.commit_sha (depending on output_mode)
```

### Processing Output Modes

| `output_mode` | Behavior |
|--------------|---------|
| `diff` | Extracts `git diff HEAD` and returns it in `OutputResult.diff` |
| `pr` | Creates a branch, pushes, opens a GitHub PR |
| `review` | Creates a memory with the diff for human review |
| `commit` | Commits and pushes directly. **Requires `commit_approved=True`** |

Call `process_output()` **before** `destroy()`.

### Cleanup

```python
# Always destroy when done — even on error paths
try:
    result = await manager.process_output(...)
finally:
    await manager.destroy(sandbox_id)

# Verify no leftover volume (optional but good practice)
# Volume name: lucent-sandbox-{sandbox_id}-workspace
```

---

## 6. Common Failure Modes and Fixes

### Container Fails to Start

**Symptoms**: `info.status != "ready"` immediately after `create()`

**Causes and fixes**:
- **Image not found locally**: Run `docker pull lucent-sandbox:base` or build it. The daemon does not auto-pull images.
- **`python:3.12-slim` used instead of `lucent-sandbox:base`**: The daemon's `_create_task_sandbox()` overrides `image` to `python:3.12-slim`. If you're using the daemon integration, this is a known bug — set `image` explicitly in your `SandboxConfig` before passing to the daemon.
- **Resource limits too high**: Reduce `memory_limit` or `cpu_limit` if the host is constrained.
- **Storage quota error**: Bug #2 (storage_opt fallback) should handle this, but verify the workaround is present.

### Network Connectivity Issues

**Symptoms**: Commands fail with "network unreachable" or "connection refused"

**Causes and fixes**:
- **`network_mode="none"`** (the default): This is correct behavior. Switch to `bridge` or `allowlist` if outbound access is needed.
- **DNS resolution fails in `bridge` mode**: Bug #3 — verify `dns=['8.8.8.8', '1.1.1.1']` is passed in `containers.run()`.
- **`allowlist` mode blocks the target host**: Add the host to `allowed_hosts`. Note that `allowed_hosts` are resolved to IPs at creation time — dynamic IPs may change.
- **Docker SDK 7.x bug**: Bug #1 — verify both `network=` and `networking_config=` are passed.

### Git Clone Failures

| Error | Fix |
|-------|-----|
| `fatal: could not read Username` | Credentials not injected. Use `git_credentials` in config or embed token in URL |
| `fatal: unable to access ... Could not resolve host` | DNS issue (Bug #3) or `network_mode="none"` |
| `remote: Invalid username or password` | Token expired or invalid. Check `git_credentials_ttl` |
| Clone hangs for > 60s | Set `timeout=120` in exec call; shallow clone with `--depth=1` |

### Command Timeout

```python
if result.timed_out:
    # Option 1: Destroy and fail
    await manager.destroy(sandbox_id)
    raise RuntimeError("Command timed out")

    # Option 2: Continue with partial results
    # result.stdout may have partial output
```

Increase `SandboxConfig.timeout_seconds` for long-running tasks. The per-command `timeout` parameter in `exec()` is independent of the sandbox lifetime timeout.

### MCP Bridge Connection Failure

**Symptoms**: Agent inside sandbox can't reach `127.0.0.1:8765`

**Causes and fixes**:
- **Bridge didn't start**: Check container logs for `mcp_bridge.py` startup errors. The bridge health-checks 5 times with 1s delay — if all fail, the sandbox is still created but bridge is non-functional.
- **Wrong address**: Bridge is `127.0.0.1:8765`, not `host.docker.internal`. It's localhost inside the container.
- **API key missing**: Ensure `task_id` is set in `SandboxConfig` — this triggers key provisioning.
- **`task_id` not set**: Without a `task_id`, no API key is provisioned and auth will fail.

### Credential Expiry Mid-Task

**Symptoms**: Git operations fail partway through a long task

**Prevention**:
- Set `git_credentials_ttl=0` for tasks expected to take more than 1 hour
- Or set TTL to cover task `timeout_seconds` + 20% buffer

**Recovery**: No in-place credential refresh is supported. The task must be restarted with fresh credentials. Credential TTL tracking is in-memory only and is lost on `SandboxManager` restart.

---

## 7. Resource Limits and Timeout Configuration

### Default Limits

| Resource | Default | Config Field |
|----------|---------|-------------|
| Memory | 2 GB | `memory_limit` |
| CPU | 2 cores | `cpu_limit` |
| Disk | 10 GB | `disk_limit` |
| Sandbox lifetime | 1800s (30 min) | `timeout_seconds` |
| Idle timeout | 300s (5 min) | `idle_timeout_seconds` |
| Credentials TTL | 3600s (1 hr) | `git_credentials_ttl` |

### Adjusting Limits

```python
# Long-running data processing task
config = SandboxConfig(
    image="lucent-sandbox:base",
    memory_limit="8g",
    cpu_limit=4.0,
    disk_limit="50g",
    timeout_seconds=7200,       # 2 hours
    idle_timeout_seconds=600,   # 10 min idle
    git_credentials_ttl=7200,   # Match sandbox lifetime
    task_id=task_id,
)
```

### Idle Timeout Behavior

Each operation (`exec`, `read_file`, `write_file`, `list_files`) calls `touch()` internally, which resets the idle timer. A sandbox becomes idle when no operations are performed for `idle_timeout_seconds`. The background sweep task checks idle sandboxes and calls `destroy()` automatically.

If your workflow has long gaps between operations (e.g., waiting for a human review), increase `idle_timeout_seconds` or call `get_live()` periodically to keep the sandbox alive — though `get_live()` does **not** currently call `touch()`, so prefer a no-op `exec` like `echo keepalive`.

### Background Sweep Task

A background asyncio task in `SandboxManager` periodically:
1. Queries active sandboxes from DB
2. Checks `timeout_seconds` and `idle_timeout_seconds`
3. Calls `destroy()` on expired sandboxes

This sweep runs on the Lucent server process. If the server restarts mid-task, the sweep loses in-memory idle timestamps. Sandboxes will not be idle-destroyed until the sweep re-discovers them via their lifetime `timeout_seconds`.

### Credential TTL Management

```python
# Check if credentials are still valid before a long git operation
# (No built-in API — track manually using the creation time + TTL)
import time
created_at = info.created_at.timestamp()
ttl = config.git_credentials_ttl
if ttl > 0 and (time.time() - created_at) > ttl * 0.9:
    # Credentials are 90%+ expired — fail fast rather than retry
    raise RuntimeError("Git credentials near expiry — restart task with fresh credentials")
```

---

## 8. Orchestrator Lifecycle Procedure (Outside-Container Control Flow)

Use this sequence when acting as the sandbox-orchestrator (not the in-sandbox worker).

### 8.1 Load Context and Announce Start

```python
await log_task_event(task_id, "progress", "Loading sandbox context and validated workarounds")
```

Use memory search before provisioning:

```python
memories = await search_memories(
    query="sandbox orchestration failures workarounds docker dns storage quota",
    tags=["validated"],
    limit=10,
)
```

### 8.2 Provision + Validate

```python
manager = get_sandbox_manager()
config = SandboxConfig(
    image="lucent-sandbox:base",
    task_id=task_id,
    request_id=request_id,
    organization_id=organization_id,
    network_mode=task_config.get("network_mode", "none"),
    allowed_hosts=task_config.get("allowed_hosts", []),
    timeout_seconds=task_config.get("timeout_seconds", 1800),
)
info = await manager.create(config)
if info.status != "ready":
    raise RuntimeError(f"Sandbox failed to become ready: {info.status}")
live = await manager.get_live(info.sandbox_id)
if live is None or live.status != "ready":
    raise RuntimeError("Sandbox container not running after create()")
```

### 8.3 Verify Workspace Before Dispatch

```python
probe = await manager.exec(info.sandbox_id, "ls -A /workspace", timeout=30)
if probe.exit_code != 0:
    raise RuntimeError(f"Workspace probe failed: {probe.stderr}")
if not probe.stdout.strip():
    raise RuntimeError("Workspace is empty; refusing to dispatch work agent")
```

### 8.4 Dispatch Worker + Monitor

```python
await log_task_event(task_id, "agent_dispatched", f"Worker running in sandbox {info.sandbox_id}")
# Dispatch worker with sandbox_id, mcp endpoint (127.0.0.1:8765), and task description.
# Worker must report progress/final state via log_task_event through MCP bridge.
```

### 8.5 Collect Outputs Before Teardown

```python
if config.output_mode:
    output = await manager.process_output(
        sandbox_id=info.sandbox_id,
        task_id=task_id,
        task_description=task_description,
        config=config,
        request_api=request_api,
        memory_api=memory_api,
        log=log,
    )
```

### 8.6 Unconditional Cleanup

```python
sandbox_id = None
try:
    info = await manager.create(config)
    sandbox_id = info.sandbox_id
    # run phases 8.3-8.5
except Exception as exc:
    await log_task_event(task_id, "failed", str(exc))
    raise
finally:
    if sandbox_id:
        await manager.destroy(sandbox_id)
        await log_task_event(task_id, "progress", f"Sandbox {sandbox_id} destroyed")
```

---

## 9. Recording Results

Always persist outcomes after teardown. Use daemon-visible tags.

### 9.1 Success Pattern

```python
memory = await create_memory(
    type="technical",
    content=(
        f"Sandbox orchestration succeeded for task {task_id}. "
        f"image={config.image}, network_mode={config.network_mode}, output_mode={config.output_mode}."
    ),
    tags=["sandbox", "sandbox-operations", "orchestration", "validated", "daemon"],
    importance=7,
    shared=True,
)
await link_task_memory(task_id, memory["id"], "created")
```

### 9.2 Failure Pattern

```python
memory = await create_memory(
    type="experience",
    content=(
        f"Sandbox orchestration failed for task {task_id}. "
        f"Failure={failure_summary}. Applied_workarounds={applied_workarounds}."
    ),
    tags=["sandbox", "sandbox-operations", "failure", "rejection-lesson", "daemon"],
    importance=8,
    shared=True,
)
await link_task_memory(task_id, memory["id"], "created")
```

Include: root cause, exact symptom, workaround attempted, and whether cleanup succeeded.

---

## 10. Anti-Patterns

1. **Running with implicit image defaults** (`python:3.12-slim`) instead of setting `image="lucent-sandbox:base"` explicitly.
2. **Skipping live status checks** after `create()` and assuming DB status implies container health.
3. **Dispatching workers before workspace validation**, causing empty-repo executions.
4. **Treating known platform bugs as intermittent**, instead of applying DNS/network/storage workarounds every time.
5. **Returning success before teardown**, leaving orphaned containers, volumes, or scoped API keys.
