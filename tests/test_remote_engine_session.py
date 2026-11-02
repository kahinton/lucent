"""Remote engine sessions: plan transport, validation, and executor behavior.

The interesting surface here is authorization and failure mode, not the model
call: a remote must never run a session the controller did not compose, and a
task must fail with a reason instead of silently falling back to shell.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from lucent.llm.remote_session import (
    REMOTE_ENGINES,
    RemoteSessionError,
    SessionPlan,
    decode_plan_from_transport,
    encode_plan_for_transport,
    plan_from_payload,
)

_AGENT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "lucent_remote_agent.py"
_spec = importlib.util.spec_from_file_location(
    "lucent_remote_agent_engine", _AGENT_PATH
)
agent = importlib.util.module_from_spec(_spec)
sys.modules["lucent_remote_agent_engine"] = agent
_spec.loader.exec_module(agent)


def _plan(**overrides) -> SessionPlan:
    base = dict(
        engine="claude_code",
        model="deepseek-v4.1-flash:cloud",
        system_message="You are a sub-agent.",
        prompt="Do the work.",
        backend={"id": str(uuid4()), "engine": "claude_code", "provider": "ollama"},
        mcp_config={"memory-server": {"type": "http", "url": "http://localhost:8766/mcp"}},
        timeout=600,
        task_id=str(uuid4()),
        request_id=str(uuid4()),
    )
    base.update(overrides)
    return SessionPlan(**base)


# ── Plan transport ─────────────────────────────────────────────────────────


def test_plan_round_trips_through_transport():
    plan = _plan(system_message="line one\nline two — unicode ✓", reasoning_effort="low")
    payload = encode_plan_for_transport(plan)
    restored = decode_plan_from_transport(payload)

    assert restored.engine == plan.engine
    assert restored.model == plan.model
    assert restored.system_message == plan.system_message
    assert restored.reasoning_effort == "low"
    assert restored.backend == plan.backend
    assert restored.task_id == plan.task_id


def test_transport_payload_keeps_system_message_readable_in_b64():
    """The prompt may contain newlines; the encoded form must survive JSON."""
    plan = _plan(system_message="a\nb\nc")
    payload = encode_plan_for_transport(plan)
    assert payload["system_message_b64"]
    # The raw field is still present (empty prompt) so the shape stays uniform.
    assert "system_message" in payload
    json.dumps(payload)  # must be JSON-serializable as-is


def test_plan_rejects_unknown_kind():
    with pytest.raises(RemoteSessionError):
        plan_from_payload({"kind": "something-else"})


def test_plan_rejects_engine_that_cannot_run_remotely():
    assert "langchain" not in REMOTE_ENGINES
    with pytest.raises(RemoteSessionError) as exc:
        plan_from_payload({"kind": "task-session", "engine": "langchain", "model": "x"})
    assert "langchain" in str(exc.value)


def test_plan_requires_a_model():
    with pytest.raises(RemoteSessionError):
        plan_from_payload({"kind": "task-session", "engine": "claude_code", "model": "  "})


def test_plan_clamps_absurd_timeouts():
    plan = plan_from_payload(
        {"kind": "task-session", "engine": "claude_code", "model": "m", "timeout": 10**9}
    )
    assert plan.timeout == 24 * 3600
    short = plan_from_payload(
        {"kind": "task-session", "engine": "claude_code", "model": "m", "timeout": 1}
    )
    assert short.timeout == 60


def test_plan_rejects_malformed_mcp_config():
    with pytest.raises(RemoteSessionError):
        plan_from_payload(
            {
                "kind": "task-session",
                "engine": "claude_code",
                "model": "m",
                "mcp_config": ["not", "an", "object"],
            }
        )


# ── Remote client: engine executor ─────────────────────────────────────────


def _engine_config(**overrides):
    base = dict(
        controller_url="http://localhost:8766",
        token="lr_" + "x" * 40,
        remote_id=str(uuid4()),
        executor="engine",
        command=[],
        poll_seconds=1,
        heartbeat_seconds=30,
        home="/tmp",
    )
    base.update(overrides)
    return agent.AgentConfig(**base)


def test_engine_executor_is_registered_but_not_the_default():
    assert "engine" in agent.EXECUTORS
    # The CLI leaves the executor unset so config_from_env keeps 'command' as
    # the default: a remote must never run model-driven work by accident.
    parser = agent.build_parser()
    assert parser.parse_args([]).executor is None


def test_engine_executor_fails_loudly_without_a_plan(tmp_path):
    result = agent.execute_engine(_engine_config(), {"id": "t1"}, str(tmp_path))
    assert result.ok is False
    assert result.exit_code == 2
    assert "did not send one" in result.output


def test_engine_executor_surfaces_the_controller_reason(tmp_path):
    task = {
        "id": "t1",
        "execution_plan_error": "Remote engine sessions are disabled on this deployment.",
    }
    result = agent.execute_engine(_engine_config(), task, str(tmp_path))
    assert result.ok is False
    assert "Remote engine sessions are disabled" in result.output


def test_engine_executor_rejects_a_corrupt_plan(tmp_path):
    task = {"id": "t1", "execution_plan": {"kind": "bogus"}}
    result = agent.execute_engine(_engine_config(), task, str(tmp_path))
    assert result.ok is False
    assert "not usable" in result.output


def test_heartbeat_advertises_the_executor_list(monkeypatch):
    """A controller only composes an engine session for a remote that says so."""
    calls: list[dict] = []

    class Client:
        def heartbeat(self, metadata=None):
            calls.append(metadata or {})
            return {"ok": True}

    remote = agent.RemoteAgent(_engine_config(), client=Client())
    remote.heartbeat()
    assert calls, "heartbeat was not sent"
    assert "engine" in calls[0]["executors"]
    assert calls[0]["executor"] == "engine"


def test_heartbeat_withholds_engine_when_the_stack_is_missing(monkeypatch):
    """Advertising an executor this host cannot run pins work that then fails."""
    monkeypatch.setattr(agent, "_engine_stack_available", lambda: False)
    calls: list[dict] = []

    class Client:
        def heartbeat(self, metadata=None):
            calls.append(metadata or {})
            return {"ok": True}

    remote = agent.RemoteAgent(_engine_config(), client=Client())
    remote.heartbeat()
    assert "engine" not in calls[0]["executors"]
    assert "shell" in calls[0]["executors"]


def test_config_rejects_engine_when_lucent_is_not_importable(monkeypatch):
    import argparse

    monkeypatch.setenv("LUCENT_CONTROLLER_URL", "http://localhost:8766")
    monkeypatch.setenv("LUCENT_REMOTE_TOKEN", "lr_" + "x" * 40)
    monkeypatch.setenv("LUCENT_REMOTE_ID", str(uuid4()))
    monkeypatch.setenv("LUCENT_REMOTE_EXECUTOR", "engine")

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("lucent.llm.remote_session"):
            raise ImportError("no lucent here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    args = argparse.Namespace(
        controller_url=None,
        token=None,
        remote_id=None,
        executor=None,
        command=None,
        shell=None,
        home=None,
        poll_seconds=1,
        heartbeat_seconds=30,
        workdir=None,
        task_timeout=60,
        once=True,
        insecure=False,
        verbose=False,
    )
    with pytest.raises(agent.ConfigError) as exc:
        agent.config_from_env(args)
    assert "'engine' executor" in str(exc.value)


# ── An engine-only remote must never receive a plan-less task ───────────────
#
# A remote configured with LUCENT_REMOTE_EXECUTOR=engine does not run fenced
# shell blocks. A pinned task with no execution_config used to dispatch as
# plain prose, fail at the agent with a bare "exit code 2", and say nothing
# about what was wrong. The controller now either composes a session or fails
# the task with an actionable reason.


def _engine_only_remote():
    return {
        "name": "engine-host",
        "capabilities": {"executor": "engine", "executors": ["engine"]},
    }


def _shell_remote():
    return {
        "name": "shell-host",
        "capabilities": {"executor": "shell", "executors": ["command", "shell"]},
    }


def test_plan_is_needed_for_an_engine_only_remote_even_without_execution_config():
    from lucent.llm.remote_planner import task_needs_remote_plan

    task = {"id": "t", "title": "x", "backend_id": "b", "model": "m"}
    assert task_needs_remote_plan(task, _engine_only_remote()) is True


def test_plain_task_on_a_shell_remote_stays_plain():
    from lucent.llm.remote_planner import task_needs_remote_plan

    task = {"id": "t", "title": "x"}
    assert task_needs_remote_plan(task, _shell_remote()) is False


def test_explicit_engine_executor_is_still_honoured_on_any_remote():
    from lucent.llm.remote_planner import task_needs_remote_plan

    task = {"id": "t", "execution_config": {"executor": "engine"}}
    assert task_needs_remote_plan(task, _shell_remote()) is True


def test_remote_without_capabilities_keeps_plain_behaviour():
    from lucent.llm.remote_planner import task_needs_remote_plan

    assert task_needs_remote_plan({"id": "t"}, None) is False
    assert task_needs_remote_plan({"id": "t"}, {"name": "x"}) is False
    # A capabilities blob that is still a JSON string must not crash the check.
    assert (
        task_needs_remote_plan(
            {"id": "t"}, {"capabilities": '{"executor": "engine"}'}
        )
        is True
    )


def test_client_reports_what_is_missing_instead_of_a_bare_exit_code(tmp_path):
    """The agent must name the task's actual gap, not just 'exit code 2'."""
    result = agent.execute_engine(
        _engine_config(),
        {"id": "t1", "agent_type": "lucent-dev", "description": "do the thing"},
        str(tmp_path),
    )
    assert result.ok is False
    assert result.exit_code == 2
    for expected in ("backend_id", "model", "execution_config"):
        assert expected in result.output
    assert "lucent-dev" in result.output
