"""Unit tests for the Codex engine (SDK faked)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lucent.llm import codex_engine as ce
from lucent.llm.engine import SessionEventType


class _FakeSandbox:
    read_only = "read-only"
    workspace_write = "workspace-write"


class _FakeCodexConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeHandle:
    def __init__(self, notes):
        self._notes = notes

    async def stream(self):
        for note in self._notes:
            yield note


class _FakeThread:
    def __init__(self, notes, final_response="done"):
        self.id = "thr_1"
        self._notes = notes
        self._final = final_response

    async def turn(self, text):
        self.prompt = text
        return _FakeHandle(self._notes)

    async def run(self, text):
        self.prompt = text
        return SimpleNamespace(final_response=self._final, items=[], usage=None)


class _FakeCodex:
    """Records constructor/thread kwargs for assertions."""

    created: list["_FakeCodex"] = []

    def __init__(self, config=None):
        self.config = config
        self.notes: list = []
        self.final = "done"
        self.start_kwargs: dict = {}
        self.resume_calls: list = []
        _FakeCodex.created.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def thread_start(self, **kwargs):
        self.start_kwargs = kwargs
        return _FakeThread(self.notes, self.final)

    async def thread_resume(self, thread_id, **kwargs):
        self.resume_calls.append((thread_id, kwargs))
        return _FakeThread(self.notes, self.final)


@pytest.fixture
def fake_sdk(monkeypatch):
    _FakeCodex.created.clear()
    monkeypatch.setattr(ce, "_sdk_available", True)
    monkeypatch.setattr(ce, "_AsyncCodex", _FakeCodex)
    monkeypatch.setattr(ce, "_CodexConfig", _FakeCodexConfig)
    monkeypatch.setattr(ce, "_Sandbox", _FakeSandbox)
    return _FakeCodex


def _note(method, **payload):
    return SimpleNamespace(method=method, payload=SimpleNamespace(**payload))


async def test_streaming_maps_message_delta_and_final(fake_sdk):
    engine = ce.CodexEngine()
    events = []

    def collect(event):
        events.append(event)

    # Configure the fake to emit notes.
    orig_init = _FakeCodex.__init__

    def _init(self, config=None):
        orig_init(self, config)
        self.notes = [
            _note("item/agentMessage/delta", delta="Hel"),
            _note("item/agentMessage/delta", delta="lo"),
            _note("item/completed", item=SimpleNamespace(type="agent_message", text="Hello")),
            _note("turn/completed"),
        ]

    fake_sdk.__init__ = _init
    try:
        result = await engine.run_session_streaming(
            model="gpt-5.1", system_message="sys", prompt="hi", on_event=collect
        )
    finally:
        fake_sdk.__init__ = orig_init

    assert result == "Hello"
    types = [e.type for e in events]
    assert SessionEventType.MESSAGE_DELTA in types
    assert SessionEventType.MESSAGE in types
    assert SessionEventType.SESSION_IDLE in types


async def test_local_provider_injection_sets_model_provider_and_env(fake_sdk):
    engine = ce.CodexEngine()
    backend = {"engine": "codex", "provider": "ollama", "config": {"base_url": "http://host:11434"}}
    await engine.run_session(
        model="llama3",
        system_message="",
        prompt="hi",
        audit_context={"engine_backend": backend},
    )
    codex = fake_sdk.created[-1]
    assert codex.start_kwargs["model_provider"] == "ollama"
    assert codex.start_kwargs["sandbox"] == "workspace-write"
    assert codex.config.kwargs["env"] == {"OLLAMA_HOST": "http://host:11434"}


async def test_read_only_sandbox_when_permissions_denied(fake_sdk):
    engine = ce.CodexEngine()
    await engine.run_session(
        model="gpt-5.1", system_message="", prompt="hi", approve_permissions=False
    )
    assert fake_sdk.created[-1].start_kwargs["sandbox"] == "read-only"


async def test_mcp_config_flows_into_config(fake_sdk):
    engine = ce.CodexEngine()
    mcp = {
        "memory-server": {
            "type": "http",
            "url": "http://mcp",
            "headers": {"A": "b"},
            "internal": True,
        }
    }
    await engine.run_session(model="gpt-5.1", system_message="", prompt="hi", mcp_config=mcp)
    config = fake_sdk.created[-1].start_kwargs["config"]
    assert config["mcp_servers"]["memory-server"] == {
        "url": "http://mcp",
        "http_headers": {"A": "b"},
    }


async def test_blocking_returns_final_response(fake_sdk):
    engine = ce.CodexEngine()
    result = await engine.run_session(model="gpt-5.1", system_message="", prompt="hi")
    assert result == "done"


async def test_resume_uses_thread_resume(fake_sdk):
    engine = ce.CodexEngine()
    await engine.run_session(
        model="gpt-5.1",
        system_message="",
        prompt="hi",
        provider_session_id="thr_x",
        resume=True,
    )
    assert fake_sdk.created[-1].resume_calls[0][0] == "thr_x"


async def test_error_notification_returns_none(fake_sdk):
    engine = ce.CodexEngine()
    orig_init = _FakeCodex.__init__

    def _init(self, config=None):
        orig_init(self, config)
        self.notes = [_note("error", error=SimpleNamespace(message="boom"))]

    fake_sdk.__init__ = _init
    try:
        result = await engine.run_session_streaming(model="gpt-5.1", system_message="", prompt="hi")
    finally:
        fake_sdk.__init__ = orig_init
    assert result is None


async def test_usage_notification_emits_usage(fake_sdk):
    engine = ce.CodexEngine()
    events = []
    orig_init = _FakeCodex.__init__

    def _init(self, config=None):
        orig_init(self, config)
        self.notes = [
            _note(
                "thread/tokenUsage/updated",
                token_usage={"input_tokens": 10, "output_tokens": 5},
            ),
            _note("item/completed", item=SimpleNamespace(type="agent_message", text="ok")),
        ]

    fake_sdk.__init__ = _init
    try:
        await engine.run_session_streaming(
            model="gpt-5.1", system_message="", prompt="hi", on_event=events.append
        )
    finally:
        fake_sdk.__init__ = orig_init
    usage_events = [e for e in events if e.type == SessionEventType.USAGE]
    assert usage_events and usage_events[0].usage["input_tokens"] == 10


def test_engine_name():
    assert ce.CodexEngine().name == "codex"


# ── native provider credential wiring ────────────────────────────────────────


async def test_native_env_reads_provider_credential(monkeypatch):
    import lucent.model_discovery as md

    async def _fake(provider, *, org_id=None):
        return "sk-openai" if provider == "openai" else ""

    monkeypatch.setattr(md, "resolve_provider_credential", _fake)
    env = await ce.CodexEngine()._native_env({"organization_id": "o1"})
    assert env == {"OPENAI_API_KEY": "sk-openai"}


def test_thread_kwargs_applies_native_env_when_not_local(fake_sdk):
    _, env = ce.CodexEngine()._thread_kwargs(
        model="m",
        mcp_config=None,
        reasoning_effort=None,
        approve_permissions=True,
        audit_context=None,
        native_env={"OPENAI_API_KEY": "k"},
    )
    assert env == {"OPENAI_API_KEY": "k"}


def test_thread_kwargs_ignores_native_env_for_local_backend(fake_sdk):
    backend = {"engine": "codex", "provider": "ollama", "config": {"base_url": "http://h:11434"}}
    _, env = ce.CodexEngine()._thread_kwargs(
        model="m",
        mcp_config=None,
        reasoning_effort=None,
        approve_permissions=True,
        audit_context={"engine_backend": backend},
        native_env={"OPENAI_API_KEY": "should-not-win"},
    )
    assert "OPENAI_API_KEY" not in env
    assert env.get("OLLAMA_HOST") == "http://h:11434"
