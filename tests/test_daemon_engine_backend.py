"""Behavioral tests for daemon engine-backend forwarding and pin failure."""

from __future__ import annotations

import pytest

import daemon.daemon as daemon_module
from daemon.daemon import LucentDaemon
from lucent.llm.engine_resolution import EngineResolution, PinnedBackendUnavailable


class _FakeEngine:
    name = "codex"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run_session_streaming(self, **kwargs):
        self.calls.append(kwargs)
        return "engine-result"


class _FakeDaemon:
    """Minimal surface ``_run_via_engine`` touches on ``self``."""

    def __init__(self) -> None:
        self.active_sessions: list[str] = []
        self._session_terminal_outcomes: dict = {}
        self._session_mcp_trackers: dict = {}
        self._session_tool_trackers: dict = {}
        self._session_usage_totals: dict = {}

    @staticmethod
    def _is_mcp_auth_failure_message(_message: str | None) -> bool:
        return False


async def _run(fake_self: _FakeDaemon, **kwargs):
    return await LucentDaemon._run_via_engine(fake_self, **kwargs)


@pytest.fixture
def resolution_backend() -> dict:
    return {
        "id": "b-pin",
        "name": "Codex via Ollama",
        "engine": "codex",
        "provider": "ollama",
        "is_enabled": True,
    }


async def test_run_via_engine_forwards_resolved_backend_to_engine(
    monkeypatch, resolution_backend
):
    engine = _FakeEngine()

    async def _fake_resolve_engine(model, **_kwargs):
        return EngineResolution(engine=engine, backend=resolution_backend, source="task_backend")

    monkeypatch.setattr(
        "lucent.llm.engine_resolution.resolve_engine", _fake_resolve_engine
    )
    fake_self = _FakeDaemon()

    result = await _run(
        fake_self,
        name="task-1",
        system_message="system",
        prompt="do the work",
        model="qwen3-coder",
        audit_context={
            "organization_id": "org-1",
            "backend_id": "b-pin",
            "source": "daemon.task",
        },
    )

    assert result == "engine-result"
    assert len(engine.calls) == 1
    forwarded = engine.calls[0]["audit_context"]
    assert forwarded["engine_backend"] is resolution_backend
    # The original pin/context is preserved alongside the backend row.
    assert forwarded["backend_id"] == "b-pin"
    assert forwarded["organization_id"] == "org-1"


async def test_run_via_engine_without_backend_does_not_inject_backend(
    monkeypatch,
):
    engine = _FakeEngine()

    async def _fake_resolve_engine(model, **_kwargs):
        return EngineResolution(engine=engine, backend=None, source="default")

    monkeypatch.setattr(
        "lucent.llm.engine_resolution.resolve_engine", _fake_resolve_engine
    )

    await _run(
        _FakeDaemon(),
        name="task-2",
        system_message="system",
        prompt="hello",
        model="gpt-5.1",
    )

    assert "engine_backend" not in engine.calls[0]["audit_context"]


async def test_run_via_engine_reraises_resolution_failure_when_pinned(monkeypatch):
    engine = _FakeEngine()

    async def _fail_resolution(model, **_kwargs):
        raise PinnedBackendUnavailable("Pinned backend 'b-pin' is missing")

    monkeypatch.setattr(
        "lucent.llm.engine_resolution.resolve_engine", _fail_resolution
    )

    with pytest.raises(PinnedBackendUnavailable, match="b-pin"):
        await _run(
            _FakeDaemon(),
            name="task-3",
            system_message="system",
            prompt="do the work",
            model="qwen3-coder",
            audit_context={"organization_id": "org-1", "backend_id": "b-pin"},
        )

    # No silent fallback: the engine must never have been invoked.
    assert engine.calls == []


async def test_run_via_engine_falls_back_when_unpinned_resolution_fails(monkeypatch):
    engine = _FakeEngine()

    async def _fail_resolution(model, **_kwargs):
        raise RuntimeError("backend lookup exploded")

    monkeypatch.setattr(
        "lucent.llm.engine_resolution.resolve_engine", _fail_resolution
    )
    monkeypatch.setattr(daemon_module, "get_engine_for_model", lambda _model: engine)

    result = await _run(
        _FakeDaemon(),
        name="task-4",
        system_message="system",
        prompt="hello",
        model="gpt-5.1",
        audit_context={"organization_id": "org-1"},
    )

    assert result == "engine-result"
    assert len(engine.calls) == 1
    assert "engine_backend" not in engine.calls[0]["audit_context"]
