"""Script executor: run a program in a sandbox without an agent session.

A task of kind ``script`` is claimed by the tracked-task dispatcher and
executed here. The executor reuses the daemon's existing sandbox lifecycle
(``_create_task_sandbox`` / ``_destroy_task_sandbox``) so a script task is
provisioned exactly like a sandboxed agent task — the only differences are
that the program, not a model, does the work, and the sandbox is torn down as
soon as stdout/stderr/exit code have been captured.

Two code sources share that lifecycle:

* ``inline`` — the body in ``execution_config.script`` is written to a file in
  the sandbox working dir and run with its interpreter.
* ``git`` — ``repo_url``/``ref``/``path`` are resolved inside the sandbox. The
  ref is shallow-fetched (``--depth=1``) into a task-scoped subdirectory, and
  the repository's own file is executed in place so its shebang and file mode
  are honoured. Only https:// URLs are accepted. A credential is read from the
  secrets service at dispatch, injected through a GIT_ASKPASS helper in the
  exec environment, and never written to config, the task row, an event, or a
  log line. A ref that is a branch or tag is reported as not pinned to a SHA.

Input:

* An explicitly authored ``execution_config.stdin`` always wins and is the
  only input written.
* Otherwise, if the task's request carries a persisted trigger payload (a
  webhook workflow), the payload envelope is written to stdin as JSON and the
  raw payload to a file inside the sandbox named by the
  ``LUCENT_TRIGGER_PAYLOAD_FILE`` environment variable. Delivery is capped at
  ``SCRIPT_PAYLOAD_MAX_BYTES``; an oversized payload fails the task before
  provisioning, without running the script, and without echoing its content.
* Both files live in the sandbox working dir and are destroyed with it.

Failure semantics:

* A nonzero exit fails the task with the raw stdout/stderr and a preview.
* A stdout payload that violates the task's ``output_contract.json_schema``
  fails the task immediately and is never retried (a deterministic wrong shape
  cannot be fixed by re-running).
* ``output_contract.on_failure='retry_then_fallback'`` with ``max_retries``
  enables a bounded retry of execution failures (timeout / nonzero exit). Any
  other policy runs once.
* A source that cannot be resolved (blocked host, failed clone, missing ref,
  missing path) fails before execution; the executor never runs on a failed
  resolution.
* The template's ``timeout_seconds`` is the hard execution cap; a timed-out
  run records ``script_timed_out`` before failing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import shlex
from typing import Any, Callable

from daemon.runtime.module_proxy import runtime
from lucent.script_tasks import (
    GIT_ASKPASS_SCRIPT,
    SCRIPT_PAYLOAD_FILE_ENV,
    SCRIPT_PAYLOAD_MAX_BYTES,
    SCRIPT_PREVIEW_CHARS,
    SCRIPT_STDERR_CAP,
    SCRIPT_STDOUT_CAP,
    TriggerPayloadTooLargeError,
    derive_interpreter,
    encode_trigger_payload,
    git_resolution_steps,
    interpreter_argv,
    interpreter_from_shebang,
    normalize_repo_path,
    parse_script_stdout,
    preview,
    ref_pinning,
    repo_host,
    template_reaches_host,
    truncate,
    validate_git_ref,
    validate_repo_url,
    validate_script_config,
)

# A script may be retried at most this many extra times, however large a
# contract's max_retries is. Retries are opt-in and always bounded.
SCRIPT_MAX_EXTRA_ATTEMPTS = 3

# Root for task-scoped git checkouts inside the sandbox working dir. Keeping
# the checkout below its own directory means a git source never lands on top of
# the template's working directory (or a repository the template already
# cloned there).
SCRIPT_GIT_DIRNAME = ".lucent-scripts"
GIT_ASKPASS_USERNAME = "x-access-token"

# stderr fragments that mean "the ref does not exist at the remote" rather
# than "the transport/authentication failed".
_GIT_REF_MISSING_HINTS = (
    "couldn't find remote ref",
    "could not find remote ref",
    "not our ref",
    "unknown revision",
    "no such ref",
)


class ScriptResolutionError(RuntimeError):
    """A git source could not be resolved; the message is user-actionable."""



def _coerce_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


async def script_entitlement_failure(
    task: dict[str, Any],
) -> dict[str, Any] | None:
    """The dispatch-time ``custom_sandboxes`` refusal for a script task.

    Dispatch re-checks the entitlement because it can be revoked between
    authoring a workflow and its run: a queued script task authored while the
    organization was entitled must not provision a sandbox after the row is
    turned off. The organization is read from the task row itself — never from
    a caller argument — so the check is always charged to the task's owner.

    There is no cache. Every dispatch reads the entitlement row through the
    shared gate helper, so a revocation is visible to the next dispatch; the
    only window is a dispatch already in flight (claimed before the write
    landed), and the sandbox lifecycle gate re-checks immediately before
    provisioning.

    A task with no owning organization is refused outright: same default-deny
    contract as a missing entitlement row.
    """
    from lucent.services.entitlements import (
        CAPABILITY_SANDBOXES,
        capability_gate_message,
    )

    org_id = str(task.get("organization_id") or "").strip()
    if not org_id:
        return {
            "stage": "script_not_entitled",
            "capability": CAPABILITY_SANDBOXES,
            "detail": (
                "Script task has no owning organization, so the "
                f"'{CAPABILITY_SANDBOXES}' entitlement cannot be resolved; "
                "the task is refused"
            ),
        }
    error = await capability_gate_message(org_id, CAPABILITY_SANDBOXES)
    if error is None:
        return None
    return {
        "stage": "script_not_entitled",
        "capability": CAPABILITY_SANDBOXES,
        "detail": error,
    }


async def _event(
    task_id: str,
    event_type: str,
    detail: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit a task event; audit failures never mask the script outcome."""
    try:
        await runtime.RequestAPI.add_event(task_id, event_type, detail, metadata)
    except Exception as exc:  # noqa: BLE001 — best-effort telemetry
        runtime.log(f"Script task {task_id[:8]} event {event_type} failed: {exc}", "WARN")


def _script_filename(task_id: str) -> str:
    return f".lucent-script-{task_id[:8]}.script"


def _stdin_filename(task_id: str) -> str:
    return f".lucent-script-{task_id[:8]}.stdin"


def _payload_filename(task_id: str) -> str:
    return f".lucent-script-{task_id[:8]}.payload"


async def _fetch_trigger_payload(
    request_id: str | None,
) -> dict[str, Any] | None:
    """Return the request's persisted trigger payload, or ``None``.

    Consulted only when the author did not author ``execution_config.stdin``
    — an authored input always wins, and the payload is not even read. A
    read failure degrades to "no payload" (today's behaviour) rather than
    failing a script that would otherwise have run; the payload itself is
    never logged.
    """
    if not request_id:
        return None
    try:
        row = await runtime.RequestAPI.get_request(request_id)
    except Exception as exc:  # noqa: BLE001 — input retrieval is best-effort
        runtime.log(
            f"Script trigger payload fetch failed for request "
            f"{str(request_id)[:8]}: {exc}",
            "WARN",
        )
        return None
    if not isinstance(row, dict):
        return None
    payload = row.get("trigger_payload")
    return payload if isinstance(payload, dict) else None


def _render_result(
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
    timed_out: bool,
    validation_errors: list[str] | None = None,
    source_lines: list[str] | None = None,
) -> str:
    lines = [f"exit_code: {exit_code}", f"timed_out: {str(timed_out).lower()}"]
    if source_lines:
        lines.extend(source_lines)
    if validation_errors:
        lines.append("validation_errors: " + "; ".join(validation_errors))
    lines.extend(
        [
            "--- stdout ---",
            stdout if stdout else "(empty)",
            "--- stderr ---",
            stderr if stderr else "(empty)",
        ]
    )
    return "\n".join(lines)


def _redact(text: str | None, secret_values: list[str]) -> str:
    """Replace any injected credential in text before it leaves the sandbox."""
    value = "" if text is None else str(text)
    for secret in secret_values:
        if secret:
            value = value.replace(secret, "***")
    return value


def _git_stderr(stderr: str | None, secret_values: list[str]) -> str:
    return preview(_redact(stderr, secret_values), SCRIPT_PREVIEW_CHARS).strip()


def _split_git_credential(credential: str) -> tuple[str, str]:
    """Split ``user:password`` credentials, matching the sandbox backend."""
    if ":" in credential:
        username, password = credential.split(":", 1)
        if username and password:
            return username, password
    return GIT_ASKPASS_USERNAME, credential


async def _resolve_script_env(
    execution_config: dict[str, Any],
    org_id: str,
    requesting_user_id: str | None,
) -> dict[str, str]:
    """Resolve secret:// / credential:// references in the script env.

    A literal token never reaches the executor because authoring rejects a
    sensitive name without a reference; this is the dispatch half that turns
    the reference into a value inside the current user's clearance.
    """
    raw = {str(k): str(v) for k, v in (execution_config.get("env") or {}).items()}
    if not raw:
        return {}

    from lucent.secrets.utils import is_credential_reference, is_secret_reference

    if not any(
        is_secret_reference(value) or is_credential_reference(value)
        for value in raw.values()
    ):
        return raw

    provider = await runtime.get_secret_provider()
    if provider is None:
        raise ScriptResolutionError(
            "execution_config.env uses secret:// or credential:// references but "
            "no secret provider is available; configure the secrets backend"
        )
    runtime.set_current_user({"id": requesting_user_id, "organization_id": org_id})
    try:
        return dict(await runtime.resolve_secret_env_vars(raw, provider))
    except (KeyError, ValueError) as exc:
        raise ScriptResolutionError(
            f"execution_config.env could not be resolved: {exc}"
        ) from exc
    finally:
        runtime.set_current_user(None)


async def _resolve_git_credential(
    execution_config: dict[str, Any],
    org_id: str,
    requesting_user_id: str | None,
) -> str | None:
    """Resolve the configured credential reference to a token, or None.

    The token is returned to the caller for the clone process only. Nothing
    here logs it, and error messages carry the reference/key, never the value.
    """
    reference = execution_config.get("credential")
    if not reference:
        return None

    from lucent.secrets.utils import (
        is_secret_reference,
        resolve_credential_reference,
        resolve_secret_reference,
    )

    provider = await runtime.get_secret_provider()
    if provider is None:
        raise ScriptResolutionError(
            "A git credential is configured but no secret provider is available; "
            "configure the secrets backend or use a public repository"
        )
    runtime.set_current_user({"id": requesting_user_id, "organization_id": org_id})
    try:
        if is_secret_reference(str(reference)):
            return await resolve_secret_reference(
                str(reference), provider, env_key="credential"
            )
        return await resolve_credential_reference(
            str(reference), provider, env_key="credential"
        )
    except (KeyError, ValueError) as exc:
        raise ScriptResolutionError(
            f"Git credential could not be resolved: {exc}. Configure the "
            "referenced secret or use a public repository"
        ) from exc
    finally:
        runtime.set_current_user(None)


async def _prepare_git_source(
    *,
    execution_config: dict[str, Any],
    sandbox_config: dict[str, Any],
    org_id: str,
    requesting_user_id: str | None,
) -> dict[str, Any]:
    """Validate a git source and resolve its credential before provisioning.

    Everything here is checkable without a sandbox, so a host outside the
    template's allowlist or an unresolvable credential fails without paying
    for a container.
    """
    repo_url = validate_repo_url(str(execution_config.get("repo_url") or ""))
    ref = validate_git_ref(str(execution_config.get("ref") or ""))
    repo_path = normalize_repo_path(str(execution_config.get("path") or ""))

    host = repo_host(repo_url)
    if not host:
        raise ScriptResolutionError(f"repo_url {repo_url!r} has no resolvable host")
    if not template_reaches_host(sandbox_config, host):
        raise ScriptResolutionError(
            f"Sandbox template cannot reach git host '{host}' "
            f"(network_mode={sandbox_config.get('network_mode')!r}, "
            f"allowed_hosts={list(sandbox_config.get('allowed_hosts') or [])!r}); "
            "add the host to the template's network allowlist"
        )

    return {
        "repo_url": repo_url,
        "ref": ref,
        "path": repo_path,
        "interpreter": execution_config.get("interpreter"),
        "credential": await _resolve_git_credential(
            execution_config, org_id, requesting_user_id
        ),
    }


async def _materialize_git_source(
    *,
    manager: Any,
    sandbox_id: str,
    spec: dict[str, Any],
    working_dir: str,
    task_id: str,
    timeout: int,
) -> dict[str, Any]:
    """Fetch the ref into a task-scoped checkout and return the run argv."""
    token = spec.get("credential")
    username, password = (
        _split_git_credential(token) if token else (GIT_ASKPASS_USERNAME, "")
    )
    # Redact both the stored form and the password half if they differ.
    secret_values = [value for value in {token, password} if value]
    base_dir = posixpath.join(working_dir, SCRIPT_GIT_DIRNAME, task_id[:8])
    repo_dir = posixpath.join(base_dir, "repo")
    askpass_path = posixpath.join(base_dir, "askpass.sh")

    prepare = await manager.exec(sandbox_id, ["mkdir", "-p", base_dir], timeout=30)
    if prepare.exit_code != 0:
        raise ScriptResolutionError(
            f"Could not create the checkout directory {base_dir!r}: "
            f"{_git_stderr(prepare.stderr, secret_values)}"
        )

    clone_env: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0"}
    if token:
        # The helper reads the token from the exec environment; neither the
        # token nor a credential-bearing URL is ever written to disk or argv.
        await manager.write_file(
            sandbox_id, askpass_path, GIT_ASKPASS_SCRIPT.encode("utf-8")
        )
        chmod = await manager.exec(
            sandbox_id, ["chmod", "700", askpass_path], timeout=30
        )
        if chmod.exit_code != 0:
            raise ScriptResolutionError(
                "Could not install the git credential helper in the sandbox"
            )
        clone_env.update(
            {
                "GIT_ASKPASS": askpass_path,
                "LUCENT_GIT_USERNAME": username,
                "LUCENT_GIT_TOKEN": password,
            }
        )

    step_timeout = min(600, max(30, timeout))
    resolved_sha = ""
    for name, command in git_resolution_steps(
        spec["repo_url"], spec["ref"], repo_dir
    ):
        result = await manager.exec(
            sandbox_id, command, env=clone_env, timeout=step_timeout
        )
        if result.exit_code == 0:
            if name == "rev_parse":
                resolved_sha = result.stdout.strip()
            continue

        stderr = _git_stderr(result.stderr, secret_values)
        if name == "init":
            if "not found" in stderr.lower() or "no such file" in stderr.lower():
                raise ScriptResolutionError(
                    "git is not available in the sandbox image; use an image "
                    "with git installed or add it to the template's "
                    "setup_commands"
                )
            raise ScriptResolutionError(f"Git initialization failed: {stderr}")
        if name == "remote":
            raise ScriptResolutionError(f"Git remote setup failed: {stderr}")
        if name == "fetch":
            if any(hint in stderr.lower() for hint in _GIT_REF_MISSING_HINTS):
                raise ScriptResolutionError(
                    f"Ref '{spec['ref']}' does not exist in {spec['repo_url']} — "
                    f"git said: {stderr}"
                )
            if not token:
                raise ScriptResolutionError(
                    f"Git fetch failed for {spec['repo_url']}: {stderr}. No git "
                    "credential is configured for this task — a private "
                    "repository requires execution_config.credential set to a "
                    "secret:// or credential:// reference"
                )
            raise ScriptResolutionError(
                f"Git fetch failed for {spec['repo_url']} at ref "
                f"'{spec['ref']}': {stderr}"
            )
        if name == "checkout":
            raise ScriptResolutionError(
                f"Git checkout failed at ref '{spec['ref']}': {stderr}"
            )
        raise ScriptResolutionError(
            f"Git resolution failed at ref '{spec['ref']}': {stderr}"
        )

    entrypoint = posixpath.join(repo_dir, spec["path"])
    exists = await manager.exec(sandbox_id, ["test", "-f", entrypoint], timeout=30)
    if exists.exit_code != 0:
        raise ScriptResolutionError(
            f"path '{spec['path']}' was not found in {spec['repo_url']} at ref "
            f"'{spec['ref']}' (resolved {resolved_sha or 'unknown'})"
        )

    explicit = spec.get("interpreter")
    executable = (
        await manager.exec(sandbox_id, ["test", "-x", entrypoint], timeout=30)
    ).exit_code == 0
    if isinstance(explicit, str) and explicit.strip():
        argv = interpreter_argv(explicit.strip()) + [entrypoint]
    elif executable:
        # Run the repository's own file: the kernel honours its shebang and
        # the mode bit comes from the checkout, not from us.
        argv = [entrypoint]
    else:
        head = await manager.exec(
            sandbox_id, ["head", "-n", "1", entrypoint], timeout=30
        )
        shebang_interpreter = (
            interpreter_from_shebang(head.stdout) if head.exit_code == 0 else None
        )
        if not shebang_interpreter:
            raise ScriptResolutionError(
                f"'{spec['path']}' is not executable and has no shebang line; "
                "set execution_config.interpreter to run it"
            )
        argv = interpreter_argv(shebang_interpreter) + [entrypoint]

    pinning = ref_pinning(spec["ref"], resolved_sha)
    return {
        "argv": argv,
        "entrypoint": entrypoint,
        "work_dir": base_dir,
        "repo_url": spec["repo_url"],
        "ref": spec["ref"],
        "resolved_sha": resolved_sha,
        "ref_pinned": pinning["pinned"],
        "warning": pinning["warning"],
        "credential_configured": bool(token),
    }


def _git_source_lines(resolution: dict[str, Any]) -> list[str]:
    lines = [
        "source: git",
        f"repo_url: {resolution['repo_url']}",
        f"ref: {resolution['ref']}",
        f"resolved_sha: {resolution['resolved_sha'] or 'unknown'}",
        f"ref_pinned: {str(resolution['ref_pinned']).lower()}",
    ]
    if resolution["warning"]:
        lines.append(f"warning: {resolution['warning']}")
    return lines


def _retry_budget(output_contract: dict[str, Any] | None) -> int:
    """Extra attempts allowed by an opt-in output contract (0 = run once)."""
    if not output_contract:
        return 0
    if output_contract.get("on_failure") != "retry_then_fallback":
        return 0
    try:
        requested = int(output_contract.get("max_retries", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(requested, SCRIPT_MAX_EXTRA_ATTEMPTS))


async def _destroy_script_sandbox(
    *, daemon: Any, task_id: str, sandbox_id: str | None
) -> None:
    """Destroy a script task's sandbox and record it in the event trail.

    The destroy runs first and independently of the audit event, exactly as the
    agent path does (destroy, then ``sandbox_destroyed``): an event-store or
    logging failure can never leak a container, mask the script outcome, or
    replace an exception already in flight out of the teardown block.

    A provisioning failure leaves no ``sandbox_id``; there is then no container
    to destroy and no destruction to record, so the event is skipped
    explicitly rather than emitted for a sandbox that never existed.
    """
    if not sandbox_id:
        return
    try:
        await daemon._destroy_task_sandbox(sandbox_id)
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        runtime.log(
            f"Script sandbox cleanup failed for {sandbox_id[:12]}: {exc}",
            "WARN",
        )
        return
    try:
        await _event(
            task_id,
            "sandbox_destroyed",
            f"Sandbox {sandbox_id[:12]} destroyed after task completion",
        )
    except Exception as exc:  # noqa: BLE001 — audit must never mask the outcome
        try:
            runtime.log(
                f"Script task {task_id[:8]} sandbox_destroyed event failed: {exc}",
                "WARN",
            )
        except Exception:  # noqa: BLE001 — teardown must never raise
            pass


async def run_script_task(
    *,
    daemon: Any,
    task: dict[str, Any],
    execution_config: dict[str, Any],
    sandbox_config: dict[str, Any],
    org_id: str,
    request_id: str | None,
    requesting_user_id: str | None,
    sequence_order: int = 0,
    on_complete: Callable[..., Any],
    on_fail: Callable[..., Any],
) -> None:
    """Execute one script task end to end; callbacks own the terminal state."""
    from lucent.sandbox.manager import get_sandbox_manager

    task_id = str(task.get("id") or "")
    try:
        validate_script_config(execution_config)
    except ValueError as exc:
        await on_fail(task_id, f"Script task config is invalid: {exc}")
        return

    source = str(execution_config.get("source") or "")
    if source not in ("inline", "git"):
        await on_fail(task_id, f"Script source {source!r} is not supported")
        return

    working_dir = str(sandbox_config.get("working_dir") or "/workspace")
    try:
        timeout = int(sandbox_config.get("timeout_seconds") or 1800)
    except (TypeError, ValueError):
        timeout = 1800
    timeout = max(1, timeout)

    output_contract = _coerce_dict(task.get("output_contract"))
    max_attempts = 1 + _retry_budget(output_contract)

    # Input precedence is strict: an explicitly authored stdin ALWAYS wins.
    # The persisted trigger payload is consulted only when no stdin was
    # authored, and is never allowed to overwrite an author's input.
    authored_stdin = execution_config.get("stdin")
    authored_stdin = authored_stdin if isinstance(authored_stdin, str) else None

    # Everything that does not need a live sandbox resolves first: a host
    # outside the template's allowlist, a literal credential, or an
    # unresolvable reference fails before a container is provisioned. The
    # trigger payload is encoded (and its cap enforced) here too, so an
    # oversized payload fails closed without paying for a sandbox.
    run_env: dict[str, str] = {}
    git_spec: dict[str, Any] | None = None
    payload_stdin_bytes: bytes | None = None
    payload_file_bytes: bytes | None = None
    payload_size = 0
    payload_digest = ""
    try:
        run_env = await _resolve_script_env(
            execution_config, org_id, requesting_user_id
        )
        if source == "git":
            git_spec = await _prepare_git_source(
                execution_config=execution_config,
                sandbox_config=sandbox_config,
                org_id=org_id,
                requesting_user_id=requesting_user_id,
            )
        if authored_stdin is None:
            trigger_payload = await _fetch_trigger_payload(request_id)
            if trigger_payload is not None:
                payload_stdin_bytes, payload_file_bytes = encode_trigger_payload(
                    trigger_payload
                )
                payload_size = len(payload_stdin_bytes)
                payload_digest = hashlib.sha256(
                    payload_stdin_bytes
                ).hexdigest()[:12]
    except TriggerPayloadTooLargeError as exc:
        await _event(
            task_id,
            "script_payload_rejected",
            str(exc),
            {
                "source": "request.trigger_payload",
                "limit_bytes": SCRIPT_PAYLOAD_MAX_BYTES,
            },
        )
        await on_fail(task_id, str(exc))
        return
    except ScriptResolutionError as exc:
        await _event(task_id, "script_source_invalid", str(exc))
        await on_fail(task_id, str(exc))
        return

    sandbox_id, runtime_config, failure_meta, reused = await daemon._create_task_sandbox(
        task_id,
        sandbox_config,
        request_id=request_id,
        requesting_user_id=requesting_user_id,
        org_id=org_id,
        sequence_order=sequence_order,
        sandbox_template_id=(
            str(task.get("sandbox_template_id"))
            if task.get("sandbox_template_id")
            else None
        ),
    )
    if not sandbox_id:
        # Provisioning failed before a sandbox id existed: there is no teardown
        # to run and no ``sandbox_destroyed`` event to emit, which is why this
        # path does not fall through to the finally teardown below.
        detail = (failure_meta or {}).get("detail") or "sandbox manager returned no sandbox_id"
        await _event(
            task_id,
            "sandbox_failed",
            f"Sandbox creation failed: {detail}",
            failure_meta,
        )
        await on_fail(task_id, f"Sandbox creation failed: {detail}")
        return

    await _event(
        task_id,
        "sandbox_reused" if reused else "sandbox_created",
        (
            f"Sandbox {sandbox_id[:12]} reused for script task"
            if reused
            else f"Sandbox {sandbox_id[:12]} created for script task"
        ),
        {"sandbox_id": sandbox_id, "reused": reused},
    )

    keepalive: asyncio.Task | None = None
    idle_timeout = int(getattr(runtime_config, "idle_timeout_seconds", 0) or 0)
    if idle_timeout > 0:
        keepalive = asyncio.create_task(
            daemon._keep_task_sandbox_alive(sandbox_id, idle_timeout),
            name=f"script-sandbox-keepalive-{task_id[:8]}",
        )

    manager = get_sandbox_manager()
    script_path = posixpath.join(working_dir, _script_filename(task_id))
    stdin_path: str | None = None
    source_lines: list[str] | None = None
    git_work_dir: str | None = None

    try:
        if git_spec is not None:
            resolution = await _materialize_git_source(
                manager=manager,
                sandbox_id=sandbox_id,
                spec=git_spec,
                working_dir=working_dir,
                task_id=task_id,
                timeout=timeout,
            )
            git_work_dir = resolution["work_dir"]
            argv = resolution["argv"]
            source_lines = _git_source_lines(resolution)
            await _event(
                task_id,
                "script_source_resolved",
                f"Resolved {git_spec['repo_url']} at ref "
                f"'{git_spec['ref']}' ({resolution['resolved_sha'] or 'unknown'})",
                {
                    "repo_url": git_spec["repo_url"],
                    "ref": git_spec["ref"],
                    "ref_kind": "commit" if resolution["ref_pinned"] else "branch_or_tag",
                    "resolved_sha": resolution["resolved_sha"],
                    "ref_pinned": resolution["ref_pinned"],
                    "credential_configured": resolution["credential_configured"],
                },
            )
            if resolution["warning"]:
                # Reproducibility depends on the SHA, so an unpinned ref is
                # recorded in both the event trail and the task result.
                await _event(
                    task_id,
                    "script_ref_unpinned",
                    resolution["warning"],
                    {
                        "ref": git_spec["ref"],
                        "resolved_sha": resolution["resolved_sha"],
                    },
                )
        else:
            await manager.write_file(
                sandbox_id,
                script_path,
                str(execution_config.get("script") or "").encode("utf-8"),
            )
            argv = interpreter_argv(derive_interpreter(execution_config)) + [
                script_path
            ]

        if authored_stdin is not None:
            stdin_path = posixpath.join(working_dir, _stdin_filename(task_id))
            await manager.write_file(
                sandbox_id, stdin_path, authored_stdin.encode("utf-8")
            )
        elif payload_stdin_bytes is not None:
            assert payload_file_bytes is not None  # encoded together above
            stdin_path = posixpath.join(working_dir, _stdin_filename(task_id))
            await manager.write_file(sandbox_id, stdin_path, payload_stdin_bytes)
            # The same payload as a file, for authors who prefer a path over
            # stdin. It lives in the sandbox working dir and is destroyed with
            # the container — no host-side temp file exists at any point.
            payload_path = posixpath.join(
                working_dir, _payload_filename(task_id)
            )
            await manager.write_file(
                sandbox_id, payload_path, payload_file_bytes
            )
            run_env[SCRIPT_PAYLOAD_FILE_ENV] = payload_path
            # Audit line carries size and a digest only — never payload bytes.
            await _event(
                task_id,
                "script_payload_attached",
                f"Trigger payload delivered to the script "
                f"({payload_size} bytes, sha256 {payload_digest})",
                {
                    "source": "request.trigger_payload",
                    "byte_size": payload_size,
                    "sha256": payload_digest,
                    "file_env": SCRIPT_PAYLOAD_FILE_ENV,
                },
            )

        if stdin_path:
            command: str | list[str] = (
                " ".join(shlex.quote(part) for part in argv)
                + f" < {shlex.quote(stdin_path)}"
            )
        else:
            command = argv

        last_failure: dict[str, Any] | None = None
        for attempt in range(1, max_attempts + 1):
            result = await manager.exec(
                sandbox_id,
                command,
                cwd=working_dir,
                env=run_env or None,
                timeout=timeout,
            )
            stdout, stdout_truncated = truncate(result.stdout, SCRIPT_STDOUT_CAP)
            stderr, stderr_truncated = truncate(result.stderr, SCRIPT_STDERR_CAP)
            metadata = {
                "attempt": attempt,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "timed_out": result.timed_out,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "stdout_preview": preview(stdout),
                "stderr_preview": preview(stderr),
            }

            validation_errors: list[str] | None = None
            if result.timed_out:
                reason = f"Script timed out after {timeout}s"
                await _event(
                    task_id, "script_timed_out", reason, metadata
                )
            elif result.exit_code != 0:
                reason = f"Script exited with code {result.exit_code}"
            else:
                parsed = parse_script_stdout(stdout, output_contract)
                if parsed["validation_status"] == "invalid":
                    validation_errors = list(parsed["validation_errors"] or [])
                    reason = "Script output failed the task's output_contract"
                    # A wrong shape is deterministic — never retry it.
                    last_failure = {
                        "reason": f"{reason}: {'; '.join(validation_errors)}",
                        "result": _render_result(
                            exit_code=result.exit_code,
                            stdout=stdout,
                            stderr=stderr,
                            timed_out=result.timed_out,
                            validation_errors=validation_errors,
                            source_lines=source_lines,
                        ),
                        "metadata": metadata,
                    }
                    await _event(
                        task_id,
                        "script_output_invalid",
                        last_failure["reason"],
                        {**metadata, "validation_errors": validation_errors},
                    )
                    break

                structured = parsed["result_structured"]
                summary = (
                    str(structured.get("summary"))[:2000]
                    if isinstance(structured, dict) and structured.get("summary")
                    else None
                )
                await _event(
                    task_id,
                    "script_completed",
                    f"Script finished with exit code 0 (attempt {attempt})",
                    metadata,
                )
                await on_complete(
                    task_id,
                    _render_result(
                        exit_code=result.exit_code,
                        stdout=stdout,
                        stderr=stderr,
                        timed_out=result.timed_out,
                        source_lines=source_lines,
                    ),
                    result_structured=structured,
                    result_summary=summary,
                    validation_status=(
                        "valid" if output_contract else "not_applicable"
                    ),
                )
                return

            # Execution failure (timeout / nonzero exit).
            last_failure = {
                "reason": reason,
                "result": _render_result(
                    exit_code=result.exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=result.timed_out,
                    source_lines=source_lines,
                ),
                "metadata": metadata,
            }
            await _event(task_id, "script_attempt_failed", reason, metadata)
            if attempt >= max_attempts:
                break
            await _event(
                task_id,
                "script_retry",
                f"Retrying script (attempt {attempt + 1} of {max_attempts})",
                {"previous_exit_code": result.exit_code},
            )

        assert last_failure is not None  # loop either returns or records one
        await _event(
            task_id,
            "script_failed",
            last_failure["reason"],
            {
                **last_failure["metadata"],
                "preview": preview(last_failure["result"], SCRIPT_PREVIEW_CHARS),
            },
        )
        await on_fail(
            task_id, last_failure["reason"], result=last_failure["result"]
        )
    except ScriptResolutionError as exc:
        # A failed resolution never proceeds to execution (including retries).
        await _event(task_id, "script_source_failed", str(exc))
        await on_fail(task_id, str(exc))
    finally:
        if keepalive is not None:
            keepalive.cancel()
            try:
                await keepalive
            except asyncio.CancelledError:
                pass
        if git_work_dir:
            try:
                await manager.exec(
                    sandbox_id, ["rm", "-rf", git_work_dir], timeout=30
                )
            except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                runtime.log(
                    f"Script checkout cleanup failed for {sandbox_id[:12]}: {exc}",
                    "WARN",
                )
        await _destroy_script_sandbox(
            daemon=daemon, task_id=task_id, sandbox_id=sandbox_id
        )
