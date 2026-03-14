"""Tests for the LLM engine abstraction layer."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lucent.llm.engine import LLMEngine, SessionEvent, SessionEventType
from lucent.llm.factory import get_engine, get_engine_name, reset_engine


class TestSessionEvent:
    def test_event_creation(self):
        event = SessionEvent(type=SessionEventType.MESSAGE, content="Hello")
        assert event.type == SessionEventType.MESSAGE
        assert event.content == "Hello"

    def test_event_types(self):
        assert SessionEventType.MESSAGE.value == "assistant.message"
        assert SessionEventType.TOOL_CALL.value == "tool.call"
        assert SessionEventType.SESSION_IDLE.value == "session.idle"
        assert SessionEventType.ERROR.value == "error"


class TestEngineFactory:
    def setup_method(self):
        reset_engine()

    def teardown_method(self):
        reset_engine()

    def test_default_engine_is_copilot(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LUCENT_LLM_ENGINE", None)
            reset_engine()
            assert get_engine_name() == "copilot"
            engine = get_engine()
            assert engine.name == "copilot"

    def test_copilot_engine_explicit(self):
        with patch.dict(os.environ, {"LUCENT_LLM_ENGINE": "copilot"}):
            reset_engine()
            engine = get_engine()
            assert engine.name == "copilot"

    def test_langchain_engine(self):
        with patch.dict(os.environ, {"LUCENT_LLM_ENGINE": "langchain"}):
            reset_engine()
            engine = get_engine()
            assert engine.name == "langchain"

    def test_invalid_engine_raises(self):
        with patch.dict(os.environ, {"LUCENT_LLM_ENGINE": "invalid"}):
            reset_engine()
            with pytest.raises(ValueError, match="Unknown LLM engine"):
                get_engine()

    def test_singleton_behavior(self):
        reset_engine()
        e1 = get_engine()
        e2 = get_engine()
        assert e1 is e2

    def test_reset_clears_singleton(self):
        reset_engine()
        e1 = get_engine()
        reset_engine()
        e2 = get_engine()
        assert e1 is not e2


class TestCopilotEngine:
    def test_name(self):
        from lucent.llm.copilot_engine import CopilotEngine

        engine = CopilotEngine()
        assert engine.name == "copilot"

    @pytest.mark.asyncio
    async def test_cleanup_is_noop(self):
        from lucent.llm.copilot_engine import CopilotEngine

        engine = CopilotEngine()
        await engine.cleanup()  # Should not raise


class TestLangChainEngine:
    def test_name(self):
        from lucent.llm.langchain_engine import LangChainEngine

        engine = LangChainEngine()
        assert engine.name == "langchain"

    @pytest.mark.asyncio
    async def test_cleanup_is_noop(self):
        from lucent.llm.langchain_engine import LangChainEngine

        engine = LangChainEngine()
        await engine.cleanup()  # Should not raise


class TestModelResolve:
    def test_resolve_anthropic(self):
        from lucent.llm.langchain_engine import _resolve_model

        provider, model_id = _resolve_model("claude-opus-4.6")
        assert provider == "anthropic"
        assert model_id == "claude-opus-4-6-20260301"

    def test_resolve_openai(self):
        from lucent.llm.langchain_engine import _resolve_model

        provider, model_id = _resolve_model("gpt-5.2")
        assert provider == "openai"
        assert model_id == "gpt-5.2"

    def test_resolve_google(self):
        from lucent.llm.langchain_engine import _resolve_model

        provider, model_id = _resolve_model("gemini-3-pro")
        assert provider == "google_genai"
        assert model_id == "gemini-3-pro"

    def test_resolve_unknown_infers_prefix(self):
        from lucent.llm.langchain_engine import _resolve_model

        provider, model_id = _resolve_model("claude-future-5.0")
        assert provider == "anthropic"

    def test_resolve_completely_unknown(self):
        from lucent.llm.langchain_engine import _resolve_model

        provider, model_id = _resolve_model("some-custom-model")
        assert provider == ""
        assert model_id == "some-custom-model"


class TestModelRegistry:
    def test_api_model_id(self):
        from lucent.model_registry import get_api_model_id

        assert get_api_model_id("claude-opus-4.6") == "claude-opus-4-6-20260301"
        assert get_api_model_id("gpt-5.2") == "gpt-5.2"
        assert get_api_model_id("unknown-model") == "unknown-model"

    def test_get_provider(self):
        from lucent.model_registry import get_provider

        assert get_provider("claude-opus-4.6") == "anthropic"
        assert get_provider("gpt-5.2") == "openai"
        assert get_provider("gemini-3-pro") == "google"
        assert get_provider("unknown") is None

    def test_provider_inference(self):
        from lucent.model_registry import get_provider

        assert get_provider("claude-future") == "anthropic"
        assert get_provider("gpt-6") == "openai"
        assert get_provider("gemini-4") == "google"
