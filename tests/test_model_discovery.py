"""Tests for provider-backed model discovery and safe DB sync."""

from uuid import uuid4

import pytest

from lucent.model_discovery import ModelDiscoveryService


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
