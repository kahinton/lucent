"""Tests for provider-backed model discovery and safe DB sync."""

from uuid import uuid4

import pytest

from lucent.model_discovery import ModelDiscoveryService


def test_extract_reasoning_efforts_from_copilot_metadata_shape():
    from lucent.model_discovery import _extract_reasoning_efforts_from_metadata

    metadata = {
        "id": "claude-opus-4.7",
        "capabilities": {"supports": {"reasoningEffort": True}},
        "supportedReasoningEfforts": ["medium"],
        "defaultReasoningEffort": "medium",
    }

    assert _extract_reasoning_efforts_from_metadata(metadata) == ["medium"]


@pytest.fixture
def discovery_prefix():
    return f"test_discovery_{str(uuid4())[:8]}_"


@pytest.mark.asyncio
async def test_openai_discovery_maps_generation_models(db_pool, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    service = ModelDiscoveryService(db_pool)

    async def fake_get_json(*_args, **_kwargs):
        return {
            "object": "list",
            "data": [
                {"id": "gpt-5.4", "owned_by": "openai"},
                {"id": "text-embedding-3-large", "owned_by": "openai"},
            ],
        }

    monkeypatch.setattr(service, "_get_json", fake_get_json)
    models = await service._discover_openai()

    assert [m.id for m in models] == ["gpt-5.4"]
    assert models[0].provider == "openai"
    assert models[0].api_model_id == "gpt-5.4"
    assert models[0].reasoning_efforts == []


@pytest.mark.asyncio
async def test_openai_discovery_uses_system_managed_provider_secret(db_pool, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service = ModelDiscoveryService(db_pool)
    captured = {}

    class FakeSecretProvider:
        async def get(self, key, scope):
            captured["secret_key"] = key
            captured["scope"] = scope
            return "db-backed-key"

    monkeypatch.setattr("lucent.model_discovery.SecretRegistry.get", lambda: FakeSecretProvider())

    async def fake_get_json(url, *, headers=None, **_kwargs):
        assert url == "https://api.openai.com/v1/models"
        assert headers == {"Authorization": "Bearer db-backed-key"}
        return {"data": [{"id": "gpt-4o-mini", "owned_by": "openai"}]}

    monkeypatch.setattr(service, "_get_json", fake_get_json)
    models = await service._discover_openai(org_id="org-123")

    assert [m.id for m in models] == ["gpt-4o-mini"]
    assert captured["secret_key"] == "model_providers.openai.api_key"
    assert captured["scope"].organization_id == "org-123"
    assert captured["scope"].system_managed is True


@pytest.mark.asyncio
async def test_openai_discovery_uses_provider_reported_reasoning_values(db_pool, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    service = ModelDiscoveryService(db_pool)

    async def fake_get_json(*_args, **_kwargs):
        return {
            "object": "list",
            "data": [
                {
                    "id": "gpt-dynamic",
                    "owned_by": "openai",
                    "capabilities": {
                        "reasoning_effort": {
                            "values": ["none", "low", "research-grade"]
                        }
                    },
                },
            ],
        }

    monkeypatch.setattr(service, "_get_json", fake_get_json)
    models = await service._discover_openai()

    assert [m.id for m in models] == ["gpt-dynamic"]
    assert models[0].reasoning_efforts == ["none", "low", "research-grade"]


@pytest.mark.asyncio
async def test_google_discovery_filters_generate_content(db_pool, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    service = ModelDiscoveryService(db_pool)

    async def fake_get_json(*_args, **_kwargs):
        return {
            "models": [
                {
                    "name": "models/gemini-3.1-pro",
                    "baseModelId": "gemini-3.1-pro",
                    "displayName": "Gemini 3.1 Pro",
                    "inputTokenLimit": 1000000,
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/text-embedding-004",
                    "baseModelId": "text-embedding-004",
                    "supportedGenerationMethods": ["embedContent"],
                },
            ]
        }

    monkeypatch.setattr(service, "_get_json", fake_get_json)
    models = await service._discover_google()

    assert [m.id for m in models] == ["gemini-3.1-pro"]
    assert models[0].context_window == 1000000
    assert models[0].supports_tools is True


@pytest.mark.asyncio
async def test_google_discovery_uses_provider_reported_thinking_levels(db_pool, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    service = ModelDiscoveryService(db_pool)

    async def fake_get_json(*_args, **_kwargs):
        return {
            "models": [
                {
                    "name": "models/gemini-dynamic",
                    "baseModelId": "gemini-dynamic",
                    "displayName": "Gemini Dynamic",
                    "supportedGenerationMethods": ["generateContent"],
                    "supportedThinkingLevels": ["adaptive", "deep"],
                },
            ]
        }

    monkeypatch.setattr(service, "_get_json", fake_get_json)
    models = await service._discover_google()

    assert models[0].reasoning_efforts == ["adaptive", "deep"]


@pytest.mark.asyncio
async def test_ollama_discovery_marks_models_without_tool_capability_false(db_pool, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    service = ModelDiscoveryService(db_pool)

    async def fake_get_json(url, **_kwargs):
        assert url.endswith("/tags")
        return {
            "models": [
                {
                    "model": "gemma3:4b",
                    "details": {"parameter_size": "4B"},
                }
            ]
        }

    async def fake_post_json(url, *, json=None, **_kwargs):
        assert url.endswith("/show")
        assert json == {"model": "gemma3:4b"}
        return {"capabilities": ["completion", "vision"], "model_info": {}}

    monkeypatch.setattr(service, "_get_json", fake_get_json)
    monkeypatch.setattr(service, "_post_json", fake_post_json)

    models = await service._discover_ollama()

    assert models[0].id == "gemma3:4b"
    assert models[0].supports_tools is False
    assert "no structured tool-call support" in models[0].notes


@pytest.mark.asyncio
async def test_ollama_discovery_uses_structured_tool_probe(db_pool, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    service = ModelDiscoveryService(db_pool)

    async def fake_get_json(url, **_kwargs):
        assert url.endswith("/tags")
        return {"models": [{"model": "qwen3:4b", "details": {}}]}

    async def fake_post_json(url, *, json=None, **_kwargs):
        assert url.endswith("/show")
        assert json == {"model": "qwen3:4b"}
        return {"capabilities": ["completion", "tools"], "model_info": {}}

    async def fake_probe(api_base, model_id):
        assert api_base == "http://localhost:11434/api"
        assert model_id == "qwen3:4b"
        return {"ok": True, "tool_call_count": 1}

    monkeypatch.setattr(service, "_get_json", fake_get_json)
    monkeypatch.setattr(service, "_post_json", fake_post_json)
    monkeypatch.setattr(service, "_probe_ollama_tool_support", fake_probe)

    models = await service._discover_ollama()

    assert models[0].supports_tools is True
    assert "tools" in models[0].tags
    assert models[0].discovery_metadata["tool_probe"] == {"ok": True, "tool_call_count": 1}


@pytest.mark.asyncio
async def test_ollama_discovery_distrusts_advertised_tools_when_probe_fails(
    db_pool,
    monkeypatch,
):
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    service = ModelDiscoveryService(db_pool)

    async def fake_get_json(url, **_kwargs):
        assert url.endswith("/tags")
        return {"models": [{"model": "qwen2.5-coder:3b", "details": {}}]}

    async def fake_post_json(url, *, json=None, **_kwargs):
        assert url.endswith("/show")
        assert json == {"model": "qwen2.5-coder:3b"}
        return {"capabilities": ["completion", "tools"], "model_info": {}}

    async def fake_probe(_api_base, _model_id):
        return {"ok": False, "tool_call_count": 0, "content_excerpt": "```json"}

    monkeypatch.setattr(service, "_get_json", fake_get_json)
    monkeypatch.setattr(service, "_post_json", fake_post_json)
    monkeypatch.setattr(service, "_probe_ollama_tool_support", fake_probe)

    models = await service._discover_ollama()

    assert models[0].supports_tools is False
    assert "advertised but structured tool-call probe failed" in models[0].notes


@pytest.mark.asyncio
async def test_sync_inserts_provider_model(db_pool, discovery_prefix):
    from lucent.db.models import ModelRepository

    model_id = f"{discovery_prefix}provider"
    repo = ModelRepository(db_pool)
    try:
        result = await repo.sync_discovered_models(
            provider="openai",
            models=[
                {
                    "model_id": model_id,
                    "provider": "openai",
                    "name": "Provider Model",
                    "category": "general",
                    "api_model_id": model_id,
                    "supports_tools": True,
                    "supports_vision": False,
                    "tags": ["general"],
                    "discovery_metadata": {"source": "test"},
                }
            ],
        )
        model = await repo.get_model(model_id)

        assert result["upserted"] == 1
        assert model["discovery_source"] == "provider"
        assert model["is_custom"] is False
        assert model["is_enabled"] is False
        assert model["last_discovered_at"] is not None
        async with db_pool.acquire() as conn:
            metadata_type = await conn.fetchval(
                "SELECT jsonb_typeof(discovery_metadata) FROM models WHERE id = $1",
                model_id,
            )
        assert metadata_type == "object"
    finally:
        await repo.delete_model(model_id)


@pytest.mark.asyncio
async def test_initial_setup_models_exclude_unconfigured_seeds_and_can_be_enabled(
    db_pool,
    discovery_prefix,
):
    from lucent.db.models import ModelRepository

    seed_id = f"{discovery_prefix}setup-seed"
    provider_id = f"{discovery_prefix}setup-provider"
    repo = ModelRepository(db_pool)
    try:
        await repo.create_model(
            model_id=seed_id,
            provider="openai",
            name="Unconfigured Seed",
            is_enabled=False,
            discovery_source="seed",
            is_custom=False,
        )
        await repo.create_model(
            model_id=provider_id,
            provider="ollama",
            name="Discovered Local Model",
            is_enabled=False,
            discovery_source="provider",
            is_custom=False,
        )

        setup_models = await repo.list_initial_setup_models()
        setup_ids = {model["id"] for model in setup_models}
        enabled_ids = await repo.enable_models([provider_id])
        provider_model = await repo.get_model(provider_id)

        assert seed_id not in setup_ids
        assert provider_id in setup_ids
        assert enabled_ids == {provider_id}
        assert provider_model["is_enabled"] is True
    finally:
        await repo.delete_model(seed_id)
        await repo.delete_model(provider_id)


@pytest.mark.asyncio
async def test_sync_replaces_stale_provider_reasoning_efforts(db_pool, discovery_prefix):
    from lucent.db.models import ModelRepository

    model_id = f"{discovery_prefix}reasoning-refresh"
    repo = ModelRepository(db_pool)
    try:
        await repo.sync_discovered_models(
            provider="copilot",
            models=[
                {
                    "model_id": model_id,
                    "provider": "copilot",
                    "name": "Reasoning Refresh",
                    "category": "reasoning",
                    "api_model_id": model_id,
                    "supports_tools": True,
                    "supports_vision": False,
                    "tags": ["reasoning-effort"],
                    "reasoning_efforts": ["low", "medium", "high", "xhigh"],
                    "discovery_metadata": {"supportedReasoningEfforts": ["medium"]},
                }
            ],
        )
        await repo.sync_discovered_models(
            provider="copilot",
            models=[
                {
                    "model_id": model_id,
                    "provider": "copilot",
                    "name": "Reasoning Refresh",
                    "category": "reasoning",
                    "api_model_id": model_id,
                    "supports_tools": True,
                    "supports_vision": False,
                    "tags": ["reasoning-effort"],
                    "reasoning_efforts": ["medium"],
                    "discovery_metadata": {"supportedReasoningEfforts": ["medium"]},
                }
            ],
        )
        model = await repo.get_model(model_id)

        assert model["reasoning_efforts"] == ["medium"]
    finally:
        await repo.delete_model(model_id)


@pytest.mark.asyncio
async def test_sync_preserves_existing_provider_enablement(db_pool, discovery_prefix):
    from lucent.db.models import ModelRepository

    model_id = f"{discovery_prefix}optin"
    repo = ModelRepository(db_pool)
    try:
        await repo.sync_discovered_models(
            provider="openai",
            models=[
                {
                    "model_id": model_id,
                    "provider": "openai",
                    "name": "Opt In Model",
                    "category": "general",
                    "api_model_id": model_id,
                    "supports_tools": True,
                    "supports_vision": False,
                    "tags": ["general"],
                }
            ],
        )
        await repo.toggle_model(model_id, True)
        await repo.sync_discovered_models(
            provider="openai",
            models=[
                {
                    "model_id": model_id,
                    "provider": "openai",
                    "name": "Opt In Model Updated",
                    "category": "reasoning",
                    "api_model_id": model_id,
                    "supports_tools": True,
                    "supports_vision": True,
                    "tags": ["reasoning"],
                }
            ],
        )
        model = await repo.get_model(model_id)

        assert model["is_enabled"] is True
        assert model["name"] == "Opt In Model Updated"
    finally:
        await repo.delete_model(model_id)


def test_visible_model_providers_hide_inactive_seed_providers():
    from lucent.web.routes.admin import _visible_model_providers

    providers = _visible_model_providers(
        [
            {"provider": "anthropic", "discovery_source": "seed", "is_custom": False},
            {"provider": "copilot", "discovery_source": "provider", "is_custom": False},
            {"provider": "openai", "discovery_source": "manual", "is_custom": True},
        ],
        {"copilot"},
    )

    assert providers == ["copilot", "openai"]


@pytest.mark.asyncio
async def test_sync_preserves_manual_custom_model(db_pool, discovery_prefix):
    from lucent.db.models import ModelRepository

    model_id = f"{discovery_prefix}manual"
    repo = ModelRepository(db_pool)
    try:
        await repo.create_model(
            model_id=model_id,
            provider="openai",
            name="Custom Name",
            category="general",
            api_model_id="custom-api-id",
            notes="human curated",
            tags=["custom"],
            discovery_source="manual",
            is_custom=True,
        )
        await repo.sync_discovered_models(
            provider="openai",
            models=[
                {
                    "model_id": model_id,
                    "provider": "openai",
                    "name": "Provider Name",
                    "category": "reasoning",
                    "api_model_id": model_id,
                    "supports_tools": True,
                    "supports_vision": True,
                    "tags": ["provider"],
                    "discovery_metadata": {"source": "test"},
                }
            ],
        )
        model = await repo.get_model(model_id)

        assert model["name"] == "Custom Name"
        assert model["category"] == "general"
        assert model["api_model_id"] == "custom-api-id"
        assert model["discovery_source"] == "manual"
        assert model["is_custom"] is True
        assert model["last_discovered_at"] is not None
    finally:
        await repo.delete_model(model_id)
