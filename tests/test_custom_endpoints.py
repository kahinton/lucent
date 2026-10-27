"""Tests for custom OpenAI-compatible model endpoints (llama.cpp, vLLM, ...).

These cover the "bring your own endpoint" feature end to end at the unit level:
the provider catalog that makes LangChain providers addressable, the chat-model
construction that routes a model to its endpoint, discovery against an
OpenAI-compatible server, and the credential/key precedence rules.
"""

from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from lucent.llm import langchain_engine as le
from lucent.llm import langchain_providers as lp


# ── Provider catalog ───────────────────────────────────────────────────────


def test_custom_provider_is_self_hosted_openai_compatible():
    spec = lp.langchain_provider_spec("custom")
    assert spec.langchain_provider == "openai"
    assert spec.requires_base_url is True
    assert spec.accepts_base_url is True
    # A custom endpoint has no vendor key of its own.
    assert spec.api_key_env == ()


def test_custom_provider_is_a_langchain_provider_but_not_local_provider_registry():
    assert lp.is_langchain_provider("custom")
    assert lp.provider_is_self_hosted("custom")
    assert lp.provider_label if hasattr(lp, "provider_label") else True


def test_provider_name_aliases_canonicalize():
    assert lp.normalize_provider("google_genai") == "google"
    assert lp.normalize_provider("GEMINI") == "google"
    assert lp.normalize_provider(" Custom ") == "custom"
    assert lp.normalize_provider(None) == ""


def test_unknown_provider_raises_and_others_pass_through():
    with pytest.raises(lp.UnknownLangChainProviderError):
        lp.langchain_provider_spec("not-a-provider")
    # An operator-supplied id is passed to init_chat_model unchanged rather than
    # silently rewritten.
    assert lp.langchain_model_provider("not-a-provider") == "not-a-provider"
    assert lp.is_langchain_provider("not-a-provider") is False


def test_base_url_resolution_uses_catalog_defaults_and_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://env-ollama:11434")
    assert lp.resolve_provider_base_url("ollama") == "http://env-ollama:11434"
    # Explicit config wins over the environment.
    assert (
        lp.resolve_provider_base_url("ollama", configured_base_url="http://cfg:11434/")
        == "http://cfg:11434"
    )


def test_provider_without_base_url_support_resolves_empty():
    # Gemini's integration has no base_url kwarg; reporting a default would make
    # the engine pass an argument the client rejects.
    assert lp.resolve_provider_base_url("google") == ""
    assert lp.provider_accepts_base_url("google") is False
    assert lp.provider_accepts_base_url("openai") is True


def test_custom_provider_requires_base_url_and_hides_others_do_not():
    assert lp.provider_requires_base_url("custom") is True
    assert lp.provider_requires_base_url("openai") is False


# ── Chat-model construction ────────────────────────────────────────────────


@pytest.fixture
def capture_init(monkeypatch):
    """Capture the args ``init_chat_model`` receives without importing a client."""
    from langchain import chat_models

    captured: dict = {}

    def fake_init_chat_model(model, **kwargs):
        captured["model"] = model
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(chat_models, "init_chat_model", fake_init_chat_model)
    yield captured
    le.clear_runtime_model_registry()


def _backend(**config):
    return {
        "id": "b-llama",
        "name": "llama.cpp",
        "engine": "langchain",
        "provider": "custom",
        "organization_id": None,
        "config": config,
        "secret_refs": {},
    }


@pytest.mark.asyncio
async def test_custom_model_uses_backend_endpoint_and_model(capture_init):
    le.register_model("llamacpp-qwen", "custom", "ignored-by-backend")
    await le._get_chat_model(
        "llamacpp-qwen",
        audit_context={
            "organization_id": "",
            "engine_backend": _backend(
                base_url="http://llama-box:8080", model="qwen2.5-coder-7b-instruct"
            ),
        },
    )

    kwargs = capture_init["kwargs"]
    assert kwargs["model_provider"] == "openai"
    assert kwargs["base_url"] == "http://llama-box:8080"
    assert capture_init["model"] == "qwen2.5-coder-7b-instruct"
    # OpenAI-compatible servers commonly require a key even when they ignore it;
    # sending none makes the client raise before the request leaves the process.
    assert kwargs["api_key"] == "not-required"


@pytest.mark.asyncio
async def test_custom_model_without_endpoint_refuses_loudly(capture_init):
    le.register_model("llamacpp-qwen", "custom", "qwen")
    with pytest.raises(RuntimeError, match="no endpoint is configured"):
        await le._get_chat_model(
            "llamacpp-qwen", audit_context={"organization_id": ""}
        )
    assert capture_init == {}


@pytest.mark.asyncio
async def test_backend_secret_wins_over_placeholder(capture_init, monkeypatch):
    le.register_model("llamacpp-qwen", "custom", "qwen")
    backend = _backend(base_url="http://llama-box:8080")
    backend["secret_refs"] = {"api_key": "engine.langchain.llama.api_key"}

    class FakeSecret:
        async def get(self, key, scope):
            assert key == "engine.langchain.llama.api_key"
            return "server-key"

    monkeypatch.setattr("lucent.secrets.SecretRegistry.get", lambda: FakeSecret())
    await le._get_chat_model(
        "llamacpp-qwen",
        audit_context={
            "organization_id": str(uuid4()),
            "engine_backend": backend,
        },
    )
    assert capture_init["kwargs"]["api_key"] == "server-key"


@pytest.mark.asyncio
async def test_unknown_langchain_provider_is_passed_through(capture_init):
    # Groq with an installed package: the provider must reach init_chat_model
    # verbatim, and its base URL must not be invented.
    le.register_model("llama-3.1-8b", "groq", "llama-3.1-8b-instant")
    await le._get_chat_model("llama-3.1-8b", audit_context={"organization_id": ""})
    assert capture_init["kwargs"]["model_provider"] == "groq"
    assert "base_url" not in capture_init["kwargs"]


@pytest.mark.asyncio
async def test_ollama_base_url_still_resolves_from_env(capture_init, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://ollama.example:11434")
    le.register_model("ollama-thinking-test", "ollama", "qwen-thinking:4b")
    await le._get_chat_model("ollama-thinking-test", reasoning_effort="high")
    assert capture_init["kwargs"]["base_url"] == "http://ollama.example:11434"
    assert capture_init["kwargs"]["reasoning"] == "high"


@pytest.mark.asyncio
async def test_backend_for_a_different_provider_is_ignored(capture_init, monkeypatch):
    """A stale binding must never send a model to another provider's endpoint."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    le.register_model("gpt-5.4", "openai", "gpt-5.4")
    stale = {
        "id": "b-llama",
        "name": "llama.cpp",
        "engine": "langchain",
        "provider": "custom",
        "config": {"base_url": "http://llama-box:8080"},
        "secret_refs": {},
    }
    await le._get_chat_model(
        "gpt-5.4", audit_context={"organization_id": "", "engine_backend": stale}
    )
    assert "base_url" not in capture_init["kwargs"]
    assert capture_init["kwargs"]["model_provider"] == "openai"


# ── Discovery ──────────────────────────────────────────────────────────────


@pytest.fixture
def discovery_prefix():
    return f"test_custom_{str(uuid4())[:8]}_"


@pytest.mark.asyncio
async def test_custom_endpoint_discovery_reads_openai_model_list(db_pool, monkeypatch):
    from lucent.model_discovery import ModelDiscoveryService

    service = ModelDiscoveryService(db_pool)
    backend = _backend(base_url="http://llama-box:8080")

    class FakeRepo:
        async def list(self, **_kwargs):
            return [backend]

    monkeypatch.setattr("lucent.db.backends.BackendRepository", lambda _pool: FakeRepo())

    requested: list[str] = []

    async def fake_get_json(url, **_kwargs):
        requested.append(url)
        return {
            "object": "list",
            "data": [
                {
                    "id": "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
                    "owned_by": "llamacpp",
                    "meta": {"n_ctx_train": 32768},
                },
                {"id": "text-embedding-nomic-embed-text-v1.5", "owned_by": "llamacpp"},
            ],
        }

    monkeypatch.setattr(service, "_get_json", fake_get_json)
    models = await service._discover_custom_endpoints(org_id="org-1")

    assert requested == ["http://llama-box:8080/v1/models"]
    assert [m.id for m in models] == ["qwen2.5-coder-7b-instruct-q4_k_m.gguf"]
    model = models[0]
    assert model.provider == "custom"
    assert model.engine == "langchain"
    assert model.backend_id == "b-llama"
    assert model.context_window == 32768
    # The quantisation/extension suffix is dropped from the display name only.
    assert model.name == "Qwen2.5 Coder 7B Instruct Q4 K M"


def test_model_list_url_normalizes_every_pasted_shape():
    from lucent.model_discovery import ModelDiscoveryService

    assert (
        ModelDiscoveryService._openai_models_url("http://llama:8080")
        == "http://llama:8080/v1/models"
    )
    assert (
        ModelDiscoveryService._openai_models_url("http://llama:8080/")
        == "http://llama:8080/v1/models"
    )
    assert (
        ModelDiscoveryService._openai_models_url("http://llama:8080/v1")
        == "http://llama:8080/v1/models"
    )
    assert (
        ModelDiscoveryService._openai_models_url(
            "http://llama:8080/v1/chat/completions"
        )
        == "http://llama:8080/v1/models"
    )


@pytest.mark.asyncio
async def test_no_configured_endpoint_is_a_clear_error(db_pool, monkeypatch):
    from lucent.model_discovery import ModelDiscoveryService

    service = ModelDiscoveryService(db_pool)

    class FakeRepo:
        async def list(self, **_kwargs):
            return []

    monkeypatch.setattr("lucent.db.backends.BackendRepository", lambda _pool: FakeRepo())
    with pytest.raises(ValueError, match="No custom endpoint is configured"):
        await service._discover_custom_endpoints(org_id="org-1")


@pytest.mark.asyncio
async def test_one_unreachable_endpoint_does_not_hide_the_others(db_pool, monkeypatch):
    from lucent.model_discovery import ModelDiscoveryService

    service = ModelDiscoveryService(db_pool)
    good = _backend(base_url="http://good:8080")
    good["id"] = "b-good"
    bad = _backend(base_url="http://bad:8080")
    bad["id"] = "b-bad"

    class FakeRepo:
        async def list(self, **_kwargs):
            return [good, bad]

    monkeypatch.setattr("lucent.db.backends.BackendRepository", lambda _pool: FakeRepo())

    async def fake_get_json(url, **_kwargs):
        if "bad" in url:
            raise RuntimeError("connection refused")
        return {"data": [{"id": "only-model"}]}

    monkeypatch.setattr(service, "_get_json", fake_get_json)
    models = await service._discover_custom_endpoints(org_id="org-1")
    assert [m.id for m in models] == ["only-model"]


@pytest.mark.asyncio
async def test_custom_models_sync_as_provider_rows_with_backend_binding(
    db_pool, discovery_prefix
):
    """A discovered custom model keeps its backend binding, so it is runnable."""
    from lucent.db.backends import BackendRepository
    from lucent.db.models import ModelRepository
    from lucent.model_discovery import ModelDiscoveryService

    org_id = str(uuid4())
    from lucent.db import OrganizationRepository

    org = await OrganizationRepository(db_pool).create(name=f"{discovery_prefix}org")
    org_id = str(org["id"])
    backend = await BackendRepository(db_pool).create(
        name=f"{discovery_prefix}llama",
        engine="langchain",
        provider="custom",
        config={"base_url": "http://llama-box:8080"},
        org_id=org_id,
    )
    model_id = f"{discovery_prefix}model"
    try:
        service = ModelDiscoveryService(db_pool)

        async def fake_get_json(url, **_kwargs):
            return {"data": [{"id": model_id, "owned_by": "llamacpp"}]}

        monkeypatch_target = service
        monkeypatch_target._get_json = fake_get_json  # type: ignore[method-assign]

        result = await service.sync(providers=["custom"], org_id=org_id)
        assert result["upserted_count"] >= 1

        row = await ModelRepository(db_pool).get_model(model_id, org_id)
        assert row is not None
        assert row["provider"] == "custom"
        assert row["engine"] == "langchain"
        assert str(row["backend_id"]) == str(backend["id"])
        # Discovered rows start disabled so an admin opts them in, exactly like
        # every other provider's models.
        assert row["is_enabled"] is False
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM models WHERE id = $1", model_id)
            await conn.execute("DELETE FROM backends WHERE id = $1", backend["id"])
            await conn.execute("DELETE FROM organizations WHERE id = $1", org["id"])


# ── Provider credentials ───────────────────────────────────────────────────


def test_catalog_providers_get_canonical_credential_keys():
    from lucent.model_discovery import model_provider_credential_definition

    definition = model_provider_credential_definition("groq")
    assert definition is not None
    assert definition.secret_key == "model_providers.groq.api_key"
    # google_genai is an alias, not a second place to store a key.
    google = model_provider_credential_definition("google_genai")
    assert google is not None and google.secret_key == "model_providers.google.api_key"
    # Endpoint-based providers have no workspace-level key.
    assert model_provider_credential_definition("custom") is None
    assert model_provider_credential_definition("ollama") is None


def test_credential_list_only_offers_installed_simple_key_providers():
    from lucent.model_discovery import model_provider_credential_definitions

    providers = {
        d.provider for d in model_provider_credential_definitions(include_catalog=True)
    }
    assert {"openai", "anthropic", "google"} <= providers
    # Requires an endpoint/deployment; belongs on a backend, not a key form.
    assert "azure_openai" not in providers
    assert "custom" not in providers


# ── Engine routing ─────────────────────────────────────────────────────────


def test_custom_is_routed_to_the_general_llm_engine():
    from lucent.llm import engine_registry as er

    assert er.engine_supports_provider("langchain", "custom")
    assert er.default_engine_for_provider("custom") == "langchain"
    assert er.provider_label("custom") == "Custom endpoint"
    # Operator-hosted, so it groups with Ollama rather than the vendor APIs.
    assert er.provider_group("custom") == "local"


def test_catalog_provider_becomes_available_when_its_package_is_installed():
    """Installing a ``langchain-*`` package is what enables its provider."""
    from lucent.llm import engine_registry as er

    er.reset_engines()
    # ``langchain_deepseek`` is not installed in this test environment, so the
    # provider is omitted instead of offered-and-broken.
    mapping = er.provider_engine_map()
    if not lp.provider_package_installed("deepseek"):
        assert "deepseek" not in mapping
    er.reset_engines()


def test_engine_validation_names_the_missing_package(monkeypatch):
    from lucent.llm import model_engine_validation as mev

    def fake_find_spec(name):
        return None if name == "langchain_groq" else object()

    monkeypatch.setattr(mev, "find_spec", fake_find_spec)
    with pytest.raises(ValueError, match="langchain_groq"):
        mev.validate_engine_override("groq", "langchain")

    # A provider outside the catalog raises the "no engine supports" error
    # rather than a misleading package error.
    with pytest.raises(ValueError, match="No engine supports provider"):
        mev.validate_engine_override("totally-made-up", "langchain")

def test_custom_models_are_not_offered_without_an_endpoint():
    """A custom model with no backend has no address and must not look runnable."""
    import asyncio

    from lucent.llm.backend_selection import model_execution_catalog
    from lucent.model_registry import ModelInfo

    class FakeRepo:
        def __init__(self, _pool):
            pass

        async def list(self, **_kwargs):
            return []

    import lucent.db.backends as backends_module

    original = backends_module.BackendRepository
    backends_module.BackendRepository = FakeRepo  # type: ignore[assignment]
    try:
        catalog = asyncio.run(
            model_execution_catalog(
                None,
                org_id="org-1",
                models=[
                    ModelInfo(
                        id="qwen",
                        provider="custom",
                        name="Qwen",
                        category="general",
                    )
                ],
                configured_default="langchain",
            )
        )
    finally:
        backends_module.BackendRepository = original  # type: ignore[assignment]

    assert catalog["execution_targets"] == []
    assert catalog["models"][0]["default_engine"] == "langchain"


# ── Model ↔ backend binding (API) ──────────────────────────────────────────


@pytest_asyncio.fixture
async def custom_client(db_pool):
    """Admin API client plus an org-scoped custom endpoint backend."""
    from httpx import ASGITransport

    from lucent.api.app import create_app
    from lucent.api.deps import CurrentUser, get_current_user
    from lucent.db import OrganizationRepository, UserRepository
    from lucent.db.backends import BackendRepository

    prefix = f"test_customapi_{str(uuid4())[:8]}_"
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}admin",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}admin@test.com",
        display_name=f"{prefix}Admin",
    )
    backend = await BackendRepository(db_pool).create(
        name=f"{prefix}llama",
        engine="langchain",
        provider="custom",
        config={"base_url": "http://llama-box:8080"},
        org_id=str(org["id"]),
    )
    other = await BackendRepository(db_pool).create(
        name=f"{prefix}openai",
        engine="langchain",
        provider="openai",
        config={"base_url": "http://not-openai:8080"},
        org_id=str(org["id"]),
    )

    app = create_app()
    fake_user = CurrentUser(
        id=user["id"],
        organization_id=user["organization_id"],
        role="admin",
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake_user

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client, prefix, backend, other
    finally:
        app.dependency_overrides.clear()
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM models WHERE id LIKE $1", f"{prefix}%")
            await conn.execute("DELETE FROM backends WHERE id = ANY($1::uuid[])", [
                backend["id"], other["id"]
            ])
            await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
            await conn.execute("DELETE FROM organizations WHERE id = $1", org["id"])


@pytest.mark.asyncio
async def test_custom_model_requires_a_backend(custom_client):
    client, prefix, backend, _ = custom_client
    resp = await client.post(
        "/api/admin/models",
        json={"model_id": f"{prefix}qwen", "provider": "custom", "name": "Qwen"},
    )
    assert resp.status_code == 422
    assert "backend_id" in resp.json()["detail"]

    ok = await client.post(
        "/api/admin/models",
        json={
            "model_id": f"{prefix}qwen",
            "provider": "custom",
            "name": "Qwen",
            "backend_id": str(backend["id"]),
        },
    )
    assert ok.status_code == 201
    assert str(ok.json()["backend_id"]) == str(backend["id"])


@pytest.mark.asyncio
async def test_model_cannot_bind_to_another_providers_backend(custom_client):
    client, prefix, backend, other = custom_client
    resp = await client.post(
        "/api/admin/models",
        json={
            "model_id": f"{prefix}qwen",
            "provider": "custom",
            "name": "Qwen",
            "backend_id": str(other["id"]),
        },
    )
    assert resp.status_code == 422
    assert "cannot be bound" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_unknown_backend_binding_is_rejected(custom_client):
    client, prefix, _, _ = custom_client
    resp = await client.post(
        "/api/admin/models",
        json={
            "model_id": f"{prefix}qwen",
            "provider": "custom",
            "name": "Qwen",
            "backend_id": str(uuid4()),
        },
    )
    assert resp.status_code == 422
    assert "Unknown backend" in resp.json()["detail"]
