"""Unit tests for backend selection (pure resolution logic)."""

from __future__ import annotations

import pytest

from lucent.db.backends import MissingOrganizationScope, resolve_backend_for_model


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
            _backend("b-other", "copilot", "openai", org="o1"),
            _backend("b-explicit", "langchain", "openai", org="o1"),
        ]
        assert (
            resolve_backend_for_model(model, org_id="o1", candidates=candidates)["id"]
            == "b-explicit"
        )

    def test_explicit_backend_id_of_other_org_never_used(self):
        model = {"id": "m", "provider": "openai", "engine": "langchain", "backend_id": "b-other"}
        candidates = [_backend("b-other", "langchain", "openai", org="o2")]
        assert resolve_backend_for_model(model, org_id="o1", candidates=candidates) is None

    def test_org_scoped_backend_matches(self):
        model = {"id": "m", "provider": "openai", "engine": "langchain", "backend_id": None}
        candidates = [
            _backend("b-org", "langchain", "openai", org="o1"),
        ]
        assert resolve_backend_for_model(model, org_id="o1", candidates=candidates)["id"] == "b-org"

    def test_shared_backend_never_used(self):
        """An org-NULL provider row is unreachable, even with no org match."""
        model = {"id": "m", "provider": "ollama", "engine": "langchain", "backend_id": None}
        candidates = [
            _backend("b-org", "langchain", "openai", org="o1"),
            _backend("b-shared", "langchain", "ollama", org=None),
        ]
        assert resolve_backend_for_model(model, org_id="o1", candidates=candidates) is None

    def test_other_org_backend_not_used(self):
        model = {"id": "m", "provider": "openai", "engine": "langchain", "backend_id": None}
        candidates = [_backend("b-other-org", "langchain", "openai", org="o2")]
        assert resolve_backend_for_model(model, org_id="o1", candidates=candidates) is None

    def test_disabled_backend_ignored(self):
        model = {"id": "m", "provider": "openai", "engine": "langchain", "backend_id": "b-off"}
        candidates = [_backend("b-off", "langchain", "openai", org="o1", enabled=False)]
        assert resolve_backend_for_model(model, org_id="o1", candidates=candidates) is None

    def test_engine_provider_mismatch_ignored(self):
        model = {"id": "m", "provider": "openai", "engine": "langchain", "backend_id": None}
        candidates = [_backend("b", "copilot", "openai", org="o1")]
        assert resolve_backend_for_model(model, org_id="o1", candidates=candidates) is None

    def test_missing_org_raises_named_error(self):
        model = {"id": "m", "provider": "openai", "engine": "langchain", "backend_id": None}
        with pytest.raises(MissingOrganizationScope, match="organization id"):
            resolve_backend_for_model(model, org_id=None, candidates=[])  # type: ignore[arg-type]
