"""Unit tests for task-level engine-backend selection helpers."""

from __future__ import annotations

import pytest

from lucent.llm import backend_selection as bs
from lucent.model_registry import ModelInfo


@pytest.fixture
def fake_backends(monkeypatch):
    """Patch BackendRepository so the helpers run without a database."""
    state: dict = {"by_id": {}, "all": []}

    class _FakeRepo:
        def __init__(self, pool):
            pass

        async def get(self, backend_id, *, org_id=None):
            return state["by_id"].get(str(backend_id))

        async def list(self, *, org_id=None, enabled_only=False, **kwargs):
            return state["all"]

    monkeypatch.setattr("lucent.db.backends.BackendRepository", _FakeRepo)
    return state


def _backend(**over):
    base = {
        "id": "b1",
        "name": "Codex via Ollama",
        "engine": "codex",
        "provider": "ollama",
        "is_enabled": True,
        "organization_id": "o1",
    }
    base.update(over)
    return base


async def test_unknown_backend_errors(fake_backends):
    assert "Unknown backend" in (
        await bs.validate_backend_selection(None, "missing", org_id="o1")
    )


async def test_disabled_backend_errors(fake_backends):
    fake_backends["by_id"]["b1"] = _backend(is_enabled=False)
    assert "disabled" in (await bs.validate_backend_selection(None, "b1", org_id="o1"))


async def test_unknown_engine_errors(fake_backends):
    fake_backends["by_id"]["b1"] = _backend(engine="nope")
    assert "not available" in (await bs.validate_backend_selection(None, "b1", org_id="o1"))


async def test_unavailable_engine_errors(fake_backends, monkeypatch):
    from lucent.llm.engine_registry import EngineAvailability

    fake_backends["by_id"]["b1"] = _backend(engine="claude_code", provider="anthropic")
    monkeypatch.setattr(
        bs,
        "engine_availability",
        lambda _name: EngineAvailability(
            available=False, detail="claude-agent-sdk is not installed"
        ),
    )
    error = await bs.validate_backend_selection(None, "b1", org_id="o1")
    assert error and "unavailable" in error
    assert "claude-agent-sdk is not installed" in error


async def test_local_backend_rejects_mismatched_model(fake_backends, monkeypatch):
    """A local Ollama backend serves Ollama models, not cloud model ids."""
    fake_backends["by_id"]["b1"] = _backend(provider="ollama")
    monkeypatch.setattr(
        "lucent.model_registry.get_model",
        lambda _id: ModelInfo(id="gpt-5.1", provider="openai", name="GPT", category="general"),
    )
    error = await bs.validate_backend_selection(None, "b1", org_id="o1", model_id="gpt-5.1")
    assert error and "cannot run on the 'ollama' backend" in error


async def test_local_backend_accepts_matching_model(fake_backends, monkeypatch):
    fake_backends["by_id"]["b1"] = _backend(provider="ollama")
    monkeypatch.setattr(
        "lucent.model_registry.get_model",
        lambda _id: ModelInfo(
            id="gemma4:31b-cloud", provider="ollama", name="Gemma", category="general"
        ),
    )
    assert (
        await bs.validate_backend_selection(
            None, "b1", org_id="o1", model_id="gemma4:31b-cloud"
        )
        is None
    )


async def test_native_backend_rejects_mismatched_model(fake_backends, monkeypatch):
    fake_backends["by_id"]["b1"] = _backend(engine="claude_code", provider="anthropic")
    monkeypatch.setattr(
        "lucent.model_registry.get_model",
        lambda _id: ModelInfo(id="gemini-3-pro", provider="google", name="G", category="general"),
    )
    error = await bs.validate_backend_selection(None, "b1", org_id="o1", model_id="gemini-3-pro")
    assert error and "cannot run on" in error


async def test_valid_native_backend_ok(fake_backends, monkeypatch):
    fake_backends["by_id"]["b1"] = _backend(engine="claude_code", provider="anthropic")
    monkeypatch.setattr(
        "lucent.model_registry.get_model",
        lambda _id: ModelInfo(
            id="claude-opus-4.7", provider="anthropic", name="Opus", category="reasoning"
        ),
    )
    assert (
        await bs.validate_backend_selection(None, "b1", org_id="o1", model_id="claude-opus-4.7")
        is None
    )


async def test_engine_catalog_lists_engines_and_backends(fake_backends):
    fake_backends["all"] = [
        _backend(id="b1", engine="codex", provider="ollama"),
        _backend(id="b2", engine="claude_code", provider="anthropic"),
    ]
    catalog = await bs.engine_catalog(None, org_id="o1")
    by_name = {e["engine"]: e for e in catalog}
    assert {"copilot", "claude_code", "codex"} <= set(by_name)
    assert [b["id"] for b in by_name["codex"]["backends"]] == ["b1"]
    assert [b["id"] for b in by_name["claude_code"]["backends"]] == ["b2"]
    assert by_name["copilot"]["backends"] == []
    # Model sources describe what each engine can run on.
    assert any("Ollama" in s for s in by_name["codex"]["model_sources"])


async def test_backend_default_model(fake_backends):
    fake_backends["by_id"]["b1"] = _backend(config={"model": "qwen3-coder"})
    fake_backends["by_id"]["b2"] = _backend(id="b2")
    assert await bs.backend_default_model(None, "b1", org_id="o1") == "qwen3-coder"
    assert await bs.backend_default_model(None, "b2", org_id="o1") is None
    assert await bs.backend_default_model(None, None, org_id="o1") is None


async def test_engine_catalog_exposes_backend_models(fake_backends, monkeypatch):
    fake_backends["all"] = [
        _backend(id="b1", engine="codex", provider="ollama", config={"model": "qwen3-coder"})
    ]

    def _fake_list_models(provider=None, include_disabled=False):
        if provider == "ollama":
            return [
                ModelInfo(
                    id="qwen3-coder", provider="ollama", name="Qwen3 Coder", category="general"
                )
            ]
        return []

    monkeypatch.setattr("lucent.model_registry.list_models", _fake_list_models)
    catalog = await bs.engine_catalog(None, org_id="o1")
    codex = next(e for e in catalog if e["engine"] == "codex")
    backend = codex["backends"][0]
    assert backend["default_model"] == "qwen3-coder"
    assert [m["id"] for m in backend["models"]] == ["qwen3-coder"]
    # The model's own binding is surfaced so agents can see the shadowing.
    model = backend["models"][0]
    assert model["engine"] is None
    assert model["backend_id"] is None
    assert model["supports_tools"] is True


# ── request-level execution pin ────────────────────────────────────────────


async def test_request_pin_requires_model_for_local_backend(fake_backends):
    fake_backends["by_id"]["b1"] = _backend(
        engine="claude_code", provider="ollama", config={}
    )
    error = await bs.validate_request_execution_pin(None, "b1", None, org_id="o1")
    assert error and "requested_model" in error


async def test_request_pin_accepts_local_backend_with_model(fake_backends):
    fake_backends["by_id"]["b1"] = _backend(
        engine="claude_code", provider="ollama", config={}
    )
    assert (
        await bs.validate_request_execution_pin(
            None, "b1", "gemma4:31b-cloud", org_id="o1"
        )
        is None
    )


async def test_request_pin_accepts_local_backend_with_config_model(fake_backends):
    fake_backends["by_id"]["b1"] = _backend(
        engine="claude_code", provider="ollama", config={"model": "qwen3-coder"}
    )
    assert await bs.validate_request_execution_pin(None, "b1", None, org_id="o1") is None


async def test_request_pin_rejects_overlong_model(fake_backends):
    fake_backends["by_id"]["b1"] = _backend(
        engine="claude_code", provider="ollama", config={}
    )
    error = await bs.validate_request_execution_pin(
        None, "b1", "x" * 65, org_id="o1"
    )
    assert error and "64 characters" in error
