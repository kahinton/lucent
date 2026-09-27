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
        task_a = await requests.create_task(
            request_id=str(request_a["id"]),
            title="Member A private task",
            org_id=str(org["id"]),
        )
        request_b = await requests.create_request(
            title="Member B private request",
            org_id=str(org["id"]),
            created_by=str(member_b["id"]),
        )
        await requests.create_task(
            request_id=str(request_b["id"]),
            title="Member B private task",
            org_id=str(org["id"]),
        )
        system_request = await requests.create_request(
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

            queued = await client.get("/api/requests/queue")
            assert queued.status_code == 200
            assert {row["id"] for row in queued.json()["items"]} == {str(task_a["id"])}

            ready = await client.get("/api/requests/queue/pending")
            assert ready.status_code == 200
            assert {row["id"] for row in ready.json()["items"]} == {str(task_a["id"])}

            cancelled_task = await client.post(f"/api/requests/tasks/{task_a['id']}/cancel")
            assert cancelled_task.status_code == 200
            assert cancelled_task.json()["status"] == "cancelled"

            active = await client.get("/api/requests/active")
            assert active.status_code == 200
            assert {row["id"] for row in active.json()["items"]} == {str(request_a["id"])}

            review = await client.get("/api/requests/review")
            assert review.status_code == 200
            assert review.json()["items"] == []

            recent = await client.get("/api/requests/recently-completed")
            assert recent.status_code == 200
            assert recent.json()["items"] == []

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

            active = await client.get("/api/requests/active")
            assert active.status_code == 200
            assert {row["id"] for row in active.json()["items"]} == {str(system_request["id"])}

        await requests.update_request_status(
            str(request_b["id"]), "review", org_id=str(org["id"])
        )
        await requests.update_request_status(
            str(request_a["id"]), "completed", org_id=str(org["id"])
        )

        async with _client_for(member_a) as client:
            review = await client.get("/api/requests/review")
            assert review.status_code == 200
            assert review.json()["items"] == []

            recent = await client.get("/api/requests/recently-completed")
            assert recent.status_code == 200
            assert {row["id"] for row in recent.json()["items"]} == {str(request_a["id"])}
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM requests WHERE organization_id = $1", org["id"])
            await conn.execute("DELETE FROM users WHERE organization_id = $1", org["id"])
            await conn.execute("DELETE FROM organizations WHERE id = $1", org["id"])


@pytest.mark.asyncio
async def test_create_task_validates_active_requesting_user(db_pool):
    prefix = f"test_req_requser_{str(uuid4())[:8]}_"
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    users = UserRepository(db_pool)
    admin = await users.create(
        external_id=f"{prefix}admin",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}admin@test.com",
        display_name="Admin",
        role="admin",
    )
    requester = await users.create(
        external_id=f"{prefix}requester",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}requester@test.com",
        display_name="Requester",
    )
    request = await RequestRepository(db_pool).create_request(
        title="Requesting user validation",
        org_id=str(org["id"]),
        created_by=str(admin["id"]),
    )

    try:
        async with _client_for(admin) as client:
            response = await client.post(
                f"/api/requests/{request['id']}/tasks",
                json={"title": "Daemon-owned task", "requesting_user_id": str(requester["id"])},
            )
            assert response.status_code == 200, response.text
            task = response.json()
            assert task["requesting_user_id"] == str(requester["id"])
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM requests WHERE id = $1", request["id"])
            await conn.execute(
                "DELETE FROM users WHERE id = ANY($1::uuid[])",
                [admin["id"], requester["id"]],
            )
            await conn.execute("DELETE FROM organizations WHERE id = $1", org["id"])


@pytest.mark.asyncio
async def test_request_memories_endpoint_handles_uuid_org(db_pool):
    prefix = f"test_req_mem_{str(uuid4())[:8]}_"
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}user@test.com",
        display_name="Member",
    )
    request = await RequestRepository(db_pool).create_request(
        title="Request memory links",
        org_id=str(org["id"]),
        created_by=str(user["id"]),
    )
    async with db_pool.acquire() as conn:
        memory_id = await conn.fetchval(
            """INSERT INTO memories (username, type, content, user_id, organization_id)
               VALUES ($1, 'experience', $2, $3, $4)
               RETURNING id""",
            f"{prefix}memory",
            f"{prefix}Linked memory",
            user["id"],
            org["id"],
        )
        await conn.execute(
            """INSERT INTO request_memories (request_id, memory_id, relation)
               VALUES ($1, $2, 'created')""",
            request["id"],
            memory_id,
        )

    try:
        async with _client_for(user) as client:
            response = await client.get(f"/api/requests/{request['id']}/memories")
            assert response.status_code == 200, response.text
            assert response.json()["items"][0]["memory_id"] == str(memory_id)
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM request_memories WHERE request_id = $1", request["id"])
            await conn.execute("DELETE FROM requests WHERE id = $1", request["id"])
            await conn.execute("DELETE FROM memories WHERE id = $1", memory_id)
            await conn.execute("DELETE FROM users WHERE id = $1", user["id"])
            await conn.execute("DELETE FROM organizations WHERE id = $1", org["id"])
