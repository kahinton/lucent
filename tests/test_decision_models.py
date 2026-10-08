"""Tests for System One decision-model integration.

Covers the explicit model_class classification boundary: discovery
classifies Ollama decision models, the registry excludes them from
chat-model selection and validates them as chat-unsuitable, the decision
client validates requests and parses /v1/systemone responses, and the
factory refuses to route chat through a decision model.
"""

import json as jsonlib

import pytest

import lucent.model_registry as model_registry
from lucent.model_discovery import ModelDiscoveryService
from lucent.model_registry import (
    ModelInfo,
    get_default_model_id,
    list_models,
    select_model_for_task,
    validate_model,
)


@pytest.fixture(autouse=True)
def _system_tenant_scope():
    """Bind the ambient scope the production model-sync runs under."""
    from lucent.db.pool import clear_tenant_scope, set_tenant_scope

    set_tenant_scope(user_id=None, organization_id=None, role="system")
    yield
    clear_tenant_scope()


@pytest.fixture(autouse=True)
def _fresh_registry_state(monkeypatch):
    """Start every test from hardcoded registry state; DB load is opt-in."""
    monkeypatch.setattr(model_registry, "_db_models", None, raising=False)
    monkeypatch.setattr(model_registry, "_db_model_by_id", {}, raising=False)
    monkeypatch.setattr(model_registry, "_db_enabled_ids", set(), raising=False)
    monkeypatch.setattr(
        model_registry,
        "_MODEL_BY_ID",
        {m.id: m for m in model_registry.MODELS},
        raising=False,
    )


_TEST_MODEL_IDS = {"nimble", "tev1", "gemma3:4b", "some-odd-name:4b", "local-chat"}


@pytest.fixture()
async def _models_cleanup(db_pool):
    """Remove the model IDs these tests write, before and after each test."""
    from lucent.db.models import ModelRepository

    repo = ModelRepository(db_pool)
    for model_id in sorted(_TEST_MODEL_IDS):
        await repo.delete_model(model_id)
    yield
    for model_id in sorted(_TEST_MODEL_IDS):
        await repo.delete_model(model_id)


def _decision_model(**overrides) -> ModelInfo:
    defaults = dict(
        id="nimble",
        provider="ollama",
        name="Nimble",
        category="decision",
        model_class="decision",
        supports_tools=False,
        engine="langchain",
    )
    defaults.update(overrides)
    return ModelInfo(**defaults)


def _ollama_discovery_service(db_pool, tags_rows, show_by_model, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    service = ModelDiscoveryService(db_pool)

    async def fake_get_json(url, **_kwargs):
        return {"models": tags_rows}

    async def fake_post_json(url, *, json=None, **_kwargs):
        return dict(show_by_model.get(json["model"], {}))

    monkeypatch.setattr(service, "_get_json", fake_get_json)
    monkeypatch.setattr(service, "_post_json", fake_post_json)
    return service


# ── Discovery classification ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ollama_discovery_classifies_decision_capability(
    db_pool, monkeypatch
):
    """A decision-capable /api/show entry classifies the model as decision."""
    service = _ollama_discovery_service(
        db_pool,
        [{"model": "some-odd-name:4b", "details": {}}],
        {"some-odd-name:4b": {"capabilities": ["decision"], "model_info": {}}},
        monkeypatch,
    )

    models = await service._discover_ollama()

    assert len(models) == 1
    model = models[0]
    assert model.model_class == "decision"
    assert model.category == "decision"
    assert model.supports_tools is False
    assert "decision" in model.tags
    assert "System One" in model.notes


@pytest.mark.asyncio
async def test_ollama_discovery_classifies_decision_model_family(
    db_pool, monkeypatch
):
    """Known decision families classify even when capabilities are silent."""
    service = _ollama_discovery_service(
        db_pool,
        [{"model": "tev1:0.8b", "details": {"parameter_size": "812MB"}}],
        {"tev1:0.8b": {"capabilities": ["completion"], "model_info": {}}},
        monkeypatch,
    )

    models = await service._discover_ollama()

    model = models[0]
    assert model.model_class == "decision"
    assert model.category == "decision"
    assert model.supports_tools is False
    # Family classification also holds without any /api/show data.
    assert "decision" in model.tags


@pytest.mark.asyncio
async def test_ollama_discovery_keeps_chat_models_language_class(
    db_pool, monkeypatch
):
    """A capability-bearing chat model stays language-classified."""
    service = _ollama_discovery_service(
        db_pool,
        [{"model": "qwen-thinking:4b", "details": {}, "capabilities": ["thinking"]}],
        {
            "qwen-thinking:4b": {
                "capabilities": ["completion", "thinking"],
                "model_info": {},
            }
        },
        monkeypatch,
    )

    models = await service._discover_ollama()

    model = models[0]
    assert model.model_class == "language"
    assert model.category != "decision"
    assert model.reasoning_efforts == ["low", "medium", "high"]


@pytest.mark.asyncio
async def test_sync_discovered_decision_model_persists_model_class(
    db_pool, _models_cleanup, monkeypatch
):
    """The model_class column survives discovery → DB sync."""
    discovered = await _ollama_discovery_service(
        db_pool,
        [
            {"model": "nimble", "details": {}},
            {"model": "gemma3:4b", "details": {}},
        ],
        {
            "nimble": {"capabilities": ["decision"], "model_info": {}},
            "gemma3:4b": {"capabilities": ["completion", "tools"], "model_info": {}},
        },
        monkeypatch,
    )._discover_ollama()

    from lucent.db.models import ModelRepository

    sync_result = await ModelRepository(db_pool).sync_discovered_models(
        provider="ollama",
        models=[m.to_repo_kwargs() for m in discovered],
    )
    assert sync_result["upserted"] == 2

    rows = {
        row["id"]: row
        for row in (await ModelRepository(db_pool).list_models(limit=100))["items"]
    }
    assert rows["nimble"]["model_class"] == "decision"
    assert rows["nimble"]["supports_tools"] is False
    assert rows["gemma3:4b"]["model_class"] == "language"


@pytest.mark.asyncio
async def test_sync_refreshes_rows_whose_derivation_version_changed(
    db_pool, _models_cleanup, monkeypatch
):
    """Digest-unchanged rows still refresh once when derivation logic changes.

    Regression: a Lucent upgrade that changes derived fields (e.g. adding
    model classification) must not be swallowed by the digest-skip path —
    rows written by older code keep old derived values forever.
    """
    from lucent.db.models import ModelRepository

    service = _ollama_discovery_service(
        db_pool,
        [{"model": "nimble", "digest": "sha256:abc", "details": {}}],
        {"nimble": {"capabilities": ["decision"], "model_info": {}}},
        monkeypatch,
    )

    from lucent.model_discovery import _DERIVED_FIELDS_VERSION

    # Seed the row the way an older Lucent version wrote it: the digest
    # matches the catalog, but there is no derived_fields_version marker.
    stale_metadata = {"digest": "sha256:abc", "show_summary": {"capabilities": ["decision"]}}
    await ModelRepository(db_pool).create_model(
        model_id="nimble",
        provider="ollama",
        name="Nimble",
        category="general",
        engine="langchain",
        discovery_source="provider",
        is_custom=False,
        discovery_metadata=dict(stale_metadata),
    )

    sync_result = await service.sync(providers=["ollama"])
    summary = sync_result["providers"][0]
    assert summary.get("upserted") == 1  # not skipped as unchanged

    new_row = await ModelRepository(db_pool).get_model("nimble")
    assert new_row["model_class"] == "decision"
    assert new_row["discovery_metadata"].get("derived_fields_version") == (
        _DERIVED_FIELDS_VERSION
    )


# ── Registry separation ───────────────────────────────────────────────────


def test_decision_models_excluded_from_chat_default_selection():
    models = [
        _decision_model(),
        ModelInfo(id="sonnet", provider="ollama", name="Chat model", category="general"),
    ]

    default_id = get_default_model_id(models)
    selection = select_model_for_task(
        agent_type="code",
        title="Refactor the dispatcher",
        description="Multi-file refactor across sustained steps",
        models=models,
    )

    # is_decision_model consults the registry, so exercise the raw helpers
    # through the selection pool instead.
    assert default_id == "sonnet"
    assert selection.model_id == "sonnet"


def test_explicit_decision_model_falls_back_with_reason():
    models = [
        _decision_model(),
        ModelInfo(id="sonnet", provider="ollama", name="Chat model", category="general"),
    ]

    selection = select_model_for_task(
        explicit_model="nimble",
        models=models,
    )

    assert selection.model_id == "sonnet"
    assert selection.source == "fallback"
    assert "decision model" in selection.reason


def test_list_decision_models_filters_by_class():
    models = [
        _decision_model(),
        _decision_model(id="tev1", name="Tev1"),
        ModelInfo(id="gemma", provider="ollama", name="Chat model", category="general"),
    ]

    decision = [m for m in models if m.model_class == "decision"]
    assert {m.id for m in decision} == {"nimble", "tev1"}


def _available_ids(models):
    from lucent.model_registry import _available_models

    return [m.id for m in _available_models(models)]


def test_available_models_filter_excludes_decision():
    """_available_models is the shared selection-pool filter."""
    models = [
        _decision_model(),
        ModelInfo(id="sonnet", provider="ollama", name="Chat model", category="general"),
    ]
    assert _available_ids(models) == ["sonnet"]


@pytest.mark.asyncio
async def test_db_loaded_registry_excludes_decision_from_default(
    db_pool, _models_cleanup
):
    """load_models_from_db + default selection: decision models stay out."""
    from lucent.db.models import ModelRepository

    repo = ModelRepository(db_pool)
    await repo.create_model(
        model_id="nimble",
        provider="ollama",
        name="Nimble",
        category="decision",
        model_class="decision",
        supports_tools=False,
        engine="langchain",
        discovery_source="provider",
        is_custom=False,
    )
    await repo.create_model(
        model_id="local-chat",
        provider="ollama",
        name="Local chat model",
        category="general",
        supports_tools=True,
        engine="langchain",
        discovery_source="provider",
        is_custom=False,
    )

    await model_registry.load_models_from_db(db_pool)

    assert "nimble" in {m.id for m in list_models()}
    assert "local-chat" in {m.id for m in list_models()}
    assert get_default_model_id() != "nimble"
    assert validate_model("nimble") is not None
    assert "decision model" in validate_model("nimble")


# ── Factory routing guard ─────────────────────────────────────────────────


def test_factory_refuses_chat_routing_for_decision_model(monkeypatch):
    from lucent.llm import factory

    monkeypatch.setattr(
        model_registry, "get_model", lambda model_id: _decision_model()
    )

    with pytest.raises(ValueError, match="decision model"):
        factory.get_engine_for_model("nimble")


# ── Decision client ───────────────────────────────────────────────────────


def _sample_response_payload():
    return {
        "model": "nimble",
        "answers": {
            "team": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.985, "technical": 0.012, "other": 0.003},
                "confidence": 0.922,
            },
            "refund": {"type": "noul", "noul": 0.997},
            "urgency": {
                "type": "score",
                "score": 0.815,
                "legend": {"0": "Routine", "1": "Soon", "2": "Urgent"},
                "probabilities": {"0": 0.378, "1": 0.429, "2": 0.193},
                "confidence": 0.046,
            },
        },
        "usage": {"input_tokens": 841, "output_tokens": 4},
    }


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = jsonlib.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    last_request: dict | None = None

    def __init__(self, payload, *args, **kwargs):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, *, json=None, **kwargs):
        _FakeAsyncClient.last_request = {"url": url, "json": json}
        return _FakeResponse(self._payload)


def _basic_request():
    from lucent.llm.decision import DecisionQuestion, DecisionRequest

    return DecisionRequest(
        model_id="nimble",
        state={"ticket": "I was charged twice. Please refund the extra payment."},
        questions={
            "team": DecisionQuestion(
                name="team",
                type="choice",
                instructions="Which team should handle this ticket?",
                criteria={
                    "billing": "Payments and refunds",
                    "technical": "Bugs and integrations",
                    "other": "None of the above",
                },
            ),
            "refund": DecisionQuestion(
                name="refund",
                type="noul",
                instructions="Does the customer explicitly ask for a refund?",
            ),
            "urgency": DecisionQuestion(
                name="urgency",
                type="score",
                instructions="How urgent is this ticket?",
                criteria=["Routine", "Soon", "Urgent"],
            ),
        },
    )


@pytest.mark.asyncio
async def test_decide_posts_systemone_and_parses_answers(monkeypatch):
    import lucent.llm.decision as decision_module

    monkeypatch.setattr(
        decision_module, "decision_base_url", lambda b=None: "http://localhost:11434"
    )

    class _Client(_FakeAsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(_sample_response_payload(), *args, **kwargs)

    monkeypatch.setattr(decision_module.httpx, "AsyncClient", _Client)

    response = await decision_module.decide(_basic_request())

    request = _FakeAsyncClient.last_request
    assert request["url"] == "http://localhost:11434/v1/systemone"
    assert request["json"]["model"] == "nimble"
    assert request["json"]["questions"]["team"]["criteria"] == {
        "billing": "Payments and refunds",
        "technical": "Bugs and integrations",
        "other": "None of the above",
    }
    assert request["json"]["questions"]["urgency"]["criteria"] == [
        "Routine", "Soon", "Urgent",
    ]

    team = response.answers["team"]
    refund = response.answers["refund"]
    urgency = response.answers["urgency"]
    assert team.choice == "billing"
    assert team.probabilities["billing"] == pytest.approx(0.985)
    assert refund.noul == pytest.approx(0.997)
    assert urgency.score == pytest.approx(0.815)
    assert urgency.legend["2"] == "Urgent"
    assert response.usage.output_tokens == 4


def test_decision_base_url_strips_api_suffix(monkeypatch):
    from lucent.llm.decision import decision_base_url

    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434/api")
    assert decision_base_url() == "http://localhost:11434"


def test_decision_base_url_requires_host(monkeypatch):
    from lucent.llm.decision import DecisionError, decision_base_url

    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    with pytest.raises(DecisionError, match="OLLAMA_HOST"):
        decision_base_url()


def test_decision_client_enforces_question_limit():
    from lucent.llm.decision import DecisionError, DecisionQuestion, DecisionRequest

    request = DecisionRequest(
        model_id="nimble",
        state="x",
        questions={
            f"q{i}": DecisionQuestion(
                name=f"q{i}", type="noul", instructions=f"question {i}"
            )
            for i in range(65)
        },
    )
    with pytest.raises(DecisionError, match="at most 64"):
        from lucent.llm.decision import _validate_request

        _validate_request(request)


def test_decision_client_validates_choice_option_bounds():
    from lucent.llm.decision import DecisionError, DecisionQuestion, DecisionRequest
    from lucent.llm.decision import _validate_request

    bad = DecisionRequest(
        model_id="nimble",
        state="x",
        questions={
            "team": DecisionQuestion(
                name="team",
                type="choice",
                instructions="pick one",
                criteria={"only": "one option"},
            )
        },
    )
    with pytest.raises(DecisionError, match="between 2 and 255"):
        _validate_request(bad)


def test_decision_client_validates_score_level_bounds():
    from lucent.llm.decision import DecisionError, DecisionQuestion, DecisionRequest
    from lucent.llm.decision import _validate_request

    bad = DecisionRequest(
        model_id="nimble",
        state="x",
        questions={
            "priority": DecisionQuestion(
                name="priority",
                type="score",
                instructions="how bad",
                criteria=[f"level-{i}" for i in range(27)],
            )
        },
    )
    with pytest.raises(DecisionError, match="between 2 and 26"):
        _validate_request(bad)


def test_decision_client_rejects_empty_state_questions_only():
    from lucent.llm.decision import DecisionError, DecisionRequest
    from lucent.llm.decision import _validate_request

    empty = DecisionRequest(model_id="nimble", state="x", questions={})
    with pytest.raises(DecisionError, match="at least one question"):
        _validate_request(empty)