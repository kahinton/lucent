"""Tests for the controlled provider → engine mapping and model catalog."""

from __future__ import annotations

import pytest

from lucent.llm import backend_selection as bs
from lucent.llm import engine_registry as er
from lucent.model_registry import ModelInfo


def test_provider_engine_map_is_explicit_and_controlled():
    mapping = er.provider_engine_map()
    assert mapping["ollama"] == ["langchain", "copilot", "claude_code", "codex"]
    assert mapping["anthropic"] == ["langchain", "copilot", "claude_code"]
    assert mapping["openai"] == ["langchain", "copilot", "codex"]
    assert mapping["google"] == ["langchain", "copilot"]
    assert mapping["copilot"] == ["copilot"]
    assert mapping["lmstudio"] == ["copilot", "codex"]
    assert mapping["xai"] == ["copilot"]
    # Providers with no implemented engine are omitted rather than offered.
    assert "custom" not in mapping


def test_engine_supports_provider_uses_the_same_map():
    assert er.engine_supports_provider("claude_code", "ollama")
    assert not er.engine_supports_provider("claude_code", "google")
    assert er.engine_supports_provider("codex", "lmstudio")
    assert not er.engine_supports_provider("langchain", "copilot")
    assert not er.engine_supports_provider("copilot", "custom")


def test_default_engine_for_provider():
    assert er.default_engine_for_provider("ollama") == "langchain"
    assert (
        er.default_engine_for_provider("openai", configured_default="copilot")
        == "copilot"
    )
    # The configured default is used only when it is compatible; otherwise
    # routing falls back to LangChain.
    assert (
        er.default_engine_for_provider("openai", configured_default="claude_code")
        == "langchain"
    )
    assert er.default_engine_for_provider("custom") is None


def test_compatible_engines_marks_default_and_capabilities():
    engines = er.compatible_engines("ollama", configured_default="langchain")
    by_name = {entry["engine"]: entry for entry in engines}
    assert set(by_name) == {"langchain", "copilot", "claude_code", "codex"}
    assert by_name["langchain"]["default"] is True
    assert by_name["claude_code"]["kind"] == "coding_agent"
    assert all("available" in entry for entry in engines)


def _model(**overrides) -> ModelInfo:
    base = dict(
        id="gemma4:31b-cloud",
        provider="ollama",
        name="Gemma 4",
        category="general",
        supports_tools=True,
    )
    base.update(overrides)
    return ModelInfo(**base)


@pytest.fixture
def fake_backends(monkeypatch):
    state = {
        "rows": [
            {
                "id": "b-langchain",
                "name": "ollama via langchain",
                "engine": "langchain",
                "provider": "ollama",
                "is_enabled": True,
                "organization_id": None,
                "config": {},
            },
            {
                "id": "b-oclauda",
                "name": "oclauda",
                "engine": "claude_code",
                "provider": "ollama",
                "is_enabled": True,
                "organization_id": "o1",
                "config": {},
            },
            {
                "id": "b-codex",
                "name": "codex via ollama",
                "engine": "codex",
                "provider": "ollama",
                "is_enabled": True,
                "organization_id": "o1",
                "config": {},
            },
        ]
    }

    class _FakeRepo:
        def __init__(self, pool):
            pass

        async def list(self, *, org_id=None, enabled_only=False, **kwargs):
            return state["rows"]

    monkeypatch.setattr("lucent.db.backends.BackendRepository", _FakeRepo)
    return state


async def test_model_execution_catalog_exposes_engines_and_targets(fake_backends):
    catalog = await bs.model_execution_catalog(
        None,
        org_id="o1",
        models=[_model()],
        configured_default="langchain",
    )
    assert catalog["provider_engine_map"]["ollama"] == [
        "langchain",
        "copilot",
        "claude_code",
        "codex",
    ]
    model = catalog["models"][0]
    assert model["provider"] == "ollama"
    assert model["provider_label"] == "Ollama"
    assert model["default_engine"] == "langchain"
    assert {entry["engine"] for entry in model["engines"]} == {
        "langchain",
        "copilot",
        "claude_code",
        "codex",
    }
    oclauda = next(
        entry for entry in model["engines"] if entry["engine"] == "claude_code"
    )
    assert [backend["id"] for backend in oclauda["backends"]] == ["b-oclauda"]

    targets = catalog["execution_targets"]
    by_backend = {
        (target["engine"], target["backend_id"]): target for target in targets
    }
    # The default engine's configured backend is the default target; the
    # coding-engine backend is an explicit alternative.
    assert by_backend[("langchain", "b-langchain")]["default"] is True
    assert by_backend[("claude_code", "b-oclauda")]["backend_name"] == "oclauda"
    assert by_backend[("codex", "b-codex")]["available"] is True


async def test_local_provider_without_backend_has_no_target(fake_backends):
    """LM Studio has no LangChain path; the Copilot/Codex default needs a
    configured backend, so no backendless target must be offered."""
    model = _model(id="qwen3-coder", provider="lmstudio", name="Qwen3 Coder")
    catalog = await bs.model_execution_catalog(
        None, org_id="o1", models=[model], configured_default="copilot"
    )
    assert catalog["models"][0]["default_engine"] == "copilot"
    assert catalog["execution_targets"] == []


async def test_model_execution_catalog_decision_model_has_no_targets(fake_backends):
    decision = _model(id="nimble:latest", model_class="decision", category="decision")
    catalog = await bs.model_execution_catalog(
        None, org_id="o1", models=[decision], configured_default="langchain"
    )
    model = catalog["models"][0]
    assert model["decision_model"] is True
    assert model["engines"] == []
    assert model["default_engine"] is None
    assert [target for target in catalog["execution_targets"] if target["model"] == decision.id] == []
    assert [entry["id"] for entry in catalog["decision_models"]] == [decision.id]
    assert catalog["decision_models"][0]["provider"] == "ollama"
