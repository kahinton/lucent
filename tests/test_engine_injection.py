"""Unit tests for local-provider → engine injection."""

from __future__ import annotations

import pytest

from lucent.llm import engine_injection as ei
from lucent.llm import local_providers as lp


def _ollama():
    return lp.local_provider_spec("ollama")


def test_copilot_injection_is_byok_openai():
    inj = ei.build_local_injection("copilot", _ollama(), base_url="http://host:11434")
    assert inj.kwargs["provider"] == {"type": "openai", "base_url": "http://host:11434/v1"}


def test_claude_code_injection_sets_base_url_env():
    inj = ei.build_local_injection("claude_code", _ollama(), base_url="http://host:11434")
    env = inj.kwargs["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://host:11434"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "ollama"
    assert env["ANTHROPIC_API_KEY"] == ""


def test_codex_injection_uses_reserved_provider_id():
    inj = ei.build_local_injection("codex", _ollama(), base_url="http://host:11434")
    assert inj.kwargs["model_provider"] == "ollama"
    # Reserve id => base URL passed via the conventional env var, not model_providers.
    assert inj.kwargs["env"] == {"OLLAMA_HOST": "http://host:11434"}
    assert "config_overrides" not in inj.kwargs


def test_codex_custom_provider_gets_model_providers_block():
    spec = lp.LocalProviderSpec(
        name="my-vllm",
        display_name="vLLM",
        default_base_url="http://vllm:8000",
        flavors=frozenset({"codex-oss"}),
        codex_provider_id="my-vllm",
    )
    inj = ei.build_local_injection("codex", spec)
    assert inj.kwargs["model_provider"] == "my-vllm"
    assert (
        inj.kwargs["config_overrides"]["model_providers"]["my-vllm"]["base_url"]
        == "http://vllm:8000/v1"
    )


def test_engine_without_local_support_raises():
    with pytest.raises(ei.LocalInjectionError):
        ei.build_local_injection("langchain", _ollama())


def test_provider_missing_required_flavor_raises():
    # LM Studio has no anthropic surface -> Claude Code cannot use it.
    with pytest.raises(ei.LocalInjectionError, match="anthropic"):
        ei.build_local_injection("claude_code", lp.local_provider_spec("lmstudio"))


def test_backend_local_injection_none_for_native_provider():
    backend = {"provider": "anthropic", "config": {}}
    assert ei.backend_local_injection("claude_code", backend) is None


def test_backend_local_injection_uses_backend_config_base_url():
    backend = {"provider": "ollama", "config": {"base_url": "http://custom:11434"}}
    inj = ei.backend_local_injection("claude_code", backend)
    assert inj is not None
    assert inj.kwargs["env"]["ANTHROPIC_BASE_URL"] == "http://custom:11434"


def test_engine_local_flavor_map():
    assert ei.engine_local_flavor("copilot") == "openai"
    assert ei.engine_local_flavor("claude_code") == "anthropic"
    assert ei.engine_local_flavor("codex") == "codex-oss"
    assert ei.engine_local_flavor("langchain") is None


# ── provider compatibility (drives the UI's engine+provider pairing) ─────────


def test_compatible_providers_claude_code_excludes_unsupported():
    names = {o["name"] for o in ei.compatible_provider_options("claude_code")}
    assert names == {"anthropic", "ollama"}
    # The bug the UI review caught: gemini must not be offered for Claude Code.
    assert "google" not in names


def test_compatible_providers_codex():
    names = {o["name"] for o in ei.compatible_provider_options("codex")}
    assert names == {"openai", "ollama", "lmstudio"}


def test_compatible_providers_copilot():
    names = {o["name"] for o in ei.compatible_provider_options("copilot")}
    assert names == {
        "anthropic",
        "copilot",
        "google",
        "lmstudio",
        "ollama",
        "openai",
        "xai",
    }


def test_compatible_providers_langchain():
    names = {o["name"] for o in ei.compatible_provider_options("langchain")}
    assert names == {"anthropic", "google", "ollama", "openai"}


def test_compatible_providers_are_grouped():
    groups = {o["group"] for o in ei.compatible_provider_options("claude_code")}
    assert groups == {"native", "local"}


def test_provider_allowed_for_engine():
    assert ei.provider_allowed_for_engine("claude_code", "anthropic")
    assert ei.provider_allowed_for_engine("claude_code", "ollama")
    assert not ei.provider_allowed_for_engine("claude_code", "google")
    assert not ei.provider_allowed_for_engine("codex", "anthropic")
    assert ei.provider_allowed_for_engine("codex", "openai")
