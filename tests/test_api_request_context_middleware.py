"""Tests for request-scoped log context middleware in the API app."""

from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import AuthenticatedUser, CurrentUser
from lucent.log_context import get_request_id, get_user_id


@pytest.mark.asyncio
async def test_request_id_header_honored_and_context_cleared():
    app = create_app()

    @app.get("/api/_test/request-context")
    async def request_context_probe():
        return {
            "request_id": get_request_id(),
            "user_id": get_user_id(),
        }

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        supplied = "req-from-client-123"
        response = await client.get(
            "/api/_test/request-context",
            headers={"X-Request-ID": supplied},
        )

    assert response.status_code == 200
    assert response.json()["request_id"] == supplied
    assert response.headers["X-Request-ID"] == supplied
    assert response.json()["user_id"] is None
    assert get_request_id() is None
    assert get_user_id() is None


@pytest.mark.asyncio
async def test_request_context_sets_generated_request_id_and_user_id_after_auth(monkeypatch):
    app = create_app()
    fake_user_id = uuid4()
    fake_org_id = uuid4()

    async def fake_authenticate_with_api_key(_authorization: str):
        return CurrentUser(
            id=fake_user_id,
            organization_id=fake_org_id,
            role="member",
            email="test@example.com",
            display_name="Test User",
            auth_method="api_key",
            api_key_scopes=["read", "write"],
        )

    monkeypatch.setattr(
        "lucent.api.deps._authenticate_with_api_key",
        fake_authenticate_with_api_key,
    )

    @app.get("/api/_test/auth-context")
    async def auth_context_probe(_user: AuthenticatedUser):
        return {
            "request_id": get_request_id(),
            "user_id": get_user_id(),
        }

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/_test/auth-context",
            headers={"Authorization": "Bearer hs_fake"},
        )

    assert response.status_code == 200
    request_id = response.json()["request_id"]
    assert request_id == response.headers["X-Request-ID"]
    assert str(UUID(request_id)) == request_id
    assert response.json()["user_id"] == str(fake_user_id)
    assert get_request_id() is None
    assert get_user_id() is None

