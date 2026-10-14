"""Unit tests for backend selection (pure resolution logic)."""

from __future__ import annotations

from lucent.db.backends import resolve_backend_for_model


def _backend(bid, engine, provider, org=None, enabled=True):
    return {
        "id": bid,
        "engine": engine,
        "provider": provider,
        "organization_id": org,
        "is_enabled": enabled,
    }


class TestResolveBackendForModel:
    def test_no_engine_no_backend_id_returns_none(self):
        model = {"id": "gpt-5.1", "provider": "openai", "engine": None, "backend_id": None}
        assert resolve_backend_for_model(model, org_id="o1", candidates=[]) is None

    def test_explicit_backend_id_wins(self):
        model = {"id": "m", "provider": "openai", "engine": "langchain", "backend_id": "b-explicit"}
        candidates = [
            _backend("b-other", "copilot", "openai"),
            _backend("b-explicit", "langchain", "openai"),
        ]
        assert (
            resolve_backend_for_model(model, org_id=None, candidates=candidates)["id"]
            == "b-explicit"
        )

    def test_org_scoped_backend_preferred_over_shared(self):
        model = {"id": "m", "provider": "openai", "engine": "langchain", "backend_id": None}
        candidates = [
            _backend("b-shared", "langchain", "openai", org=None),
            _backend("b-org", "langchain", "openai", org="o1"),
        ]
        assert resolve_backend_for_model(model, org_id="o1", candidates=candidates)["id"] == "b-org"

    def test_shared_backend_used_when_no_org_match(self):
        model = {"id": "m", "provider": "ollama", "engine": "langchain", "backend_id": None}
        candidates = [_backend("b-shared", "langchain", "ollama", org=None)]
        assert (
            resolve_backend_for_model(model, org_id="o1", candidates=candidates)["id"] == "b-shared"
        )

    def test_other_org_backend_not_used(self):
        model = {"id": "m", "provider": "openai", "engine": "langchain", "backend_id": None}
        candidates = [_backend("b-other-org", "langchain", "openai", org="o2")]
        assert resolve_backend_for_model(model, org_id="o1", candidates=candidates) is None

    def test_disabled_backend_ignored(self):
        model = {"id": "m", "provider": "openai", "engine": "langchain", "backend_id": "b-off"}
        candidates = [_backend("b-off", "langchain", "openai", enabled=False)]
        assert resolve_backend_for_model(model, org_id=None, candidates=candidates) is None

    def test_engine_provider_mismatch_ignored(self):
        model = {"id": "m", "provider": "openai", "engine": "langchain", "backend_id": None}
        candidates = [_backend("b", "copilot", "openai")]
        assert resolve_backend_for_model(model, org_id=None, candidates=candidates) is None
