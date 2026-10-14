"""Unit tests for the engine registry and registry-driven factory."""

from __future__ import annotations

import pytest

from lucent.llm import engine_registry as er


class TestEngineRegistry:
    def test_implemented_engine_names_order(self):
        assert er.engine_names() == ["langchain", "copilot", "claude_code", "codex"]

    def test_all_builtin_engines_are_implemented(self):
        for name in ("langchain", "copilot", "claude_code", "codex"):
            assert er.engine_spec(name).implemented
            assert er.engine_spec(name).factory is not None

    def test_kinds(self):
        assert er.engine_kind("langchain") == "llm_access"
        assert er.engine_kind("copilot") == "coding_agent"
        assert er.engine_kind("claude_code") == "coding_agent"
        assert er.engine_kind("codex") == "coding_agent"

    def test_unknown_engine_raises(self):
        with pytest.raises(er.UnknownEngineError):
            er.engine_spec("does-not-exist")

    def test_engine_names_filter_by_kind(self):
        assert er.engine_names(kind="llm_access") == ["langchain"]
        assert er.engine_names(kind="coding_agent") == ["copilot", "claude_code", "codex"]

    def test_default_for_provider(self):
        assert er.engine_default_for_provider("ollama") == "langchain"
        # Cloud providers have no forced engine — they use the configured default.
        assert er.engine_default_for_provider("openai") is None
        assert er.engine_default_for_provider("anthropic") is None

    def test_capabilities_langchain_is_general(self):
        caps = er.capabilities_for("langchain")
        assert caps.native_agent_loop is False
        assert caps.expected_surface is True
        assert caps.managed_tools is True
        assert caps.hooks is True
        assert caps.resume is False

    def test_capabilities_copilot_is_coding_agent(self):
        caps = er.capabilities_for("copilot")
        assert caps.native_agent_loop is True
        assert caps.resume is True
        assert caps.managed_tools is False
        assert caps.expected_surface is False

    def test_capabilities_for_instance_and_unknown(self):
        class _Fake:
            name = "langchain"

        assert er.capabilities_for(_Fake()).native_agent_loop is False
        # Unknown engines yield an all-False set rather than raising.
        unknown = er.capabilities_for("nope")
        assert unknown.native_agent_loop is False
        assert unknown.resume is False

    def test_config_schema_is_attached(self):
        copilot_fields = {f.key for f in er.engine_spec("copilot").config_schema}
        assert {"base_url", "provider_type", "api_key"} <= copilot_fields
        # langchain carries no extra engine-backend fields.
        assert er.engine_spec("langchain").config_schema == ()

    def test_availability_shape(self):
        avail = er.engine_availability("langchain")
        assert isinstance(avail.available, bool)
        assert avail.available is True  # langchain is a base dependency


class TestFactoryRegistryDriven:
    def setup_method(self):
        from lucent.llm.factory import reset_engine

        reset_engine()

    def teardown_method(self):
        from lucent.llm.factory import reset_engine

        reset_engine()

    def test_default_engine_from_registry(self, monkeypatch):
        from lucent.llm.factory import get_engine, get_engine_name, reset_engine

        monkeypatch.delenv("LUCENT_LLM_ENGINE", raising=False)
        reset_engine()
        assert get_engine_name() == "copilot"
        assert get_engine().name == "copilot"

    def test_unknown_default_engine_raises(self, monkeypatch):
        from lucent.llm.factory import get_engine, reset_engine

        monkeypatch.setattr("lucent.llm.factory.get_engine_name", lambda: "invalid")
        reset_engine()
        with pytest.raises(ValueError, match="Unknown LLM engine"):
            get_engine()

    def test_declared_but_unimplemented_engine_not_constructed(self):
        # A declared-but-unimplemented engine cannot be constructed; registration
        # with implemented=False keeps the guard honest for future engines.
        from dataclasses import replace

        original = er.ENGINE_REGISTRY.get("codex")
        try:
            er.ENGINE_REGISTRY["codex"] = replace(
                er.engine_spec("codex"), implemented=False, factory=None
            )
            with pytest.raises(er.UnknownEngineError):
                er.get_engine("codex")
        finally:
            if original is not None:
                er.ENGINE_REGISTRY["codex"] = original
