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
        },
    )
    assert create.status_code == 201
    assert create.json()["engine"] == "copilot"

    update = await models_client.put(
        f"/api/admin/models/{model_id}",
        json={"engine": "copilot", "notes": "updated"},
    )
    assert update.status_code == 200
    assert update.json()["engine"] == "copilot"

    listed = await models_client.get("/api/admin/models")
    assert listed.status_code == 200
    items = listed.json()["items"]
    model = next(m for m in items if m["id"] == model_id)
    assert model["engine"] == "copilot"


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
