"""Unit tests for the Claude Code engine (SDK faked)."""

from __future__ import annotations

import pytest

from lucent.llm import claude_code_engine as cce
from lucent.llm.engine import SessionEventType

# ── fake SDK message types (names must match the ones the engine switches on) ──


class TextBlock:
    def __init__(self, text):
        self.text = text


class ThinkingBlock:
    def __init__(self, thinking):
        self.thinking = thinking


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class ToolResultBlock:
    def __init__(self, tool_use_id, content):
        self.tool_use_id = tool_use_id
        self.content = content


class AssistantMessage:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage


class StreamEvent:
    def __init__(self, event):
        self.event = event


class ResultMessage:
    def __init__(self, result=None, is_error=False, usage=None):
        self.result = result
        self.is_error = is_error
        self.usage = usage


class SystemMessage:
    def __init__(self, data=None):
        self.data = data


class _FakeOptions:
    instances: list["_FakeOptions"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeOptions.instances.append(self)


@pytest.fixture
def fake_sdk(monkeypatch):
    _FakeOptions.instances.clear()
    monkeypatch.setattr(cce, "_sdk_available", True)
    monkeypatch.setattr(cce, "_ClaudeAgentOptions", _FakeOptions)
    return _FakeOptions


def _install_query(monkeypatch, messages):
    async def _query(*, prompt, options=None):
        for m in messages:
            yield m

    monkeypatch.setattr(cce, "_query", _query)


async def test_text_block_and_result_final(fake_sdk, monkeypatch):
    _install_query(
        monkeypatch,
        [
            AssistantMessage([TextBlock("Hello "), TextBlock("world")]),
            ResultMessage(result="Hello world"),
        ],
    )
    events = []
    engine = cce.ClaudeCodeEngine()
    result = await engine.run_session_streaming(
        model="claude-sonnet-4.5", system_message="sys", prompt="hi", on_event=events.append
    )
    assert result == "Hello world"
    assert any(e.type == SessionEventType.MESSAGE and e.content == "Hello " for e in events)
    assert SessionEventType.SESSION_IDLE in [e.type for e in events]


async def test_tool_use_and_result_blocks(fake_sdk, monkeypatch):
    _install_query(
        monkeypatch,
        [
            AssistantMessage(
                [
                    ToolUseBlock("t1", "Read", {"path": "/x"}),
                    ToolResultBlock("t1", [{"type": "text", "text": "file body"}]),
                ]
            ),
            ResultMessage(result="done"),
        ],
    )
    events = []
    engine = cce.ClaudeCodeEngine()
    await engine.run_session_streaming(
        model="claude-sonnet-4.5", system_message="", prompt="hi", on_event=events.append
    )
    tool_calls = [e for e in events if e.type == SessionEventType.TOOL_CALL]
    tool_results = [e for e in events if e.type == SessionEventType.TOOL_RESULT]
    assert tool_calls and tool_calls[0].tool_name == "Read"
    assert tool_calls[0].tool_input == {"path": "/x"}
    assert tool_results and tool_results[0].tool_output == "file body"


async def test_thinking_block_is_reasoning(fake_sdk, monkeypatch):
    _install_query(
        monkeypatch, [AssistantMessage([ThinkingBlock("hmm")]), ResultMessage(result="ok")]
    )
    events = []
    engine = cce.ClaudeCodeEngine()
    await engine.run_session_streaming(
        model="m", system_message="", prompt="hi", on_event=events.append
    )
    reasoning = [e for e in events if e.type == SessionEventType.OTHER]
    assert reasoning and reasoning[0].tool_name == "_reasoning"


async def test_local_provider_injection_sets_anthropic_base_url(fake_sdk, monkeypatch):
    _install_query(monkeypatch, [ResultMessage(result="ok")])
    engine = cce.ClaudeCodeEngine()
    backend = {
        "engine": "claude_code",
        "provider": "ollama",
        "config": {"base_url": "http://host:11434"},
    }
    await engine.run_session(
        model="deepseek",
        system_message="",
        prompt="hi",
        audit_context={"engine_backend": backend},
    )
    opts = fake_sdk.instances[-1].kwargs
    assert opts["env"]["ANTHROPIC_BASE_URL"] == "http://host:11434"
    assert opts["env"]["ANTHROPIC_AUTH_TOKEN"] == "ollama"


async def test_mcp_config_translated_to_claude_shape(fake_sdk, monkeypatch):
    _install_query(monkeypatch, [ResultMessage(result="ok")])
    engine = cce.ClaudeCodeEngine()
    mcp = {
        "memory-server": {
            "type": "http",
            "url": "http://mcp",
            "headers": {"A": "b"},
            "internal": True,
        },
        "npx-thing": {"type": "stdio", "command": "npx", "args": ["-y", "x"], "env": {"K": "v"}},
    }
    await engine.run_session(model="m", system_message="", prompt="hi", mcp_config=mcp)
    servers = fake_sdk.instances[-1].kwargs["mcp_servers"]
    assert servers["memory-server"] == {"type": "http", "url": "http://mcp", "headers": {"A": "b"}}
    assert servers["npx-thing"] == {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "x"],
        "env": {"K": "v"},
    }


async def test_permission_mode_mapping(fake_sdk, monkeypatch):
    _install_query(monkeypatch, [ResultMessage(result="ok")])
    engine = cce.ClaudeCodeEngine()
    await engine.run_session(model="m", system_message="", prompt="hi", approve_permissions=False)
    assert fake_sdk.instances[-1].kwargs["permission_mode"] == "default"
    await engine.run_session(model="m", system_message="", prompt="hi", approve_permissions=True)
    assert fake_sdk.instances[-1].kwargs["permission_mode"] == "acceptEdits"


async def test_session_id_and_resume_wiring(fake_sdk, monkeypatch):
    _install_query(monkeypatch, [ResultMessage(result="ok")])
    engine = cce.ClaudeCodeEngine()
    await engine.run_session(model="m", system_message="", prompt="hi", provider_session_id="s1")
    assert fake_sdk.instances[-1].kwargs["session_id"] == "s1"
    await engine.run_session(
        model="m", system_message="", prompt="hi", provider_session_id="s1", resume=True
    )
    assert fake_sdk.instances[-1].kwargs["resume"] == "s1"


async def test_stream_event_delta(fake_sdk, monkeypatch):
    _install_query(
        monkeypatch,
        [
            StreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "chunk"}}
            ),
            ResultMessage(result="chunk"),
        ],
    )
    events = []
    engine = cce.ClaudeCodeEngine()
    await engine.run_session_streaming(
        model="m", system_message="", prompt="hi", on_event=events.append
    )
    deltas = [e for e in events if e.type == SessionEventType.MESSAGE_DELTA]
    assert deltas and deltas[0].content == "chunk"


async def test_error_result_returns_none(fake_sdk, monkeypatch):
    _install_query(monkeypatch, [ResultMessage(result="bad", is_error=True)])
    engine = cce.ClaudeCodeEngine()
    result = await engine.run_session(model="m", system_message="", prompt="hi")
    assert result is None


def test_engine_name():
    assert cce.ClaudeCodeEngine().name == "claude_code"


# ── native provider credential wiring ────────────────────────────────────────


async def test_native_env_reads_provider_credential(monkeypatch):
    import lucent.model_discovery as md

    async def _fake(provider, *, org_id=None):
        return "sk-ant" if provider == "anthropic" else ""

    monkeypatch.setattr(md, "resolve_provider_credential", _fake)
    env = await cce.ClaudeCodeEngine()._native_env({"organization_id": "o1"})
    assert env == {"ANTHROPIC_API_KEY": "sk-ant"}


def _native_options(monkeypatch_engine, audit_context, native_env):
    return monkeypatch_engine._build_options(
        model="m",
        system_message="",
        mcp_config=None,
        reasoning_effort=None,
        approve_permissions=True,
        provider_session_id=None,
        resume=False,
        audit_context=audit_context,
        include_partial=False,
        native_env=native_env,
    )


def test_build_options_applies_native_env_when_not_local(fake_sdk):
    opts = _native_options(cce.ClaudeCodeEngine(), None, {"ANTHROPIC_API_KEY": "k"})
    assert opts.kwargs["env"]["ANTHROPIC_API_KEY"] == "k"


def test_build_options_ignores_native_env_for_local_backend(fake_sdk):
    backend = {"engine": "claude_code", "provider": "ollama", "config": {"base_url": "http://h:11434"}}
    opts = _native_options(
        cce.ClaudeCodeEngine(),
        {"engine_backend": backend},
        {"ANTHROPIC_API_KEY": "should-not-win"},
    )
    env = opts.kwargs["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://h:11434"
    assert env["ANTHROPIC_API_KEY"] == ""
