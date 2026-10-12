"""Tests for chat model validation and per-model engine routing."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lucent.api.routers import chat
from lucent.auth_providers import SESSION_COOKIE_NAME
from lucent.llm import copilot_engine
from lucent.llm.model_auto import AutoChoice


class _FakeRequest:
    cookies = {SESSION_COOKIE_NAME: "session-token"}


class _FakeEngine:
    name = "langchain"

    async def run_session(self, **kwargs):
        return f"routed:{kwargs['model']}"

    async def run_session_streaming(self, **kwargs):
        return f"routed:{kwargs['model']}"


class _FakeStreamingEngine:
    name = "langchain"

    async def run_session_streaming(self, **kwargs):
        from lucent.llm.engine import SessionEvent, SessionEventType

        on_event = kwargs["on_event"]
        on_event(
            SessionEvent(
                type=SessionEventType.TOOL_CALL,
                tool_name="search_memories",
                tool_input={"query": "project notes", "limit": 3},
            )
        )
        on_event(
            SessionEvent(
                type=SessionEventType.TOOL_RESULT,
                tool_name="search_memories",
                tool_output='{"memories": []}',
            )
        )
        on_event(SessionEvent(type=SessionEventType.MESSAGE, content="done"))
        return None


class _FakeSummaryEngine:
    name = "fake-summary"

    def __init__(self, seen: dict):
        self.seen = seen

    async def run_session(self, **kwargs):
        self.seen.update(kwargs)
        return (
            "## Session Summary\n\n"
            "The session produced a useful narrative summary.\n\n"
            "## What Happened\n\n"
            "- The summarizer followed the capture skill.\n\n"
            "## Why It Matters\n\n"
            "The captured memory has real content.\n\n"
            "## Follow-up\n\n"
            "- None identified."
        )


class _FakeCaptureRepo:
    def __init__(self):
        self.updated_metadata = None
        self.capture_called = False

    async def evaluate_experience_capture(self, *_args, **_kwargs):
        return {
            "session": {"id": "session-id", "metadata": {}},
            "evaluation": {"should_capture": True, "score": 4, "reasons": ["mutating_tools"]},
        }

    async def update_session(self, *_args, metadata=None, **_kwargs):
        self.updated_metadata = metadata
        return {"id": "session-id", "metadata": metadata or {}}

    async def maybe_capture_experience(self, *_args, **_kwargs):
        self.capture_called = True
        raise AssertionError("maybe_capture_experience should not run")


async def _fake_session_user(_request):
    return {
        "id": "user-id",
        "organization_id": "org-id",
        "display_name": "Tester",
    }, object()


async def _fake_system_prompt(_user, _pool, _page_context):
    return "system"


async def _allow_model(_user, _pool, _model_id):
    return True


@pytest.mark.asyncio
async def test_active_user_context_contains_authenticated_users_memory(monkeypatch):
    class _UserRepo:
        def __init__(self, _pool):
            pass

        async def get_individual_memory_for_user(self, user_id, organization_id=None):
            assert user_id == "authenticated-user"
            return {"content": "Prefers the unique Glasswing workflow."}

    monkeypatch.setattr(chat, "UserRepository", _UserRepo)

    context = await chat._render_active_user_context(
        {
            "id": "authenticated-user",
            "organization_id": "org-id",
            "display_name": "Active User",
            "role": "member",
        },
        object(),
    )

    assert "## Active User Context" in context
    assert '"id": "authenticated-user"' in context
    assert "Prefers the unique Glasswing workflow." in context


@pytest.mark.asyncio
async def test_chat_stream_uses_engine_for_selected_model(monkeypatch):
    seen: dict[str, str] = {}

    def _fake_get_engine_for_model(model_id: str):
        seen["model"] = model_id
        return _FakeEngine()

    monkeypatch.setattr(chat, "_get_session_user", _fake_session_user)
    monkeypatch.setattr(chat, "_build_system_prompt", _fake_system_prompt)
    monkeypatch.setattr(chat, "_can_user_access_model", _allow_model)
    monkeypatch.setattr("lucent.model_registry.validate_model", lambda _model: None)
    monkeypatch.setattr("lucent.llm.get_engine_for_model", _fake_get_engine_for_model)

    response = await chat.chat_stream(
        _FakeRequest(),
        chat.ChatRequest(
            messages=[chat.ChatMessage(role="user", content="hello")],
            model="local-ollama",
        ),
    )

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert seen["model"] == "local-ollama"
    assert "routed:local-ollama" in body


@pytest.mark.asyncio
async def test_chat_stream_rejects_disabled_model(monkeypatch):
    monkeypatch.setattr(chat, "_get_session_user", _fake_session_user)
    monkeypatch.setattr("lucent.model_registry.validate_model", lambda _model: "disabled")

    with pytest.raises(HTTPException) as exc:
        await chat.chat_stream(
            _FakeRequest(),
            chat.ChatRequest(
                messages=[chat.ChatMessage(role="user", content="hello")],
                model="disabled-model",
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "disabled"


@pytest.mark.asyncio
async def test_chat_stream_rejects_inaccessible_model(monkeypatch):
    async def deny_model(_user, _pool, _model_id):
        return False

    monkeypatch.setattr(chat, "_get_session_user", _fake_session_user)
    monkeypatch.setattr(chat, "_can_user_access_model", deny_model)
    monkeypatch.setattr("lucent.model_registry.validate_model", lambda _model: None)

    with pytest.raises(HTTPException) as exc:
        await chat.chat_stream(
            _FakeRequest(),
            chat.ChatRequest(
                messages=[chat.ChatMessage(role="user", content="hello")],
                model="private-model",
            ),
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Model is not available to this user"


@pytest.mark.asyncio
async def test_chat_stream_v2_surfaces_langchain_tool_input(monkeypatch):
    monkeypatch.setattr(chat, "_get_session_user", _fake_session_user)
    monkeypatch.setattr(chat, "_can_user_access_model", _allow_model)
    monkeypatch.setattr("lucent.model_registry.validate_model", lambda _model: None)
    monkeypatch.setattr("lucent.llm.get_engine_for_model", lambda _model: _FakeStreamingEngine())

    response = await chat.chat_stream_v2(
        _FakeRequest(),
        chat.ChatStreamRequest(
            messages=[chat.ChatMessage(role="user", content="find project notes")],
            model="qwen3:4b",
        ),
    )

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert '"type": "tool_call"' in body
    assert '"tool": "search_memories"' in body
    assert '\\"query\\": \\"project notes\\"' in body


@pytest.mark.asyncio
async def test_chat_stream_v2_includes_assigned_agent_mcp_servers(monkeypatch):
    captured: dict = {}

    class _AgentRepo:
        def __init__(self, _pool):
            pass

        async def get_agent(self, _agent_id, _organization_id):
            return {"id": "agent-id", "name": "code", "content": "# Code agent"}

        async def get_agent_skills(self, _agent_id):
            return []

        async def get_agent_managed_tools(self, _agent_id):
            return []

        async def get_agent_mcp_servers(self, _agent_id):
            return [
                {
                    "id": "github-server",
                    "server_type": "http",
                    "url": "https://mcp.example.test",
                    "headers": {"X-Token": "token"},
                    "allowed_tools": ["search_repositories"],
                },
                {"id": "copilot-github", "server_type": "copilot_github"},
            ]

        async def get_agent_hooks(self, _agent_id):
            return []

    class _CapturingEngine:
        name = "copilot"

        async def run_session_streaming(self, **kwargs):
            captured.update(kwargs)
            return "done"

    async def fake_prepare_persistent_session(**_kwargs):
        return chat.PersistentChatSession(
            session_id="session-id",
            provider_session_id=None,
            provider_initialized=False,
            turn_id="turn-id",
            user_message_id="message-id",
            previous_messages=[],
            repo=None,
        )

    async def fake_render_active_user_context(*_args):
        return "context"

    monkeypatch.setattr(chat, "_get_session_user", _fake_session_user)
    monkeypatch.setattr(chat, "_can_user_access_model", _allow_model)
    monkeypatch.setattr(chat, "_render_active_user_context", fake_render_active_user_context)
    monkeypatch.setattr(chat, "_prepare_persistent_chat_session", fake_prepare_persistent_session)
    monkeypatch.setattr("lucent.model_registry.validate_model", lambda _model: None)
    monkeypatch.setattr("lucent.llm.get_engine_for_model", lambda _model: _CapturingEngine())
    monkeypatch.setattr("lucent.db.definitions.DefinitionRepository", _AgentRepo)
    monkeypatch.setattr("lucent.secrets.SecretRegistry.get", lambda: object())

    response = await chat.chat_stream_v2(
        _FakeRequest(),
        chat.ChatStreamRequest(
            messages=[chat.ChatMessage(role="user", content="search repositories")],
            model="claude-haiku-4.5",
            agent_id="agent-id",
        ),
    )

    async for _chunk in response.body_iterator:
        pass

    server = captured["mcp_config"]["mcp-github-server"]
    assert server["url"] == "https://mcp.example.test"
    assert server["headers"] == {"X-Token": "token"}
    assert server["tools"] == ["search_repositories"]
    assert captured["enable_config_discovery"] is True


def test_chat_prompt_blocks_invented_security_protocols():
    instructions = chat._chat_tool_grounding_instructions()

    assert "Do not invent Lucent security policies" in instructions
    assert "If tools are unavailable" in instructions
    assert "may quote or summarize" in instructions
    assert "use `create_request` directly" in instructions
    assert "Do not create a goal" in instructions
    assert "Use `list_available_models`" in instructions


def test_chat_mcp_config_exposes_all_lucent_tools():
    config = chat._build_mcp_config("session-token")
    tools = config["memory-server"]["tools"]

    assert tools == ["*"]


def test_definition_engineer_chat_gets_all_lucent_tools():
    tools = chat._chat_allowed_tools_for_agent("definition-engineer", [])

    assert tools == ["*"]


def test_workflow_composer_chat_gets_all_lucent_tools():
    tools = chat._chat_allowed_tools_for_agent("workflow-composer", [])

    assert tools == ["*"]


def test_copilot_restricted_session_excludes_built_in_tools():
    engine = copilot_engine.CopilotEngine()

    kwargs = engine._make_session_kwargs(
        model="test-model",
        system_message="system",
        mcp_config={"memory-server": {"tools": ["create_workflow"]}},
        approve_permissions=False,
    )

    assert "bash" in kwargs["excluded_tools"]
    assert "skill" in kwargs["excluded_tools"]
    assert "view" in kwargs["excluded_tools"]
    assert "available_tools" not in kwargs
    assert kwargs["mcp_servers"]["memory-server"]["tools"] == ["create_workflow"]


def test_chat_mcp_config_threads_llm_session_headers():
    config = chat._build_mcp_config(
        "session-token",
        llm_session_id="session-id",
        llm_turn_id="turn-id",
        llm_message_id="message-id",
    )
    headers = config["memory-server"]["headers"]

    assert headers["Authorization"] == "Bearer session-token"
    assert headers["X-Lucent-LLM-Session-Id"] == "session-id"
    assert headers["X-Lucent-LLM-Turn-Id"] == "turn-id"
    assert headers["X-Lucent-LLM-Message-Id"] == "message-id"


def test_chat_history_rehydrates_persisted_tool_events():
    template = Path("src/lucent/web/templates/chat.html").read_text()

    assert "renderSessionMessages(session.messages || [], session.events || [])" in template
    assert "function replaySessionEvents(assistantEl, events)" in template
    assert "const turnId = event.turn_id || turnIdByMessageId.get(event.message_id);" in template
    assert "turnEvents.length && !assistantTurns.has(msg.turn_id)" in template
    assert "appendToolCall(toolsEl, event.tool_name || 'unknown', event.tool_input)" in template
    assert "updateToolResult(toolsEl, event.tool_name || 'unknown', event.tool_output)" in template
    assert "replaySessionEvents(assistantEl, eventsByTurn.get(msg.turn_id) || [])" in template


def test_chat_history_supports_soft_deleting_sessions():
    template = Path("src/lucent/web/templates/chat.html").read_text()

    assert 'id="archive-session-dialog"' in template
    assert 'deleteButton.dataset.archiveSessionId = session.id' in template
    assert "body: JSON.stringify({ status: 'archived' })" in template
    assert "await archiveSession(sessionId);" in template
    assert "document.getElementById('archive-session-confirm').disabled = false;" in template
    assert "finally {\n        this.disabled = false;" in template
    assert "function updateSessionStatus()" in template
    assert (
        "state.sessions = state.sessions.filter(session => session.id !== sessionId);\n"
        "    updateSessionStatus();" in template
    )
    assert "Related files, requests, and provenance will remain available." in template


def test_session_experience_summary_defaults_and_context_not_capped():
    assert chat.SESSION_EXPERIENCE_TIMEOUT == 180

    long_content = "x" * 20000
    formatted = chat._format_session_experience_context(
        {
            "id": "session-id",
            "title": "Long session",
            "kind": "chat",
            "messages": [{"role": "user", "content": long_content}],
            "events": [],
            "requests": [],
        },
        {"score": 3, "reasons": ["substantial_transcript"]},
    )

    assert long_content in formatted
    assert "[middle truncated" not in formatted


@pytest.mark.asyncio
async def test_session_experience_summary_uses_skill_and_no_tools(monkeypatch):
    seen: dict = {}

    monkeypatch.setattr(chat, "SESSION_EXPERIENCE_SUMMARY_ENABLED", True)
    monkeypatch.setattr(chat, "SESSION_EXPERIENCE_MODEL", "summary-model")
    monkeypatch.setattr("lucent.model_registry.validate_model", lambda _model: None)
    monkeypatch.setattr(
        "lucent.llm.get_engine_for_model",
        lambda _model: _FakeSummaryEngine(seen),
    )

    content, model, error = await chat._summarize_session_experience(
        session={
            "id": "session-id",
            "title": "Capture summary",
            "kind": "chat",
            "messages": [
                {"role": "user", "content": "Please summarize this session."},
                {"role": "assistant", "content": "I created a narrative summary."},
            ],
            "events": [],
            "requests": [],
        },
        evaluation={"score": 3, "reasons": ["mutating_tools"]},
    )

    assert error is None
    assert model == "summary-model"
    assert content.startswith("## Session Summary")
    assert "Session Experience Capture" in seen["system_message"]
    assert seen["mcp_config"] == {}


@pytest.mark.asyncio
async def test_session_experience_summary_can_be_disabled(monkeypatch):
    monkeypatch.setattr(chat, "SESSION_EXPERIENCE_SUMMARY_ENABLED", False)

    content, model, error = await chat._summarize_session_experience(
        session={"id": "session-id", "messages": [], "events": [], "requests": []},
        evaluation={"score": 3, "reasons": ["mutating_tools"]},
    )

    assert content is None
    assert model is None
    assert error == "disabled"


@pytest.mark.asyncio
async def test_session_experience_model_can_skip_capture(monkeypatch):
    repo = _FakeCaptureRepo()

    async def no_experience_summary(**_kwargs):
        return None, "summary-model", "no_experience_needed"

    monkeypatch.setattr(chat, "_summarize_session_experience", no_experience_summary)

    await chat._maybe_capture_session_experience(
        user={"id": "user-id", "organization_id": "org-id"},
        chat_session=chat.PersistentChatSession(
            session_id="session-id",
            provider_session_id=None,
            provider_initialized=False,
            turn_id="turn-id",
            user_message_id="message-id",
            previous_messages=[],
            repo=repo,
        ),
    )

    assert repo.capture_called is False
    assert repo.updated_metadata["experience_capture"]["status"] == "skipped"
    assert (
        repo.updated_metadata["experience_capture"]["reason"]
        == "model_no_experience_needed"
    )


# ── Auto model selection (Phase 4) ───────────────────────────────────────


async def _fake_auto_choice(**kwargs):
    return AutoChoice(
        model_id="picked-a",
        source="arbiter",
        reason="arbiter pick",
        confidence=0.9,
        arbiter_model_id="nimble",
    )


def _patch_stream_base(monkeypatch):
    """Common patches so chat_stream runs without the DB."""
    monkeypatch.setattr(chat, "_get_session_user", _fake_session_user)
    monkeypatch.setattr(chat, "_build_system_prompt", _fake_system_prompt)
    monkeypatch.setattr(chat, "_can_user_access_model", _allow_model)
    monkeypatch.setattr(
        "lucent.model_registry.validate_model", lambda _model: None
    )
    monkeypatch.setattr("lucent.llm.get_engine_for_model", lambda _model: _FakeEngine())


@pytest.mark.asyncio
async def test_chat_stream_flag_off_auto_sentinel_is_a_400(monkeypatch):
    monkeypatch.setattr(chat, "_get_session_user", _fake_session_user)
    monkeypatch.setattr("lucent.settings.auto_selection_enabled", lambda **_kw: False)

    with pytest.raises(HTTPException) as exc:
        await chat.chat_stream(
            _FakeRequest(),
            chat.ChatRequest(
                messages=[chat.ChatMessage(role="user", content="hello")],
                model="auto",
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "Auto model selection is disabled"


@pytest.mark.asyncio
async def test_chat_stream_flag_read_failure_fails_open_to_400(monkeypatch):
    def _broken_flag(**_kwargs):
        raise RuntimeError("settings db down")

    monkeypatch.setattr(chat, "_get_session_user", _fake_session_user)
    monkeypatch.setattr("lucent.settings.auto_selection_enabled", _broken_flag)

    with pytest.raises(HTTPException) as exc:
        await chat.chat_stream(
            _FakeRequest(),
            chat.ChatRequest(
                messages=[chat.ChatMessage(role="user", content="hello")],
                model="Auto",
            ),
        )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_chat_stream_flag_on_auto_flows_arbiter_pick(monkeypatch):
    seen_engine: dict[str, str] = {}
    seen_prepare: dict = {}
    auto_calls: list[dict] = []

    async def fake_auto(**kwargs):
        auto_calls.append(kwargs)
        return AutoChoice(
            model_id="picked-a",
            source="arbiter",
            reason="arbiter pick",
            confidence=0.9,
            arbiter_model_id="nimble",
        )

    async def fake_prepare(**kwargs):
        seen_prepare.update(kwargs)
        return chat.PersistentChatSession(
            session_id=None,
            provider_session_id=None,
            provider_initialized=False,
            turn_id="turn-id",
            user_message_id=None,
            previous_messages=[],
            repo=None,
        )

    def fake_engine(model_id):
        seen_engine["model"] = model_id
        return _FakeEngine()

    _patch_stream_base(monkeypatch)
    monkeypatch.setattr(chat, "_auto_chat_model_choice", fake_auto)
    monkeypatch.setattr(chat, "_prepare_persistent_chat_session", fake_prepare)
    monkeypatch.setattr("lucent.llm.get_engine_for_model", fake_engine)
    monkeypatch.setattr("lucent.settings.auto_selection_enabled", lambda **_kw: True)

    response = await chat.chat_stream(
        _FakeRequest(),
        chat.ChatRequest(
            messages=[chat.ChatMessage(role="user", content="hello")],
            model="auto",
        ),
    )
    async for _chunk in response.body_iterator:
        pass

    assert auto_calls[0]["session_id"] is None
    # The RESOLVED id drives the engine and the session row — never "auto".
    assert seen_engine["model"] == "picked-a"
    assert seen_prepare["model"] == "picked-a"


@pytest.mark.asyncio
async def test_chat_unset_model_flag_on_consults_arbiter(monkeypatch):
    auto_calls: list[dict] = []

    async def fake_auto(**kwargs):
        auto_calls.append(kwargs)
        return AutoChoice(
            model_id="picked-a",
            source="arbiter",
            reason="arbiter pick",
            confidence=0.9,
        )

    _patch_stream_base(monkeypatch)
    monkeypatch.setattr(chat, "_auto_chat_model_choice", fake_auto)
    monkeypatch.setattr("lucent.settings.auto_selection_enabled", lambda **_kw: True)

    response = await chat.chat_stream(
        _FakeRequest(),
        chat.ChatRequest(messages=[chat.ChatMessage(role="user", content="hello")]),
    )
    async for _chunk in response.body_iterator:
        pass

    assert len(auto_calls) == 1


@pytest.mark.asyncio
async def test_chat_unset_model_flag_off_keeps_the_chain(monkeypatch):
    auto_calls: list[dict] = []
    seen_engine: dict[str, str] = {}

    async def fake_auto(**kwargs):
        auto_calls.append(kwargs)
        raise AssertionError("arbiter must not run with the flag off")

    _patch_stream_base(monkeypatch)

    def fake_engine(model_id):
        seen_engine["model"] = model_id
        return _FakeEngine()

    monkeypatch.setattr(chat, "_auto_chat_model_choice", fake_auto)
    monkeypatch.setattr("lucent.llm.get_engine_for_model", fake_engine)
    monkeypatch.setattr("lucent.settings.auto_selection_enabled", lambda **_kw: False)

    response = await chat.chat_stream(
        _FakeRequest(),
        chat.ChatRequest(messages=[chat.ChatMessage(role="user", content="hello")]),
    )
    async for _chunk in response.body_iterator:
        pass

    assert auto_calls == []
    assert seen_engine["model"] == chat._resolve_chat_model()


@pytest.mark.asyncio
async def test_chat_stream_v2_flag_on_auto_flows_arbiter_pick(monkeypatch):
    seen_engine: dict[str, str] = {}

    async def fake_auto(**_kwargs):
        return AutoChoice(
            model_id="picked-b",
            source="fallback",
            reason="chain",
            fallback_reason="arbiter down",
        )

    _patch_stream_base(monkeypatch)
    monkeypatch.setattr(chat, "_auto_chat_model_choice", fake_auto)

    def fake_engine(model_id):
        seen_engine["model"] = model_id
        return _FakeEngine()

    monkeypatch.setattr("lucent.llm.get_engine_for_model", fake_engine)
    monkeypatch.setattr("lucent.settings.auto_selection_enabled", lambda **_kw: True)

    response = await chat.chat_stream_v2(
        _FakeRequest(),
        chat.ChatStreamRequest(
            messages=[chat.ChatMessage(role="user", content="hello")],
            model="auto",
        ),
    )
    async for _chunk in response.body_iterator:
        pass

    assert seen_engine["model"] == "picked-b"


@pytest.mark.asyncio
async def test_chat_models_payload_reports_auto_selection_flag(monkeypatch):
    class _FakeRepo:
        def __init__(self, _pool):
            pass

        async def list_models_accessible_by(self, *_args, **_kwargs):
            return {
                "items": [
                    {
                        "id": "m1",
                        "name": "M1",
                        "provider": "ollama",
                        "category": "general",
                        "reasoning_efforts": [],
                        "supports_vision": False,
                        "context_window": 0,
                        "is_enabled": True,
                        "model_class": "language",
                    }
                ]
            }

    async def fake_pool(_pool, _principal):
        return _pool

    monkeypatch.setattr(chat, "_get_session_user", _fake_session_user)
    monkeypatch.setattr("lucent.db.models.get_authorized_models_pool", fake_pool)
    monkeypatch.setattr("lucent.db.models.ModelRepository", _FakeRepo)
    monkeypatch.setattr("lucent.settings.auto_selection_enabled", lambda **_kw: False)

    payload = await chat.chat_models(_FakeRequest())
    assert payload["auto_selection_enabled"] is False

    monkeypatch.setattr("lucent.settings.auto_selection_enabled", lambda **_kw: True)
    payload = await chat.chat_models(_FakeRequest())
    assert payload["auto_selection_enabled"] is True
    assert payload["models"][0]["id"] == "m1"


class _CapturingRepo:
    def __init__(self):
        self.messages = []

    async def add_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        return {"id": "m1"}

    async def update_session(self, *_args, **_kwargs):
        return {}

    async def list_messages(self, *_args, **_kwargs):
        return []


@pytest.mark.asyncio
async def test_chat_stream_auto_turn_metadata_reaches_message_and_sse(monkeypatch):
    repo = _CapturingRepo()

    async def fake_auto(**_kwargs):
        return AutoChoice(
            model_id="picked-a",
            source="arbiter",
            reason="arbiter pick",
            confidence=0.81,
            arbiter_model_id="nimble",
        )

    async def fake_prepare(**_kwargs):
        return chat.PersistentChatSession(
            session_id="session-id",
            provider_session_id=None,
            provider_initialized=False,
            turn_id="turn-id",
            user_message_id="message-id",
            previous_messages=[],
            repo=repo,
        )

    _patch_stream_base(monkeypatch)
    monkeypatch.setattr(chat, "_auto_chat_model_choice", fake_auto)
    monkeypatch.setattr(chat, "_prepare_persistent_chat_session", fake_prepare)
    monkeypatch.setattr("lucent.settings.auto_selection_enabled", lambda **_kw: True)

    response = await chat.chat_stream(
        _FakeRequest(),
        chat.ChatRequest(
            messages=[chat.ChatMessage(role="user", content="hello")],
            model="auto",
        ),
    )
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    # Live badge: the session event carries the provenance.
    session_line = next(line for line in body.splitlines() if '"model_selected"' in line)
    badge = json.loads(session_line[len("data: "):])["model_selected"]
    assert badge["chosen"] == "picked-a"
    assert badge["source"] == "arbiter"
    assert badge["confidence"] == 0.81

    # Persisted: the assistant turn carries the same provenance.
    (_args, kwargs) = repo.messages[0]
    assert kwargs["role"] == "assistant"
    auto_meta = kwargs["metadata"]["auto_selection"]
    assert auto_meta["chosen"] == "picked-a"
    assert auto_meta["confidence"] == 0.81


def test_chat_template_renders_auto_selection_notes():
    template = Path("src/lucent/web/templates/chat.html").read_text()

    assert "function appendAutoSelectionNote(assistantEl, info)" in template
    assert "appendAutoSelectionNote(assistantEl, event.model_selected);" in template
    assert "if (autoInfo) appendAutoSelectionNote(assistantEl, autoInfo);" in template


def test_hook_event_payload_promotes_decision_gate_fields():
    event = SimpleNamespace(
        raw={
            "hook": "message_memory_lookup",
            "memory_count": 2,
            "memories": [
                {"id": "a1b2c3d4", "tags": ["t"], "content": "c", "full": True},
                {"id": "beefcafe", "tags": [], "content": "d", "full": False},
            ],
            "decision_model": "nimble:latest",
            "decision_gated": 1,
            "decision_scores": {"a1b2c3d4": 0.84, "beefcafe": 0.11},
        },
        content="",
    )
    payload = chat._hook_event_payload(event)
    assert payload["decision_model"] == "nimble:latest"
    assert payload["decision_gated"] == 1
    assert payload["decision_scores"]["a1b2c3d4"] == 0.84
    # Keys are aligned with the promoted "memories" entries so the UI can
    # render a score next to each injected memory by id prefix.
    assert {m["id"] for m in payload["injected"]} == set(payload["decision_scores"])


def test_hook_event_payload_omits_gate_fields_when_absent():
    event = SimpleNamespace(
        raw={"hook": "message_memory_lookup", "memory_count": 1, "memories": []},
        content="",
    )
    payload = chat._hook_event_payload(event)
    for key in ("decision_model", "decision_gated", "decision_scores", "decision_skipped"):
        assert key not in payload


def test_message_ui_renders_memory_gate_confidence():
    js = Path("src/lucent/web/static/chat-message-ui.js").read_text()

    assert "decision_scores" in js
    assert "hook-memory-score" in js
    assert "Relevance gate" in js
    template = Path("src/lucent/web/templates/chat.html").read_text()
    assert ".hook-memory-score" in template
