"""Tests for the LLM engine abstraction layer."""

import os
from unittest.mock import patch

import pytest

from lucent.llm.engine import SessionEvent, SessionEventType
from lucent.llm.factory import get_engine, get_engine_for_model, get_engine_name, reset_engine


class TestSessionEvent:
    def test_event_creation(self):
        event = SessionEvent(type=SessionEventType.MESSAGE, content="Hello")
        assert event.type == SessionEventType.MESSAGE
        assert event.content == "Hello"

    def test_event_types(self):
        assert SessionEventType.MESSAGE.value == "assistant.message"
        assert SessionEventType.TOOL_CALL.value == "tool.call"
        assert SessionEventType.SESSION_IDLE.value == "session.idle"
        assert SessionEventType.ERROR.value == "error"


class TestEngineFactory:
    def setup_method(self):
        reset_engine()

    def teardown_method(self):
        reset_engine()

    def test_default_engine_is_copilot(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LUCENT_LLM_ENGINE", None)
            reset_engine()
            assert get_engine_name() == "copilot"
            engine = get_engine()
            assert engine.name == "copilot"

    def test_copilot_engine_explicit(self):
        with patch.dict(os.environ, {"LUCENT_LLM_ENGINE": "copilot"}):
            reset_engine()
            engine = get_engine()
            assert engine.name == "copilot"

    def test_langchain_engine(self):
        with patch.dict(os.environ, {"LUCENT_LLM_ENGINE": "langchain"}):
            reset_engine()
            engine = get_engine()
            assert engine.name == "langchain"

    def test_invalid_engine_raises(self):
        with patch.dict(os.environ, {"LUCENT_LLM_ENGINE": "invalid"}):
            reset_engine()
            with pytest.raises(ValueError, match="Unknown LLM engine"):
                get_engine()

    def test_singleton_behavior(self):
        reset_engine()
        e1 = get_engine()
        e2 = get_engine()
        assert e1 is e2

    def test_reset_clears_singleton(self):
        reset_engine()
        e1 = get_engine()
        reset_engine()
        e2 = get_engine()
        assert e1 is not e2


class TestCopilotEngine:
    def test_name(self):
        from lucent.llm.copilot_engine import CopilotEngine

        engine = CopilotEngine()
        assert engine.name == "copilot"

    @pytest.mark.asyncio
    async def test_cleanup_is_noop(self):
        from lucent.llm.copilot_engine import CopilotEngine

        engine = CopilotEngine()
        await engine.cleanup()  # Should not raise

    @pytest.mark.asyncio
    async def test_provider_github_token_uses_system_managed_provider_secret(self, monkeypatch):
        from lucent.llm.copilot_engine import CopilotEngine

        captured = {}

        class FakeSecretProvider:
            async def get(self, key, scope):
                captured["secret_key"] = key
                captured["scope"] = scope
                return "saved-copilot-token"

        monkeypatch.setattr("lucent.secrets.SecretRegistry.get", lambda: FakeSecretProvider())

        engine = CopilotEngine(github_token="env-token")
        token = await engine._provider_github_token({"organization_id": "org-123"})

        assert token == "saved-copilot-token"
        assert captured["secret_key"] == "model_providers.copilot.github_token"
        assert captured["scope"].organization_id == "org-123"
        assert captured["scope"].system_managed is True

    def test_enable_config_discovery_session_kwarg(self):
        from lucent.llm.copilot_engine import CopilotEngine

        engine = CopilotEngine()
        kwargs = engine._make_session_kwargs(
            "claude-opus-4.7",
            "system",
            {},
            enable_config_discovery=True,
        )

        assert kwargs["enable_config_discovery"] is True

    def test_mutable_mcp_permission_uses_legacy_compat(self):
        from lucent.llm.copilot_engine import _should_use_legacy_permission_response

        class Request:
            kind = "mcp"
            read_only = False

        class Result:
            kind = "approved"

        assert _should_use_legacy_permission_response(Request(), Result()) is True

    def test_read_only_mcp_permission_uses_legacy_compat(self):
        from lucent.llm.copilot_engine import _should_use_legacy_permission_response

        class Request:
            kind = "mcp"
            read_only = True

        class Result:
            kind = "approved"

        assert _should_use_legacy_permission_response(Request(), Result()) is True

    def test_bash_permission_uses_legacy_compat(self):
        from lucent.llm.copilot_engine import _should_use_legacy_permission_response

        class Request:
            kind = "tool"
            tool_name = "bash"

        class Result:
            kind = "approved"

        assert _should_use_legacy_permission_response(Request(), Result()) is True

    @pytest.mark.asyncio
    async def test_legacy_mcp_permission_payload_shape(self):
        from lucent.llm.copilot_engine import _send_legacy_permission_response

        calls = []

        class Client:
            async def request(self, method, payload):
                calls.append((method, payload))
                return {"success": True}

        class Permissions:
            _client = Client()

        class Rpc:
            permissions = Permissions()

        class Session:
            session_id = "session-123"
            rpc = Rpc()

        class Result:
            kind = "approved"
            feedback = None
            message = None
            path = None

        await _send_legacy_permission_response(Session(), "perm-123", Result())

        assert calls == [
            (
                "session.permissions.handlePendingPermissionRequest",
                {
                    "requestId": "perm-123",
                    "result": {"kind": "approve-once"},
                    "sessionId": "session-123",
                },
            )
        ]

    def test_copilot_compaction_token_payload_compat(self):
        pytest.importorskip("copilot.generated.session_events")
        import copilot.generated.session_events as session_events

        from lucent.llm import copilot_engine

        copilot_engine._session_event_compat_installed = False
        copilot_engine._install_copilot_session_event_compat()

        compaction_cls = getattr(session_events, "CompactionTokensUsed", None) or getattr(
            session_events,
            "CompactionCompleteCompactionTokensUsed",
        )
        parsed = compaction_cls.from_dict({"inputTokens": "42", "outputTokens": 7})

        assert getattr(parsed, "cached_input", getattr(parsed, "cache_read_tokens", None)) == 0.0
        assert getattr(parsed, "input", getattr(parsed, "input_tokens", None)) == 42.0
        assert getattr(parsed, "output", getattr(parsed, "output_tokens", None)) == 7.0

    def test_copilot_ping_response_iso_timestamp_compat(self):
        pytest.importorskip("copilot.client")
        from copilot.client import PingResponse

        from lucent.llm import copilot_engine

        copilot_engine._session_event_compat_installed = False
        copilot_engine._install_copilot_session_event_compat()

        parsed = PingResponse.from_dict(
            {
                "message": "pong: lucent",
                "timestamp": "2026-05-29T13:04:15.613Z",
                "protocolVersion": 1,
            }
        )

        assert parsed.message == "pong: lucent"
        assert parsed.timestamp == 1780059855613
        assert parsed.protocolVersion == 1


class TestLangChainEngine:
    def test_name(self):
        from lucent.llm.langchain_engine import LangChainEngine

        engine = LangChainEngine()
        assert engine.name == "langchain"

    @pytest.mark.asyncio
    async def test_cleanup_is_noop(self):
        from lucent.llm.langchain_engine import LangChainEngine

        engine = LangChainEngine()
        await engine.cleanup()  # Should not raise

    @pytest.mark.asyncio
    async def test_builtin_tools_are_bound_and_executed(self, tmp_path, monkeypatch):
        """LangChain engine binds built-in tools and runs them with no MCP config."""
        from langchain_core.messages import AIMessage

        from lucent.llm import langchain_engine
        from lucent.llm.langchain_engine import LangChainEngine

        monkeypatch.setenv("LUCENT_LANGCHAIN_TOOLS_ROOT", str(tmp_path))

        bound_schemas: dict = {}

        class FakeChatModel:
            def __init__(self):
                self._call = 0

            def bind_tools(self, schemas):
                bound_schemas["names"] = {s["function"]["name"] for s in schemas}
                return self

            async def ainvoke(self, messages):
                self._call += 1
                if self._call == 1:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "create_file",
                                "args": {"path": "out.txt", "content": "hi"},
                                "id": "call_1",
                            }
                        ],
                    )
                return AIMessage(content="done")

        async def fake_get_chat_model(*_args, **_kwargs):
            return FakeChatModel()

        monkeypatch.setattr(langchain_engine, "_get_chat_model", fake_get_chat_model)

        engine = LangChainEngine()
        result = await engine.run_session(
            model="qwen3.6:latest",
            system_message="sys",
            prompt="make a file",
            mcp_config=None,
        )

        assert result == "done"
        # Built-in tools were bound even without any MCP config.
        assert "create_file" in bound_schemas["names"]
        assert "web_fetch" in bound_schemas["names"]
        # The tool actually executed against the confined root.
        assert (tmp_path / "out.txt").read_text() == "hi"

    @pytest.mark.asyncio
    async def test_failed_mcp_bridge_does_not_abort_session(self, tmp_path, monkeypatch):
        """An SSRF-blocked/unreachable MCP server is skipped, not fatal.

        Built-in tools must still run so the task can complete.
        """
        from langchain_core.messages import AIMessage

        from lucent.llm import langchain_engine, mcp_bridge
        from lucent.llm.langchain_engine import LangChainEngine
        from lucent.url_validation import SSRFError

        monkeypatch.setenv("LUCENT_LANGCHAIN_TOOLS_ROOT", str(tmp_path))

        # Simulate the real failure: bridge construction rejects the URL.
        def boom(*_args, **_kwargs):
            raise SSRFError("blocked address 127.0.0.1")

        monkeypatch.setattr(mcp_bridge, "MCPToolBridge", boom)

        class FakeChatModel:
            def __init__(self):
                self._call = 0

            def bind_tools(self, schemas):
                return self

            async def ainvoke(self, messages):
                self._call += 1
                if self._call == 1:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "create_file", "args": {"path": "z.txt", "content": "ok"}, "id": "c1"}
                        ],
                    )
                return AIMessage(content="done")

        async def fake_get_chat_model(*_args, **_kwargs):
            return FakeChatModel()

        monkeypatch.setattr(langchain_engine, "_get_chat_model", fake_get_chat_model)

        engine = LangChainEngine()
        result = await engine.run_session(
            model="qwen3.6:latest",
            system_message="sys",
            prompt="make a file",
            mcp_config={
                "memory-server": {
                    "type": "http",
                    "url": "http://localhost:8765/mcp",
                    "headers": {},
                    "tools": ["*"],
                }
            },
        )

        assert result == "done"
        # The blocked bridge did not abort the session; built-in tool ran.
        assert (tmp_path / "z.txt").read_text() == "ok"

    @pytest.mark.asyncio
    async def test_internal_mcp_config_bypasses_ssrf_validation(self, monkeypatch):
        """Lucent's own MCP endpoint (internal=True) must skip SSRF validation.

        On a clean install the endpoint defaults to a loopback URL
        (http://localhost:8766/mcp). With no allowlist configured that host
        would be SSRF-blocked, the bridge would be skipped, and tools like
        get_skill_definition would never load — so the model just emits raw
        tool-call JSON. The internal flag is what keeps it working out of the
        box; external (user-supplied) servers must still be validated.
        """
        from lucent.llm.langchain_engine import LangChainEngine

        # Ensure no allowlist is set, mirroring a clean `docker-compose up`.
        monkeypatch.delenv("LUCENT_MCP_URL_ALLOWLIST", raising=False)

        captured: list[tuple[str, bool]] = []

        class FakeBridge:
            def __init__(self, *, mcp_url, skip_url_validation=False, **_kwargs):
                captured.append((mcp_url, skip_url_validation))

            async def discover_tools(self):
                return []

            async def close(self):
                return None

        import lucent.llm.mcp_bridge as mcp_bridge

        monkeypatch.setattr(mcp_bridge, "MCPToolBridge", FakeBridge)

        engine = LangChainEngine()
        await engine._create_bridges(
            {
                "memory-server": {
                    "type": "http",
                    "url": "http://localhost:8766/mcp",
                    "headers": {},
                    "tools": ["*"],
                    "internal": True,
                },
                "external": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headers": {},
                    "tools": ["*"],
                },
            }
        )

        by_url = dict(captured)
        # Internal endpoint skips validation; external one does not.
        assert by_url["http://localhost:8766/mcp"] is True
        assert by_url["https://example.com/mcp"] is False

    @pytest.mark.asyncio
    async def test_builtin_tools_excluded_for_restricted_web_chat(self, monkeypatch):
        """approve_permissions=False (restricted web chat) binds no built-ins."""
        from langchain_core.messages import AIMessage

        from lucent.llm import langchain_engine
        from lucent.llm.langchain_engine import LangChainEngine

        bound: dict = {"called": False}

        class FakeChatModel:
            def bind_tools(self, schemas):
                bound["called"] = True
                return self

            async def ainvoke(self, messages):
                return AIMessage(content="ok")

        async def fake_get_chat_model(*_args, **_kwargs):
            return FakeChatModel()

        monkeypatch.setattr(langchain_engine, "_get_chat_model", fake_get_chat_model)

        engine = LangChainEngine()
        result = await engine.run_session(
            model="qwen3.6:latest",
            system_message="sys",
            prompt="hi",
            mcp_config=None,
            approve_permissions=False,
        )
        assert result == "ok"
        # No tools to bind → bind_tools never called.
        assert bound["called"] is False



class TestModelResolve:
    def test_resolve_anthropic(self):
        from lucent.llm.langchain_engine import _resolve_model

        provider, model_id = _resolve_model("claude-opus-4.6")
        assert provider == "anthropic"
        assert model_id == "claude-opus-4-6-20260301"

    def test_resolve_anthropic_latest_default(self):
        from lucent.llm.langchain_engine import _resolve_model

        provider, model_id = _resolve_model("claude-opus-4.7")
        assert provider == "anthropic"
        assert model_id == "claude-opus-4.7"

    def test_resolve_openai(self):
        from lucent.llm.langchain_engine import _resolve_model

        provider, model_id = _resolve_model("gpt-5.2")
        assert provider == "openai"
        assert model_id == "gpt-5.2"

    def test_resolve_google(self):
        from lucent.llm.langchain_engine import _resolve_model

        provider, model_id = _resolve_model("gemini-3-pro")
        assert provider == "google_genai"
        assert model_id == "gemini-3-pro"

    def test_resolve_unknown_infers_prefix(self):
        from lucent.llm.langchain_engine import _resolve_model

        provider, model_id = _resolve_model("claude-future-5.0")
        assert provider == "anthropic"

    def test_resolve_completely_unknown(self):
        from lucent.llm.langchain_engine import _resolve_model

        provider, model_id = _resolve_model("some-custom-model")
        assert provider == ""
        assert model_id == "some-custom-model"


class TestEngineRoutingByModel:
    def teardown_method(self):
        from lucent.llm.langchain_engine import _runtime_model_registry

        _runtime_model_registry.clear()
        reset_engine()

    def test_explicit_langchain_override_wins(self):
        from lucent.llm.langchain_engine import register_model

        register_model(
            "anthropic-via-langchain",
            "anthropic",
            "claude-sonnet-4-6-20260115",
            "langchain",
        )
        engine = get_engine_for_model("anthropic-via-langchain")
        assert engine.name == "langchain"

    def test_explicit_copilot_override_wins(self):
        from lucent.llm.langchain_engine import register_model

        register_model("ollama-via-copilot", "ollama", "llama3.2", "copilot")
        engine = get_engine_for_model("ollama-via-copilot")
        assert engine.name == "copilot"

    def test_auto_override_falls_back_to_provider(self):
        from lucent.llm.langchain_engine import register_model

        register_model("ollama-auto-routing", "ollama", "llama3.2", "auto")
        engine = get_engine_for_model("ollama-auto-routing")
        assert engine.name == "langchain"

    def test_null_engine_preserves_auto_detection(self):
        from lucent.llm.langchain_engine import register_model

        with patch.dict(os.environ, {"LUCENT_LLM_ENGINE": "copilot"}):
            register_model("openai-auto-routing", "openai", "gpt-5.2", None)
            engine = get_engine_for_model("openai-auto-routing")
            assert engine.name == "copilot"

    def test_copilot_override_openai_uses_copilot_sdk(self):
        from lucent.llm.langchain_engine import register_model

        register_model("openai-via-copilot", "openai", "gpt-5.2", "copilot")
        engine = get_engine_for_model("openai-via-copilot")
        assert engine.name == "copilot"

    def test_static_engine_override_for_copilot_hosted_model(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo

        monkeypatch.setattr(model_registry, "_db_models", None)
        monkeypatch.setitem(
            model_registry._MODEL_BY_ID,
            "copilot-static-test",
            ModelInfo(
                id="copilot-static-test",
                provider="copilot",
                name="Copilot Static Test",
                category="general",
                api_model_id="copilot-static-test",
                engine="copilot",
            ),
        )
        with patch.dict(os.environ, {"LUCENT_LLM_ENGINE": "langchain"}):
            reset_engine()
            engine = get_engine_for_model("copilot-static-test")
            assert engine.name == "copilot"

    def test_register_model_override_replaces_auto_detection(self):
        from lucent.llm.langchain_engine import register_model

        register_model("switching-model", "ollama", "llama3.2", None)
        assert get_engine_for_model("switching-model").name == "langchain"
        register_model("switching-model", "ollama", "llama3.2", "copilot")
        assert get_engine_for_model("switching-model").name == "copilot"

    def test_registry_update_during_runtime_is_stable(self):
        from lucent.llm.langchain_engine import register_model

        register_model("runtime-race-model", "ollama", "llama3.2", None)
        first_engine = get_engine_for_model("runtime-race-model")
        register_model("runtime-race-model", "ollama", "llama3.2", "copilot")
        second_engine = get_engine_for_model("runtime-race-model")
        assert first_engine.name == "langchain"
        assert second_engine.name == "copilot"

    def test_restart_resync_reloads_engine_preferences(self):
        from lucent.llm.langchain_engine import _runtime_model_registry, register_model

        register_model("restart-sync-model", "anthropic", "claude-sonnet-4-6-20260115", "langchain")
        _runtime_model_registry.clear()  # simulate daemon restart
        register_model("restart-sync-model", "anthropic", "claude-sonnet-4-6-20260115", "langchain")
        engine = get_engine_for_model("restart-sync-model")
        assert engine.name == "langchain"


class TestModelEngineValidation:
    def test_invalid_engine_rejected(self):
        from lucent.llm.model_engine_validation import normalize_engine

        with pytest.raises(ValueError, match="Invalid engine value"):
            normalize_engine("bad-engine")

    def test_copilot_unsupported_provider_warns(self):
        from lucent.llm.model_engine_validation import validate_engine_override

        warnings = validate_engine_override("mistral", "copilot")
        assert warnings
        assert "may not be supported by Copilot SDK" in warnings[0]

    def test_langchain_missing_provider_package_errors(self, monkeypatch):
        from lucent.llm import model_engine_validation as mev

        def _fake_find_spec(name: str):
            if name == "langchain":
                return object()
            if name == "langchain_anthropic":
                return None
            return object()

        monkeypatch.setattr(mev, "find_spec", _fake_find_spec)
        with pytest.raises(ValueError, match="requires package 'langchain_anthropic'"):
            mev.validate_engine_override("anthropic", "langchain")


class TestModelRegistry:
    def setup_method(self):
        from lucent import model_registry

        model_registry._db_models = None
        model_registry._db_model_by_id = {}
        model_registry._db_enabled_ids = set()
        model_registry._MODEL_BY_ID = {m.id: m for m in model_registry.MODELS}

    def test_api_model_id(self):
        from lucent.model_registry import get_api_model_id

        assert get_api_model_id("claude-opus-4.6") == "claude-opus-4-6-20260301"
        assert get_api_model_id("claude-opus-4.7") == "claude-opus-4.7"
        assert get_api_model_id("gpt-5.2") == "gpt-5.2"
        assert get_api_model_id("unknown-model") == "unknown-model"

    def test_get_provider(self):
        from lucent.model_registry import get_provider

        assert get_provider("claude-opus-4.6") == "anthropic"
        assert get_provider("claude-opus-4.7") == "anthropic"
        assert get_provider("gpt-5.2") == "openai"
        assert get_provider("gemini-3-pro") == "google"
        assert get_provider("unknown") is None

    def test_provider_inference(self):
        from lucent.model_registry import get_provider

        assert get_provider("claude-future") == "anthropic"
        assert get_provider("gpt-6") == "openai"
        assert get_provider("gemini-4") == "google"

    def test_default_model_prefers_enabled_general_model(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, get_default_model_id

        models = [
            ModelInfo(id="expensive-reasoner", provider="x", name="Reasoner", category="reasoning"),
            ModelInfo(id="balanced-default", provider="x", name="Balanced", category="general"),
        ]
        monkeypatch.setattr(model_registry, "_db_models", models)
        monkeypatch.setattr(model_registry, "_db_enabled_ids", {m.id for m in models})
        monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {m.id: m for m in models})

        assert get_default_model_id() == "balanced-default"

    def test_task_selection_uses_default_without_clear_specialized_need(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, select_model_for_task

        models = [
            ModelInfo(id="balanced-default", provider="x", name="Balanced", category="general"),
            ModelInfo(id="expensive-reasoner", provider="x", name="Reasoner", category="reasoning"),
        ]
        monkeypatch.setattr(model_registry, "_db_models", models)
        monkeypatch.setattr(model_registry, "_db_enabled_ids", {m.id for m in models})
        monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {m.id: m for m in models})

        selection = select_model_for_task(agent_type="planning", title="Break down request")

        assert selection.model_id == "balanced-default"
        assert selection.source == "default"

    def test_task_selection_specializes_for_clear_reasoning_signal(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, select_model_for_task

        models = [
            ModelInfo(id="balanced-default", provider="x", name="Balanced", category="general"),
            ModelInfo(id="deep-reasoner", provider="x", name="Deep", category="reasoning"),
        ]
        monkeypatch.setattr(model_registry, "_db_models", models)
        monkeypatch.setattr(model_registry, "_db_enabled_ids", {m.id for m in models})
        monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {m.id: m for m in models})

        selection = select_model_for_task(
            agent_type="research",
            title="Investigate root cause of a complex architecture issue",
        )

        assert selection.model_id == "deep-reasoner"
        assert selection.source == "specialized"

    def test_planning_selection_can_override_fast_default_for_complex_work(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, select_model_for_task

        models = [
            ModelInfo(id="deep-reasoner", provider="x", name="Deep", category="reasoning"),
            ModelInfo(id="cheap-fast", provider="x", name="Cheap", category="fast"),
        ]
        monkeypatch.setattr(model_registry, "_db_models", models)
        monkeypatch.setattr(model_registry, "_db_enabled_ids", {m.id for m in models})
        monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {m.id: m for m in models})

        selection = select_model_for_task(
            agent_type="planning",
            title="Build proof-of-concept business planning repo",
            description="Create a multi-step strategy, synthesis, roadmap, and risk plan.",
            require_tools=True,
        )

        assert selection.default_model_id == "cheap-fast"
        assert selection.model_id == "deep-reasoner"
        assert selection.requested_category == "reasoning"
        assert selection.source == "specialized"

    def test_task_selection_uses_fast_for_memory_when_available(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, select_model_for_task

        models = [
            ModelInfo(id="balanced-default", provider="x", name="Balanced", category="general"),
            ModelInfo(id="cheap-fast", provider="x", name="Cheap", category="fast"),
        ]
        monkeypatch.setattr(model_registry, "_db_models", models)
        monkeypatch.setattr(model_registry, "_db_enabled_ids", {m.id for m in models})
        monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {m.id: m for m in models})

        selection = select_model_for_task(agent_type="memory", title="Tag memories")

        assert selection.model_id == "cheap-fast"
        assert selection.source == "specialized"

    def test_task_selection_filters_non_tool_models_when_required(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, select_model_for_task

        models = [
            ModelInfo(
                id="cheap-fast-no-tools",
                provider="ollama",
                name="Cheap",
                category="fast",
                supports_tools=False,
            ),
            ModelInfo(id="tool-default", provider="x", name="Tool", category="general"),
        ]
        monkeypatch.setattr(model_registry, "_db_models", models)
        monkeypatch.setattr(model_registry, "_db_enabled_ids", {m.id for m in models})
        monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {m.id: m for m in models})

        selection = select_model_for_task(
            agent_type="memory",
            title="Tag memories",
            require_tools=True,
        )

        assert selection.model_id == "tool-default"

    def test_task_selection_does_not_honor_explicit_non_tool_model_for_tool_task(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, select_model_for_task

        models = [
            ModelInfo(
                id="local-text-only",
                provider="ollama",
                name="Text Only",
                category="fast",
                supports_tools=False,
            ),
            ModelInfo(id="tool-default", provider="x", name="Tool", category="general"),
        ]
        monkeypatch.setattr(model_registry, "_db_models", models)
        monkeypatch.setattr(model_registry, "_db_enabled_ids", {m.id for m in models})
        monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {m.id: m for m in models})

        selection = select_model_for_task(
            agent_type="memory",
            title="Tag memories",
            explicit_model="local-text-only",
            require_tools=True,
        )

        assert selection.model_id == "tool-default"
        assert "lacks required capability" in selection.reason

    def test_high_risk_memory_task_prefers_reasoning_over_fast(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, select_model_for_task

        models = [
            ModelInfo(id="balanced-default", provider="x", name="Balanced", category="general"),
            ModelInfo(id="cheap-fast", provider="x", name="Cheap", category="fast"),
            ModelInfo(id="deep-reasoner", provider="x", name="Deep", category="reasoning"),
        ]
        monkeypatch.setattr(model_registry, "_db_models", models)
        monkeypatch.setattr(model_registry, "_db_enabled_ids", {m.id for m in models})
        monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {m.id: m for m in models})

        selection = select_model_for_task(
            agent_type="memory",
            title="Soft-delete retired memories and verify zero active records",
            require_tools=True,
        )

        assert selection.model_id == "deep-reasoner"
        assert selection.requested_category == "reasoning"

    def test_validate_model_can_require_tools(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, validate_model

        m = ModelInfo(
            id="text-only",
            provider="ollama",
            name="Text Only",
            category="general",
            supports_tools=False,
        )
        monkeypatch.setattr(model_registry, "_db_models", [m])
        monkeypatch.setattr(model_registry, "_db_model_by_id", {m.id: m})
        monkeypatch.setattr(model_registry, "_db_enabled_ids", {m.id})

        assert validate_model("text-only") is None
        error = validate_model("text-only", require_tools=True)
        assert error is not None
        assert "does not support tool" in error

    @pytest.mark.asyncio
    async def test_db_load_keeps_null_engine_for_existing_models(self, monkeypatch):
        from lucent import model_registry

        class _Repo:
            def __init__(self, _pool):
                pass

            async def list_models(self, **_kwargs):
                return {
                    "items": [
                        {
                            "id": "legacy-null-engine-model",
                            "provider": "openai",
                            "name": "Legacy",
                            "category": "general",
                            "api_model_id": "gpt-4.1",
                            "engine": None,
                            "is_enabled": True,
                        }
                    ]
                }

        monkeypatch.setattr("lucent.db.models.ModelRepository", _Repo)
        loaded = await model_registry.load_models_from_db(object())
        match = next(m for m in loaded if m.id == "legacy-null-engine-model")
        assert match.engine is None

    @pytest.mark.asyncio
    async def test_db_reload_resyncs_engine_preferences(self, monkeypatch):
        from lucent import model_registry

        class _Repo:
            def __init__(self, _pool):
                pass

            async def list_models(self, **_kwargs):
                return {
                    "items": [
                        {
                            "id": "resync-model",
                            "provider": "anthropic",
                            "name": "Resync",
                            "category": "general",
                            "api_model_id": "claude-sonnet-4-6-20260115",
                            "engine": "langchain",
                            "is_enabled": True,
                        }
                    ]
                }

        monkeypatch.setattr("lucent.db.models.ModelRepository", _Repo)
        loaded = await model_registry.load_models_from_db(object())
        match = next(m for m in loaded if m.id == "resync-model")
        assert match.engine == "langchain"

    @pytest.mark.asyncio
    async def test_db_load_registers_ollama_model_for_runtime_routing(self, monkeypatch):
        from lucent import model_registry
        from lucent.llm.langchain_engine import _resolve_model, _runtime_model_registry

        class _Repo:
            def __init__(self, _pool):
                pass

            async def list_models(self, **_kwargs):
                return {
                    "items": [
                        {
                            "id": "local-llama",
                            "provider": "ollama",
                            "name": "Local Llama",
                            "category": "general",
                            "api_model_id": "llama3.2:latest",
                            "engine": None,
                            "is_enabled": True,
                        }
                    ]
                }

        _runtime_model_registry.clear()
        monkeypatch.setattr("lucent.db.models.ModelRepository", _Repo)
        try:
            await model_registry.load_models_from_db(object())
            provider, provider_model_id = _resolve_model("local-llama")
            assert provider == "ollama"
            assert provider_model_id == "llama3.2:latest"
            assert get_engine_for_model("local-llama").name == "langchain"
        finally:
            _runtime_model_registry.clear()
            monkeypatch.setattr(model_registry, "_db_models", None)
            monkeypatch.setattr(
                model_registry,
                "_MODEL_BY_ID",
                {m.id: m for m in model_registry.MODELS},
            )


class TestValidateModel:
    """Tests for validate_model — strict/lenient modes, known/unknown/disabled models."""

    def test_known_hardcoded_model_accepted(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import MODELS, validate_model

        # Reset to hardcoded models (prior tests may have called load_models_from_db)
        monkeypatch.setattr(model_registry, "_db_models", None)
        monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {m.id: m for m in MODELS})
        assert validate_model("claude-sonnet-4.6") is None

    def test_unknown_model_rejected_strict(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import validate_model

        monkeypatch.setattr(model_registry, "_db_models", None)
        monkeypatch.delenv("LUCENT_MODEL_VALIDATION", raising=False)
        result = validate_model("totally-fake-model-xyz")
        assert result is not None
        assert "Unknown model" in result
        assert "totally-fake-model-xyz" in result
        assert "list_available_models" in result

    def test_unknown_model_accepted_lenient(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import validate_model

        monkeypatch.setattr(model_registry, "_db_models", None)
        monkeypatch.setenv("LUCENT_MODEL_VALIDATION", "lenient")
        assert validate_model("totally-fake-model-xyz") is None

    def test_disabled_db_model_rejected(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, validate_model

        disabled = ModelInfo(id="off-model", provider="openai", name="Off", category="general")
        monkeypatch.setattr(model_registry, "_db_models", [disabled])
        monkeypatch.setattr(model_registry, "_db_model_by_id", {"off-model": disabled})
        monkeypatch.setattr(model_registry, "_db_enabled_ids", set())
        result = validate_model("off-model")
        assert result is not None
        assert "disabled" in result.lower()

    def test_enabled_db_model_accepted(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, validate_model

        m = ModelInfo(id="on-model", provider="openai", name="On", category="general")
        monkeypatch.setattr(model_registry, "_db_models", [m])
        monkeypatch.setattr(model_registry, "_db_model_by_id", {"on-model": m})
        monkeypatch.setattr(model_registry, "_db_enabled_ids", {"on-model"})
        assert validate_model("on-model") is None

    def test_unknown_db_model_rejected_strict(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, validate_model

        m = ModelInfo(id="real-model", provider="openai", name="Real", category="general")
        monkeypatch.setattr(model_registry, "_db_models", [m])
        monkeypatch.setattr(model_registry, "_db_model_by_id", {"real-model": m})
        monkeypatch.setattr(model_registry, "_db_enabled_ids", {"real-model"})
        monkeypatch.delenv("LUCENT_MODEL_VALIDATION", raising=False)
        result = validate_model("not-in-db")
        assert result is not None
        assert "Unknown model" in result
        assert "real-model" in result  # listed in available models

    def test_unknown_db_model_accepted_lenient(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import ModelInfo, validate_model

        m = ModelInfo(id="real-model", provider="openai", name="Real", category="general")
        monkeypatch.setattr(model_registry, "_db_models", [m])
        monkeypatch.setattr(model_registry, "_db_model_by_id", {"real-model": m})
        monkeypatch.setattr(model_registry, "_db_enabled_ids", {"real-model"})
        monkeypatch.setenv("LUCENT_MODEL_VALIDATION", "lenient")
        assert validate_model("not-in-db") is None

    def test_strict_is_default(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import validate_model

        monkeypatch.setattr(model_registry, "_db_models", None)
        monkeypatch.delenv("LUCENT_MODEL_VALIDATION", raising=False)
        assert validate_model("nonexistent-model") is not None

    def test_error_message_lists_available_models(self, monkeypatch):
        from lucent import model_registry
        from lucent.model_registry import MODELS, validate_model

        monkeypatch.setattr(model_registry, "_db_models", None)
        monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {m.id: m for m in MODELS})
        monkeypatch.delenv("LUCENT_MODEL_VALIDATION", raising=False)
        result = validate_model("bad-model")
        assert "claude-sonnet-4.6" in result  # hardcoded model appears in list
