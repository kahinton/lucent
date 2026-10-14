"""Unit tests for Copilot BYOK local-provider wiring."""

from __future__ import annotations

from lucent.llm.copilot_engine import CopilotEngine


def test_backend_provider_from_local_backend():
    engine = CopilotEngine()
    backend = {
        "engine": "copilot",
        "provider": "ollama",
        "config": {"base_url": "http://host:11434"},
    }
    provider = engine._backend_provider({"engine_backend": backend}, "llama3")
    assert provider == {"type": "openai", "base_url": "http://host:11434/v1"}


def test_backend_provider_uses_provider_default_base_url(monkeypatch):
    engine = CopilotEngine()
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    provider = engine._backend_provider(
        {"engine_backend": {"provider": "ollama", "config": {}}}, "llama3"
    )
    assert provider == {"type": "openai", "base_url": "http://localhost:11434/v1"}


def test_backend_provider_none_for_native_or_absent():
    engine = CopilotEngine()
    assert engine._backend_provider({"engine_backend": {"provider": "copilot"}}, "gpt-4") is None
    assert engine._backend_provider(None, "gpt-4") is None
    assert engine._backend_provider({}, "gpt-4") is None


def test_session_kwargs_include_provider_when_set():
    engine = CopilotEngine()
    kwargs = engine._make_session_kwargs(
        "llama3", "sys", None, provider={"type": "openai", "base_url": "http://x/v1"}
    )
    assert kwargs["provider"] == {"type": "openai", "base_url": "http://x/v1"}
    assert kwargs["model"] == "llama3"


def test_session_kwargs_omit_provider_when_absent():
    engine = CopilotEngine()
    kwargs = engine._make_session_kwargs("gpt-5.1", "sys", None)
    assert "provider" not in kwargs
