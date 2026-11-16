"""Tests for the ``script`` task kind: config, output parsing, and executor."""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio

from daemon.dispatch import script_executor
from lucent.sandbox.models import ExecResult
from lucent.script_tasks import (
    derive_interpreter,
    git_resolution_steps,
    interpreter_from_shebang,
    normalize_repo_path,
    parse_script_stdout,
    ref_pinning,
    repo_host,
    validate_credential_reference,
    validate_git_ref,
    validate_repo_url,
    validate_script_config,
    validate_script_task,
)
from lucent.task_kinds import task_kind_catalog

TASK_ID = "11111111-1111-1111-1111-111111111111"
REQUEST_ID = "22222222-2222-2222-2222-222222222222"
ORG_ID = "33333333-3333-3333-3333-333333333333"
USER_ID = "44444444-4444-4444-4444-444444444444"
TEMPLATE_ID = "55555555-5555-5555-5555-555555555555"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _inline(script: str = "print('{}')", **overrides):
    config = {"source": "inline", "script": script}
    config.update(overrides)
    return config


def _contract(**overrides):
    contract = {
        "json_schema": {
            "type": "object",
            "required": ["total"],
            "properties": {"total": {"type": "integer"}},
        },
        "on_failure": "fail",
    }
    contract.update(overrides)
    return contract


def _task(**overrides):
    task = {
        "id": TASK_ID,
        "request_id": REQUEST_ID,
        "organization_id": ORG_ID,
        "title": "Run the script",
        "sandbox_template_id": TEMPLATE_ID,
        "output_contract": None,
    }
    task.update(overrides)
    return task


def _sandbox_config(timeout_seconds: int = 1800) -> dict:
    return {"working_dir": "/workspace", "timeout_seconds": timeout_seconds}


# ── config validation ───────────────────────────────────────────────────


def test_catalog_entry_requires_approved_template():
    catalog = {entry["kind"]: entry for entry in task_kind_catalog()}
    script = catalog["script"]
    assert script["config_field"] == "execution_config"
    assert script["config_schema"]["required"] == ["source"]
    assert script["requirements"]["sandbox"] == "required"


def test_rejects_missing_template():
    with pytest.raises(ValueError, match="requires sandbox_template_id"):
        validate_script_task(config=_inline(), sandbox_template_id=None)


def test_rejects_inline_without_body():
    with pytest.raises(ValueError, match="non-empty 'script' body"):
        validate_script_config({"source": "inline"})
    with pytest.raises(ValueError, match="non-empty 'script' body"):
        validate_script_config({"source": "inline", "script": "   "})


def test_rejects_unknown_source():
    with pytest.raises(ValueError, match="source must be one of"):
        validate_script_config({"source": "webhook"})
    with pytest.raises(ValueError, match="requires execution_config"):
        validate_script_config(None)


def test_rejects_inline_sandbox_config():
    with pytest.raises(ValueError, match="inline sandbox_config"):
        validate_script_task(
            config=_inline(),
            sandbox_template_id=TEMPLATE_ID,
            sandbox_config={"image": "node:20"},
            inline_sandbox_config=True,
        )


def test_rejects_unreachable_git_host_and_names_it():
    config = {
        "source": "git",
        "repo_url": "https://gitlab.com/acme/tool.git",
        "ref": "main",
        "path": "tool.py",
    }
    template = {"network_mode": "allowlist", "allowed_hosts": ["github.com"]}
    with pytest.raises(ValueError) as excinfo:
        validate_script_task(
            config=config,
            sandbox_template_id=TEMPLATE_ID,
            template_config=template,
        )
    message = str(excinfo.value)
    assert "gitlab.com" in message
    assert "cannot reach" in message


def test_reachable_git_source_is_accepted():
    config = {
        "source": "git",
        "repo_url": "https://github.com/acme/tool.git",
        "ref": "main",
        "path": "tool.py",
    }
    template = {"network_mode": "allowlist", "allowed_hosts": ["github.com"]}
    assert repo_host(config["repo_url"]) == "github.com"
    # No raise: a reachable git source is now a supported config.
    validate_script_task(
        config=config,
        sandbox_template_id=TEMPLATE_ID,
        template_config=template,
    )


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("git@github.com:acme/tool.git", "ssh/scp-style"),
        ("ssh://git@github.com/acme/tool.git", "must use https"),
        ("git://github.com/acme/tool.git", "must use https"),
        ("http://github.com/acme/tool.git", "must use https"),
        ("https://user:ghp_token@github.com/acme/tool.git", "must not embed credentials"),
    ],
)
def test_rejects_non_https_or_credential_bearing_repo_url(url, needle):
    with pytest.raises(ValueError, match=needle):
        validate_repo_url(url)


def test_rejects_unsafe_ref_and_path():
    with pytest.raises(ValueError, match="must not start with '-'"):
        validate_git_ref("--upload-pack=/bin/sh")
    with pytest.raises(ValueError, match="relative to the repository root"):
        normalize_repo_path("/etc/passwd")
    with pytest.raises(ValueError, match="name a file inside the repository"):
        normalize_repo_path("../../escape.py")
    assert normalize_repo_path("scripts/./run.py") == "scripts/run.py"


def test_rejects_literal_credential():
    with pytest.raises(ValueError, match="literal token must never be stored"):
        validate_credential_reference("ghp_literaltoken")
    with pytest.raises(ValueError, match="literal token must never be stored"):
        validate_script_config(
            {
                "source": "git",
                "repo_url": "https://github.com/acme/tool.git",
                "ref": "main",
                "path": "tool.py",
                "credential": "ghp_literaltoken",
            }
        )


def test_rejects_literal_sensitive_env_value():
    with pytest.raises(ValueError, match="must use a secret:// or credential://"):
        validate_script_config(
            _inline(env={"GITHUB_TOKEN": "ghp_literaltoken"})
        )


def test_ref_pinning_reports_branches_and_confirms_shas():
    pinned = ref_pinning(SHA, SHA)
    assert pinned["pinned"] is True
    assert pinned["warning"] is None

    unpinned = ref_pinning("main", SHA)
    assert unpinned["pinned"] is False
    assert unpinned["ref_kind"] == "branch_or_tag"
    assert "not pinned" in unpinned["warning"]

    assert ref_pinning("v1.2.3", SHA)["pinned"] is False


def test_interpreter_from_shebang_and_resolution_steps():
    assert interpreter_from_shebang("#!/usr/bin/env bash") == "bash"
    assert interpreter_from_shebang("print(1)") is None

    steps = git_resolution_steps(
        "https://github.com/acme/tool.git", "main", "/workspace/.lucent-scripts/x/repo"
    )
    names = [name for name, _command in steps]
    assert names == ["init", "remote", "fetch", "checkout", "rev_parse"]
    fetch = dict(steps)["fetch"]
    assert "--depth=1" in fetch
    # Every step is argv, never a shell string, so a URL cannot inject.
    assert all(isinstance(step, list) for _name, step in steps)


def test_derive_interpreter_from_shebang_or_default():
    assert derive_interpreter({"source": "inline", "script": "#!/usr/bin/env bash\nx"}) == "bash"
    assert derive_interpreter({"source": "inline", "script": "#!/bin/sh\nx"}) == "/bin/sh"
    assert derive_interpreter(_inline("print(1)")) == "python3"
    assert derive_interpreter(_inline("print(1)", interpreter="python3 -u")) == "python3 -u"


# ── repository normalization (the shared authoring backstop) ────────────


def _normalize(**overrides):
    from lucent.db.requests import _normalize_task_payload

    payload = {
        "agent_type": "code",
        "output_contract": None,
        "decision_config": None,
        "fusion_config": None,
        "task_kind": "script",
        "execution_config": _inline(),
        "sandbox_config": None,
        "sandbox_template_id": TEMPLATE_ID,
    }
    payload.update(overrides)
    return _normalize_task_payload(**payload)


def test_repository_rejects_script_without_template():
    with pytest.raises(ValueError, match="requires sandbox_template_id"):
        _normalize(sandbox_template_id=None)


def test_repository_rejects_inline_config_without_template():
    with pytest.raises(ValueError, match="requires sandbox_template_id"):
        _normalize(
            sandbox_template_id=None,
            sandbox_config={"image": "node:20"},
        )


def test_repository_rejects_cross_kind_config():
    with pytest.raises(ValueError, match="cannot carry decision_config"):
        _normalize(
            execution_config=None,
            decision_config={"questions": []},
        )


def test_repository_accepts_valid_script_payload():
    kind, resolved = _normalize()
    assert kind == "script"
    assert resolved["execution_config"] == _inline()
    assert resolved["decision_config"] is None
    assert resolved["fusion_config"] is None


# ── stdout parsing ──────────────────────────────────────────────────────


def test_parse_without_contract_stores_parsed_json_or_none():
    parsed = parse_script_stdout('{"total": 42}', None)
    assert parsed["result_structured"] == {"total": 42}

    text = parse_script_stdout("not json", None)
    assert text["result_structured"] is None
    assert text["validation_status"] == "not_applicable"


def test_parse_with_contract_reports_violation():
    parsed = parse_script_stdout('{"total": "nope"}', _contract())
    assert parsed["validation_status"] == "invalid"
    assert parsed["result_structured"] is None
    assert any("total" in error for error in parsed["validation_errors"])


# ── executor ────────────────────────────────────────────────────────────


class _FakeManager:
    def __init__(self, results: list[ExecResult]):
        self._results = list(results)
        self.writes: list[tuple[str, bytes]] = []
        self.execs: list[dict] = []

    async def write_file(self, sandbox_id, path, content):
        self.writes.append((path, content))

    async def exec(self, sandbox_id, command, **kwargs):
        self.execs.append({"command": command, **kwargs})
        return self._results.pop(0)


class _FakeDaemon:
    def __init__(self, create_result=None):
        self.created: list[tuple] = []
        self.destroyed: list[str] = []
        self.sessions: list[tuple] = []
        self._create_result = create_result

    async def _create_task_sandbox(self, task_id, config, **kwargs):
        self.created.append((task_id, config, kwargs))
        if self._create_result is not None:
            return self._create_result
        return "sb-test-1234", SimpleNamespace(idle_timeout_seconds=0), None, False

    async def _destroy_task_sandbox(self, sandbox_id):
        self.destroyed.append(sandbox_id)

    async def _keep_task_sandbox_alive(self, sandbox_id, idle_timeout):
        raise AssertionError("keepalive should not start when idle timeout is 0")

    async def run_session(self, *args, **kwargs):
        self.sessions.append((args, kwargs))
        raise AssertionError("a script task must never create an agent session")


class _Callbacks:
    def __init__(self):
        self.completed: list[tuple] = []
        self.failed: list[tuple] = []

    async def complete(self, task_id, result, **kwargs):
        self.completed.append((task_id, result, kwargs))

    async def fail(self, task_id, error, result=None):
        self.failed.append((task_id, error, result))


def _events_recorder(provider=None):
    events: list[tuple] = []
    logs: list[tuple] = []

    async def _add_event(task_id, event_type, detail=None, metadata=None):
        events.append((event_type, detail, metadata))

    async def _get_secret_provider():
        return provider

    def _set_current_user(user):
        return None

    async def _resolve_secret_env_vars(env_vars, _provider):
        return dict(env_vars)

    return events, SimpleNamespace(
        RequestAPI=SimpleNamespace(add_event=_add_event),
        log=lambda *a, **k: logs.append((a, k)),
        logs=logs,
        get_secret_provider=_get_secret_provider,
        set_current_user=_set_current_user,
        resolve_secret_env_vars=_resolve_secret_env_vars,
    )


async def _run(
    monkeypatch,
    *,
    results,
    config=None,
    task=None,
    sandbox_config=None,
    daemon=None,
    provider=None,
):
    manager = _FakeManager(results)
    events, fake_runtime = _events_recorder(provider)
    monkeypatch.setattr(
        "lucent.sandbox.manager.get_sandbox_manager", lambda: manager
    )
    monkeypatch.setattr(script_executor, "runtime", fake_runtime)
    daemon = daemon or _FakeDaemon()
    callbacks = _Callbacks()
    await script_executor.run_script_task(
        daemon=daemon,
        task=task or _task(),
        execution_config=config or _inline(),
        sandbox_config=sandbox_config or _sandbox_config(),
        org_id=ORG_ID,
        request_id=REQUEST_ID,
        requesting_user_id=USER_ID,
        on_complete=callbacks.complete,
        on_fail=callbacks.fail,
    )
    return SimpleNamespace(
        manager=manager,
        daemon=daemon,
        callbacks=callbacks,
        events=events,
        runtime=fake_runtime,
    )


@pytest.mark.asyncio
async def test_inline_execution_completes_with_structured_output(monkeypatch):
    run = await _run(
        monkeypatch,
        results=[ExecResult(exit_code=0, stdout='{"total": 42}', stderr="")],
        config=_inline("print('{\"total\": 42}')"),
        task=_task(output_contract=_contract()),
    )

    assert run.callbacks.failed == []
    assert len(run.callbacks.completed) == 1
    _task_id, _result, kwargs = run.callbacks.completed[0]
    assert kwargs["result_structured"] == {"total": 42}
    assert kwargs["validation_status"] == "valid"
    # Sandbox was provisioned then destroyed, and the script was written in
    # the template's working directory.
    assert run.daemon.created[0][0] == TASK_ID
    assert run.daemon.destroyed == ["sb-test-1234"]
    assert run.manager.writes[0][0].startswith("/workspace/")
    assert run.daemon.sessions == []
    event_types = [etype for etype, _d, _m in run.events]
    assert "script_completed" in event_types


@pytest.mark.asyncio
async def test_schema_violation_fails_with_stdout_and_preview_and_never_retries(monkeypatch):
    run = await _run(
        monkeypatch,
        results=[ExecResult(exit_code=0, stdout='{"total": "nope"}', stderr="")],
        config=_inline(),
        # Even with retries opted in, a schema violation is not retried.
        task=_task(
            output_contract=_contract(
                on_failure="retry_then_fallback", max_retries=3
            )
        ),
    )

    assert run.callbacks.completed == []
    assert len(run.callbacks.failed) == 1
    _task_id, error, result = run.callbacks.failed[0]
    assert "output_contract" in error
    assert '{"total": "nope"}' in (result or "")
    assert len(run.manager.execs) == 1
    event_types = [etype for etype, _d, _m in run.events]
    assert "script_output_invalid" in event_types
    assert "script_retry" not in event_types
    assert run.daemon.destroyed == ["sb-test-1234"]


@pytest.mark.asyncio
async def test_nonzero_exit_fails_with_output(monkeypatch):
    run = await _run(
        monkeypatch,
        results=[ExecResult(exit_code=2, stdout="partial out", stderr="boom")],
    )

    assert run.callbacks.completed == []
    _task_id, error, result = run.callbacks.failed[0]
    assert "exited with code 2" in error
    assert "partial out" in result
    assert "boom" in result
    assert run.daemon.destroyed == ["sb-test-1234"]


@pytest.mark.asyncio
async def test_timeout_is_recorded_and_fails(monkeypatch):
    run = await _run(
        monkeypatch,
        results=[
            ExecResult(exit_code=124, stdout="", stderr="", timed_out=True)
        ],
        sandbox_config=_sandbox_config(timeout_seconds=90),
    )

    assert run.callbacks.completed == []
    _task_id, error, _result = run.callbacks.failed[0]
    assert "timed out after 90s" in error
    event_types = [etype for etype, _d, _m in run.events]
    assert "script_timed_out" in event_types
    assert run.manager.execs[0]["timeout"] == 90
    assert run.daemon.destroyed == ["sb-test-1234"]


@pytest.mark.asyncio
async def test_retry_honoured_only_when_opted_in(monkeypatch):
    run = await _run(
        monkeypatch,
        results=[
            ExecResult(exit_code=1, stdout="", stderr="transient"),
            ExecResult(exit_code=0, stdout='{"total": 7}', stderr=""),
        ],
        task=_task(
            output_contract=_contract(
                on_failure="retry_then_fallback", max_retries=1
            )
        ),
    )

    assert run.callbacks.failed == []
    assert len(run.callbacks.completed) == 1
    assert len(run.manager.execs) == 2
    event_types = [etype for etype, _d, _m in run.events]
    assert "script_retry" in event_types


@pytest.mark.asyncio
async def test_sandbox_destroyed_when_provisioning_fails(monkeypatch):
    daemon = _FakeDaemon(
        create_result=(
            None,
            None,
            {"stage": "sandbox_create", "detail": "image missing"},
            False,
        )
    )
    run = await _run(monkeypatch, results=[], daemon=daemon)

    assert run.callbacks.completed == []
    _task_id, error, _result = run.callbacks.failed[0]
    assert "image missing" in error
    # Nothing was provisioned, so there is nothing to destroy.
    assert run.daemon.destroyed == []
    assert run.manager.execs == []


@pytest.mark.asyncio
async def test_dispatch_routes_script_to_executor_without_agent_session(monkeypatch):
    """A script task is handed to the script executor, never an agent session."""
    from daemon.daemon import LucentDaemon

    task = {
        "id": UUID(TASK_ID),
        "request_id": UUID(REQUEST_ID),
        "organization_id": UUID(ORG_ID),
        "title": "Run the script",
        "agent_type": "code",
        "requesting_user_id": UUID(USER_ID),
        "task_kind": "script",
        "execution_config": {"source": "inline", "script": "print(1)"},
        "sandbox_config": _sandbox_config(),
        "sandbox_template_id": UUID(TEMPLATE_ID),
    }
    calls = {"script": [], "sessions": [], "completed": [], "failed": [], "events": []}

    async def _noop(*_args, **_kwargs):
        return None

    async def _pending():
        return [task]

    async def _claim(task_id, _instance_id):
        return {"id": task_id, "organization_id": str(ORG_ID)}

    async def _matches(_claimed):
        return True

    async def _start(task_id, _instance_id=None):
        return {"id": task_id}

    async def _complete(task_id, result, instance_id=None, **kwargs):
        calls["completed"].append((task_id, result, kwargs))
        return {"id": task_id}

    async def _fail(task_id, error, instance_id=None, result=None):
        calls["failed"].append((task_id, error, result))
        return {"id": task_id}

    async def _event(task_id, event_type, detail=None, metadata=None):
        calls["events"].append(event_type)
        return {"id": task_id}

    async def _run_script(**kwargs):
        calls["script"].append(kwargs)
        await kwargs["on_complete"](str(kwargs["task"]["id"]), "done")

    async def _forbidden_session(*args, **kwargs):
        calls["sessions"].append((args, kwargs))
        raise AssertionError("agent session must not be created for a script task")

    async def _forbidden_model(**_kwargs):
        raise AssertionError("model selection must not run for a script task")

    async def _entitled(_task):
        return None

    daemon = LucentDaemon()
    monkeypatch.setattr(daemon, "_ensure_request_review_tasks", _noop)
    monkeypatch.setattr(daemon, "_task_matches_bound_org", _matches)
    monkeypatch.setattr(daemon, "run_session", _forbidden_session)
    monkeypatch.setattr(script_executor, "run_script_task", _run_script)
    monkeypatch.setattr(
        script_executor, "script_entitlement_failure", _entitled
    )
    monkeypatch.setattr("daemon.daemon._select_model_for_user", _forbidden_model)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_pending_tasks", _pending)
    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", _claim)
    monkeypatch.setattr("daemon.daemon.RequestAPI.start_task", _start)
    monkeypatch.setattr("daemon.daemon.RequestAPI.complete_task", _complete)
    monkeypatch.setattr("daemon.daemon.RequestAPI.fail_task", _fail)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _event)

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert len(calls["script"]) == 1
    assert calls["sessions"] == []
    assert calls["completed"] and calls["failed"] == []
    assert "script_dispatched" in calls["events"]


@pytest.mark.asyncio
async def test_dispatch_resolves_template_for_script_without_config_snapshot(monkeypatch):
    """A workflow-authored script task carries only the template id."""
    from daemon.daemon import LucentDaemon

    task = {
        "id": UUID(TASK_ID),
        "request_id": UUID(REQUEST_ID),
        "organization_id": UUID(ORG_ID),
        "title": "Run the stored script",
        "agent_type": "code",
        "requesting_user_id": UUID(USER_ID),
        "task_kind": "script",
        "execution_config": {"source": "inline", "script": "print(1)"},
        "sandbox_config": None,
        "sandbox_template_id": UUID(TEMPLATE_ID),
    }
    calls = {"script": [], "resolved": [], "completed": [], "failed": []}

    async def _noop(*_args, **_kwargs):
        return None

    async def _pending():
        return [task]

    async def _claim(task_id, _instance_id):
        return {"id": task_id, "organization_id": str(ORG_ID)}

    async def _matches(_claimed):
        return True

    async def _start(task_id, _instance_id=None):
        return {"id": task_id}

    async def _complete(task_id, result, instance_id=None, **kwargs):
        calls["completed"].append((task_id, result, kwargs))
        return {"id": task_id}

    async def _fail(task_id, error, instance_id=None, result=None):
        calls["failed"].append((task_id, error, result))
        return {"id": task_id}

    async def _event(*_args, **_kwargs):
        return None

    async def _resolve_template(template_id, *, org_id, requesting_user_id):
        calls["resolved"].append((template_id, org_id, requesting_user_id))
        return _sandbox_config()

    async def _run_script(**kwargs):
        calls["script"].append(kwargs)
        await kwargs["on_complete"](str(kwargs["task"]["id"]), "done")

    async def _entitled(_task):
        return None

    daemon = LucentDaemon()
    monkeypatch.setattr(daemon, "_ensure_request_review_tasks", _noop)
    monkeypatch.setattr(daemon, "_task_matches_bound_org", _matches)
    monkeypatch.setattr(daemon, "_resolve_sandbox_template", _resolve_template)
    monkeypatch.setattr(script_executor, "run_script_task", _run_script)
    monkeypatch.setattr(
        script_executor, "script_entitlement_failure", _entitled
    )
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_pending_tasks", _pending)
    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", _claim)
    monkeypatch.setattr("daemon.daemon.RequestAPI.start_task", _start)
    monkeypatch.setattr("daemon.daemon.RequestAPI.complete_task", _complete)
    monkeypatch.setattr("daemon.daemon.RequestAPI.fail_task", _fail)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _event)

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert calls["resolved"] and calls["resolved"][0][0] == TEMPLATE_ID
    assert len(calls["script"]) == 1
    assert calls["script"][0]["sandbox_config"] == _sandbox_config()
    assert calls["failed"] == [] and calls["completed"]


# ── git source resolution ───────────────────────────────────────────────


def _git_config(**overrides):
    config = {
        "source": "git",
        "repo_url": "https://github.com/acme/tool.git",
        "ref": "main",
        "path": "scripts/run.py",
    }
    config.update(overrides)
    return config


def _ok(stdout: str = "") -> ExecResult:
    return ExecResult(exit_code=0, stdout=stdout, stderr="")


def _git_success_results(
    *,
    sha: str = SHA,
    executable: bool = False,
    credential: bool = False,
    script_stdout: str = "{}",
) -> list[ExecResult]:
    """Canned results for mkdir → clone steps → probes → run → cleanup."""
    results = [_ok()]  # mkdir -p
    if credential:
        results.append(_ok())  # chmod askpass
    results.extend(
        [
            _ok(),  # git init
            _ok(),  # git remote add
            _ok(),  # git fetch --depth=1
            _ok(),  # git checkout FETCH_HEAD
            _ok(sha + "\n"),  # git rev-parse HEAD
            _ok(),  # test -f
        ]
    )
    if executable:
        results.append(_ok())  # test -x
    else:
        results.extend(
            [
                ExecResult(exit_code=1, stdout="", stderr=""),  # test -x
                _ok("#!/usr/bin/env python3\n"),  # head -n 1
            ]
        )
    results.append(_ok(script_stdout))  # the script
    results.append(_ok())  # rm -rf checkout (finally)
    return results


def _run_command(run) -> list[str] | str:
    return next(
        entry["command"]
        for entry in run.manager.execs
        if entry.get("cwd") == "/workspace"
    )


@pytest.mark.asyncio
async def test_git_source_fetches_into_task_subdir_and_runs_repo_file(monkeypatch):
    run = await _run(monkeypatch, results=_git_success_results(), config=_git_config())

    assert run.callbacks.failed == []
    _task_id, result, _kwargs = run.callbacks.completed[0]
    assert "source: git" in result
    assert "repo_url: https://github.com/acme/tool.git" in result
    # A branch was requested, so the result must say it was not pinned.
    assert "ref_pinned: false" in result
    assert "not pinned to a commit SHA" in result
    assert f"resolved_sha: {SHA}" in result

    commands = [entry["command"] for entry in run.manager.execs]
    repo_dir = "/workspace/.lucent-scripts/11111111/repo"
    # Shallow, ref-addressed checkout into a task-scoped subdirectory — never
    # over the template's working dir root.
    assert ["git", "init", "-q", repo_dir] in commands
    assert ["git", "-C", repo_dir, "fetch", "--depth=1", "origin", "main"] in commands
    # Not executable in the repo: honour its shebang instead of copying it.
    assert _run_command(run) == ["python3", f"{repo_dir}/scripts/run.py"]
    event_types = [etype for etype, _d, _m in run.events]
    assert "script_source_resolved" in event_types
    assert "script_ref_unpinned" in event_types
    assert run.daemon.destroyed == ["sb-test-1234"]


@pytest.mark.asyncio
async def test_git_sha_ref_is_pinned_and_exec_mode_is_respected(monkeypatch):
    run = await _run(
        monkeypatch,
        results=_git_success_results(executable=True),
        config=_git_config(ref=SHA),
    )

    _task_id, result, _kwargs = run.callbacks.completed[0]
    assert "ref_pinned: true" in result
    assert "warning:" not in result
    # Executable in the repository: run the file itself, not an interpreter.
    assert _run_command(run) == [
        "/workspace/.lucent-scripts/11111111/repo/scripts/run.py"
    ]
    assert "script_ref_unpinned" not in [e for e, _d, _m in run.events]


@pytest.mark.asyncio
async def test_git_fetch_failure_surfaces_git_stderr(monkeypatch):
    run = await _run(
        monkeypatch,
        results=[
            _ok(),  # mkdir
            _ok(),  # init
            _ok(),  # remote
            ExecResult(
                exit_code=128,
                stdout="",
                stderr=(
                    "fatal: repository "
                    "'https://github.com/acme/tool.git/' not found"
                ),
            ),
        ],
        config=_git_config(),
    )

    assert run.callbacks.completed == []
    _task_id, error, _result = run.callbacks.failed[0]
    assert "Git fetch failed" in error
    assert "repository 'https://github.com/acme/tool.git/' not found" in error
    assert run.daemon.destroyed == ["sb-test-1234"]
    # Never proceeded to execute.
    assert _run_command_or_none(run) is None


def _run_command_or_none(run):
    try:
        return _run_command(run)
    except StopIteration:
        return None


@pytest.mark.asyncio
async def test_git_missing_ref_fails_specifically(monkeypatch):
    run = await _run(
        monkeypatch,
        results=[
            _ok(),
            _ok(),
            _ok(),
            ExecResult(
                exit_code=128,
                stdout="",
                stderr="fatal: couldn't find remote ref release-9",
            ),
        ],
        config=_git_config(ref="release-9"),
    )

    _task_id, error, _result = run.callbacks.failed[0]
    assert "Ref 'release-9' does not exist" in error
    assert "couldn't find remote ref" in error
    assert _run_command_or_none(run) is None


@pytest.mark.asyncio
async def test_git_missing_path_fails_after_clone(monkeypatch):
    results = [
        _ok(),  # mkdir
        _ok(),  # init
        _ok(),  # remote
        _ok(),  # fetch
        _ok(),  # checkout
        _ok(SHA + "\n"),  # rev-parse
        ExecResult(exit_code=1, stdout="", stderr=""),  # test -f
    ]
    run = await _run(
        monkeypatch,
        results=results,
        config=_git_config(path="scripts/absent.py"),
    )

    _task_id, error, _result = run.callbacks.failed[0]
    assert "path 'scripts/absent.py' was not found" in error
    assert SHA in error
    assert _run_command_or_none(run) is None


@pytest.mark.asyncio
async def test_git_missing_binary_names_the_image_problem(monkeypatch):
    run = await _run(
        monkeypatch,
        results=[
            _ok(),  # mkdir
            ExecResult(exit_code=127, stdout="", stderr="git: not found"),
        ],
        config=_git_config(),
    )

    _task_id, error, _result = run.callbacks.failed[0]
    assert "git is not available in the sandbox image" in error


@pytest.mark.asyncio
async def test_git_private_repo_without_credential_says_so(monkeypatch):
    run = await _run(
        monkeypatch,
        results=[
            _ok(),
            _ok(),
            _ok(),
            ExecResult(
                exit_code=128,
                stdout="",
                stderr=(
                    "fatal: could not read Username for 'https://github.com': "
                    "terminal prompts disabled"
                ),
            ),
        ],
        config=_git_config(),
    )

    _task_id, error, _result = run.callbacks.failed[0]
    assert "No git credential is configured" in error
    assert "execution_config.credential" in error


@pytest.mark.asyncio
async def test_git_host_outside_allowlist_fails_before_provisioning(monkeypatch):
    run = await _run(
        monkeypatch,
        results=[],
        config=_git_config(),
        sandbox_config={
            "working_dir": "/workspace",
            "timeout_seconds": 60,
            "network_mode": "none",
            "allowed_hosts": [],
        },
    )

    assert run.callbacks.completed == []
    _task_id, error, _result = run.callbacks.failed[0]
    assert "cannot reach git host 'github.com'" in error
    assert run.daemon.created == []
    assert run.manager.execs == []


@pytest.mark.asyncio
async def test_git_credential_is_injected_but_never_persisted(monkeypatch):
    """The token reaches the clone process and appears nowhere else."""
    token = "ghp_SUPERSECRETVALUE123"
    seen: dict = {}

    async def _fake_resolve(reference, _provider, *, env_key=""):
        assert reference == "secret://git.token"
        assert env_key == "credential"
        seen["reference"] = reference
        return token

    monkeypatch.setattr(
        "lucent.secrets.utils.resolve_secret_reference", _fake_resolve
    )
    config = _git_config(credential="secret://git.token")
    run = await _run(
        monkeypatch,
        results=_git_success_results(credential=True),
        config=config,
        provider=object(),
    )

    assert seen["reference"] == "secret://git.token"
    assert run.callbacks.failed == []

    # The token was injected through the credential helper's environment...
    fetch = next(
        entry
        for entry in run.manager.execs
        if isinstance(entry["command"], list)
        and entry["command"][3:6] == ["fetch", "--depth=1", "origin"]
    )
    assert fetch["env"]["LUCENT_GIT_TOKEN"] == token
    assert fetch["env"]["GIT_ASKPASS"].endswith("askpass.sh")
    askpass = [content for path, content in run.manager.writes if path.endswith("askpass.sh")]
    assert askpass, "the credential helper must be written for an authenticated clone"
    # The helper reads the token from the exec environment; the token itself is
    # never written to disk.
    assert all(token not in content.decode("utf-8", "replace") for content in askpass)

    # ...and nowhere in the task result, event trail, logs, or config.
    _task_id, result, kwargs = run.callbacks.completed[0]
    serialized = json.dumps(
        {
            "result": result,
            "kwargs": kwargs,
            "events": run.events,
            "logs": run.runtime.logs,
            "config": config,
        },
        default=str,
    )
    assert token not in serialized
    assert token not in json.dumps(run.events, default=str)
    assert token not in json.dumps(run.runtime.logs, default=str)


@pytest.mark.asyncio
async def test_git_stderr_token_is_redacted(monkeypatch):
    token = "ghp_SUPERSECRETVALUE123"

    async def _fake_resolve(_reference, _provider, *, env_key=""):
        return token

    monkeypatch.setattr(
        "lucent.secrets.utils.resolve_secret_reference", _fake_resolve
    )
    run = await _run(
        monkeypatch,
        results=[
            _ok(),  # mkdir
            _ok(),  # chmod
            _ok(),  # init
            _ok(),  # remote
            ExecResult(
                exit_code=128,
                stdout="",
                stderr=f"fatal: remote rejected credential {token}",
            ),
        ],
        config=_git_config(credential="secret://git.token"),
        provider=object(),
    )

    _task_id, error, _result = run.callbacks.failed[0]
    assert token not in error
    assert "***" in error


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_local_fixture_repo_resolves_branch_tag_and_sha(tmp_path):
    """The real command sequence resolves all three ref kinds, shallowly.

    Deterministic local fixture (``file://``) rather than an external fetch:
    no network, no allowlist, no flake. A file:// remote supports ``--depth``.
    """

    def _git(*args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    assert _git("init", "-q", cwd=fixture).returncode == 0
    _git("config", "user.email", "test@example.com", cwd=fixture)
    _git("config", "user.name", "Test", cwd=fixture)
    # Allow fetch-by-SHA from this local remote (GitHub enables the same).
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=fixture)
    entry = fixture / "run.sh"
    entry.write_text("#!/bin/sh\necho ok\n")
    entry.chmod(0o755)
    assert _git("add", "run.sh", cwd=fixture).returncode == 0
    assert _git("commit", "-q", "-m", "initial", cwd=fixture).returncode == 0
    sha = _git("rev-parse", "HEAD", cwd=fixture).stdout.strip()
    assert _git("branch", "feature", cwd=fixture).returncode == 0
    assert _git("tag", "v1", cwd=fixture).returncode == 0

    repo_url = f"file://{fixture}"
    for ref in ("feature", "v1", sha):
        repo_dir = tmp_path / f"clone-{ref[:7]}" / "repo"
        repo_dir.parent.mkdir(parents=True)
        for step, command in git_resolution_steps(repo_url, ref, str(repo_dir)):
            result = subprocess.run(command, capture_output=True, text=True)
            assert result.returncode == 0, f"{step} failed: {result.stderr}"

        resolved = _git("rev-parse", "HEAD", cwd=repo_dir).stdout.strip()
        assert resolved == sha, f"ref {ref!r} resolved to {resolved}"
        assert (repo_dir / "run.sh").exists()
        depth = _git("rev-list", "--count", "HEAD", cwd=repo_dir).stdout.strip()
        assert depth == "1", "the checkout must be shallow (depth 1)"


@pytest.mark.asyncio
async def test_inline_config_round_trips_through_workflow_storage(
    db_pool, test_organization, test_user
):
    """A workflow action carries the inline body through store and read."""
    from lucent.db.schedules import ScheduleRepository, workflow_actions_for_schedule

    inline = {
        "source": "inline",
        "script": "#!/usr/bin/env python3\nprint('{\"total\": 42}')\n",
        "env": {"MODE": "report"},
    }
    action = {
        "action_type": "task",
        "title": "Compute the report",
        "task_kind": "script",
        "execution_config": inline,
        "sandbox_template_id": TEMPLATE_ID,
    }
    repo = ScheduleRepository(db_pool)
    org_id = str(test_organization["id"])
    try:
        created = await repo.create_schedule(
            title="script roundtrip",
            org_id=org_id,
            schedule_type="manual",
            actions=[action],
            created_by=str(test_user["id"]),
        )

        fetched = await repo.get_schedule(str(created["id"]), org_id)
        assert fetched is not None
        stored = fetched["actions"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        assert stored[0]["task_kind"] == "script"
        assert stored[0]["execution_config"] == inline

        # The trigger's read normalization preserves the same payload.
        normalized = workflow_actions_for_schedule(fetched)[0]
        assert normalized["task_kind"] == "script"
        assert normalized["execution_config"] == inline
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM schedules WHERE organization_id = $1::uuid", org_id
            )


def _as_json(value):
    return json.loads(value) if isinstance(value, str) else value


async def _insert_script_template(db_pool, org_id: str) -> str:
    """An approved template is the FK target every script task needs."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO sandbox_templates
               (organization_id, name, image, scope, status, created_by)
               VALUES ($1::uuid, 'script-roundtrip-tpl', 'python:3.12-slim',
                       'built-in', 'approved', NULL)
               ON CONFLICT (organization_id, name)
               DO UPDATE SET image = EXCLUDED.image
               RETURNING id""",
            org_id,
        )
    return str(row["id"])


@pytest.mark.asyncio
async def test_inline_script_task_persists_and_reads_back_through_repository(
    db_pool, capable_organization, test_user
):
    """create_task → get_task keeps the inline body byte for byte."""
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(db_pool)
    org_id = str(capable_organization["id"])
    template_id = await _insert_script_template(db_pool, org_id)
    inline = _inline(
        "#!/usr/bin/env python3\nprint('{\"total\": 42}')\n",
        env={"MODE": "report"},
        stdin="input\n",
    )

    try:
        request = await repo.create_request(
            title="inline script roundtrip",
            org_id=org_id,
            created_by=str(test_user["id"]),
        )
        created = await repo.create_task(
            request_id=str(request["id"]),
            title="Run the inline script",
            org_id=org_id,
            task_kind="script",
            execution_config=inline,
            sandbox_template_id=template_id,
        )
        assert created["task_kind"] == "script"
        assert _as_json(created["execution_config"]) == inline

        fetched = await repo.get_task(str(created["id"]), org_id=org_id)
        assert fetched is not None
        assert fetched["task_kind"] == "script"
        assert _as_json(fetched["execution_config"]) == inline
        assert str(fetched["sandbox_template_id"]) == template_id
    finally:
        # The request's created_by FK is restrictive; clear it before the
        # session-level test-user cleanup runs.
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM requests WHERE organization_id = $1::uuid", org_id
            )


@pytest.mark.asyncio
async def test_workflow_script_action_materializes_task_with_inline_config(
    db_pool, capable_organization, test_user
):
    """A stored script action expands into a script task carrying its body."""
    from lucent.api.routers.schedules import _trigger_schedule_execution
    from lucent.db.schedules import ScheduleRepository, webhook_secret_hash

    org_id = str(capable_organization["id"])
    user_id = str(test_user["id"])
    template_id = await _insert_script_template(db_pool, org_id)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_definitions (
                   name, description, content, status, scope,
                   organization_id, created_by, owner_user_id
               )
               VALUES (
                   'code', 'test code agent', 'code agent content', 'active',
                   'instance', $1::uuid, $2::uuid, $2::uuid
               )
               ON CONFLICT (name, organization_id) DO UPDATE
               SET status = 'active', updated_at = now()""",
            org_id,
            user_id,
        )

    inline = _inline(
        "#!/usr/bin/env python3\nprint('{\"total\": 42}')\n",
        env={"MODE": "report"},
    )
    action = {
        "action_type": "task",
        "title": "Compute the report",
        "task_kind": "script",
        "execution_config": inline,
        "sandbox_template_id": template_id,
    }
    repo = ScheduleRepository(db_pool)
    workflow = await repo.create_schedule(
        title="script workflow",
        org_id=org_id,
        schedule_type="webhook",
        trigger_type="webhook",
        created_by=user_id,
        webhook_secret_hash=webhook_secret_hash("script-roundtrip-secret"),
        request_template={"title": "Script run"},
        actions=[action],
    )

    try:
        result = await _trigger_schedule_execution(
            workflow["id"], None, pool=db_pool, force=True
        )
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT task_kind, execution_config, sandbox_template_id
                   FROM tasks WHERE request_id = $1::uuid""",
                result["request"]["id"],
            )
        assert row is not None
        assert row["task_kind"] == "script"
        assert _as_json(row["execution_config"]) == inline
        assert str(row["sandbox_template_id"]) == template_id
    finally:
        async with db_pool.acquire() as conn:
            # schedule_runs.request_id is restrictive; loosen it so the
            # session-level org cleanup can delete the run's request.
            await conn.execute(
                """UPDATE schedule_runs SET request_id = NULL
                   WHERE schedule_id = $1::uuid""",
                workflow["id"],
            )
            await conn.execute(
                "DELETE FROM schedules WHERE id = $1::uuid", workflow["id"]
            )
            # The run's request carries the owner FK; clear it before the
            # session-level test-user cleanup runs.
            await conn.execute(
                "DELETE FROM requests WHERE organization_id = $1::uuid", org_id
            )


# ── custom_sandboxes entitlement: script creation and dispatch ──────────


async def _delete_org_requests(db_pool, org_id: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM requests WHERE organization_id = $1::uuid", org_id
        )


async def _script_request(db_pool, org_id: str, title: str):
    from lucent.db.requests import RequestRepository

    return await RequestRepository(db_pool).create_request(
        title=title, org_id=org_id, created_by=None
    )


@pytest.mark.asyncio
async def test_script_task_creation_refused_without_sandbox_entitlement(
    db_pool, test_organization
):
    """No entitlement row means default-deny, and the error names the capability."""
    from lucent.db.requests import RequestRepository

    org_id = str(test_organization["id"])
    repo = RequestRepository(db_pool)
    template_id = await _insert_script_template(db_pool, org_id)
    request = await _script_request(db_pool, org_id, "script denied")
    try:
        with pytest.raises(ValueError) as raised:
            await repo.create_task(
                request_id=str(request["id"]),
                title="Run the script",
                org_id=org_id,
                task_kind="script",
                execution_config=_inline(),
                sandbox_template_id=template_id,
            )
        message = str(raised.value)
        assert "custom_sandboxes" in message
        assert "not enabled for this organization" in message
        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM tasks WHERE request_id = $1::uuid",
                str(request["id"]),
            )
        assert count == 0, "a refused creation must not leave a task row"
    finally:
        await _delete_org_requests(db_pool, org_id)


@pytest.mark.asyncio
async def test_script_task_creation_allowed_with_sandbox_entitlement(
    db_pool, capable_organization
):
    from lucent.db.requests import RequestRepository

    org_id = str(capable_organization["id"])
    repo = RequestRepository(db_pool)
    template_id = await _insert_script_template(db_pool, org_id)
    request = await _script_request(db_pool, org_id, "script allowed")
    try:
        task = await repo.create_task(
            request_id=str(request["id"]),
            title="Run the script",
            org_id=org_id,
            task_kind="script",
            execution_config=_inline(),
            sandbox_template_id=template_id,
        )
        assert task["task_kind"] == "script"
    finally:
        await _delete_org_requests(db_pool, org_id)


@pytest.mark.asyncio
async def test_script_task_in_plan_refused_without_entitlement(
    db_pool, test_organization
):
    """The bulk authoring path (used by workflow/plan expansion) is gated too."""
    from lucent.db.requests import RequestRepository

    org_id = str(test_organization["id"])
    template_id = await _insert_script_template(db_pool, org_id)
    repo = RequestRepository(db_pool)
    with pytest.raises(ValueError) as raised:
        await repo.create_request_with_tasks(
            org_id=org_id,
            title="script plan",
            task_specs=[
                {
                    "title": "Run the script",
                    "task_kind": "script",
                    "execution_config": _inline(),
                    "sandbox_template_id": template_id,
                }
            ],
        )
    message = str(raised.value)
    assert "custom_sandboxes" in message
    assert "task_specs[0]" in message
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM requests WHERE organization_id = $1::uuid",
            org_id,
        )
    assert count == 0, "a refused plan must not leave a request behind"


@pytest.mark.asyncio
async def test_non_script_task_creation_unaffected_without_entitlement(
    db_pool, test_organization
):
    """The gate binds the script kind only — an agent task still creates."""
    from lucent.db.requests import RequestRepository

    org_id = str(test_organization["id"])
    repo = RequestRepository(db_pool)
    request = await _script_request(db_pool, org_id, "agent task")
    try:
        task = await repo.create_task(
            request_id=str(request["id"]),
            title="Agent task",
            org_id=org_id,
            agent_type="code",
        )
        assert task["task_kind"] == "agent"
    finally:
        await _delete_org_requests(db_pool, org_id)


@pytest.mark.asyncio
async def test_script_dispatch_gate_default_denies_without_row(
    db_pool, test_organization
):
    org_id = str(test_organization["id"])
    failure = await script_executor.script_entitlement_failure(
        {"organization_id": org_id}
    )
    assert failure is not None
    assert failure["stage"] == "script_not_entitled"
    assert failure["capability"] == "custom_sandboxes"
    assert "custom_sandboxes" in failure["detail"]
    assert "not enabled for this organization" in failure["detail"]


@pytest.mark.asyncio
async def test_script_dispatch_gate_refuses_task_without_owning_org():
    failure = await script_executor.script_entitlement_failure(
        {"organization_id": None}
    )
    assert failure is not None
    assert failure["capability"] == "custom_sandboxes"
    assert "no owning organization" in failure["detail"]


@pytest.mark.asyncio
async def test_script_dispatch_gate_allows_entitled_org(
    db_pool, capable_organization
):
    assert (
        await script_executor.script_entitlement_failure(
            {"organization_id": str(capable_organization["id"])}
        )
        is None
    )


@pytest.mark.asyncio
async def test_script_dispatch_gate_refuses_after_revocation(
    db_pool, capable_organization
):
    """Entitled at authoring, revoked before dispatch: the run is refused."""
    from lucent.db import EntitlementRepository

    org_id = str(capable_organization["id"])
    task = {"organization_id": org_id}
    assert await script_executor.script_entitlement_failure(task) is None

    repo = EntitlementRepository(db_pool)
    await repo.set_entitlement(org_id, "custom_sandboxes", False, source="override")
    try:
        failure = await script_executor.script_entitlement_failure(task)
        assert failure is not None and failure["stage"] == "script_not_entitled"
        assert "custom_sandboxes" in failure["detail"]
    finally:
        await repo.set_entitlement(
            org_id, "custom_sandboxes", True, source="override"
        )


@pytest.mark.asyncio
async def test_dispatch_refuses_revoked_org_before_any_sandbox(monkeypatch):
    """A queued script task for a revoked org fails with no sandbox created."""
    from daemon.daemon import LucentDaemon

    task = {
        "id": UUID(TASK_ID),
        "request_id": UUID(REQUEST_ID),
        "organization_id": UUID(ORG_ID),
        "title": "Run the stored script",
        "agent_type": "code",
        "requesting_user_id": UUID(USER_ID),
        "task_kind": "script",
        "execution_config": {"source": "inline", "script": "print(1)"},
        "sandbox_config": _sandbox_config(),
        "sandbox_template_id": UUID(TEMPLATE_ID),
    }
    calls = {
        "script": [],
        "sandbox": [],
        "claimed": [],
        "failed": [],
        "events": [],
    }

    async def _noop(*_args, **_kwargs):
        return None

    async def _pending():
        return [task]

    async def _refusal(_task):
        return {
            "stage": "script_not_entitled",
            "capability": "custom_sandboxes",
            "detail": (
                "The 'custom_sandboxes' capability is not enabled for this "
                "organization"
            ),
        }

    async def _forbidden_script(**_kwargs):
        calls["script"].append(_kwargs)
        raise AssertionError("a refused script task must never reach the executor")

    async def _forbidden_sandbox(*args, **kwargs):
        calls["sandbox"].append((args, kwargs))
        raise AssertionError("a refused script task must never get a sandbox")

    async def _claim(task_id, _instance_id):
        calls["claimed"].append(task_id)
        return {"id": task_id, "organization_id": str(ORG_ID)}

    async def _fail(task_id, error, instance_id=None, result=None):
        calls["failed"].append((task_id, error, result))
        return {"id": task_id}

    async def _event(task_id, event_type, detail=None, metadata=None):
        calls["events"].append(event_type)
        return {"id": task_id}

    daemon = LucentDaemon()
    monkeypatch.setattr(daemon, "_ensure_request_review_tasks", _noop)
    monkeypatch.setattr(daemon, "_create_task_sandbox", _forbidden_sandbox)
    monkeypatch.setattr(script_executor, "run_script_task", _forbidden_script)
    monkeypatch.setattr(
        script_executor, "script_entitlement_failure", _refusal
    )
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_pending_tasks", _pending)
    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", _claim)
    monkeypatch.setattr("daemon.daemon.RequestAPI.fail_task", _fail)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _event)

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert calls["script"] == []
    assert calls["sandbox"] == []
    assert calls["claimed"] == []
    assert len(calls["failed"]) == 1
    _task_id, error, _result = calls["failed"][0]
    assert "custom_sandboxes" in error
    assert "not enabled for this organization" in error
    assert "dispatch_denied" in calls["events"]
    assert "script_dispatched" not in calls["events"]
    assert "sandbox_created" not in calls["events"]



# ── Authoring surfaces: workflow/schedule script actions ────────────────────
#
# A workflow action is stored verbatim in ``schedules.actions`` (JSONB), so a
# script configuration must survive the authoring model, be validated where the
# author can act on the error, and expand into a script task — never a chat
# agent.


def _make_api_client(user):
    """Build an app + client whose auth dependency resolves to ``user``."""
    import httpx
    from httpx import ASGITransport

    from lucent.api.app import create_app
    from lucent.api.deps import CurrentUser, get_current_user

    app = create_app()
    fake_user = CurrentUser(
        id=user["id"],
        organization_id=user["organization_id"],
        role=user.get("role", "member"),
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["daemon-tasks"],
    )

    async def _override_get_current_user():
        return fake_user

    app.dependency_overrides[get_current_user] = _override_get_current_user
    return app, httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


@pytest_asyncio.fixture
async def script_api_client(db_pool, capable_organization, test_user):
    app, client = _make_api_client(test_user)
    async with client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def bare_script_api_client(db_pool, test_organization, test_user):
    """A client whose org holds no ``custom_sandboxes`` entitlement."""
    app, client = _make_api_client(test_user)
    async with client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def script_surface_cleanup(db_pool, capable_organization):
    yield
    org_id = str(capable_organization["id"])
    async with db_pool.acquire() as conn:
        req_ids = [
            row["id"]
            for row in await conn.fetch(
                "SELECT id FROM requests WHERE organization_id = $1::uuid",
                org_id,
            )
        ]
        if req_ids:
            task_ids = [
                row["id"]
                for row in await conn.fetch(
                    "SELECT id FROM tasks WHERE request_id = ANY($1)", req_ids
                )
            ]
            if task_ids:
                await conn.execute(
                    "DELETE FROM task_events WHERE task_id = ANY($1)", task_ids
                )
                await conn.execute(
                    "DELETE FROM task_outputs WHERE task_id = ANY($1)", task_ids
                )
            await conn.execute("DELETE FROM tasks WHERE request_id = ANY($1)", req_ids)
            await conn.execute(
                "UPDATE schedule_runs SET request_id = NULL WHERE request_id = ANY($1)",
                req_ids,
            )
            await conn.execute(
                "DELETE FROM requests WHERE organization_id = $1::uuid", org_id
            )
        await conn.execute(
            "DELETE FROM schedules WHERE organization_id = $1::uuid", org_id
        )


async def _persisted_actions(db_pool, schedule_id: str) -> list[dict]:
    async with db_pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT actions FROM schedules WHERE id = $1::uuid", schedule_id
        )
    return _as_json(raw) or []


@pytest.mark.asyncio
async def test_workflow_api_persists_script_action_kind_and_config(
    script_api_client, db_pool, capable_organization, script_surface_cleanup
):
    """``POST /api/workflows`` must not drop task_kind/execution_config."""
    org_id = str(capable_organization["id"])
    template_id = await _insert_script_template(db_pool, org_id)
    inline = _inline(
        '#!/usr/bin/env python3\nprint(\'{"total": 42}\')\n',
        env={"MODE": "report"},
    )

    resp = await script_api_client.post(
        "/api/workflows",
        json={
            "title": "Script pipeline",
            "trigger_type": "manual",
            "actions": [
                {
                    "action_type": "task",
                    "title": "Compute the report",
                    "task_kind": "script",
                    "execution_config": inline,
                    "sandbox_template_id": template_id,
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    workflow = resp.json()

    stored = _as_json(workflow["actions"])[0]
    assert stored["task_kind"] == "script"
    assert stored["execution_config"] == inline
    assert stored["sandbox_template_id"] == template_id

    persisted = await _persisted_actions(db_pool, workflow["id"])
    assert persisted[0]["task_kind"] == "script"
    assert persisted[0]["execution_config"] == inline
    assert persisted[0]["sandbox_template_id"] == template_id


@pytest.mark.asyncio
async def test_workflow_script_action_expands_to_script_task_without_agent_session(
    script_api_client, db_pool, capable_organization, script_surface_cleanup
):
    """The expanded action is a script task — no agent definition, no session.

    No ``code`` agent definition is created for this org on purpose: a script
    action must not depend on one, and must not fall back to a chat agent.
    """
    org_id = str(capable_organization["id"])
    template_id = await _insert_script_template(db_pool, org_id)
    inline = _inline(
        '#!/usr/bin/env python3\nprint(\'{"total": 42}\')\n',
        env={"MODE": "report"},
    )

    create_resp = await script_api_client.post(
        "/api/workflows",
        json={
            "title": "Script run pipeline",
            "trigger_type": "manual",
            "actions": [
                {
                    "action_type": "task",
                    "title": "Compute the report",
                    "task_kind": "script",
                    "execution_config": inline,
                    "sandbox_template_id": template_id,
                }
            ],
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    workflow = create_resp.json()

    trigger_resp = await script_api_client.post(
        f"/api/workflows/{workflow['id']}/trigger"
    )
    assert trigger_resp.status_code == 200, trigger_resp.text
    triggered = trigger_resp.json()
    assert len(triggered["tasks"]) == 1
    task_id = triggered["tasks"][0]["id"]
    run_id = triggered["run"]["id"]

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            """SELECT task_kind, execution_config, sandbox_template_id,
                      agent_definition_id, agent_type
               FROM tasks WHERE id = $1::uuid""",
            task_id,
        )
        sessions = await conn.fetchval(
            """SELECT COUNT(*) FROM llm_sessions
               WHERE task_id = $1::uuid OR schedule_run_id = $2::uuid""",
            task_id,
            run_id,
        )

    assert task["task_kind"] == "script"
    assert _as_json(task["execution_config"]) == inline
    assert str(task["sandbox_template_id"]) == template_id
    assert task["agent_definition_id"] is None
    # No chat/agent session was created for the expanded action.
    assert sessions == 0


@pytest.mark.asyncio
async def test_workflow_api_refuses_script_action_without_template(
    script_api_client, capable_organization, script_surface_cleanup
):
    resp = await script_api_client.post(
        "/api/workflows",
        json={
            "title": "Untemplated script",
            "trigger_type": "manual",
            "actions": [
                {
                    "action_type": "task",
                    "title": "No template",
                    "task_kind": "script",
                    "execution_config": _inline(),
                }
            ],
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "sandbox_template_id" in detail
    assert "No template" in detail


@pytest.mark.asyncio
async def test_workflow_api_refuses_script_action_with_unresolvable_template(
    script_api_client, db_pool, capable_organization, script_surface_cleanup
):
    missing_template_id = str(__import__("uuid").uuid4())
    resp = await script_api_client.post(
        "/api/workflows",
        json={
            "title": "Missing template script",
            "trigger_type": "manual",
            "actions": [
                {
                    "action_type": "task",
                    "title": "Missing template",
                    "task_kind": "script",
                    "execution_config": _inline(),
                    "sandbox_template_id": missing_template_id,
                }
            ],
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "Missing template" in detail
    assert missing_template_id in detail
    assert "not found or is not cleared" in detail


@pytest.mark.asyncio
async def test_workflow_api_refuses_script_action_without_entitlement(
    bare_script_api_client, db_pool, test_organization
):
    org_id = str(test_organization["id"])
    template_id = await _insert_script_template(db_pool, org_id)
    try:
        resp = await bare_script_api_client.post(
            "/api/workflows",
            json={
                "title": "Unentitled script",
                "trigger_type": "manual",
                "actions": [
                    {
                        "action_type": "task",
                        "title": "Unentitled",
                        "task_kind": "script",
                        "execution_config": _inline(),
                        "sandbox_template_id": template_id,
                    }
                ],
            },
        )
        assert resp.status_code in (409, 422), resp.text
        detail = resp.json()["detail"]
        assert "custom_sandboxes" in detail
        # A script action with no template never reaches the general sandbox
        # gate, so the kind gate itself must name the capability.
        no_template = await bare_script_api_client.post(
            "/api/workflows",
            json={
                "title": "Unentitled script without template",
                "trigger_type": "manual",
                "actions": [
                    {
                        "action_type": "task",
                        "title": "Unentitled no template",
                        "task_kind": "script",
                        "execution_config": _inline(),
                    }
                ],
            },
        )
        assert no_template.status_code == 422, no_template.text
        assert "custom_sandboxes" in no_template.json()["detail"]
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM schedules WHERE organization_id = $1::uuid", org_id
            )


@pytest.mark.asyncio
async def test_schedule_api_refuses_script_action_without_template(
    script_api_client, capable_organization, script_surface_cleanup
):
    """The /schedules surface enforces the same authoring rule."""
    resp = await script_api_client.post(
        "/api/schedules",
        json={
            "title": "Schedule without template",
            "schedule_type": "interval",
            "interval_seconds": 3600,
            "actions": [
                {
                    "action_type": "task",
                    "title": "No template either",
                    "task_kind": "script",
                    "execution_config": _inline(),
                }
            ],
        },
    )
    assert resp.status_code == 422, resp.text
    assert "sandbox_template_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_schedule_update_refuses_script_action_without_template(
    script_api_client, capable_organization, script_surface_cleanup
):
    """Authoring-time validation also covers an update, not only create."""
    created = await script_api_client.post(
        "/api/schedules",
        json={
            "title": "Updatable schedule",
            "schedule_type": "interval",
            "interval_seconds": 3600,
            "actions": [
                {
                    "action_type": "task",
                    "title": "Agent action",
                    "description": "Plain agent work.",
                    "agent_type": "code",
                }
            ],
        },
    )
    assert created.status_code == 200, created.text
    schedule_id = created.json()["id"]

    resp = await script_api_client.put(
        f"/api/schedules/{schedule_id}",
        json={
            "actions": [
                {
                    "action_type": "task",
                    "title": "Now a script",
                    "task_kind": "script",
                    "execution_config": _inline(),
                }
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    assert "sandbox_template_id" in resp.json()["detail"]
