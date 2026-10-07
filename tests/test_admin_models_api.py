"""API tests for /api/admin/models engine override behavior."""

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.deps import CurrentUser, get_current_user


@pytest_asyncio.fixture
async def mdl_prefix(db_pool):
    test_id = str(uuid4())[:8]
    prefix = f"test_models_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM models WHERE id LIKE $1", f"{prefix}%")
        await conn.execute(
            "DELETE FROM users WHERE external_id LIKE $1",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM organizations WHERE name LIKE $1",
            f"{prefix}%",
        )


@pytest_asyncio.fixture
async def models_client(db_pool, mdl_prefix):
    from lucent.api.app import create_app
    from lucent.db import OrganizationRepository, UserRepository

    org = await OrganizationRepository(db_pool).create(name=f"{mdl_prefix}org")
    user = await UserRepository(db_pool).create(
        external_id=f"{mdl_prefix}admin",
        provider="local",
        organization_id=org["id"],
        email=f"{mdl_prefix}admin@test.com",
        display_name=f"{mdl_prefix}Admin",
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

    async def override_get_current_user():
        return fake_user

    app.dependency_overrides[get_current_user] = override_get_current_user
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_post_put_get_models_engine_roundtrip(models_client, mdl_prefix):
    model_id = f"{mdl_prefix}roundtrip"
    create = await models_client.post(
        "/api/admin/models",
        json={
            "model_id": model_id,
            "provider": "openai",
            "name": "Roundtrip",
            "category": "general",
            "engine": "copilot",
            "reasoning_efforts": ["low", "high"],
        },
    )
    assert create.status_code == 201
    assert create.json()["engine"] == "copilot"
    assert create.json()["reasoning_efforts"] == ["low", "high"]

    update = await models_client.put(
        f"/api/admin/models/{model_id}",
        json={"engine": "copilot", "notes": "updated", "reasoning_efforts": ["medium"]},
    )
    assert update.status_code == 200
    assert update.json()["engine"] == "copilot"
    assert update.json()["reasoning_efforts"] == ["medium"]

    listed = await models_client.get("/api/admin/models")
    assert listed.status_code == 200
    items = listed.json()["items"]
    model = next(m for m in items if m["id"] == model_id)
    assert model["engine"] == "copilot"
    assert model["reasoning_efforts"] == ["medium"]


@pytest.mark.asyncio
async def test_custom_reasoning_effort_values_are_allowed(models_client, mdl_prefix):
    resp = await models_client.post(
        "/api/admin/models",
        json={
            "model_id": f"{mdl_prefix}custom-effort",
            "provider": "openai",
            "name": "CustomEffort",
            "reasoning_efforts": ["ultra"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["reasoning_efforts"] == ["ultra"]


@pytest.mark.asyncio
async def test_model_owner_fields_are_retired(
    models_client, db_pool, mdl_prefix
):
    """Models belong to the organization (131): payloads carrying the old
    owner fields are rejected, and edits clear any stale ownership."""
    from lucent.db import ModelRepository

    async with db_pool.acquire() as conn:
        admin_id = await conn.fetchval(
            "SELECT id FROM users WHERE external_id = $1", f"{mdl_prefix}admin"
        )

    model_id = f"{mdl_prefix}ownerless"
    create = await models_client.post(
        "/api/admin/models",
        json={
            "model_id": model_id,
            "provider": "openai",
            "name": "Org Model",
            "owner_user_id": str(uuid4()),
        },
    )
    assert create.status_code == 422
    assert "not permitted" in create.text
    assert "owner_user_id" in create.text

    plain = await models_client.post(
        "/api/admin/models",
        json={
            "model_id": model_id,
            "provider": "openai",
            "name": "Org Model",
        },
    )
    assert plain.status_code == 201
    model = await ModelRepository(db_pool).get_model(model_id)
    assert model["owner_user_id"] is None

    # Seed a stale owner out-of-band; the UPDATE trigger files an owner
    # clearance for it, and the next API edit must sweep both away.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE models SET owner_user_id = $2 WHERE id = $1",
            model_id,
            admin_id,
        )
    async with db_pool.acquire() as conn:
        owner_rows = await conn.fetch(
            """SELECT 1 FROM auth_clearances acl JOIN models m ON m.auth_id = acl.auth_id
               WHERE m.id = $1 AND acl.role = 'owner'""",
            model_id,
        )
    assert owner_rows

    update = await models_client.put(
        f"/api/admin/models/{model_id}",
        json={"name": "Org Model v2"},
    )
    assert update.status_code == 200
    assert update.json()["owner_user_id"] is None
    assert update.json()["owner_group_id"] is None
    async with db_pool.acquire() as conn:
        owner_rows = await conn.fetch(
            """SELECT 1 FROM auth_clearances acl JOIN models m ON m.auth_id = acl.auth_id
               WHERE m.id = $1 AND acl.role = 'owner'""",
            model_id,
        )
    assert owner_rows == []


@pytest.mark.asyncio
async def test_invalid_engine_rejected(models_client, mdl_prefix):
    model_id = f"{mdl_prefix}invalid"
    resp = await models_client.post(
        "/api/admin/models",
        json={
            "model_id": model_id,
            "provider": "openai",
            "name": "Invalid",
            "engine": "invalid-engine",
        },
    )
    assert resp.status_code == 422
    assert "Invalid engine value" in resp.text


@pytest.mark.asyncio
async def test_langchain_missing_provider_package_returns_clear_error(
    models_client, mdl_prefix, monkeypatch
):
    from lucent.llm import model_engine_validation as mev

    def _fake_find_spec(name: str):
        if name == "langchain":
            return object()
        if name == "langchain_anthropic":
            return None
        return object()

    monkeypatch.setattr(mev, "find_spec", _fake_find_spec)
    resp = await models_client.post(
        "/api/admin/models",
        json={
            "model_id": f"{mdl_prefix}missingpkg",
            "provider": "anthropic",
            "name": "MissingPkg",
            "engine": "langchain",
        },
    )
    assert resp.status_code == 400
    assert "langchain_anthropic" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_copilot_unsupported_provider_warns_not_errors(models_client, mdl_prefix):
    resp = await models_client.post(
        "/api/admin/models",
        json={
            "model_id": f"{mdl_prefix}warn",
            "provider": "mistral",
            "name": "WarnOnly",
            "engine": "copilot",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["engine"] == "copilot"
    assert "warnings" in data
    assert "may not be supported by Copilot SDK" in data["warnings"][0]


@pytest.mark.asyncio
async def test_discover_models_endpoint_syncs_configured_providers(
    models_client, monkeypatch
):
    async def fake_sync(self, *, providers=None, org_id=None, disable_missing=False):
        return {
            "providers": [
                {
                    "provider": "openai",
                    "configured": True,
                    "discovered": 1,
                    "upserted": 1,
                    "disabled_missing": 0,
                }
            ],
            "provider_count": 1,
            "discovered_count": 1,
            "upserted_count": 1,
            "errors": [],
            "synced_at": "2026-04-27T00:00:00+00:00",
        }

    monkeypatch.setattr("lucent.model_discovery.ModelDiscoveryService.sync", fake_sync)
    resp = await models_client.post(
        "/api/admin/models/discover",
        json={"providers": ["openai"]},
    )

    assert resp.status_code == 200
    assert resp.json()["upserted_count"] == 1


@pytest.mark.asyncio
async def test_model_management_is_org_predicated(models_client, mdl_prefix, db_pool):
    """Management by-ID reads/writes never reach another organization's model."""
    from lucent.db import OrganizationRepository
    from lucent.db.models import ModelRepository

    repo = ModelRepository(db_pool)
    other_org = await OrganizationRepository(db_pool).create(name=f"{mdl_prefix}other-org")
    other_model_id = f"{mdl_prefix}foreign"
    await repo.create_model(
        model_id=other_model_id,
        provider="openai",
        name="Foreign",
        category="general",
        org_id=str(other_org["id"]),
    )

    update = await models_client.put(
        f"/api/admin/models/{other_model_id}", json={"name": "Hijack"}
    )
    assert update.status_code == 404

    updated = await repo.get_model(other_model_id, str(other_org["id"]))
    assert updated["name"] == "Foreign"

    assert await repo.delete_model(other_model_id, str(uuid4())) is False
    assert await repo.get_model(other_model_id, str(other_org["id"])) is not None
