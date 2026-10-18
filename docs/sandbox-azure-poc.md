# ACI sandbox backend — live proof-of-concept gate

**This file is the gate.** The ACI backend is unit-tested against faked Azure
SDK clients and a faked WebSocket, but it must not be called usable until the
checks below have been run against a real subscription and recorded. ACI exec
is terminal emulation, not an argv API; several guarantees can only be proven
live.

Run this before enabling `LUCENT_SANDBOX_BACKEND=aci` anywhere that matters.

---

## 0. What is being validated, in one paragraph

A sandbox is an ACI **container group** with a user container and the trusted
MCP-bridge sidecar sharing one network namespace, deployed **Private** into a
delegated VNet subnet. The user container receives `primary_env` only; the
task-scoped API key reaches the sidecar only. A group for a public image
carries **no managed identity**; a group pulling from a private Azure Container
Registry (`*.azurecr.io`) carries the dedicated ACR-pull identity in **both**
the group `identity` block and `imageRegistryCredentials[].identity` (ACI
requires both, and the identity is reachable from untrusted sandbox code over
IMDS — see §8.3). Exec is implemented by calling `containers.execute_command()`,
opening the returned WebSocket, sending the password as the first frame, and
reading terminal output while a sentinel appended by `/bin/sh -c` carries the
exit code. `stderr` is merged into `stdout`; binary and very large output are
best-effort. `rebind_bridge` always returns `False`, so reuse creates a fresh
group. `allowlist` egress fails closed unless the subnet-wide NSG deviation is
explicitly acknowledged.

---

## 1. Prerequisites

- An Azure subscription and a resource group for Lucent sandboxes.
- A VNet subnet **delegated to `Microsoft.ContainerInstance/containerGroups`**.
  See `deploy/terraform/` (owned separately) for the expected subnet id.
- An image that runs as a **non-root user baked into the image** (see §7) and
  contains `git` and `python3` for the checks below. `python:3.12-slim` works
  for the exec tests; the real sandbox image should be used for the final pass.
- The trusted bridge image (`LUCENT_SANDBOX_BRIDGE_IMAGE`) pushed somewhere ACI
  can pull it. A public registry needs no identity. For a private ACR image,
  set `LUCENT_SANDBOX_ACI_IDENTITY_ID` to a **dedicated** identity that holds
  only `AcrPull` on the registry and no other role anywhere (see §8.3).
- The Lucent server URL reachable from the ACI subnet. **Do not** leave
  `LUCENT_SANDBOX_BRIDGE_API_URL` at the Docker default
  `http://host.docker.internal:8766/api`; the backend fails closed on it.
- `az` CLI logged in with permission to create/delete container groups and
  read them, and the application identity with `Microsoft.ContainerInstance`
  permissions. The backend uses `DefaultAzureCredential`; set `AZURE_CLIENT_ID`
  when selecting a user-assigned identity.

Environment used by every command below:

```sh
export AZURE_SUBSCRIPTION_ID=<subscription-id>
export LUCENT_SANDBOX_BACKEND=aci
export LUCENT_SANDBOX_ACI_RESOURCE_GROUP=<resource-group>
export LUCENT_SANDBOX_ACI_SUBNET_ID=/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<delegated-subnet>
export LUCENT_SANDBOX_ACI_LOCATION=<region>
export LUCENT_SANDBOX_ACI_IDENTITY_ID=/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/<acr-pull-identity>  # dedicated AcrPull-only identity
export LUCENT_SANDBOX_BRIDGE_IMAGE=<registry>/lucent-sandbox:base
export LUCENT_SANDBOX_BRIDGE_API_URL=https://<lucent-host>/api
```

Start from a clean unit-test baseline:

```sh
.venv/bin/ruff check src/lucent/sandbox/aci_backend.py src/lucent/sandbox/manager.py
.venv/bin/pytest tests/test_aci_backend.py -q
```

**Assert:** all unit tests pass. Do not proceed otherwise.

---

## 2. Create a plain sandbox and record its group

Run this driver from the repository root. It creates a sandbox with a public
Python image, no task/bridge, and prints the group name.

```sh
.venv/bin/python - <<'PY'
import asyncio
from lucent.sandbox.aci_backend import AzureContainerInstancesBackend
from lucent.sandbox.models import SandboxConfig

async def main():
    backend = AzureContainerInstancesBackend(ready_timeout=600)
    info = await backend.create(SandboxConfig(
        name="aci-poc",
        image="python:3.12-slim",
        network_mode="bridge",
        timeout_seconds=7200,
        idle_timeout_seconds=0,
    ))
    print("status:", info.status.value, "error:", info.error)
    print("id:", info.id)
    print("group:", info.container_id)
    assert info.status.value == "ready", info.error

asyncio.run(main())
PY
```

**Assert:**

- `status == ready` within 10 minutes.
- `az container show -g "$LUCENT_SANDBOX_ACI_RESOURCE_GROUP" -n <group> -o json`
  shows `provisioningState: Succeeded` and the sandbox container `Running`.
- `ipAddress.type == "Private"`; there is **no** `dnsNameLabel`, no public IP.
- `subnetIds[0].id` equals `$LUCENT_SANDBOX_ACI_SUBNET_ID`.
- `identity` is absent/null for this public image (no managed identity; the
  private-ACR case is covered in §8.3).
- `volumes == [{"name": "workspace", "emptyDir": {}}]`.
- `restartPolicy == "Never"`.
- tags include `lucent-managed=true` and `lucent-sandbox-id=<id>`.
- The declared `ipAddress.ports` entry is the backend's unused placeholder
  (`65535`). Nothing listens on it; confirm from outside the VNet that the
  private IP is unreachable and from inside that a connection to `65535`
  is refused (not served).

Keep the group name and id for the next sections. If the driver destroyed it,
re-run and hold the process open.

---

## 3. Exec: exit codes, stream separation, timeout

Run each command through `backend.exec`, printing `exit_code`, `timed_out`,
`repr(stdout)`, `repr(stderr)`. Create a fresh backend and resolve the sandbox
id from the group tag (`lucent-sandbox-id`) or reuse the id printed above.

```sh
.venv/bin/python - <<'PY'
import asyncio
from lucent.sandbox.aci_backend import AzureContainerInstancesBackend

SANDBOX_ID = "<id from section 2>"

async def main():
    backend = AzureContainerInstancesBackend()
    checks = [
        ("echo hello", 0, "hello"),
        ("printf 'a\\nb\\n'", 0, "a\nb"),
        ("echo out; echo err >&2; exit 7", 7, "out"),
        ("exit 42", 42, ""),
    ]
    for command, expected_rc, expected_text in checks:
        result = await backend.exec(SANDBOX_ID, command, timeout=60)
        print(command, "->", result.exit_code, result.timed_out,
              repr(result.stdout), repr(result.stderr))
        assert result.exit_code == expected_rc, (command, result)
        assert expected_text in result.stdout
        # stderr is merged into stdout by ACI; it is never separate.
        assert result.stderr == ""

    # Timeout: the process is abandoned (not killed) on the ACI side.
    result = await backend.exec(SANDBOX_ID, "sleep 60", timeout=2)
    print("timeout ->", result.exit_code, result.timed_out, result.stderr)
    assert result.exit_code == 124 and result.timed_out

asyncio.run(main())
PY
```

**Assert:**

- Exit codes 0, 7 and 42 survive the sentinel, including when the command
  itself exits non-zero.
- `stderr == ""` always, and both streams appear in `stdout`. This is expected
  and must be documented in user-facing material.
- The timeout returns `124`, `timed_out=True`, and `stderr` names the timeout.
- After the timeout, run `ps` in the sandbox and confirm the abandoned `sleep`
  may still be running (ACI provides no kill channel). Confirm a subsequent
  exec still works.

### Sentinel robustness

```sh
# Run each of these at least 20 times in a loop.
true
false
exit 1
(exit 3); echo after
printf 'no trailing newline'
sh -c 'trap "" TERM; echo trapped'
```

**Assert:** every run returns the correct synthesized code, no run reports the
"stream ended without an exit sentinel" error, and output is not truncated.
If any run fails the sentinel, the backend is not usable for that command shape.

Also record the known sentinel-breaking shapes: `exec ...` replaces the shell
and `kill $$` kills it, so neither reaches the sentinel. The backend reports
exit 1 with "ended without an exit sentinel". Confirm callers wrap such commands
(e.g. `sh -c 'exec ...'` still replaces the inner shell, so use a background
process plus `wait`, or avoid `exec`).

---

## 4. Large, binary, concurrent and long-running output

```sh
.venv/bin/python - <<'PY'
import asyncio
import base64
from lucent.sandbox.aci_backend import AzureContainerInstancesBackend

SANDBOX_ID = "<id from section 2>"

async def main():
    backend = AzureContainerInstancesBackend()

    # Large text: 20k lines, ~108 KiB.
    r = await backend.exec(SANDBOX_ID, "seq 1 20000 | wc -l", timeout=120)
    print("large:", r.exit_code, repr(r.stdout), repr(r.stderr))
    assert r.exit_code == 0 and r.stdout.strip() == "20000"

    # Large pass-through: check for truncation, not just the count.
    r = await backend.exec(SANDBOX_ID, "seq 1 20000", timeout=120)
    lines = [line for line in r.stdout.splitlines() if line.strip()]
    print("lines received:", len(lines), "rc:", r.exit_code)
    assert r.exit_code == 0 and len(lines) == 20000

    # "Binary": transport binary as base64, then decode and compare lengths.
    r = await backend.exec(
        SANDBOX_ID,
        "head -c 4096 /dev/urandom | base64 -w 0",
        timeout=60,
    )
    raw = base64.b64decode("".join(r.stdout.split()), validate=True)
    print("binary transport bytes:", len(raw))
    assert r.exit_code == 0 and len(raw) == 4096

    # Raw binary through the terminal is expected to be unreliable. Record what
    # happens; do not depend on it.
    r = await backend.exec(
        SANDBOX_ID,
        "printf '\\000\\001\\377' | od -An -tx1 | tr -d ' \\n'",
        timeout=60,
    )
    print("raw binary via od:", r.exit_code, repr(r.stdout))
    assert r.exit_code == 0 and r.stdout.strip() == "0001ff"

    # Concurrency: eight simultaneous sessions.
    results = await asyncio.gather(*[
        backend.exec(SANDBOX_ID, f"sleep 1; echo session-{i}", timeout=60)
        for i in range(8)
    ])
    assert all(r.exit_code == 0 and f"session-{i}" in r.stdout
               for i, r in enumerate(results))
    print("concurrency: ok")

    # Long-running command within timeout.
    r = await backend.exec(SANDBOX_ID, "sleep 8; echo done", timeout=30)
    assert r.exit_code == 0 and "done" in r.stdout
    print("long-running: ok")

asyncio.run(main())
PY
```

**Assert:**

- 20,000 lines arrive intact (no truncation, no dropped tail, sentinel present).
- 4 KiB of random bytes round-trip through base64.
- Eight concurrent sessions all succeed and do not interleave.
- A command longer than a few seconds still returns its output.
- Record the largest output size that survives intact. **This becomes the
  documented `_MAX_FILE_BYTES`-adjacent exec limit**; the built-in file guard is
  1 MiB, but exec is not the file path.

**If large output truncates:** the backend is unusable for commands that emit
large output (repo clones, test suites). Either lower the supported ceiling and
enforce it in callers, or replace the exec transport before shipping.

---

## 5. Disconnects

```sh
.venv/bin/python - <<'PY'
import asyncio
from lucent.sandbox.aci_backend import AzureContainerInstancesBackend

SANDBOX_ID = "<id from section 2>"

async def main():
    backend = AzureContainerInstancesBackend()
    group = await asyncio.to_thread(backend._find_group, SANDBOX_ID)
    uri, password = await asyncio.to_thread(
        backend._request_exec, group.name, "sandbox",
        "/bin/sh -c 'echo start; sleep 20; echo end'",
    )
    ws = await backend._open_websocket(uri)
    await ws.send(password)
    first = await asyncio.wait_for(ws.recv(), timeout=10)
    print("first frame:", repr(first))
    await ws.close()  # deliberate client disconnect mid-command

    # A fresh exec must still work, and the abandoned command may still run.
    result = await backend.exec(SANDBOX_ID, "echo still-alive", timeout=30)
    print("after disconnect:", result.exit_code, repr(result.stdout))
    assert result.exit_code == 0 and "still-alive" in result.stdout

    # A second ticket for the same sandbox must be independent.
    result = await backend.exec(SANDBOX_ID, "echo second", timeout=30)
    assert result.exit_code == 0 and "second" in result.stdout
    print("reconnect: ok")

asyncio.run(main())
PY
```

**Assert:**

- Closing the WebSocket mid-command does not wedge the container or the
  sandbox.
- The next `execute_command` ticket works immediately.
- The abandoned process may continue; the backend does not claim it was killed.

---

## 6. User container exits / restart policy

Use a **throwaway** sandbox for this section.

```sh
.venv/bin/python - <<'PY'
import asyncio
from lucent.sandbox.aci_backend import AzureContainerInstancesBackend
from lucent.sandbox.models import SandboxConfig

async def main():
    backend = AzureContainerInstancesBackend(ready_timeout=600)
    info = await backend.create(SandboxConfig(
        name="aci-poc-exit", image="python:3.12-slim", network_mode="bridge"
    ))
    assert info.status.value == "ready", info.error
    print("group:", info.container_id)

    # The keepalive shell is PID 1 and traps TERM, exiting 0.
    result = await backend.exec(info.id, "kill -TERM 1", timeout=30)
    print("kill ->", result.exit_code, repr(result.stdout), repr(result.stderr))

    for _ in range(30):
        live = await backend.get(info.id)
        print("state:", live.status.value if live else None)
        if live and live.status.value in {"stopped", "failed"}:
            break
        await asyncio.sleep(2)

    live = await backend.get(info.id)
    assert live is not None
    assert live.status.value in {"stopped", "failed"}, live.status

    # No automatic restart: a later exec must not reach a live container.
    after = await backend.exec(info.id, "echo resurrected", timeout=20)
    print("post-exit exec:", after.exit_code, repr(after.stderr))
    assert after.exit_code != 0

    await backend.destroy(info.id)

asyncio.run(main())
PY
```

**Assert:**

- The user container exits 0 on TERM (our keepalive trap).
- `restartPolicy: Never` does not resurrect it. If ACI restarts it anyway,
  `get` must not keep reporting READY and the group must not silently outlive
  the sandbox — this is a gate failure until a monitor/delete loop exists.
- `get` maps the terminated state to `stopped` (exit 0) or `failed` (nonzero),
  never `ready`.
- After exit, exec fails rather than hanging forever.

---

## 7. Files and path confinement

```sh
.venv/bin/python - <<'PY'
import asyncio, os
from lucent.sandbox.aci_backend import AzureContainerInstancesBackend

SANDBOX_ID = "<id from section 2>"

async def main():
    backend = AzureContainerInstancesBackend()
    payload = os.urandom(256 * 1024)  # well under the 1 MiB transport guard
    await backend.write_file(SANDBOX_ID, "/workspace/poc/blob.bin", payload)
    assert await backend.read_file(SANDBOX_ID, "/workspace/poc/blob.bin") == payload
    print("256 KiB round-trip: ok")

    # list_files sees the file.
    entries = await backend.list_files(SANDBOX_ID, "/workspace/poc")
    print(entries)
    assert any(entry["name"] == "blob.bin" and entry["size"] == len(payload)
               for entry in entries)

    # Writes outside /workspace are refused by the trusted backend.
    for path in ["/etc/passwd", "/workspace/../etc/passwd"]:
        try:
            await backend.write_file(SANDBOX_ID, path, b"x")
        except ValueError:
            print("refused:", path)
        else:
            raise AssertionError(f"write escaped the workspace: {path}")

    # The transport guard refuses oversized files rather than corrupting them.
    try:
        await backend.read_file(SANDBOX_ID, "/workspace/poc/huge.bin")
    except FileNotFoundError:
        pass
    try:
        await backend.write_file(
            SANDBOX_ID, "/workspace/poc/huge.bin", b"x" * (1024 * 1024 + 1)
        )
    except ValueError as exc:
        print("size guard:", exc)
    else:
        raise AssertionError("oversized write was not refused")

asyncio.run(main())
PY
```

**Assert:**

- A 256 KiB binary file round-trips byte-for-byte through base64. Also test a
  payload just under 1 MiB: the backend writes in 48 KiB chunks because
  `/bin/sh -c` rejects a single argument over 128 KiB (`MAX_ARG_STRLEN`).
- `list_files` returns names, sizes, and directory flags; the `find -printf`
  path works on the image (if it does not, the `ls -la` fallback is exercised).
- Absolute and `..` escapes are refused.
- A file over 1 MiB is refused, not silently truncated. Record what a file just
  under 1 MiB does; if the ARM exec request body rejects it, lower
  `_MAX_FILE_BYTES`.

---

## 8. Bridge, env split, and ACR identity

### 8.1 Bridge env split and reachability

Create a **task-linked** sandbox so the sidecar is present. The PoC supplies a
fake key; the real manager mints one. The bridge will fail API calls with the
fake key, but `/health` must answer.

```sh
.venv/bin/python - <<'PY'
import asyncio
from lucent.sandbox.aci_backend import AzureContainerInstancesBackend
from lucent.sandbox.models import SandboxConfig

async def main():
    backend = AzureContainerInstancesBackend(
        ready_timeout=600, bridge_timeout=60
    )
    config = SandboxConfig(
        name="aci-poc-bridge",
        image="python:3.12-slim",
        network_mode="bridge",
        task_id="poc-task",
        mcp_bridge_port=8765,
        env_vars={
            "LUCENT_SANDBOX_MCP_ENABLED": "1",
            "LUCENT_SANDBOX_MCP_API_KEY": "poc-not-a-real-key",
            "LUCENT_API_URL": "https://<lucent-host>/api",
        },
    )
    info = await backend.create(config)
    print("status:", info.status.value, "error:", info.error)
    assert info.status.value == "ready", info.error
    print("group:", info.container_id, "id:", info.id)
    # Keep this sandbox alive for the az inspection below.

asyncio.run(main())
PY
```

Then inspect the group:

```sh
az container show -g "$LUCENT_SANDBOX_ACI_RESOURCE_GROUP" -n <group> -o json \
  > /tmp/aci-poc-group.json
```

**Assert (in `/tmp/aci-poc-group.json`):**

- The `sandbox` container's environment has **no**
  `LUCENT_SANDBOX_MCP_API_KEY`, no `LUCENT_SANDBOX_TASK_ID`, and no
  `LUCENT_API_URL`; it does have `LUCENT_SANDBOX_MCP_ENABLED=1`.
- The `mcp-bridge` container's definition lists `LUCENT_SANDBOX_MCP_API_KEY` as
  a `secureValue` (the value itself is absent from the GET response), plus
  `LUCENT_SANDBOX_TASK_ID`, `LUCENT_API_URL`, `LUCENT_SANDBOX_MCP_PORT`, and
  `LUCENT_MCP_BRIDGE_SOURCE_B64` as ordinary values.
- The `mcp-bridge` container is `Running` and, because this uses a public
  image, the group has no managed identity.
- **The task key appears nowhere in `/tmp/aci-poc-group.json`.** If it does,
  stop: `secureValue` was not honoured.
- From the **user** container, the bridge answers on loopback:

```sh
.venv/bin/python - <<'PY'
import asyncio
from lucent.sandbox.aci_backend import AzureContainerInstancesBackend

SANDBOX_ID = "<bridge sandbox id>"

async def main():
    backend = AzureContainerInstancesBackend()
    result = await backend.exec(
        SANDBOX_ID,
        "python3 -c \"import urllib.request; "
        "print(urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).status)\"",
        timeout=30,
    )
    print(result.exit_code, repr(result.stdout), repr(result.stderr))
    assert result.exit_code == 0 and "200" in result.stdout

asyncio.run(main())
PY
```

**Assert:** the user container reaches `127.0.0.1:8765` because it shares the
group's network namespace. This is the reason ACI was chosen.

### 8.2 Security context

**Assert:** the `sandbox` container has `securityContext.privileged == false`
and **no** `runAsUser`, `runAsGroup`, `capabilities`, `seccompProfile` or
`allowPrivilegeEscalation` fields. Standard ACI rejects those fields with
`SecurityContextNotSupported`; the service supports only `privileged`. Record
the API version the SDK used (`2023-05-01` by default).

Then verify non-root the only way ACI permits: run `id -u` in the user container
and confirm the image's baked-in `USER` is non-zero. If the image runs as root,
**stop**: the backend cannot enforce non-root at the platform layer. Fix the
image (`USER 1000` and a writable `/workspace`, or chown in a trusted build),
or accept a documented risk decision.

**Assert:** `id -u` is not `0`.

### 8.3 ACR pull identity

ACI mandates that the **same** user-assigned identity appear in both the
container group's `identity` block and `imageRegistryCredentials[].identity`.
Without the group identity, a private pull fails with
`InvalidImageRegistryIdentity`. This is a platform constraint, not a design
choice. The backend therefore attaches the identity named by
`LUCENT_SANDBOX_ACI_IDENTITY_ID` in **both** places, and only when the image
comes from `*.azurecr.io`. A public image attaches no identity and no registry
credentials. The backend never sends a registry username, password, or key.

The exposure is real and must be stated plainly: **untrusted code in any
container of the group can reach IMDS and obtain a token for this identity.**
The mitigation is the identity's scope, not secrecy. It must be a
**dedicated** pull identity whose only role assignment is `AcrPull` on the
container registry. A token then grants registry read (pull images) and
nothing else: no storage, no database, no Key Vault, no application API, and
no tenant data. It must never be the app, controller, or provisioner identity.
The backend cannot verify role assignments, so this is a deployment invariant:
Terraform provisions the identity with exactly `AcrPull`, and the checks below
verify it live.

Run a sandbox with a private ACR image (`<acr>.azurecr.io/...` for
`config.image`) and the dedicated identity configured.

**Assert (live, all mandatory):**

- Creation succeeds, proving the pull works with the identity.
- `az container show ... -o json` lists the identity in **both**
  `identity.userAssignedIdentities` (with `type: UserAssigned`) and
  `imageRegistryCredentials[0].identity`.
- `imageRegistryCredentials` has `server` and `identity` only — **no**
  `username`, `password`, or key.
- **The identity has no other role assignments.** Check all scopes:
  ```sh
  az role assignment list --assignee <pull-identity-principal-id> --all -o json
  ```
  The only assignment must be `AcrPull` on the container registry. Any second
  assignment — even Reader on the resource group — is a gate failure; fix the
  Terraform before proceeding.
- **The token is registry-read only.** From the user container, obtain a
  token from IMDS for the identity (this proves the exposure is real):
  ```sh
  curl -s -H Metadata:true \
    'http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/'
  ```
  Confirm an ACR pull succeeds while calls outside the registry pull
  permission are denied. Exchange the token at the ACR login endpoint
  (`/oauth2/exchange`) and confirm registry read works; then attempt ARM,
  Key Vault, or Storage operations and confirm the service rejects them with
  an authorization error. Record the exact commands and status codes.
- A public-image group (e.g. `python:3.12-slim`) still has no `identity` and
  no `imageRegistryCredentials`.

### 8.4 Git credentials are visible to the Azure control plane

ACI has no per-exec env channel. The backend exports `LUCENT_GIT_USERNAME` /
`LUCENT_GIT_TOKEN` inside the `execute_command` request body, so a principal
with `Microsoft.ContainerInstance/containerGroups/containers/exec` (or ARM
request-log access) can observe them for the life of the request. Prefer
short-lived deploy tokens / GitHub App tokens and keep `git_credentials_ttl`
low. Confirm the token is absent from the container definition in
`/tmp/aci-poc-group.json` (it must only exist in the exec request, never in
container env).

---

## 9. Egress modes

### 9.1 `allowlist` fails closed

```sh
.venv/bin/python - <<'PY'
import asyncio
from lucent.sandbox.aci_backend import AzureContainerInstancesBackend
from lucent.sandbox.models import SandboxConfig

async def main():
    backend = AzureContainerInstancesBackend(allow_subnet_egress=False)
    info = await backend.create(SandboxConfig(
        name="aci-poc-allowlist",
        image="python:3.12-slim",
        network_mode="allowlist",
        allowed_hosts=["example.com"],
    ))
    print(info.status.value, info.error)
    assert info.status.value == "failed"
    assert "LUCENT_SANDBOX_ACI_ALLOW_SUBNET_EGRESS" in info.error
    print("allowlist refused: ok")

asyncio.run(main())
PY
```

**Assert:** the sandbox is `failed`, the message explains that NSGs are
subnet-wide and names the opt-in variable, and **no container group was
created** (`az container list` shows no new group).

### 9.2 The opt-in is a subnet-wide deviation, not a per-sandbox allowlist

With `LUCENT_SANDBOX_ACI_ALLOW_SUBNET_EGRESS=1`, create an allowlist sandbox
and prove the deviation: from inside it, reach a host **not** in
`allowed_hosts` (for example `curl -sS https://example.org`). If it succeeds,
subnet egress is wider than requested and the opt-in is doing exactly what it
says. Record this in the deployment documentation.

**Assert:** with the opt-in, creation succeeds and the deviation is observable.
Without the opt-in, creation fails closed. Never treat `allowlist` as `bridge`
silently.

### 9.3 `none`

Create a `network_mode="none"` sandbox and confirm the group is Private with no
public IP. From inside, test whether egress works. **Assert:** the result is
recorded. ACI cannot enforce per-group egress, so `none` is only as isolated as
the subnet NSG. If the subnet allows egress, `none` does **not** isolate; the
deployment must provide a deny-egress NSG (or a separate subnet) for this to be
true isolation.

---

## 10. Lifecycle and restart recovery

1. Restart the Lucent process (or construct a brand-new
   `AzureContainerInstancesBackend`), then call `get(<sandbox id>)`.
   **Assert:** it returns the live sandbox by tag, with the correct status,
   without any in-process cache.
2. Call `list_all()`. **Assert:** only groups tagged
   `lucent-managed=true` are returned; a hand-created group in the same
   resource group is not.
3. Call `rebind_bridge(<id>, config_with_new_task)`. **Assert:** it returns
   `False`, and the manager-level path revokes the new key and creates a fresh
   group (unit test covers the manager path; verify the live behaviour by
   observing a new group id).
4. Call `stop(<id>)`, then `get(<id>)`. **Assert:** stop maps to destroy: the
   group is gone and there is no resumable state. Data in the `emptyDir` is
   gone.
5. Call `destroy(<id>)` twice. **Assert:** the second call is a no-op (404 is
   swallowed).
6. Let a sandbox hit `timeout_seconds` through the manager and confirm the
   group is deleted by the auto-destroy task.

---

## 11. Scale and operational limits

- Create a group while eight concurrent execs run; confirm the ticket request
  and WebSocket open are independent and no shared client state corrupts them.
- Confirm the group's total CPU/memory includes the bridge sidecar
  (`0.25` vCPU / `0.25` GB) and respects the ACI group minimum of 1 vCPU / 1 GB.
  Request a deliberately tiny `cpu_limit`/`memory_limit` and confirm the
  backend clamps them rather than failing deployment.
- Confirm `cpu_limit > 4` and large `memory_limit` behave as ACI allows, or
  record the platform rejection. CPU/memory are best-effort, not security.
- Confirm `docker_bind_mounts` are ignored with a warning (ACI has no host bind
  mounts) and do **not** cause a failure.
- Confirm a `dockerfile` request is failed closed (ACI cannot build images).
- Confirm group name length stays within 63 characters for long
  `config.name` values.

---

## 12. Sign-off checklist

Copy this list into the deployment record and tick each item:

- [ ] Unit tests (`tests/test_aci_backend.py`) and `ruff check` pass.
- [ ] Group is Private, subnet-delegated, no DNS label; identity absent for
      public images.
- [ ] User container has no task-scoped key or bridge routing env.
- [ ] Bridge sidecar has the key as `secureValue`; the key is absent from GET.
- [ ] Bridge sidecar answers `/health` from the user container.
- [ ] Exit codes 0 / 7 / 42 survive the sentinel, repeatedly.
- [ ] stderr is merged into stdout; documented for users.
- [ ] 20k-line output, 4 KiB base64 binary, 8 concurrent execs, long command.
- [ ] Disconnect mid-command; next exec still works.
- [ ] User container exit maps to stopped/failed and never restarts silently.
- [ ] 256 KiB file round-trip; workspace-escape writes refused; 1 MiB guard.
- [ ] `id -u` is non-zero (non-root baked into the image) and documented.
- [ ] ACR pull: identity in **both** the group and the registry credential;
      the dedicated identity has **only** `AcrPull`; its IMDS token is
      registry-read only; no registry password anywhere.
- [ ] `allowlist` fails closed; opt-in deviation demonstrated and documented.
- [ ] `none` egress behaviour recorded; subnet NSG requirement documented.
- [ ] `get` after process restart; `list_all` filters; `rebind_bridge` False.
- [ ] `stop`/`destroy` idempotent; timeout cleanup observed.
- [ ] `LUCENT_SANDBOX_BACKEND=<unknown>` fails closed (unit test).

Until every box is ticked, keep `LUCENT_SANDBOX_BACKEND=docker` (or
`kubernetes`) in production.
