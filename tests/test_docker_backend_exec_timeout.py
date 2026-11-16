"""Regression tests for DockerBackend.exec() timeout enforcement.

Docker exec runs in a worker thread, so awaiting it with a timeout used to
return control to the caller while the process kept running inside the
container — and because the blocking call never raised ``TimeoutError``, the
``timed_out`` branch was unreachable. These tests pin the real contract:

1. A command that exceeds its timeout returns ``timed_out=True`` close to the
   configured timeout, carrying the output captured before expiry.
2. The process is actually stopped inside the container afterwards.
3. A command that finishes in time is unaffected.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

import docker
from lucent.sandbox.docker_backend import DockerBackend, _wrap_exec_command

_IMAGE = "alpine:latest"


# ---------------------------------------------------------------------------
# Fakes for the Docker exec API
# ---------------------------------------------------------------------------


class _FakeExecApi:
    """Minimal stand-in for docker-py's exec API.

    The main exec streams ``chunks`` and then either returns immediately or
    blocks until the kill exec runs, mirroring how a real container behaves.
    """

    def __init__(self, *, chunks=(), exit_code=0, blocks_until_killed=False):
        self.created: list[tuple[str, list[str], dict]] = []
        self.kills: list[str] = []
        self._chunks = chunks
        self._exit_code = exit_code
        self._blocks = blocks_until_killed
        self._stop = threading.Event()
        self._counter = 0

    def _command_for(self, exec_id: str) -> str:
        index = int(exec_id.split("-")[-1]) - 1
        return " ".join(self.created[index][1])

    def exec_create(self, container_id, cmd, **kwargs):  # noqa: ARG002
        self._counter += 1
        exec_id = f"exec-{self._counter}"
        self.created.append((exec_id, list(cmd), kwargs))
        return {"Id": exec_id}

    def exec_start(self, exec_id, **kwargs):
        script = self._command_for(exec_id)
        if "kill -KILL" in script:
            self.kills.append(script)
            self._stop.set()
            return (b"", b"")
        if script.startswith("sh -c cat "):
            # pid of the running command in the container's namespace
            return b"42\n"
        if kwargs.get("stream"):
            def generator():
                for chunk in self._chunks:
                    yield chunk
                if self._blocks:
                    self._stop.wait(10)
            return generator()
        return (b"", b"")

    def exec_inspect(self, exec_id):
        if not self._blocks:
            return {"Running": False, "ExitCode": self._exit_code}
        if self._stop.is_set():
            return {"Running": False, "ExitCode": 137}
        return {"Running": True, "ExitCode": None}


def _backend_with(api: _FakeExecApi) -> DockerBackend:
    container = MagicMock()
    container.id = "container-exec-timeout"
    container.client.api = api
    backend = DockerBackend()
    backend._find_container = MagicMock(return_value=container)
    return backend


# ---------------------------------------------------------------------------
# Unit-level contract
# ---------------------------------------------------------------------------


class TestExecTimeoutContract:
    @pytest.mark.asyncio
    async def test_timeout_returns_partial_output_and_kills_process_group(self):
        api = _FakeExecApi(
            chunks=[(b"starting work\n", None), (None, b"warning\n")],
            blocks_until_killed=True,
        )
        backend = _backend_with(api)

        started = time.monotonic()
        result = await backend.exec("sb-timeout", "long-running", timeout=1)
        elapsed = time.monotonic() - started

        assert result.timed_out is True
        assert result.exit_code == -1
        # Output produced before expiry is preserved, not discarded.
        assert "starting work" in result.stdout
        assert "warning" in result.stderr
        assert "Command timed out after 1s" in result.stderr
        # The caller regains control near the deadline, not the command's runtime.
        assert elapsed < 3
        assert 900 <= result.duration_ms < 3000
        # The kill targeted the wrapper's process group, not a host pid.
        assert api.kills, "timeout must issue a kill exec"
        assert "kill -KILL -42" in api.kills[0]

    @pytest.mark.asyncio
    async def test_command_under_timeout_is_unaffected(self):
        api = _FakeExecApi(
            chunks=[(b"hello\n", None), (None, b"note\n")],
            exit_code=0,
        )
        backend = _backend_with(api)

        result = await backend.exec("sb-fast", "echo hello", timeout=30)

        assert result.timed_out is False
        assert result.exit_code == 0
        assert result.stdout == "hello\n"
        assert result.stderr == "note\n"
        assert result.duration_ms < 1000
        assert api.kills == [], "a command that finishes must never be killed"

    @pytest.mark.asyncio
    async def test_nonzero_exit_code_propagates(self):
        api = _FakeExecApi(chunks=[(None, b"boom\n")], exit_code=7)
        backend = _backend_with(api)

        result = await backend.exec("sb-fail", "false", timeout=30)

        assert result.timed_out is False
        assert result.exit_code == 7
        assert result.stderr == "boom\n"


class TestExecWrapper:
    def test_string_command_records_namespace_pid(self):
        wrapped = _wrap_exec_command("echo hi", "/tmp/.lucent-exec-abc.pid")

        assert wrapped[0] == "sh" and wrapped[1] == "-c"
        script = wrapped[2]
        assert "echo $$ > /tmp/.lucent-exec-abc.pid" in script
        assert script.endswith("echo hi")
        # The pidfile is removed on the normal-exit path.
        assert "trap 'rm -f /tmp/.lucent-exec-abc.pid' EXIT" in script

    def test_list_command_preserves_argv(self):
        argv = ["mkdir", "-p", "/workspace/a b"]
        wrapped = _wrap_exec_command(argv, "/tmp/.lucent-exec-abc.pid")

        # ``sh -c SCRIPT sh argv...`` sets $0="sh" and passes argv through
        # "$@" without shell splitting.
        assert wrapped[:2] == ["sh", "-c"]
        assert wrapped[3] == "sh"
        assert wrapped[4:] == argv
        assert '"$@"' in wrapped[2]


# ---------------------------------------------------------------------------
# Real Docker: proves the process is actually gone
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def docker_client():
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(_IMAGE)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Docker not available for exec timeout integration test: {exc}")
    return client


@pytest.mark.asyncio
async def test_real_docker_timeout_stops_the_process(docker_client):
    sandbox_id = "sb-exec-timeout-real"
    name = "lucent-exec-timeout-test"
    try:
        docker_client.containers.get(name).remove(force=True)
    except docker.errors.NotFound:
        pass

    container = docker_client.containers.run(
        _IMAGE,
        command=["sleep", "300"],
        detach=True,
        name=name,
        labels={"io.lucent.sandbox.id": sandbox_id},
        auto_remove=False,
    )
    backend = DockerBackend()
    backend._client = docker_client
    try:
        started = time.monotonic()
        result = await backend.exec(
            sandbox_id, "echo started; exec sleep 617", timeout=2,
        )
        elapsed = time.monotonic() - started

        assert result.timed_out is True
        assert "started" in result.stdout, "partial output must survive the kill"
        assert elapsed < 7, f"exec returned after {elapsed:.1f}s, timeout was 2s"
        assert result.duration_ms >= 1500

        # The process must no longer exist inside the container.
        probe = await backend.exec(sandbox_id, "ps -o pid=,args=", timeout=10)
        survivors = [
            line for line in probe.stdout.splitlines() if "sleep 617" in line
        ]
        assert survivors == [], f"timed-out process still running: {survivors}"
    finally:
        container.remove(force=True)
