"""Tests for model reasoning effort controls."""


def test_validate_reasoning_effort_accepts_model_specific_dynamic_value(monkeypatch):
    from lucent import model_registry
    from lucent.model_registry import ModelInfo, validate_reasoning_effort

    monkeypatch.setitem(
        model_registry._MODEL_BY_ID,
        "dynamic-reasoner",
        ModelInfo(
            id="dynamic-reasoner",
            provider="test",
            name="Dynamic Reasoner",
            category="reasoning",
            reasoning_efforts=["turbo", "patient"],
        ),
    )

    assert validate_reasoning_effort("dynamic-reasoner", "turbo") is None


def test_validate_reasoning_effort_rejects_unlisted_model_specific_value(monkeypatch):
    from lucent import model_registry
    from lucent.model_registry import ModelInfo, validate_reasoning_effort

    monkeypatch.setitem(
        model_registry._MODEL_BY_ID,
        "dynamic-reasoner",
        ModelInfo(
            id="dynamic-reasoner",
            provider="test",
            name="Dynamic Reasoner",
            category="reasoning",
            reasoning_efforts=["turbo", "patient"],
        ),
    )

    error = validate_reasoning_effort("dynamic-reasoner", "xhigh")
    assert error is not None
    assert "does not allow" in error


def test_validate_reasoning_effort_rejects_model_without_controls():
    from lucent.model_registry import validate_reasoning_effort

    error = validate_reasoning_effort("gpt-4.1", "low")
    assert error is not None
    assert "does not expose" in error


def test_langchain_openai_reasoning_effort_passed(monkeypatch):
    from lucent.llm import langchain_engine

    captured = {}

    def fake_init_chat_model(model, **kwargs):
        captured["model"] = model
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)

    langchain_engine._get_chat_model("gpt-5.1", reasoning_effort="medium")

    assert captured["model"] == "gpt-5.1"
    assert captured["kwargs"]["model_provider"] == "openai"
    assert captured["kwargs"]["reasoning_effort"] == "medium"


def test_langchain_anthropic_reasoning_effort_maps_to_effort(monkeypatch):
    from lucent.llm import langchain_engine

    captured = {}

    def fake_init_chat_model(model, **kwargs):
        captured["model"] = model
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)

    langchain_engine._get_chat_model("claude-opus-4.7", reasoning_effort="xhigh")

    assert captured["kwargs"]["model_provider"] == "anthropic"
    assert captured["kwargs"]["effort"] == "xhigh"


def test_langchain_google_reasoning_effort_maps_to_thinking_level(monkeypatch):
    from lucent.llm import langchain_engine

    captured = {}

    def fake_init_chat_model(model, **kwargs):
        captured["model"] = model
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)

    langchain_engine._get_chat_model("gemini-3-pro", reasoning_effort="low")

    assert captured["kwargs"]["model_provider"] == "google_genai"
    assert captured["kwargs"]["thinking_level"] == "low"
