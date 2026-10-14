"""Unit tests for the local-provider registry."""

from __future__ import annotations

import pytest

from lucent.llm import local_providers as lp


def test_ollama_exposes_all_surfaces():
    spec = lp.local_provider_spec("ollama")
    assert spec.codex_provider_id == "ollama"
    assert {"openai", "anthropic", "codex-oss"} <= spec.flavors


def test_names_and_choices_in_order():
    assert lp.local_provider_names()[0] == "ollama"
    assert "ollama" in lp.local_provider_choices()


def test_unknown_provider_raises():
    with pytest.raises(lp.UnknownLocalProviderError):
        lp.local_provider_spec("nope")


def test_is_local_provider():
    assert lp.is_local_provider("ollama")
    assert lp.is_local_provider("OLLAMA")
    assert not lp.is_local_provider("anthropic")


def test_non_ollama_provider_is_modelled(monkeypatch):
    # The abstraction is not Ollama-specific.
    spec = lp.local_provider_spec("lmstudio")
    assert spec.codex_provider_id == "lmstudio"
    assert "openai" in spec.flavors
    assert "anthropic" not in spec.flavors


def test_resolve_base_url_config_wins(monkeypatch):
    spec = lp.local_provider_spec("ollama")
    monkeypatch.setenv("OLLAMA_HOST", "http://env-host:11434")
    assert (
        lp.resolve_local_base_url(spec, configured_base_url="http://cfg:11434/")
        == "http://cfg:11434"
    )


def test_resolve_base_url_env_then_default(monkeypatch):
    spec = lp.local_provider_spec("ollama")
    monkeypatch.setenv("OLLAMA_HOST", "http://env-host:11434")
    assert lp.resolve_local_base_url(spec) == "http://env-host:11434"
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert lp.resolve_local_base_url(spec) == "http://localhost:11434"


def test_provider_supports_flavor():
    assert lp.provider_supports_flavor(lp.local_provider_spec("ollama"), "anthropic")
    assert not lp.provider_supports_flavor(lp.local_provider_spec("lmstudio"), "anthropic")
