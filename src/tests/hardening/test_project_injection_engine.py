"""Projects v2 injection: system-prompt native project context (2026-09-12).

Covers the engine-path redesign that replaced v1's hook-mediated project
injection:

- ``lucent.llm.project_context`` renders the metadata-only standing block
  (``## Project: <name>`` + instructions verbatim + one manifest line per
  file) directly into the system message in ``LangChainEngine._run_with_tools``.
- No file contents ever appear in the block; the manifest carries name, file
  id, and a one-line description only.
- Fail-closed: absent/malformed threaded context or a disabled runtime gate
  yields no block and no event.
- The engine emits the ``project_context`` observability event (name, file
  count, block size) so the chat chip keeps working through the same
  ``_hook`` event funnel.
- The v1 16k-token budget machinery is gone: no budget setting, no
  retrieval-select helpers, no hook definition, no hook dispatch branch.

Engine tests drive the real ``_run_with_tools`` with the chat model stubbed
(the same seams the production loop uses — no server, no network, no model).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lucent.llm.langchain_engine import LangChainEngine
from lucent.llm.project_context import (
    PROJECT_CONTEXT_HOOK_NAME,
    project_context_event_metadata,
    render_project_context_block,
)

ORG_ID = "0f9abaa4-7489-47ab-8d6c-7c5be8d69d51"
USER_ID = "7b2129ec-392c-4787-85b7-184ae8c23ed4"

PROJECT_CTX = {
    "project": {
        "id": "aaaaaaaa-0000-0000-0000-000000000001",
        "name": "Apollo",
        "instructions": "Answer only from the mission specs.\nBe terse.",
    },
    "files": [
        {
            "id": "bbbbbbbb-0000-0000-0000-000000000001",
            "filename": "spec.md",
            "display_name": "Mission spec",
            "mime_type": "text/markdown",
            "size_bytes": 123456,
            "metadata": {"description": "The full mission specification"},
        },
        {
            "id": "bbbbbbbb-0000-0000-0000-000000000002",
            "filename": "telemetry.csv",
            "display_name": "telemetry.csv",
            "mime_type": "text/csv",
            "size_bytes": 9876543,
            "metadata": {},
        },
    ],
    "memory_ids": ["cccccccc-0000-0000-0000-000000000001"],
}


def _state(ctx: Any) -> dict[str, Any]:
    return {"_project_context": ctx}


# ── Renderer: block format ─────────────────────────────────────────────────


def test_block_contains_header_instructions_and_manifest_ids():
    block = render_project_context_block(_state(PROJECT_CTX))
    assert block is not None
    assert block.startswith("## Project: Apollo\n")
    assert "Answer only from the mission specs.\nBe terse." in block
    assert "## Project files" in block
    # One line per file with name, file_id, and description — never contents.
    assert (
        "- Mission spec (file_id: bbbbbbbb-0000-0000-0000-000000000001) — "
        "The full mission specification" in block
    )
    # display_name == filename → no redundant label; description falls back.
    assert "telemetry.csv (file_id: bbbbbbbb-0000-0000-0000-000000000002)" in block


def test_block_never_carries_file_contents():
    big = PROJECT_CTX["files"][0]["metadata"]["description"]
    block = render_project_context_block(_state(PROJECT_CTX))
    assert block is not None
    # The huge size_bytes file contributes exactly one manifest line.
    file_lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert len(file_lines) == 2
    for line in file_lines:
        assert "file_id:" in line
        assert len(line) < 300
    # No markdown body, no CSV rows: nothing that looks like file content.
    assert "```" not in block
    assert big == "The full mission specification"  # sanity on the fixture


def test_block_instructions_verbatim():
    ctx = {
        "project": {"id": "p1", "name": "X", "instructions": "Line one\n\nLine two"},
        "files": [],
    }
    block = render_project_context_block(_state(ctx))
    assert "Line one\n\nLine two" in block
    assert "No durable files are attached to this project yet." in block


def test_block_without_instructions_and_without_files():
    ctx = {"project": {"id": "p1", "name": "Bare"}, "files": [], "memory_ids": []}
    block = render_project_context_block(_state(ctx))
    assert block is not None
    assert block.startswith("## Project: Bare")
    assert "No standing instructions" in block
    assert "No durable files" in block


def test_block_scales_with_file_count_not_size():
    files = [
        {
            "id": f"bbbbbbbb-0000-0000-0000-{i:012d}",
            "filename": f"f{i}.txt",
            "display_name": f"f{i}.txt",
            "mime_type": "text/plain",
            "size_bytes": 50 * 1024 * 1024,  # 50 MB each — size must not matter
            "metadata": {},
        }
        for i in range(20)
    ]
    ctx = {"project": {"id": "p1", "name": "Scale", "instructions": "do"}, "files": files}
    block = render_project_context_block(_state(ctx))
    assert block is not None
    lines = block.splitlines()
    assert sum(1 for ln in lines if ln.startswith("- ")) == 20
    # Metadata-only: 21 content lines total (header block + 20 manifest lines).
    assert len([ln for ln in lines if ln.strip()]) <= 24
    assert len(block.encode("utf-8")) < 3000


# ── Fail-closed + scoping-by-construction ─────────────────────────────────


def test_no_state_no_block():
    assert render_project_context_block(None) is None
    assert render_project_context_block({}) is None


@pytest.mark.parametrize(
    "bad",
    [
        "junk",
        {},
        {"project": None},
        {"project": {}},
        {"project": {"name": "no id"}},
        {"_project_context": "wrong nesting"},
    ],
)
def test_malformed_context_fail_closed(bad):
    state = {"_project_context": bad} if isinstance(bad, dict) else None
    if state is None:
        state = bad
    assert render_project_context_block(state) is None


def test_non_list_files_renders_defensively():
    """Malformed files value: no crash, no contents, project still renders."""
    state = {"_project_context": {"project": {"id": "p1", "name": "X"}, "files": "not-a-list"}}
    block = render_project_context_block(state)
    assert block is not None
    assert "file_id:" not in block
    assert "not-a-list" not in block


def test_disabled_gate_fail_closed():
    from lucent import settings as runtime_settings

    runtime_settings.set_runtime_setting_cache(
        ORG_ID, "projects.context_enabled", False
    )
    try:
        audit = {"organization_id": ORG_ID, "user_id": USER_ID}
        assert render_project_context_block(_state(PROJECT_CTX), audit_context=audit) is None
    finally:
        runtime_settings.clear_runtime_setting_cache(organization_id=ORG_ID)


def test_gate_scoped_to_calling_org():
    from lucent import settings as runtime_settings

    runtime_settings.set_runtime_setting_cache(
        ORG_ID, "projects.context_enabled", False
    )
    try:
        other_audit = {"organization_id": "eeeeeeee-0000-0000-0000-000000000009"}
        assert (
            render_project_context_block(_state(PROJECT_CTX), audit_context=other_audit)
            is not None
        )
    finally:
        runtime_settings.clear_runtime_setting_cache(organization_id=ORG_ID)


def test_metadata_never_interpreted_as_content():
    ctx = {
        "project": {"id": "p1", "name": "N", "instructions": ""},
        "files": [
            {
                "id": "f1",
                "filename": "a.md",
                "display_name": "a.md",
                "metadata": "not-json",
            }
        ],
    }
    block = render_project_context_block(_state(ctx))
    assert block is not None
    assert "- a.md (file_id: f1)" in block


# ── Event metadata (chip observability) ───────────────────────────────────


def test_event_metadata_shape():
    block = render_project_context_block(_state(PROJECT_CTX))
    meta = project_context_event_metadata(_state(PROJECT_CTX), block=block)
    assert meta["project_id"] == "aaaaaaaa"
    assert meta["name"] == "Apollo"
    assert meta["file_count"] == 2
    assert meta["bytes"] == len(block.encode("utf-8"))


def test_emit_event_through_engine_funnel():
    events: list[Any] = []
    from lucent.llm.engine import SessionEventType

    class _Sink:
        def __call__(self, event):
            events.append(event)

    block = render_project_context_block(_state(PROJECT_CTX))
    from lucent.llm.project_context import emit_project_context_event

    emit_project_context_event(_Sink := _Sink(), _state(PROJECT_CTX), block=block)
    assert len(events) == 1
    event = events[0]
    assert event.tool_name == "_hook"
    assert event.raw["hook"] == PROJECT_CONTEXT_HOOK_NAME
    assert event.raw["file_count"] == 2
    assert event.raw["bytes"] == len(block.encode("utf-8"))
    assert event.content.startswith("## Project: Apollo")


def test_emit_event_safe_when_no_callback():
    from lucent.llm.project_context import emit_project_context_event

    block = render_project_context_block(_state(PROJECT_CTX))
    emit_project_context_event(None, _state(PROJECT_CTX), block=block)  # no-op


# ── Engine loop: block in system prompt, refresh, no hook mediation ───────


class _AIMessageStub:
    def __init__(self, content: str = "ok"):
        self.content = content
        self.tool_calls = []


class _FakeChatModel:
    """Minimal chat model: echoes the system prompt's project block."""

    last_messages: list = []

    last_messages: list = []

    def bind_tools(self, schemas):
        return self

    def astream(self, messages):
        _FakeChatModel.last_messages = messages

        async def _gen():
            yield _AIMessageStub(content="ok")

        return _gen()

    async def ainvoke(self, messages, **kwargs):
        _FakeChatModel.last_messages = messages

        return _AIMessageStub()


@pytest.fixture
def stubbed_engine(monkeypatch):
    from lucent.llm import langchain_engine as le

    async def fake_get_chat_model(*args, **kwargs):
        return _FakeChatModel()

    monkeypatch.setattr(le, "_get_chat_model", fake_get_chat_model)
    events: list[Any] = []
    return le, events


def _events_recorder(events):
    from lucent.llm.engine import SessionEvent

    def on_event(event: SessionEvent) -> None:
        events.append(event)

    return on_event


@pytest.mark.asyncio
async def test_engine_appends_project_block_to_system_prompt(stubbed_engine):
    le, events = stubbed_engine
    engine = LangChainEngine()
    result = await engine._run_with_tools(
        model="glm-5.3-flash:cloud",
        system_message="You are Lucent.",
        prompt="hello",
        session_state=_state(PROJECT_CTX),
        audit_context={"organization_id": ORG_ID, "user_id": USER_ID},
        on_event=_events_recorder(events),
    )
    assert result == "ok"
    system_text = _FakeChatModel.last_messages[0].content
    assert system_text.startswith("You are Lucent.")  # append-only: prompt first
    assert "## Project: Apollo" in system_text
    assert "file_id: bbbbbbbb-0000-0000-0000-000000000001" in system_text
    # Observability event emitted from the injection path.
    hook_events = [e for e in events if (e.raw or {}).get("hook") == "project_context"]
    assert len(hook_events) == 1
    assert hook_events[0].raw["file_count"] == 2
    assert hook_events[0].raw["bytes"] > 0


@pytest.mark.asyncio
async def test_engine_refreshes_block_every_turn(stubbed_engine):
    """New snapshot in session_state → new block next turn (per-turn refresh)."""
    le, events = stubbed_engine
    engine = LangChainEngine()
    await engine._run_with_tools(
        model="glm-5.3-flash:cloud",
        system_message="You are Lucent.",
        prompt="turn 1",
        session_state=_state(PROJECT_CTX),
        audit_context={"organization_id": ORG_ID, "user_id": USER_ID},
    )
    first = _FakeChatModel.last_messages[0].content
    assert "## Project: Apollo" in first

    edited = json.loads(json.dumps(PROJECT_CTX))
    edited["project"]["instructions"] = "UPDATED INSTRUCTION"
    edited["project"]["name"] = "Apollo Renamed"
    edited["files"] = edited["files"][:1]
    await engine._run_with_tools(
        model="glm-5.3-flash:cloud",
        system_message="You are Lucent.",
        prompt="turn 2",
        session_state=_state(edited),
        audit_context={"organization_id": ORG_ID, "user_id": USER_ID},
    )
    second = _FakeChatModel.last_messages[0].content
    assert "## Project: Apollo Renamed" in second
    assert "UPDATED INSTRUCTION" in second
    assert "telemetry.csv" not in second  # file move reflects next turn


@pytest.mark.asyncio
async def test_engine_no_block_for_unfiled_chat(stubbed_engine):
    le, events = stubbed_engine
    engine = LangChainEngine()
    result = await engine._run_with_tools(
        model="glm-5.3-flash:cloud",
        system_message="You are Lucent.",
        prompt="hello",
        session_state={"_latest_user_text": "hi"},
        audit_context={"organization_id": ORG_ID, "user_id": USER_ID},
        on_event=_events_recorder(events),
    )
    assert result == "ok"
    assert "## Project:" not in _FakeChatModel.last_messages[0].content
    assert not [e for e in events if (e.raw or {}).get("hook") == "project_context"]


@pytest.mark.asyncio
async def test_engine_disabled_gate_no_block_no_event(stubbed_engine):
    le, events = stubbed_engine
    from lucent import settings as runtime_settings

    runtime_settings.set_runtime_setting_cache(
        ORG_ID, "projects.context_enabled", False
    )
    try:
        engine = LangChainEngine()
        result = await engine._run_with_tools(
            model="glm-5.3-flash:cloud",
            system_message="You are Lucent.",
            prompt="hello",
            session_state=_state(PROJECT_CTX),
            audit_context={"organization_id": ORG_ID, "user_id": USER_ID},
            on_event=_events_recorder(events),
        )
        assert result == "ok"
        assert "## Project:" not in _FakeChatModel.last_messages[0].content
        assert not [e for e in events if (e.raw or {}).get("hook") == "project_context"]
    finally:
        runtime_settings.clear_runtime_setting_cache(organization_id=ORG_ID)


@pytest.mark.asyncio
async def test_engine_block_composes_alongside_memory_hooks(stubbed_engine, monkeypatch):
    """Append-only: global prompt + hook memory injection + project block."""
    le, events = stubbed_engine
    engine = LangChainEngine()

    class _Bridge:
        calls: list[tuple[str, dict[str, Any]]] = []

        async def call_tool(self, name, payload):
            self.calls.append((name, payload))
            return json.dumps({
                "memories": [
                    {
                        "id": "dddddddd-0000-0000-0000-000000000001",
                        "content": "memory standing fact",
                        "tags": ["t"],
                        "similarity_score": 0.9,
                    }
                ]
            })

        async def close(self):
            return None

    async def fake_create_bridges(self, mcp_config, audit_context=None):
        bridge = _Bridge()
        return [bridge], {"search_memories": bridge}, bridge, []

    monkeypatch.setattr(le.LangChainEngine, "_create_bridges", fake_create_bridges)
    hooks = [{
        "name": "message-memory-lookup",
        "trigger_event": "before_model_call",
        "action_type": "message_memory_lookup",
        "content": "",
        "config": {"max_memories": 3},
    }]
    await engine._run_with_tools(
        model="glm-5.3-flash:cloud",
        system_message="You are Lucent.",
        prompt="what do you remember about threshold filtering?",
        mcp_config={"memory-server": {"url": "http://unused"}},
        session_state=_state(PROJECT_CTX),
        audit_context={"organization_id": ORG_ID, "user_id": USER_ID},
        hooks=hooks,
    )
    # The memory hook runs against the fake bridge; both the hook injection
    # and the project block must coexist in the model-visible context.
    system_text = _FakeChatModel.last_messages[0].content
    assert "## Project: Apollo" in system_text
    hook_context = [
        m for m in _FakeChatModel.last_messages if getattr(m, "type", "") == "system"
    ]
    assert hook_context, "hook injection appends a second system message"
    combined = system_text + "\n" + "\n".join(
        str(getattr(m, "content", "")) for m in hook_context
    )
    assert "memory standing fact" in combined
    # Append-only: the project block follows the global prompt, hook context
    # is a separate system message — neither replaced the other.
    assert system_text.index("You are Lucent.") < system_text.index("## Project: Apollo")


def test_hook_payload_promotes_project_metadata():
    """chat._hook_event_payload promotes name/file_count/bytes to the payload
    top level so the chip renders the project summary identically on live
    SSE payloads and replayed persisted rows (same contract as phase/
    decision/memory_count promotion)."""
    from types import SimpleNamespace

    from lucent.api.routers.chat import _hook_event_payload

    event = SimpleNamespace(
        raw={
            "hook": "project_context",
            "phase": "system_prompt",
            "decision": "inject",
            "project_id": "323a9d36",
            "name": "Apollo Mission",
            "file_count": 2,
            "bytes": 498,
        },
        content="## Project: Apollo Mission\n…",
    )
    payload = _hook_event_payload(event)
    assert payload["type"] == "hook_context"
    assert payload["hook"] == "project_context"
    assert payload["name"] == "Apollo Mission"
    assert payload["file_count"] == 2
    assert payload["bytes"] == 498
    assert payload["phase"] == "system_prompt"
    # Metadata copy preserved (legacy consumers read raw.metadata).
    assert payload["metadata"]["file_count"] == 2

    # Memory-hook events carry none of these keys — promotion stays absent
    # rather than writing nulls.
    memory_event = SimpleNamespace(
        raw={"hook": "message_memory_lookup", "memory_count": 2},
        content="memory block",
    )
    memory_payload = _hook_event_payload(memory_event)
    assert "name" not in memory_payload
    assert "file_count" not in memory_payload
    assert "bytes" not in memory_payload


# ── v1 removal: hook-mediated injection must be gone ──────────────────────


def test_no_hook_mediated_project_injection_remains():
    import lucent.llm.hooks as hooks_mod

    assert not hasattr(hooks_mod, "DEFAULT_PROJECT_CONTEXT_HOOK")
    assert not hasattr(hooks_mod, "PROJECT_CONTEXT_ACTION")
    assert not hasattr(hooks_mod, "_run_project_context_hook")
    assert not hasattr(hooks_mod, "_select_project_files")
    assert not hasattr(hooks_mod, "_project_context_byte_budget")
    assert not hasattr(hooks_mod, "_project_rag_trigger_files")
    assert not hasattr(hooks_mod, "_fetch_project_file_content")
    # Ranking-only helper survives (round-2 contract).
    assert hasattr(hooks_mod, "_project_memory_ids_from_session_state")
    source = open(hooks_mod.__file__, encoding="utf-8").read()
    assert "action_type\"] == \"project_context\"" not in source.replace("'", '"')


def test_v1_budget_settings_removed():
    import lucent.settings as settings

    assert not hasattr(settings, "project_context_byte_budget")
    assert not hasattr(settings, "project_rag_trigger_files")
    assert hasattr(settings, "project_context_enabled")
    assert settings.get_runtime_setting_definition("projects.context_byte_budget") is None
    assert settings.get_runtime_setting_definition("projects.rag_trigger_files") is None
    assert settings.get_runtime_setting_definition("projects.context_enabled") is not None


def test_hook_manager_defaults_without_project_hook():
    from lucent.llm.hooks import HookManager

    manager = HookManager()
    names = [h.get("name") for h in manager.hooks]
    assert "project-context" not in names
    assert "file-memory-lookup" in names
    assert "message-memory-lookup" in names


def test_legacy_db_hook_row_is_ignored():
    """A lingering project-context definition dedupes/ignores, never injects."""
    from lucent.llm.hooks import HookManager

    manager = HookManager(hooks=[{
        "name": "project-context",
        "trigger_event": "before_model_call",
        "action_type": "project_context",
        "content": "",
        "config": {},
    }])
    names = [h.get("name") for h in manager.hooks]
    assert "project-context" in names  # tolerated, unknown action type


@pytest.mark.asyncio
async def test_legacy_db_hook_row_never_injects():
    from lucent.llm.hooks import HookManager

    manager = HookManager(hooks=[{
        "name": "project-context",
        "trigger_event": "before_model_call",
        "action_type": "project_context",
        "content": "SHOULD NOT APPEAR",
        "config": {},
    }], session_state=_state(PROJECT_CTX))
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "hello"}],
        memory_bridge=None,
    )
    texts = "\n".join(e.text for e in outcome.injectable_executions)
    assert "SHOULD NOT APPEAR" not in texts
    assert "## Project:" not in texts