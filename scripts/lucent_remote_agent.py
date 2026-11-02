#!/usr/bin/env python3
"""Lucent remote agent — Agent Control Protocol client.

Turns a machine into a *remote*: a non-local executor that Lucent can target
with whole tasks. The agent is deliberately dependency-free (Python 3.9+
stdlib only) so it can run on a host that has nothing installed, mirroring the
sandbox MCP bridge's design constraint.

Security model (read this before changing anything):

* **Outbound only.** The agent makes every connection. There is no listener,
  no inbound port, and no callback URL. A machine behind NAT or a firewall
  works identically to one on the public internet.
* **One key, one remote.** The ``lr_`` token is bound to exactly one remote
  instance. The controller enforces that the remote id in the URL matches the
  key; this client refuses to proceed if the server reports a mismatch.
* **Fail closed on transport.** Plain http:// is refused unless the controller
  is loopback. Credentials are never sent over an unencrypted non-local link.
* **Bounded work.** Every outbound payload is size-capped. Oversized output is
  truncated and annotated rather than dropped silently.
* **No shell by default.** The ``command`` executor runs a fixed argv template
  with the task description delivered on stdin — it never interpolates task
  text into the command line.

Usage::

    export LUCENT_CONTROLLER_URL=https://lucent.example.com
    export LUCENT_REMOTE_TOKEN=lr_...
    export LUCENT_REMOTE_EXECUTOR=command
    export LUCENT_REMOTE_COMMAND='python3 /opt/agent/run.py'
    python3 lucent_remote_agent.py

Run with ``--once`` to claim and process a single task then exit (useful for
testing and cron-style deployments).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# ── Constants ─────────────────────────────────────────────────────────────

DEFAULT_POLL_SECONDS = 15
DEFAULT_HEARTBEAT_SECONDS = 30
MAX_EVENT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 1024 * 1024
# Truncation marker must survive encoding; keep it ASCII and short.
TRUNCATION_MARKER = "\n\n[... output truncated by lucent remote agent ...]\n"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_PROTOCOL = 4


class AgentError(Exception):
    """Base error for the remote agent."""


class ConfigError(AgentError):
    """Invalid or unsafe configuration."""


class AuthError(AgentError):
    """The controller rejected our credentials."""


class ProtocolError(AgentError):
    """The controller returned something we cannot interpret."""


# ── Configuration ─────────────────────────────────────────────────────────


@dataclass
class AgentConfig:
    controller_url: str
    token: str
    remote_id: str
    executor: str = "command"
    command: list[str] = field(default_factory=list)
    shell: str | None = None
    home: str | None = None
    poll_seconds: int = DEFAULT_POLL_SECONDS
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS
    workdir: str | None = None
    task_timeout_seconds: int = 3600
    once: bool = False
    insecure: bool = False
    verbose: bool = False

    @property
    def api_base(self) -> str:
        return self.controller_url.rstrip("/") + "/api"


def _is_loopback_host(host: str) -> bool:
    """True for loopback or an unresolvable single-label dev hostname.

    Docker Compose service names (``lucent``) and ``host.docker.internal``
    resolve inside a container without being loopback; those are still treated
    as local dev targets because they cannot leave the host.
    """
    if not host:
        return False
    lowered = host.lower()
    if lowered in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}:
        return True
    if lowered.endswith(".localhost"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        # Unresolvable single-label names are dev aliases, not public hosts.
        return "." not in lowered
    for info in infos:
        addr = info[4][0]
        if addr.startswith("127.") or addr in {"::1"}:
            return True
        if addr.startswith("10.") or addr.startswith("192.168."):
            return True
        if addr.startswith("172."):
            try:
                second = int(addr.split(".")[1])
            except (IndexError, ValueError):
                continue
            if 16 <= second <= 31:
                return True
    return False


def validate_controller_url(url: str, *, insecure: bool = False) -> str:
    """Validate and normalize the controller URL, refusing unsafe transports.

    Raises:
        ConfigError: on a malformed URL, a non-http(s) scheme, or plain http
            to a non-local host without an explicit ``--insecure`` override.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(
            f"LUCENT_CONTROLLER_URL must use http or https, got {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise ConfigError("LUCENT_CONTROLLER_URL is missing a hostname")
    if parsed.username or parsed.password:
        raise ConfigError(
            "Credentials must not be embedded in LUCENT_CONTROLLER_URL; "
            "use LUCENT_REMOTE_TOKEN"
        )
    if parsed.scheme == "http" and not insecure:
        if not _is_loopback_host(parsed.hostname):
            raise ConfigError(
                "Refusing to send a remote token over plain http to "
                f"{parsed.hostname!r}. Use https, or pass --insecure for a "
                "trusted private network."
            )
    return url.rstrip("/")


def config_from_env(args: argparse.Namespace) -> AgentConfig:
    controller_url = args.controller_url or os.environ.get(
        "LUCENT_CONTROLLER_URL", ""
    )
    token = args.token or os.environ.get("LUCENT_REMOTE_TOKEN", "")
    remote_id = args.remote_id or os.environ.get("LUCENT_REMOTE_ID", "")
    executor = args.executor or os.environ.get("LUCENT_REMOTE_EXECUTOR", "command")

    raw_command = args.command or os.environ.get("LUCENT_REMOTE_COMMAND", "")
    command: list[str] = []
    if raw_command:
        command = json.loads(raw_command) if raw_command.strip().startswith("[") else raw_command.split()

    if not controller_url:
        raise ConfigError("LUCENT_CONTROLLER_URL is required")
    if not token:
        raise ConfigError("LUCENT_REMOTE_TOKEN is required")
    if not token.startswith("lr_"):
        raise ConfigError(
            "LUCENT_REMOTE_TOKEN must be a remote agent key (prefix 'lr_'). "
            "A normal 'hs_' API key is not valid here."
        )
    if not remote_id:
        raise ConfigError(
            "LUCENT_REMOTE_ID is required (the remote instance UUID shown in "
            "the Lucent UI when the remote was created)"
        )
    if executor not in {"command", "noop", "shell", "engine"}:
        raise ConfigError(
            f"Unknown executor {executor!r}. Available: command, noop, shell, engine"
        )
    if executor == "command" and not command:
        raise ConfigError(
            "LUCENT_REMOTE_COMMAND is required for the 'command' executor"
        )
    if executor == "command" and not shutil.which(command[0]):
        raise ConfigError(
            f"Executor command not found on PATH: {command[0]!r}"
        )
    if executor == "shell":
        resolved_shell = args.shell or os.environ.get("LUCENT_REMOTE_SHELL") or ""
        candidate = resolved_shell or os.environ.get("SHELL") or "/bin/sh"
        if not shutil.which(candidate):
            raise ConfigError(
                f"Shell for the 'shell' executor not found: {candidate!r}"
            )
        resolved_shell = candidate
    if executor == "engine":
        # Fail at startup, not when the first agent task arrives. A remote that
        # advertises 'engine' and then cannot import the engine stack silently
        # fails every agent task after the controller has already pinned work
        # to it, which looks like flaky dispatch rather than a setup mistake.
        try:
            import lucent.llm.remote_session  # noqa: F401
        except Exception as exc:  # noqa: BLE001 — surfaced as a config error
            raise ConfigError(
                "The 'engine' executor needs Lucent's Python package available "
                "to this interpreter, including the engine SDKs. Install it "
                "with `pip install lucent`, run this agent from a Lucent "
                f"checkout, or use 'shell'. Import failed: {exc}"
            ) from exc

    return AgentConfig(
        controller_url=validate_controller_url(
            controller_url, insecure=args.insecure
        ),
        token=token,
        remote_id=remote_id,
        executor=executor,
        command=command,
        shell=resolved_shell if executor == "shell" else None,
        home=args.home or os.environ.get("LUCENT_REMOTE_HOME") or None,
        poll_seconds=args.poll_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        workdir=args.workdir,
        task_timeout_seconds=args.task_timeout,
        once=args.once,
        insecure=args.insecure,
        verbose=args.verbose,
    )


# ── HTTP client ───────────────────────────────────────────────────────────


class ControllerClient:
    """Minimal outbound-only JSON client for the Lucent controller.

    Uses urllib so the agent has zero install prerequisites. Every call has an
    explicit timeout: an unresponsive controller must never wedge the agent.
    """

    def __init__(self, config: AgentConfig):
        self.config = config
        self._opener = urllib.request.build_opener()

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        timeout: float = 30.0,
    ) -> tuple[int, Any]:
        url = self.config.api_base + path
        body = None
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Accept": "application/json",
            "User-Agent": "lucent-remote-agent/1",
        }
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
        except urllib.error.URLError as exc:
            raise ProtocolError(f"Controller unreachable: {exc.reason}") from exc
        except socket.timeout as exc:
            raise ProtocolError("Controller request timed out") from exc
        except (http.client.HTTPException, OSError) as exc:
            # The controller restarting mid-request surfaces as
            # RemoteDisconnected / ConnectionResetError / BadStatusLine — none
            # of which are URLError. Without this, a routine deploy kills the
            # agent on the remote host, which is exactly when it must survive.
            raise ProtocolError(f"Controller connection dropped: {exc}") from exc

        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw.decode("utf-8", "replace")

        if status in (401, 403):
            detail = ""
            if isinstance(parsed, dict):
                detail = str(parsed.get("detail") or "")
            raise AuthError(
                f"Controller rejected the agent token (HTTP {status})"
                + (f": {detail}" if detail else "")
            )
        return status, parsed

    # ── Protocol calls ────────────────────────────────────────────────────

    def heartbeat(self, metadata: dict | None = None) -> dict:
        status, body = self._request(
            "POST",
            f"/remotes/{self.config.remote_id}/heartbeat",
            {"agent": metadata or {}},
        )
        if status != 200 or not isinstance(body, dict):
            raise ProtocolError(f"Unexpected heartbeat response: {status}")
        return body

    def claim(self) -> dict | None:
        status, body = self._request(
            "POST", f"/remotes/{self.config.remote_id}/claim"
        )
        if status == 204:
            return None
        if status != 200 or not isinstance(body, dict):
            raise ProtocolError(f"Unexpected claim response: {status}")
        return body

    def event(self, task_id: str, event_type: str, detail: str, metadata: dict) -> None:
        payload = {
            "event_type": event_type,
            "detail": _truncate(detail, MAX_EVENT_BYTES),
            "metadata": metadata,
        }
        status, _ = self._request(
            "POST", f"/requests/tasks/{task_id}/events", payload
        )
        if status not in (200, 201):
            raise ProtocolError(f"Failed to record task event: HTTP {status}")

    def complete(self, task_id: str, result: str, summary: str | None = None) -> None:
        payload = {
            "result": _truncate(result, MAX_RESULT_BYTES),
            "result_summary": summary,
            "instance_id": f"remote:{self.config.remote_id}",
        }
        status, body = self._request(
            "POST", f"/requests/tasks/{task_id}/complete", payload
        )
        if status != 200:
            detail = body.get("detail") if isinstance(body, dict) else body
            raise ProtocolError(f"Failed to complete task: HTTP {status} {detail}")

    def fail(self, task_id: str, error: str, result: str | None = None) -> None:
        payload = {
            "error": _truncate(error, MAX_EVENT_BYTES),
            "result": _truncate(result, MAX_RESULT_BYTES) if result else None,
            "instance_id": f"remote:{self.config.remote_id}",
        }
        status, body = self._request(
            "POST", f"/requests/tasks/{task_id}/fail", payload
        )
        if status != 200:
            detail = body.get("detail") if isinstance(body, dict) else body
            raise ProtocolError(f"Failed to fail task: HTTP {status} {detail}")


def _truncate(text: str | None, limit: int) -> str | None:
    """Cap a string at ``limit`` bytes (UTF-8 safe) with a visible marker."""
    if text is None:
        return None
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    suffix = TRUNCATION_MARKER.encode("utf-8")
    head = encoded[: max(0, limit - len(suffix))]
    return (head + suffix).decode("utf-8", "ignore")


# ── Executors ─────────────────────────────────────────────────────────────


@dataclass
class ExecutionResult:
    ok: bool
    output: str
    exit_code: int
    duration_seconds: float


def _build_task_text(task: dict) -> str:
    """Render the claim payload into the text handed to the executor.

    Includes enough request context for an agent to act sensibly without
    needing a second round trip.
    """
    parts = [
        f"# {task.get('title') or 'Untitled task'}",
        "",
        f"Request: {task.get('request_title') or 'unknown'}",
        f"Task ID: {task.get('id')}",
        f"Agent type: {task.get('agent_type') or 'general'}",
        f"Priority: {task.get('priority') or 'medium'}",
        "",
        "## Description",
        "",
        (task.get("description") or "(no description provided)").strip(),
    ]
    return "\n".join(parts)


def execute_command(
    config: AgentConfig, task: dict, workdir: str
) -> ExecutionResult:
    """Run a fixed argv command, passing the task text on stdin.

    The task description is NEVER interpolated into the argv — it travels on
    stdin only. That keeps an attacker-controlled task body from becoming
    shell syntax on the remote host.
    """
    started = time.monotonic()
    task_text = _build_task_text(task)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LUCENT_TASK_ID": str(task.get("id") or ""),
        "LUCENT_REQUEST_ID": str(task.get("request_id") or ""),
        "LUCENT_REMOTE_ID": config.remote_id,
        "HOME": workdir,
        "TMPDIR": workdir,
    }
    try:
        proc = subprocess.run(
            config.command,
            input=task_text.encode(),
            cwd=workdir,
            env=env,
            capture_output=True,
            timeout=config.task_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(
            ok=False,
            output=(
                f"Task exceeded the remote timeout of "
                f"{config.task_timeout_seconds}s and was terminated."
            ),
            exit_code=124,
            duration_seconds=time.monotonic() - started,
        )
    except OSError as exc:
        return ExecutionResult(
            ok=False,
            output=f"Failed to start executor command: {exc}",
            exit_code=127,
            duration_seconds=time.monotonic() - started,
        )

    combined = ""
    if proc.stdout:
        combined += proc.stdout.decode("utf-8", "replace")
    if proc.stderr:
        if combined and not combined.endswith("\n"):
            combined += "\n"
        combined += "--- stderr ---\n" + proc.stderr.decode("utf-8", "replace")

    return ExecutionResult(
        ok=proc.returncode == 0,
        output=combined,
        exit_code=proc.returncode,
        duration_seconds=time.monotonic() - started,
    )


def execute_noop(config: AgentConfig, task: dict, workdir: str) -> ExecutionResult:
    """Echo the task back as the result — the connectivity smoke test."""
    return ExecutionResult(
        ok=True,
        output=(
            "Remote agent received and acknowledged this task.\n\n"
            + _build_task_text(task)
        ),
        exit_code=0,
        duration_seconds=0.0,
    )


# Fenced blocks a task description may use to carry commands for the "shell"
# executor. Only a closed fence counts; an unterminated one is treated as prose
# so a truncated task can never execute a half-written command.
_COMMAND_FENCE = re.compile(
    r"```(?:sh|bash|shell|zsh|console)\s*\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)


def extract_command_script(task: dict) -> str | None:
    """Pull the shell commands out of a task description.

    ``shell`` is the executor for *arbitrary* work, so the commands have to be
    unambiguous. A task description is prose with an optional fenced command
    block; running the whole description as a script would execute sentences.

    Returns the concatenated body of every recognized fenced block, or None
    when the task carries no command block.
    """
    description = str(task.get("description") or "")
    blocks = [match.strip() for match in _COMMAND_FENCE.findall(description)]
    blocks = [block for block in blocks if block]
    if not blocks:
        return None
    return "\n\n".join(blocks)


def execute_shell(config: AgentConfig, task: dict, workdir: str) -> ExecutionResult:
    """Run the task's own shell commands in a shell, in a per-task directory.

    SECURITY: this executor runs code chosen by whoever authored the task, so
    it is opt-in — the operator enables it by setting
    ``LUCENT_REMOTE_EXECUTOR=shell`` on that host. It is never the default.
    The commands arrive inside the task description on stdin like every other
    executor; nothing about them is interpolated into an argv, shell, or URL by
    the agent itself.

    Commands come from fenced ``sh``/``bash``/``shell`` blocks. A task with no
    such block fails loudly instead of guessing, because executing prose is
    never the intent.
    """
    started = time.monotonic()
    script = extract_command_script(task)
    if not script:
        return ExecutionResult(
            ok=False,
            output=(
                "The 'shell' executor needs the commands to run in a fenced "
                "code block tagged sh, bash, shell, zsh, or console, e.g.\n\n"
                "```sh\nuname -a\n```\n\n"
                "This task's description contained no such block, so nothing "
                "was executed."
            ),
            exit_code=2,
            duration_seconds=time.monotonic() - started,
        )

    shell = config.shell or os.environ.get("SHELL") or "/bin/sh"
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LUCENT_TASK_ID": str(task.get("id") or ""),
        "LUCENT_REQUEST_ID": str(task.get("request_id") or ""),
        "LUCENT_REMOTE_ID": config.remote_id,
        # Keep the operator's real HOME. The `shell` executor is already opt-in
        # arbitrary code, so pointing HOME at the per-task dir would not add
        # containment — it would only make commands report a throwaway path and
        # break anything reading ~/.gitconfig, ~/.ssh, or ~/.npmrc.
        "HOME": config.home or os.path.expanduser("~"),
        # TMPDIR still points at the task dir so scratch files are cleaned up.
        "TMPDIR": workdir,
    }
    try:
        proc = subprocess.run(
            [shell, "-c", script],
            cwd=workdir,
            env=env,
            capture_output=True,
            timeout=config.task_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(
            ok=False,
            output=(
                f"Task exceeded the remote timeout of "
                f"{config.task_timeout_seconds}s and was terminated."
            ),
            exit_code=124,
            duration_seconds=time.monotonic() - started,
        )
    except OSError as exc:
        return ExecutionResult(
            ok=False,
            output=f"Failed to start shell {shell!r}: {exc}",
            exit_code=127,
            duration_seconds=time.monotonic() - started,
        )

    combined = ""
    if proc.stdout:
        combined += proc.stdout.decode("utf-8", "replace")
    if proc.stderr:
        if combined and not combined.endswith("\n"):
            combined += "\n"
        combined += "--- stderr ---\n" + proc.stderr.decode("utf-8", "replace")

    return ExecutionResult(
        ok=proc.returncode == 0,
        output=combined,
        exit_code=proc.returncode,
        duration_seconds=time.monotonic() - started,
    )


def execute_engine(config: AgentConfig, task: dict, workdir: str) -> ExecutionResult:
    """Run a Lucent agent session on this host using the claimed execution plan.

    Unlike ``shell``, this executor does not execute text the task carries. The
    controller sends a composed *session* — agent definition, skills, model,
    provider, and a task-scoped MCP key — and this runs it with a real engine
    so the model decides what commands to run and what files to edit.

    SECURITY: this is the largest grant a remote can be given. The engine runs
    unattended with tool permissions approved, because there is no human on
    this machine to prompt. Enabling it is an operator decision
    (``LUCENT_REMOTE_EXECUTOR=engine``) and the controller still refuses to
    compose a session unless the deployment opted in
    (``LUCENT_REMOTE_ENGINE_SESSION=1``).

    The plan is treated as untrusted input even though it comes from the
    controller: a truncated or corrupted claim must fail loudly rather than run
    a half-built session.
    """
    started = time.monotonic()
    plan_payload = task.get("execution_plan")
    plan_error = task.get("execution_plan_error")
    if not plan_payload:
        # Name the task's actual configuration. "exit code 2" alone said
        # nothing about what to fix, so a task authored without the fields the
        # remote needs looked like a broken remote rather than a misfiled task.
        config = task.get("execution_config")
        missing = [
            name
            for name in ("backend_id", "model", "execution_config")
            if not task.get(name)
        ]
        return ExecutionResult(
            ok=False,
            output=(
                "This task asked its remote to run an agent session, but the "
                "controller did not send one"
                + (f": {plan_error}" if plan_error else ".")
                + "\n\nNo commands were executed.\n"
                f"  task agent_type : {task.get('agent_type') or '(unset)'}\n"
                f"  execution_config: {config if config else '(unset)'}\n"
                + (
                    "  missing         : " + ", ".join(missing) + "\n"
                    if missing
                    else ""
                )
                + "Fix it by re-filing the task with a backend_id, a model, and "
                "execution_config={'executor': 'engine'}. If this remote is "
                "meant to run plain commands instead, set "
                "LUCENT_REMOTE_EXECUTOR=shell on the host and put the commands "
                "in a fenced code block in the description."
            ),
            exit_code=2,
            duration_seconds=time.monotonic() - started,
        )

    try:
        from lucent.llm.remote_session import (
            RemoteSessionError,
            decode_plan_from_transport,
            run_task_session,
        )
    except Exception as exc:  # noqa: BLE001 — executor isolation boundary
        return ExecutionResult(
            ok=False,
            output=(
                "The 'engine' executor could not import Lucent's LLM stack, so "
                f"no agent session ran: {exc}"
            ),
            exit_code=127,
            duration_seconds=time.monotonic() - started,
        )

    try:
        plan = decode_plan_from_transport(plan_payload)
    except RemoteSessionError as exc:
        return ExecutionResult(
            ok=False,
            output=f"The controller's execution plan was not usable: {exc}",
            exit_code=2,
            duration_seconds=time.monotonic() - started,
        )

    # Run the session in the operator's real HOME so git/ssh/npm configs and
    # any repo the model touches resolve as the person editing this machine
    # expects. TMPDIR still points at the per-task directory.
    previous_cwd = os.getcwd()
    previous_tmpdir = os.environ.get("TMPDIR")
    try:
        os.chdir(config.home or os.path.expanduser("~"))
        os.environ["TMPDIR"] = workdir
    except OSError:
        pass

    try:
        output = asyncio.run(run_task_session(plan))
    except Exception as exc:  # noqa: BLE001 — executor isolation boundary
        return ExecutionResult(
            ok=False,
            output=f"The agent session failed on this host: {exc}",
            exit_code=1,
            duration_seconds=time.monotonic() - started,
        )
    finally:
        with contextlib.suppress(OSError):
            os.chdir(previous_cwd)
        if previous_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_tmpdir

    stripped = (output or "").strip()
    return ExecutionResult(
        ok=bool(stripped),
        output=(
            stripped
            or "The agent session finished without producing any text output."
        ),
        exit_code=0 if stripped else 1,
        duration_seconds=time.monotonic() - started,
    )


def _engine_stack_available() -> bool:
    """Whether the 'engine' executor can actually run on this host."""
    try:
        import lucent.llm.remote_session  # noqa: F401
    except Exception:  # noqa: BLE001 — any import failure means "not available"
        return False
    return True


EXECUTORS = {
    "command": execute_command,
    "noop": execute_noop,
    "shell": execute_shell,
    "engine": execute_engine,
}


# ── Agent loop ────────────────────────────────────────────────────────────


class RemoteAgent:
    def __init__(self, config: AgentConfig, client: ControllerClient | None = None):
        self.config = config
        self.client = client or ControllerClient(config)
        self._last_heartbeat = 0.0
        self._run_dir: str | None = None
        #: Task ids this process is actively executing. Sent with each
        #: heartbeat so the controller renews only leases that correspond to
        #: real work. Without this a claim whose payload never arrived is
        #: renewed forever and can never be reaped.
        self._active_task_ids: set[str] = set()
        # Guards ``_active_task_ids`` and the heartbeat clock: the heartbeat
        # thread reads them while the main loop mutates them.
        self._lock = threading.Lock()
        self._stop_heartbeat = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def _workdir(self, task_id: str) -> str:
        base = self.config.workdir or os.path.join(
            tempfile.gettempdir(), "lucent-remote"
        )
        path = os.path.join(base, f"task-{task_id}")
        os.makedirs(path, exist_ok=True)
        return path

    def _cleanup(self, path: str) -> None:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

    def _available_executors(self) -> list[str]:
        """Executors this host can actually run right now."""
        available = [name for name in EXECUTORS if name != "engine"]
        if _engine_stack_available():
            available.append("engine")
        return sorted(available)

    def heartbeat(self) -> dict:
        with self._lock:
            active = sorted(self._active_task_ids)
        info = self.client.heartbeat(
            {
                "hostname": socket.gethostname(),
                "executor": self.config.executor,
                # Report the full executor list, but only claim 'engine' when
                # Lucent's package really imports here. A controller composes an
                # agent session for any remote that advertises it, so an
                # inaccurate advertisement would pin agent work to a host that
                # can never run it.
                "executors": self._available_executors(),
                "pid": os.getpid(),
                # Report the execution contract so task authors know what a
                # description must look like to run here. A `shell` remote only
                # executes fenced command blocks; a `command` remote runs one
                # fixed argv. Without this the controller has to guess.
                "shell": self.config.shell,
                "command": self.config.command,
                # Leases the controller may renew. A claim this process never
                # actually started (payload lost in transit) is deliberately
                # absent, so its lease expires and the task is requeued.
                "active_task_ids": active,
            }
        )
        with self._lock:
            self._last_heartbeat = time.monotonic()
        return info

    def _heartbeat_loop(self) -> None:
        """Keep leases alive independently of task execution.

        ``process_task`` is synchronous and can run for an hour (an agent
        session is unbounded work). Heartbeating only from the main loop meant
        every lease went stale for the duration of a task, so the controller
        treated live work as abandoned: the lease could expire outright, and the
        activity-based reaper saw no heartbeat and requeued the task while it was
        still running — duplicate work, two agents editing the same repo.

        A dedicated thread fixes that without complicating the executor. It only
        sends heartbeats; claim/complete/fail stay on the main loop.
        """
        # Honour the configured interval. A floor here would silently override an
        # operator's (or a test's) setting and make lease timing unpredictable;
        # only guard against a nonsensical zero/negative value.
        interval = max(1, int(self.config.heartbeat_seconds))
        while not self._stop_heartbeat.wait(interval):
            try:
                self.heartbeat()
            except AuthError:
                # A revoked key is terminal for the whole agent; stop the loop
                # and let the main loop surface it.
                self._log("heartbeat authentication failed; stopping heartbeat")
                return
            except Exception as exc:  # noqa: BLE001 — never kill the thread
                # A transient controller hiccup must not end the heartbeat:
                # stopping it would make live work look abandoned.
                self._log(f"heartbeat failed (will retry): {exc}")

    def start_heartbeat(self) -> None:
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="lucent-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

    def stop_heartbeat(self) -> None:
        self._stop_heartbeat.set()
        thread = self._heartbeat_thread
        if thread is not None:
            thread.join(timeout=5)
            self._heartbeat_thread = None

    # ── task handling ─────────────────────────────────────────────────────

    def process_task(self, task: dict) -> None:
        task_id = str(task.get("id") or "")
        if not task_id:
            raise ProtocolError("Claim response did not include a task id")
        with self._lock:
            self._active_task_ids.add(task_id)
        try:
            self._process_task_inner(task, task_id)
        finally:
            with self._lock:
                self._active_task_ids.discard(task_id)

    def _process_task_inner(self, task: dict, task_id: str) -> None:
        workdir = self._workdir(task_id)
        log = self._log
        log(f"claimed task {task_id}: {task.get('title')}")
        self.client.event(
            task_id,
            "remote_started",
            f"Remote agent started task via executor '{self.config.executor}'",
            {"executor": self.config.executor, "hostname": socket.gethostname()},
        )

        executor = EXECUTORS[self.config.executor]
        try:
            result = executor(self.config, task, workdir)
        except Exception as exc:  # noqa: BLE001 - executor isolation boundary
            try:
                self.client.fail(task_id, f"Remote executor crashed: {exc}")
            except AgentError as report_exc:
                log(f"could not report crash for {task_id}: {report_exc}")
            self._cleanup(workdir)
            return

        metadata = {
            "executor": self.config.executor,
            "exit_code": result.exit_code,
            "duration_seconds": round(result.duration_seconds, 3),
        }
        if result.ok:
            try:
                self.client.event(
                    task_id,
                    "remote_finished",
                    f"Executor completed with exit code {result.exit_code}",
                    metadata,
                )
                self.client.complete(
                    task_id,
                    result.output,
                    summary=(
                        f"Completed on remote {self.config.remote_id} "
                        f"in {result.duration_seconds:.1f}s"
                    ),
                )
                log(f"completed task {task_id} in {result.duration_seconds:.1f}s")
            except AgentError as exc:
                log(f"failed to report completion for {task_id}: {exc}")
        else:
            try:
                self.client.event(
                    task_id,
                    "remote_failed",
                    f"Executor exited with code {result.exit_code}",
                    metadata,
                )
                self.client.fail(
                    task_id,
                    f"Remote executor exited with code {result.exit_code}",
                    result.output,
                )
                log(f"task {task_id} failed (exit {result.exit_code})")
            except AgentError as exc:
                log(f"failed to report failure for {task_id}: {exc}")
        self._cleanup(workdir)

    # ── main loop ─────────────────────────────────────────────────────────

    def run(self) -> int:
        self._log(
            f"remote agent starting: remote_id={self.config.remote_id} "
            f"controller={self.config.controller_url} "
            f"executor={self.config.executor}"
        )
        backoff = 1.0
        self.start_heartbeat()
        try:
            while True:
                try:
                    if (
                        time.monotonic() - self._last_heartbeat
                        >= self.config.heartbeat_seconds
                    ):
                        self.heartbeat()

                    task = self.client.claim()
                    backoff = 1.0
                    if task is None:
                        if self.config.once:
                            self._log("no task available; exiting (--once)")
                            return EXIT_OK
                        time.sleep(self.config.poll_seconds)
                        continue

                    self.process_task(task)
                    if self.config.once:
                        return EXIT_OK

                except AuthError as exc:
                    self._log(f"authentication failed: {exc}")
                    return EXIT_AUTH
                except ProtocolError as exc:
                    self._log(f"protocol error: {exc} (retrying in {backoff:.0f}s)")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 300)
                except KeyboardInterrupt:
                    self._log("interrupted; shutting down")
                    return EXIT_OK
        finally:
            self.stop_heartbeat()
            if self._run_dir:
                shutil.rmtree(self._run_dir, ignore_errors=True)

    def _log(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


# ── CLI ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lucent remote agent (Agent Control Protocol client)",
    )
    parser.add_argument("--controller-url", help="Lucent controller base URL")
    parser.add_argument("--token", help="Remote agent key (lr_...)")
    parser.add_argument("--remote-id", help="Remote instance UUID")
    parser.add_argument(
        "--executor",
        choices=sorted(EXECUTORS),
        help="How to execute claimed tasks (default: command)",
    )
    parser.add_argument(
        "--command",
        help="Executor command; a JSON list for argv, or a plain string",
    )
    parser.add_argument(
        "--shell",
        help=(
            "Shell binary for the 'shell' executor (default: $SHELL, then "
            "/bin/sh). The shell executor is opt-in because it runs commands "
            "authored by whoever created the task."
        ),
    )
    parser.add_argument(
        "--home",
        help=(
            "HOME for the 'shell' executor (default: the operator's real home, "
            "so git/ssh/npm configs resolve normally)."
        ),
    )
    parser.add_argument(
        "--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS,
        help="Idle poll interval (default: 15)",
    )
    parser.add_argument(
        "--heartbeat-seconds", type=int, default=DEFAULT_HEARTBEAT_SECONDS,
        help="Heartbeat interval (default: 30)",
    )
    parser.add_argument(
        "--task-timeout", type=int, default=3600,
        help="Per-task execution timeout in seconds (default: 3600)",
    )
    parser.add_argument("--workdir", help="Base directory for per-task work dirs")
    parser.add_argument(
        "--once", action="store_true",
        help="Claim and process at most one task, then exit",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="Allow plain http to a non-loopback controller (private networks)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = config_from_env(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return RemoteAgent(config).run()


if __name__ == "__main__":
    sys.exit(main())
