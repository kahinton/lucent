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
