"""Unit tests for backend-aware engine resolution."""

from __future__ import annotations

import pytest

from lucent.llm import engine_resolution as er
from lucent.model_registry import ModelInfo


def _backend(bid, engine, provider, org=None, enabled=True):
    return {
        "id": bid,
        "engine": engine,
        "provider": provider,
        "organization_id": org,
        "is_enabled": enabled,
    }


_MODEL = ModelInfo(
    id="gpt-5.1",
    provider="openai",
    name="GPT-5.1",
    category="general",
    engine=None,
    backend_id=None,
)


@pytest.fixture
def patch_model(monkeypatch):
    def _apply(model=_MODEL):
        monkeypatch.setattr("lucent.model_registry.get_model", lambda _id: model)

    return _apply


@pytest.fixture
def patch_backends(monkeypatch):
    def _apply(candidates):
        async def _fake(**_kwargs):
            return candidates

        monkeypatch.setattr(er, "_load_backends", _fake)

    return _apply


@pytest.fixture
def patch_fallback(monkeypatch):
    """Patch the default-route fallback to a sentinel engine."""
    sentinel = object()
    import lucent.llm as llm_pkg

    monkeypatch.setattr(llm_pkg, "get_engine_for_model", lambda _model_id: sentinel)
    return sentinel


async def test_requested_backend_wins(patch_model, patch_backends, patch_fallback):
    patch_model()
    patch_backends([_backend("b-lc", "langchain", "openai"), _backend("b-cp", "copilot", "openai")])
    res = await er.resolve_engine("gpt-5.1", org_id="o1", requested_backend="b-cp")
    assert res.source == "requested_backend"
    assert res.engine.name == "copilot"
    assert res.backend["id"] == "b-cp"


async def test_task_backend_beats_model_backend(patch_model, patch_backends, patch_fallback):
    patch_model()
    patch_backends([_backend("b-task", "langchain", "openai")])
    res = await er.resolve_engine("gpt-5.1", org_id="o1", task_backend_id="b-task")
    assert res.source == "task_backend"
    assert res.engine.name == "langchain"


async def test_model_engine_provider_match(patch_model, patch_backends, patch_fallback):
    # Model pins engine=langchain and a matching backend exists.
    patch_model(
        ModelInfo(
            id="gpt-5.1",
            provider="openai",
            name="GPT-5.1",
            category="general",
            engine="langchain",
            backend_id=None,
        )
    )
    patch_backends([_backend("b-lc", "langchain", "openai")])
    res = await er.resolve_engine("gpt-5.1", org_id="o1")
    assert res.source == "model_backend"
    assert res.engine.name == "langchain"


async def test_default_fallback_when_no_backends(patch_model, patch_backends, patch_fallback):
    patch_model()
    patch_backends([])
    res = await er.resolve_engine("gpt-5.1", org_id="o1")
    assert res.source == "default"
    assert res.engine is patch_fallback
    assert res.backend is None


async def test_auto_route_picks_coding_agent_for_agentic_work(
    patch_model, patch_backends, patch_fallback, monkeypatch
):
    patch_model()
    patch_backends([_backend("b-lc", "langchain", "openai"), _backend("b-cp", "copilot", "openai")])
    monkeypatch.setattr("lucent.settings.engine_auto_route", lambda **_kw: True)
    res = await er.resolve_engine(
        "gpt-5.1",
        org_id="o1",
        agent_type="code",
        title="large refactor across modules",
    )
    assert res.source == "auto_route"
    assert res.requested_kind == "coding_agent"
    assert res.engine.name == "copilot"


async def test_auto_route_picks_general_engine_for_plain_chat(
    patch_model, patch_backends, patch_fallback, monkeypatch
):
    patch_model()
    patch_backends([_backend("b-lc", "langchain", "openai"), _backend("b-cp", "copilot", "openai")])
    monkeypatch.setattr("lucent.settings.engine_auto_route", lambda **_kw: True)
    res = await er.resolve_engine("gpt-5.1", org_id="o1", agent_type=None)
    assert res.source == "auto_route"
    assert res.requested_kind == "llm_access"
    assert res.engine.name == "langchain"


async def test_auto_route_disabled_falls_back(
    patch_model, patch_backends, patch_fallback, monkeypatch
):
    patch_model()
    patch_backends([_backend("b-cp", "copilot", "openai")])
    monkeypatch.setattr("lucent.settings.engine_auto_route", lambda **_kw: False)
    res = await er.resolve_engine("gpt-5.1", org_id="o1", agent_type="code")
    assert res.source == "default"


async def test_decision_model_refused(patch_model, patch_backends, patch_fallback):
    patch_model(
        ModelInfo(
            id="nimble",
            provider="ollama",
            name="Nimble",
            category="general",
            model_class="decision",
        )
    )
    patch_backends([])
    with pytest.raises(ValueError, match="decision model"):
        await er.resolve_engine("nimble", org_id="o1")
