"""Request API ownership and system visibility tests."""

from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.daemon_identity import ensure_daemon_service_user
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.requests import RequestRepository


@asynccontextmanager
async def _client_for(user):
    app = create_app()
    current_user = CurrentUser(
        id=user["id"],
        organization_id=user["organization_id"],
        role=user["role"],
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return current_user

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_request_api_scopes_members_and_system_work(db_pool):
    prefix = f"test_req_api_{str(uuid4())[:8]}_"
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    users = UserRepository(db_pool)
    member_a = await users.create(
        external_id=f"{prefix}member_a",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}a@test.com",
        display_name="Member A",
    )
    member_b = await users.create(
        external_id=f"{prefix}member_b",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}b@test.com",
        display_name="Member B",
    )
    admin = await users.create(
        external_id=f"{prefix}admin",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}admin@test.com",
        display_name="Admin",
        role="admin",
    )

    try:
        async with db_pool.acquire() as conn:
            daemon = await ensure_daemon_service_user(conn, str(org["id"]))

        requests = RequestRepository(db_pool)
        request_a = await requests.create_request(
            title="Member A private request",
            org_id=str(org["id"]),
            created_by=str(member_a["id"]),
        )
        await requests.create_request(
            title="Member B private request",
            org_id=str(org["id"]),
            created_by=str(member_b["id"]),
        )
        await requests.create_request(
            title="System daemon request",
            org_id=str(org["id"]),
            created_by=str(daemon["id"]),
        )

        async with _client_for(member_a) as client:
            response = await client.get("/api/requests")
            assert response.status_code == 200
            payload = response.json()
            rows = payload.get("items", payload.get("requests", []))
            assert [row["title"] for row in rows] == [
                "Member A private request"
            ]
            hidden = await client.get(f"/api/requests/{request_a['id']}")
            assert hidden.status_code == 200

        async with _client_for(member_b) as client:
            hidden = await client.get(f"/api/requests/{request_a['id']}")
            assert hidden.status_code == 404

        async with _client_for(admin) as client:
            response = await client.get("/api/requests")
            assert response.status_code == 200
            payload = response.json()
            rows = payload.get("items", payload.get("requests", []))
            titles = {row["title"] for row in rows}
            assert titles == {"System daemon request"}
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM requests WHERE organization_id = $1", org["id"])
            await conn.execute("DELETE FROM users WHERE organization_id = $1", org["id"])
            await conn.execute("DELETE FROM organizations WHERE id = $1", org["id"])
