"""Tests for chat model validation and per-model engine routing."""

import pytest
from fastapi import HTTPException

from lucent.api.routers import chat
from lucent.auth_providers import SESSION_COOKIE_NAME


class _FakeRequest:
    cookies = {SESSION_COOKIE_NAME: "session-token"}


class _FakeEngine:
    name = "langchain"

    async def run_session(self, **kwargs):
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


async def _fake_session_user(_request):
    return {
        "id": "user-id",
        "organization_id": "org-id",
        "display_name": "Tester",
    }, object()


async def _fake_system_prompt(_user, _pool, _page_context):
    return "system"


@pytest.mark.asyncio
async def test_chat_stream_uses_engine_for_selected_model(monkeypatch):
    seen: dict[str, str] = {}

    def _fake_get_engine_for_model(model_id: str):
        seen["model"] = model_id
        return _FakeEngine()

    monkeypatch.setattr(chat, "_get_session_user", _fake_session_user)
    monkeypatch.setattr(chat, "_build_system_prompt", _fake_system_prompt)
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
async def test_chat_stream_v2_surfaces_langchain_tool_input(monkeypatch):
    monkeypatch.setattr(chat, "_get_session_user", _fake_session_user)
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


def test_chat_prompt_blocks_invented_security_protocols():
    instructions = chat._chat_tool_grounding_instructions()

    assert "Do not invent Lucent security policies" in instructions
    assert "If tools are unavailable" in instructions
    assert "may quote or summarize" in instructions
    assert "use `create_request` directly" in instructions
    assert "Do not create a goal" in instructions
    assert "Use `list_available_models`" in instructions


def test_chat_mcp_config_uses_narrow_tool_allowlist():
    config = chat._build_mcp_config("session-token")
    tools = config["memory-server"]["tools"]

    assert "get_current_user_context" in tools
    assert "search_memories" in tools
    assert "list_active_work" in tools
    assert "create_request" in tools
    assert "list_available_models" in tools
    assert "create_task" not in tools
    assert "create_agent_definition" not in tools
    assert tools != ["*"]


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
