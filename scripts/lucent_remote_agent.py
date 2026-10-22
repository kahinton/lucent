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
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
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
    if executor not in {"command", "noop"}:
        raise ConfigError(
            f"Unknown executor {executor!r}. Available: command, noop"
        )
    if executor == "command" and not command:
        raise ConfigError(
            "LUCENT_REMOTE_COMMAND is required for the 'command' executor"
        )
    if executor == "command" and not shutil.which(command[0]):
        raise ConfigError(
            f"Executor command not found on PATH: {command[0]!r}"
        )

    return AgentConfig(
        controller_url=validate_controller_url(
            controller_url, insecure=args.insecure
        ),
        token=token,
        remote_id=remote_id,
        executor=executor,
        command=command,
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


EXECUTORS = {"command": execute_command, "noop": execute_noop}


# ── Agent loop ────────────────────────────────────────────────────────────


class RemoteAgent:
    def __init__(self, config: AgentConfig, client: ControllerClient | None = None):
        self.config = config
        self.client = client or ControllerClient(config)
        self._last_heartbeat = 0.0
        self._run_dir: str | None = None

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

    def heartbeat(self) -> dict:
        info = self.client.heartbeat(
            {
                "hostname": socket.gethostname(),
                "executor": self.config.executor,
                "pid": os.getpid(),
            }
        )
        self._last_heartbeat = time.monotonic()
        return info

    # ── task handling ─────────────────────────────────────────────────────

    def process_task(self, task: dict) -> None:
        task_id = str(task.get("id") or "")
        if not task_id:
            raise ProtocolError("Claim response did not include a task id")

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
