"""Remote agent client tests (scripts/lucent_remote_agent.py).

These are pure unit tests — no database, no network — covering the client's
security posture (transport validation, key-prefix enforcement) and its
execution/reporting behavior against a fake controller.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

# The agent lives in scripts/ (stdlib-only, not an installable module), so load
# it by path.
_AGENT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "lucent_remote_agent.py"
_spec = importlib.util.spec_from_file_location("lucent_remote_agent", _AGENT_PATH)
agent = importlib.util.module_from_spec(_spec)
sys.modules["lucent_remote_agent"] = agent
_spec.loader.exec_module(agent)


class FakeClient:
    """Records protocol calls instead of making them."""

    def __init__(self, tasks=None):
        self.tasks = list(tasks or [])
        self.events: list[tuple] = []
        self.completions: list[tuple] = []
        self.failures: list[tuple] = []
        self.heartbeats = 0

    def heartbeat(self, metadata=None):
        self.heartbeats += 1
        return {"ok": True}

    def claim(self):
        return self.tasks.pop(0) if self.tasks else None

    def event(self, task_id, event_type, detail, metadata):
        self.events.append((task_id, event_type, detail, metadata))

    def complete(self, task_id, result, summary=None):
        self.completions.append((task_id, result, summary))

    def fail(self, task_id, error, result=None):
        self.failures.append((task_id, error, result))


def _config(**overrides):
    base = dict(
        controller_url="http://localhost:8766",
        token="lr_" + "x" * 40,
        remote_id=str(uuid4()),
        executor="noop",
        command=[],
        poll_seconds=1,
        heartbeat_seconds=1,
        workdir=None,
        task_timeout_seconds=30,
        once=True,
        insecure=False,
        verbose=False,
    )
    base.update(overrides)
    return agent.AgentConfig(**base)


# ── Transport / configuration safety ─────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8766",
        "http://127.0.0.1:8766",
        "http://host.docker.internal:8766",
        "https://lucent.example.com",
    ],
)
def test_accepts_safe_controller_urls(url):
    assert agent.validate_controller_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://lucent.example.com",  # plain http to a public host
        "http://203.0.113.10:8766",  # plain http to a public IP
    ],
)
def test_refuses_plaintext_to_public_hosts(url):
    with pytest.raises(agent.ConfigError) as exc:
        agent.validate_controller_url(url)
    assert "Refusing to send a remote token over plain http" in str(exc.value)


def test_insecure_flag_allows_plaintext_private_network():
    url = "http://lucent.example.com"
    assert agent.validate_controller_url(url, insecure=True) == url


def test_rejects_non_http_schemes():
    for url in ("ftp://host/x", "file:///etc/passwd", "gopher://host"):
        with pytest.raises(agent.ConfigError):
            agent.validate_controller_url(url)


def test_rejects_embedded_credentials():
    with pytest.raises(agent.ConfigError) as exc:
        agent.validate_controller_url("https://user:pass@lucent.example.com")
    assert "must not be embedded" in str(exc.value)


def test_requires_remote_prefixed_token(monkeypatch):
    monkeypatch.setenv("LUCENT_CONTROLLER_URL", "http://localhost:8766")
    monkeypatch.setenv("LUCENT_REMOTE_TOKEN", "hs_notaremotekey")
    monkeypatch.setenv("LUCENT_REMOTE_ID", str(uuid4()))
    args = agent.build_parser().parse_args([])
    with pytest.raises(agent.ConfigError) as exc:
        agent.config_from_env(args)
    assert "prefix 'lr_'" in str(exc.value)


def test_requires_remote_id(monkeypatch):
    monkeypatch.setenv("LUCENT_CONTROLLER_URL", "http://localhost:8766")
    monkeypatch.setenv("LUCENT_REMOTE_TOKEN", "lr_" + "x" * 40)
    monkeypatch.delenv("LUCENT_REMOTE_ID", raising=False)
    args = agent.build_parser().parse_args([])
    with pytest.raises(agent.ConfigError) as exc:
        agent.config_from_env(args)
    assert "LUCENT_REMOTE_ID is required" in str(exc.value)


def test_command_executor_requires_existing_binary(monkeypatch):
    monkeypatch.setenv("LUCENT_CONTROLLER_URL", "http://localhost:8766")
    monkeypatch.setenv("LUCENT_REMOTE_TOKEN", "lr_" + "x" * 40)
    monkeypatch.setenv("LUCENT_REMOTE_ID", str(uuid4()))
    monkeypatch.setenv("LUCENT_REMOTE_EXECUTOR", "command")
    monkeypatch.setenv("LUCENT_REMOTE_COMMAND", "/nonexistent/binary --flag")
    args = agent.build_parser().parse_args([])
    with pytest.raises(agent.ConfigError) as exc:
        agent.config_from_env(args)
    assert "not found on PATH" in str(exc.value)


# ── Payload shaping ──────────────────────────────────────────────────────


def test_task_text_includes_context_but_no_secrets():
    task = {
        "id": str(uuid4()),
        "request_id": str(uuid4()),
        "title": "Build the thing",
        "description": "Detailed instructions here",
        "request_title": "Parent request",
        "agent_type": "code",
        "priority": "high",
        "sandbox_config": {"secret": "should not leak"},
    }
    text = agent._build_task_text(task)
    assert "Build the thing" in text
    assert "Detailed instructions here" in text
    assert "Parent request" in text
    assert "should not leak" not in text


def test_truncate_is_utf8_safe_and_marked():
    marker = agent.TRUNCATION_MARKER
    text = "é" * 5000
    truncated = agent._truncate(text, 200)
    assert len(truncated.encode()) <= 200 + len(marker.encode())
    assert marker.strip() in truncated
    # Small inputs pass through untouched.
    assert agent._truncate("short", 1000) == "short"
    assert agent._truncate(None, 1000) is None


# ── Execution ────────────────────────────────────────────────────────────


def test_noop_executor_succeeds():
    config = _config()
    result = agent.execute_noop(config, {"id": "t1", "title": "x"}, "/tmp")
    assert result.ok
    assert result.exit_code == 0
    assert "acknowledged" in result.output


def test_command_executor_passes_task_on_stdin_not_argv(tmp_path):
    """Task text must never be interpolated into the command line."""
    # `cat` echoes stdin; if the task text leaked into argv, `cat` would fail
    # trying to open a file with that name.
    config = _config(executor="command", command=["cat"])
    task = {"id": str(uuid4()), "title": "echo me", "description": "body text"}
    result = agent.execute_command(config, task, str(tmp_path))
    assert result.ok, result.output
    assert "echo me" in result.output
    assert "body text" in result.output


def test_command_executor_captures_failure_and_stderr(tmp_path):
    config = _config(
        executor="command",
        command=["sh", "-c", "echo oops >&2; exit 3"],
    )
    result = agent.execute_command(config, {"id": "t"}, str(tmp_path))
    assert not result.ok
    assert result.exit_code == 3
    assert "oops" in result.output


def test_command_executor_enforces_timeout(tmp_path):
    config = _config(
        executor="command",
        command=["sh", "-c", "sleep 5"],
        task_timeout_seconds=1,
    )
    result = agent.execute_command(config, {"id": "t"}, str(tmp_path))
    assert not result.ok
    assert result.exit_code == 124
    assert "timeout" in result.output.lower()


def test_command_executor_survives_missing_binary(tmp_path):
    config = _config(executor="command", command=["definitely-not-a-real-binary"])
    result = agent.execute_command(config, {"id": "t"}, str(tmp_path))
    assert not result.ok
    assert result.exit_code == 127


# ── Task processing / reporting ─────────────────────────────────────────


def test_process_task_reports_completion(tmp_path):
    task_id = str(uuid4())
    client = FakeClient()
    config = _config(workdir=str(tmp_path))
    remote = agent.RemoteAgent(config, client=client)

    remote.process_task(
        {"id": task_id, "title": "do work", "request_id": str(uuid4())}
    )

    assert len(client.events) == 2
    assert client.events[0][1] == "remote_started"
    assert client.events[1][1] == "remote_finished"
    assert len(client.completions) == 1
    assert client.completions[0][0] == task_id
    assert not client.failures
    # Work dir is cleaned up after the task.
    assert not (tmp_path / f"task-{task_id}").exists()


def test_process_task_reports_failure(tmp_path):
    task_id = str(uuid4())
    client = FakeClient()
    config = _config(
        workdir=str(tmp_path),
        executor="command",
        command=["sh", "-c", "exit 9"],
    )
    remote = agent.RemoteAgent(config, client=client)

    remote.process_task({"id": task_id, "title": "will fail"})

    assert len(client.failures) == 1
    assert client.failures[0][0] == task_id
    assert "exited with code 9" in client.failures[0][1]
    assert not client.completions


def test_process_task_requires_task_id():
    client = FakeClient()
    remote = agent.RemoteAgent(_config(), client=client)
    with pytest.raises(agent.ProtocolError):
        remote.process_task({"title": "no id"})


def test_once_mode_exits_after_no_task():
    client = FakeClient(tasks=[])
    config = _config(once=True)
    remote = agent.RemoteAgent(config, client=client)
    # Guard the loop: if `once` is honored it returns immediately.
    assert remote.run() == agent.EXIT_OK
    assert client.heartbeats == 1


def test_once_mode_processes_one_task(tmp_path):
    task_id = str(uuid4())
    client = FakeClient(
        tasks=[{"id": task_id, "title": "only task", "request_id": str(uuid4())}]
    )
    config = _config(once=True, workdir=str(tmp_path))
    remote = agent.RemoteAgent(config, client=client)
    assert remote.run() == agent.EXIT_OK
    assert len(client.completions) == 1
    assert client.claim() is None  # queue drained, loop stopped after one


def test_auth_error_exits_with_auth_code():
    class DeniedClient(FakeClient):
        def heartbeat(self, metadata=None):
            raise agent.AuthError("Controller rejected the agent token")

    remote = agent.RemoteAgent(_config(), client=DeniedClient())
    assert remote.run() == agent.EXIT_AUTH


def test_protocol_error_retries_then_recovers(monkeypatch):
    """A transient protocol error must not kill the agent."""

    class FlakyClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def claim(self):
            self.calls += 1
            if self.calls == 1:
                raise agent.ProtocolError("temporary blip")
            return None

    monkeypatch.setattr(agent.time, "sleep", lambda _s: None)
    client = FlakyClient()
    remote = agent.RemoteAgent(_config(once=True), client=client)
    assert remote.run() == agent.EXIT_OK
    assert client.calls == 2


# ── ControllerClient HTTP behavior ──────────────────────────────────────


def test_client_rejects_hs_token_style_requests_are_sent_correctly(monkeypatch):
    """The Authorization header must carry the lr_ token verbatim."""
    captured = {}

    class FakeResponse:
        status = 200

        def read(self):
            return json.dumps({"ok": True}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_open(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["method"] = request.get_method()
        return FakeResponse()

    config = _config()
    client = agent.ControllerClient(config)
    monkeypatch.setattr(client._opener, "open", fake_open)

    client.heartbeat({"x": 1})

    assert captured["url"].endswith(f"/api/remotes/{config.remote_id}/heartbeat")
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == f"Bearer {config.token}"


def test_client_maps_403_to_auth_error(monkeypatch):
    import urllib.error

    def fake_open(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            __import__("io").BytesIO(b'{"detail": "bound to a different remote"}'),
        )

    client = agent.ControllerClient(_config())
    monkeypatch.setattr(client._opener, "open", fake_open)
    with pytest.raises(agent.AuthError) as exc:
        client.claim()
    assert "different remote" in str(exc.value)


# ── Shell executor (opt-in arbitrary work) ──────────────────────────────


def _shell_config(**overrides):
    base = dict(
        executor="shell",
        shell="/bin/sh",
        task_timeout_seconds=30,
        once=True,
    )
    base.update(overrides)
    return _config(**base)


def test_extract_command_script_pulls_fenced_blocks():
    task = {
        "description": (
            "Report the host.\n\n"
            "```sh\nuname -s\n```\n\n"
            "And the architecture:\n\n"
            "```bash\nuname -m\n```\n"
        )
    }
    assert agent.extract_command_script(task) == "uname -s\n\nuname -m"


def test_extract_command_script_ignores_untagged_and_unterminated_fences():
    assert agent.extract_command_script(
        {"description": "```\nrm -rf /\n```"}
    ) is None
    assert agent.extract_command_script(
        {"description": "```sh\nrm -rf /\n"}
    ) is None
    assert agent.extract_command_script({"description": "just prose"}) is None
    assert agent.extract_command_script({}) is None


def test_shell_executor_runs_the_fenced_command(tmp_path):
    result = agent.execute_shell(
        _shell_config(),
        {"id": "t1", "description": "```sh\necho hello-from-remote\n```"},
        str(tmp_path),
    )
    assert result.ok, result.output
    assert "hello-from-remote" in result.output


def test_shell_executor_keeps_the_operator_home(tmp_path):
    """HOME must stay real so git/ssh/npm configs resolve.

    The per-task dir is not containment for an executor that already runs
    task-authored code; pointing HOME at it only made commands report a
    throwaway path and broke tools reading ~/.gitconfig or ~/.ssh.
    """
    result = agent.execute_shell(
        _shell_config(),
        {"id": "t1", "description": "```sh\necho \"$HOME\"\n```"},
        str(tmp_path),
    )
    assert result.ok, result.output
    assert str(tmp_path) not in result.output
    assert os.path.expanduser("~") in result.output


def test_shell_executor_honours_explicit_home(tmp_path):
    result = agent.execute_shell(
        _shell_config(home="/tmp/custom-home"),
        {"id": "t1", "description": "```sh\necho \"$HOME\"\n```"},
        str(tmp_path),
    )
    assert result.ok, result.output
    assert "/tmp/custom-home" in result.output


def test_shell_executor_runs_in_the_per_task_directory(tmp_path):
    result = agent.execute_shell(
        _shell_config(),
        {"id": "t1", "description": "```sh\npwd\n```"},
        str(tmp_path),
    )
    assert result.ok, result.output
    assert str(tmp_path) in result.output


def test_shell_executor_exposes_task_identity_to_the_command(tmp_path):
    result = agent.execute_shell(
        _shell_config(),
        {
            "id": "task-abc",
            "request_id": "req-xyz",
            "description": '```sh\necho "$LUCENT_TASK_ID $LUCENT_REQUEST_ID"\n```',
        },
        str(tmp_path),
    )
    assert result.ok, result.output
    assert "task-abc req-xyz" in result.output


def test_shell_executor_refuses_to_execute_prose(tmp_path):
    """Running a prose description as a script is never the intent."""
    result = agent.execute_shell(
        _shell_config(),
        {"id": "t1", "description": "Please report the hostname and uptime."},
        str(tmp_path),
    )
    assert not result.ok
    assert result.exit_code == 2
    assert "fenced code block" in result.output


def test_shell_executor_reports_failure_exit_code(tmp_path):
    result = agent.execute_shell(
        _shell_config(),
        {"id": "t1", "description": "```sh\necho bad >&2; exit 7\n```"},
        str(tmp_path),
    )
    assert not result.ok
    assert result.exit_code == 7
    assert "bad" in result.output


def test_shell_executor_enforces_timeout(tmp_path):
    result = agent.execute_shell(
        _shell_config(task_timeout_seconds=1),
        {"id": "t1", "description": "```sh\nsleep 5\n```"},
        str(tmp_path),
    )
    assert not result.ok
    assert result.exit_code == 124
    assert "timeout" in result.output.lower()


def test_shell_executor_is_registered_but_not_the_default():
    assert "shell" in agent.EXECUTORS
    # The executor that runs task-authored code must be opt-in.
    assert agent.AgentConfig(
        controller_url="http://localhost:8766",
        token="lr_" + "x" * 40,
        remote_id=str(uuid4()),
    ).executor == "command"


def test_config_rejects_unknown_executor(monkeypatch):
    monkeypatch.setenv("LUCENT_CONTROLLER_URL", "http://localhost:8766")
    monkeypatch.setenv("LUCENT_REMOTE_TOKEN", "lr_" + "x" * 40)
    monkeypatch.setenv("LUCENT_REMOTE_ID", str(uuid4()))
    monkeypatch.setenv("LUCENT_REMOTE_EXECUTOR", "python-eval")
    args = agent.build_parser().parse_args([])
    with pytest.raises(agent.ConfigError) as exc:
        agent.config_from_env(args)
    assert "Available: command, noop, shell" in str(exc.value)


def test_config_accepts_shell_executor(monkeypatch):
    monkeypatch.setenv("LUCENT_CONTROLLER_URL", "http://localhost:8766")
    monkeypatch.setenv("LUCENT_REMOTE_TOKEN", "lr_" + "x" * 40)
    monkeypatch.setenv("LUCENT_REMOTE_ID", str(uuid4()))
    monkeypatch.setenv("LUCENT_REMOTE_EXECUTOR", "shell")
    monkeypatch.setenv("LUCENT_REMOTE_SHELL", "/bin/sh")
    monkeypatch.setenv("SHELL", "/bin/sh")
    args = agent.build_parser().parse_args([])
    config = agent.config_from_env(args)
    assert config.executor == "shell"
    assert config.shell == "/bin/sh"


def test_client_survives_controller_restart_mid_request(monkeypatch):
    """A controller restart must not kill the agent.

    A routine deploy closes the connection mid-request, which surfaces as
    http.client.RemoteDisconnected — NOT urllib.error.URLError. Unhandled, that
    kills the process exactly when it needs to survive and reconnect.
    """
    import http.client

    def fake_open(request, timeout=None):
        raise http.client.RemoteDisconnected(
            "Remote end closed connection without response"
        )

    client = agent.ControllerClient(_config())
    monkeypatch.setattr(client._opener, "open", fake_open)
    with pytest.raises(agent.ProtocolError) as exc:
        client.claim()
    assert "connection dropped" in str(exc.value)


def test_client_survives_connection_reset(monkeypatch):
    def fake_open(request, timeout=None):
        raise ConnectionResetError("reset by peer")

    client = agent.ControllerClient(_config())
    monkeypatch.setattr(client._opener, "open", fake_open)
    with pytest.raises(agent.ProtocolError):
        client.heartbeat({"executor": "shell"})
