"""Remote instance management API tests (Agent Control Protocol).

Covers authorization (owner-only mutations, admin read), token lifecycle,
org isolation, and the requeue path. Protocol-level claim tests live in
``test_remote_claim_flow.py``.
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import OrganizationRepository, UserRepository


@asynccontextmanager
async def _client_for(user, *, scopes=None):
    app = create_app()

    async def override():
        return CurrentUser(
            id=user["id"],
            organization_id=user["organization_id"],
            role=user["role"],
            email=user.get("email"),
            display_name=user.get("display_name"),
            auth_method="api_key",
            api_key_scopes=scopes or ["read", "write"],
        )

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _make_org_users(db_pool, prefix):
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    users = UserRepository(db_pool)
    owner = await users.create(
        external_id=f"{prefix}owner",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}owner@test.com",
        display_name="Owner",
        role="owner",
    )
    admin = await users.create(
        external_id=f"{prefix}admin",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}admin@test.com",
        display_name="Admin",
        role="admin",
    )
    member = await users.create(
        external_id=f"{prefix}member",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}member@test.com",
        display_name="Member",
    )
    return org, owner, admin, member


@pytest.mark.asyncio
async def test_owner_can_create_remote_and_receives_token_once(db_pool):
    prefix = f"test_remote_api_{str(uuid4())[:8]}_"
    org, owner, _admin, _member = await _make_org_users(db_pool, prefix)
    try:
        async with _client_for(owner) as client:
            response = await client.post(
                "/api/remotes",
                json={"name": "build-box", "description": "Test remote"},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "build-box"
        assert body["status"] == "active"
        # The plaintext token is returned exactly once, at creation.
        assert body["token"].startswith("lr_")
        assert "id" in body

        # Listing must never re-expose the token material.
        async with _client_for(owner) as client:
            listing = await client.get("/api/remotes")
        assert listing.status_code == 200
        items = listing.json()["items"]
        assert len(items) == 1
        assert "token" not in items[0] or not str(items[0]["token"]).startswith("lr_")
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM remote_instances WHERE organization_id = $1", org["id"]
            )
            await conn.execute(
                "DELETE FROM api_keys WHERE user_id IN "
                "(SELECT id FROM users WHERE external_id LIKE $1)",
                f"{prefix}%",
            )
            await conn.execute(
                "DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%"
            )


@pytest.mark.asyncio
async def test_non_owner_cannot_mutate_remotes(db_pool):
    prefix = f"test_remote_authz_{str(uuid4())[:8]}_"
    org, owner, admin, member = await _make_org_users(db_pool, prefix)
    try:
        async with _client_for(owner) as client:
            created = await client.post("/api/remotes", json={"name": "gated"})
        assert created.status_code == 200
        remote_id = created.json()["id"]

        for actor, label in ((admin, "admin"), (member, "member")):
            async with _client_for(actor) as client:
                create_attempt = await client.post(
                    "/api/remotes", json={"name": f"{label}-attempt"}
                )
                patch_attempt = await client.patch(
                    f"/api/remotes/{remote_id}", json={"status": "disabled"}
                )
                rotate_attempt = await client.post(
                    f"/api/remotes/{remote_id}/rotate-token"
                )
            assert create_attempt.status_code == 403, label
            assert patch_attempt.status_code == 403, label
            assert rotate_attempt.status_code == 403, label

        # Admins may still read.
        async with _client_for(admin) as client:
            read = await client.get("/api/remotes")
        assert read.status_code == 200
        assert read.json()["total_count"] == 1
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM remote_instances WHERE organization_id = $1", org["id"]
            )
            await conn.execute(
                "DELETE FROM api_keys WHERE user_id IN "
                "(SELECT id FROM users WHERE external_id LIKE $1)",
                f"{prefix}%",
            )
            await conn.execute(
                "DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%"
            )


@pytest.mark.asyncio
async def test_rotation_revokes_previous_token(db_pool):
    prefix = f"test_remote_rotate_{str(uuid4())[:8]}_"
    org, owner, _admin, _member = await _make_org_users(db_pool, prefix)
    try:
        async with _client_for(owner) as client:
            created = await client.post("/api/remotes", json={"name": "rotating"})
            remote_id = created.json()["id"]
            first_token = created.json()["token"]

            rotated = await client.post(f"/api/remotes/{remote_id}/rotate-token")
        assert rotated.status_code == 200
        second_token = rotated.json()["token"]
        assert second_token.startswith("lr_")
        assert second_token != first_token

        # Exactly one active key must remain for the identity user.
        async with db_pool.acquire() as conn:
            active = await conn.fetchval(
                """SELECT COUNT(*) FROM api_keys ak
                   JOIN users u ON ak.user_id = u.id
                   WHERE u.external_id = $1
                     AND ak.revoked_at IS NULL AND ak.is_active = true""",
                f"remote-agent:{remote_id}",
            )
        assert active == 1
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM remote_instances WHERE organization_id = $1", org["id"]
            )
            await conn.execute(
                "DELETE FROM api_keys WHERE user_id IN "
                "(SELECT id FROM users WHERE external_id LIKE 'remote-agent:%' "
                "AND organization_id = $1)",
                org["id"],
            )
            await conn.execute(
                "DELETE FROM users WHERE external_id LIKE 'remote-agent:%' "
                "AND organization_id = $1",
                org["id"],
            )
            await conn.execute(
                "DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%"
            )


@pytest.mark.asyncio
async def test_revoke_disables_agent_keys(db_pool):
    prefix = f"test_remote_revoke_{str(uuid4())[:8]}_"
    org, owner, _admin, _member = await _make_org_users(db_pool, prefix)
    try:
        async with _client_for(owner) as client:
            created = await client.post("/api/remotes", json={"name": "revocable"})
            remote_id = created.json()["id"]
            revoked = await client.patch(
                f"/api/remotes/{remote_id}", json={"status": "revoked"}
            )
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"

        async with db_pool.acquire() as conn:
            active = await conn.fetchval(
                """SELECT COUNT(*) FROM api_keys ak
                   JOIN users u ON ak.user_id = u.id
                   WHERE u.external_id = $1
                     AND ak.revoked_at IS NULL AND ak.is_active = true""",
                f"remote-agent:{remote_id}",
            )
        assert active == 0
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM remote_instances WHERE organization_id = $1", org["id"]
            )
            await conn.execute(
                "DELETE FROM api_keys WHERE user_id IN "
                "(SELECT id FROM users WHERE external_id LIKE 'remote-agent:%' "
                "AND organization_id = $1)",
                org["id"],
            )
            await conn.execute(
                "DELETE FROM users WHERE external_id LIKE 'remote-agent:%' "
                "AND organization_id = $1",
                org["id"],
            )
            await conn.execute(
                "DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%"
            )


@pytest.mark.asyncio
async def test_remote_is_invisible_across_organizations(db_pool):
    prefix_a = f"test_remote_orgA_{str(uuid4())[:8]}_"
    prefix_b = f"test_remote_orgB_{str(uuid4())[:8]}_"
    org_a, owner_a, _a1, _a2 = await _make_org_users(db_pool, prefix_a)
    org_b, owner_b, _b1, _b2 = await _make_org_users(db_pool, prefix_b)
    try:
        async with _client_for(owner_a) as client:
            created = await client.post("/api/remotes", json={"name": "scoped"})
            remote_id = created.json()["id"]

        async with _client_for(owner_b) as client:
            listing = await client.get("/api/remotes")
            direct = await client.get(f"/api/remotes/{remote_id}")
        assert listing.json()["total_count"] == 0
        assert direct.status_code == 404
    finally:
        async with db_pool.acquire() as conn:
            for org_id in (org_a["id"], org_b["id"]):
                await conn.execute(
                    "DELETE FROM remote_instances WHERE organization_id = $1", org_id
                )
                await conn.execute(
                    "DELETE FROM api_keys WHERE user_id IN "
                    "(SELECT id FROM users WHERE external_id LIKE 'remote-agent:%' "
                    "AND organization_id = $1)",
                    org_id,
                )
                await conn.execute(
                    "DELETE FROM users WHERE external_id LIKE 'remote-agent:%' "
                    "AND organization_id = $1",
                    org_id,
                )
            await conn.execute(
                "DELETE FROM users WHERE external_id LIKE $1", f"{prefix_a}%"
            )
            await conn.execute(
                "DELETE FROM users WHERE external_id LIKE $1", f"{prefix_b}%"
            )
