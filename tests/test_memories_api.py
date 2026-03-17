"""API endpoint tests for the memories CRUD router.

Tests /api/memories endpoints:
- POST /api/memories (create)
- GET /api/memories/{memory_id} (get)
- PATCH /api/memories/{memory_id} (update)
- DELETE /api/memories/{memory_id} (delete)
- POST /api/memories/{memory_id}/share (share)
- POST /api/memories/{memory_id}/unshare (unshare)
- GET /api/memories/tags/list (list tags)
- GET /api/memories/tags/suggest (suggest tags)
"""

from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def mem_prefix(db_pool):
    """Create and clean up test data for memory API tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_mem_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memory_audit_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memory_access_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM memories WHERE username LIKE $1", f"{prefix}%")
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def mem_user(db_pool, mem_prefix):
    """Create a test user for memory API tests."""
    from lucent.db import OrganizationRepository, UserRepository

    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{mem_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{mem_prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{mem_prefix}user@test.com",
        display_name=f"{mem_prefix}User",
    )
    return user


@pytest_asyncio.fixture
async def mem_user_b(db_pool, mem_prefix, mem_user):
    """Create a second user in the same org."""
    from lucent.db import UserRepository

    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{mem_prefix}user_b",
        provider="local",
        organization_id=mem_user["organization_id"],
        email=f"{mem_prefix}userb@test.com",
        display_name=f"{mem_prefix}UserB",
    )
    return user


@pytest_asyncio.fixture
async def mem_client(db_pool, mem_user):
    """AsyncClient authenticated as mem_user."""
    app = create_app()
    fake_user = CurrentUser(
        id=mem_user["id"],
        organization_id=mem_user["organization_id"],
        role=mem_user.get("role", "member"),
        email=mem_user.get("email"),
        display_name=mem_user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake_user

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def mem_client_b(db_pool, mem_user_b):
    """AsyncClient authenticated as mem_user_b."""
    app = create_app()
    fake_user = CurrentUser(
        id=mem_user_b["id"],
        organization_id=mem_user_b["organization_id"],
        role=mem_user_b.get("role", "member"),
        email=mem_user_b.get("email"),
        display_name=mem_user_b.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake_user

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# Helper to create a memory via the API
async def _create_memory(client, prefix, **overrides):
    payload = {
        "username": f"{prefix}user",
        "type": "experience",
        "content": f"{prefix}Test memory content",
        "tags": ["test"],
        "importance": 5,
    }
    payload.update(overrides)
    resp = await client.post("/api/memories", json=payload)
    return resp


# ============================================================================
# Create Memory
# ============================================================================


class TestCreateMemory:
    """POST /api/memories"""

    async def test_create_experience(self, mem_client, mem_prefix):
        resp = await _create_memory(mem_client, mem_prefix)
        assert resp.status_code == 201
        data = resp.json()
        assert data["type"] == "experience"
        assert data["content"] == f"{mem_prefix}Test memory content"
        assert "test" in data["tags"]
        assert data["importance"] == 5
        assert data["id"] is not None

    async def test_create_technical(self, mem_client, mem_prefix):
        resp = await _create_memory(mem_client, mem_prefix, type="technical")
        assert resp.status_code == 201
        assert resp.json()["type"] == "technical"

    async def test_create_procedural(self, mem_client, mem_prefix):
        resp = await _create_memory(mem_client, mem_prefix, type="procedural")
        assert resp.status_code == 201
        assert resp.json()["type"] == "procedural"

    async def test_create_goal(self, mem_client, mem_prefix):
        resp = await _create_memory(mem_client, mem_prefix, type="goal")
        assert resp.status_code == 201
        assert resp.json()["type"] == "goal"

    async def test_create_invalid_type(self, mem_client, mem_prefix):
        resp = await _create_memory(mem_client, mem_prefix, type="invalid_type")
        assert resp.status_code == 400
        assert "invalid memory type" in resp.json()["detail"].lower()

    async def test_create_individual_rejected(self, mem_client, mem_prefix):
        """Individual memories cannot be created via API."""
        resp = await _create_memory(mem_client, mem_prefix, type="individual")
        assert resp.status_code == 400
        assert "individual" in resp.json()["detail"].lower()

    async def test_create_with_metadata(self, mem_client, mem_prefix):
        resp = await _create_memory(
            mem_client,
            mem_prefix,
            type="technical",
            metadata={"repo": "test-repo", "language": "python"},
        )
        assert resp.status_code == 201
        assert resp.json()["metadata"]["repo"] == "test-repo"

    async def test_create_defaults_username(self, mem_client, mem_prefix):
        """If username is not provided, should default to user's display name."""
        resp = await mem_client.post(
            "/api/memories",
            json={
                "type": "experience",
                "content": f"{mem_prefix}No username given",
            },
        )
        assert resp.status_code == 201
        # Should use display_name, email, or user ID as fallback
        assert resp.json()["username"] is not None

    async def test_create_with_importance_bounds(self, mem_client, mem_prefix):
        resp = await _create_memory(mem_client, mem_prefix, importance=10)
        assert resp.status_code == 201
        assert resp.json()["importance"] == 10

    async def test_create_importance_out_of_range(self, mem_client, mem_prefix):
        resp = await _create_memory(mem_client, mem_prefix, importance=11)
        assert resp.status_code == 422  # pydantic validation


# ============================================================================
# Get Memory
# ============================================================================


class TestGetMemory:
    """GET /api/memories/{memory_id}"""

    async def test_get_own_memory(self, mem_client, mem_prefix):
        create_resp = await _create_memory(mem_client, mem_prefix)
        memory_id = create_resp.json()["id"]

        resp = await mem_client.get(f"/api/memories/{memory_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == memory_id
        assert resp.json()["content"] == f"{mem_prefix}Test memory content"

    async def test_get_not_found(self, mem_client):
        fake_id = str(uuid4())
        resp = await mem_client.get(f"/api/memories/{fake_id}")
        assert resp.status_code == 404

    async def test_get_invalid_uuid(self, mem_client):
        resp = await mem_client.get("/api/memories/not-a-uuid")
        assert resp.status_code == 422


# ============================================================================
# Update Memory
# ============================================================================


class TestUpdateMemory:
    """PATCH /api/memories/{memory_id}"""

    async def test_update_content(self, mem_client, mem_prefix):
        create_resp = await _create_memory(mem_client, mem_prefix)
        memory_id = create_resp.json()["id"]

        resp = await mem_client.patch(
            f"/api/memories/{memory_id}",
            json={
                "content": f"{mem_prefix}Updated content",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["content"] == f"{mem_prefix}Updated content"

    async def test_update_tags(self, mem_client, mem_prefix):
        create_resp = await _create_memory(mem_client, mem_prefix)
        memory_id = create_resp.json()["id"]

        resp = await mem_client.patch(
            f"/api/memories/{memory_id}",
            json={
                "tags": ["updated", "new-tag"],
            },
        )
        assert resp.status_code == 200
        assert "updated" in resp.json()["tags"]

    async def test_update_importance(self, mem_client, mem_prefix):
        create_resp = await _create_memory(mem_client, mem_prefix)
        memory_id = create_resp.json()["id"]

        resp = await mem_client.patch(
            f"/api/memories/{memory_id}",
            json={
                "importance": 9,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["importance"] == 9

    async def test_update_not_found(self, mem_client):
        fake_id = str(uuid4())
        resp = await mem_client.patch(
            f"/api/memories/{fake_id}",
            json={
                "content": "nope",
            },
        )
        assert resp.status_code == 404

    async def test_update_other_users_memory_forbidden(self, mem_client, mem_client_b, mem_prefix):
        """User B cannot update User A's memory."""
        create_resp = await _create_memory(mem_client, mem_prefix)
        memory_id = create_resp.json()["id"]

        resp = await mem_client_b.patch(
            f"/api/memories/{memory_id}",
            json={
                "content": "hacked",
            },
        )
        # Should be 403 or 404 (not leaking existence)
        assert resp.status_code in (403, 404)


# ============================================================================
# Delete Memory
# ============================================================================


class TestDeleteMemory:
    """DELETE /api/memories/{memory_id}"""

    async def test_delete_own_memory(self, mem_client, mem_prefix):
        create_resp = await _create_memory(mem_client, mem_prefix)
        memory_id = create_resp.json()["id"]

        resp = await mem_client.delete(f"/api/memories/{memory_id}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # Verify it's gone
        get_resp = await mem_client.get(f"/api/memories/{memory_id}")
        assert get_resp.status_code == 404

    async def test_delete_not_found(self, mem_client):
        fake_id = str(uuid4())
        resp = await mem_client.delete(f"/api/memories/{fake_id}")
        assert resp.status_code == 404

    async def test_delete_other_users_memory_forbidden(self, mem_client, mem_client_b, mem_prefix):
        """User B cannot delete User A's memory."""
        create_resp = await _create_memory(mem_client, mem_prefix)
        memory_id = create_resp.json()["id"]

        resp = await mem_client_b.delete(f"/api/memories/{memory_id}")
        assert resp.status_code in (403, 404)


# ============================================================================
# Share / Unshare
# ============================================================================


class TestShareMemory:
    """POST /api/memories/{memory_id}/share and /unshare"""

    async def test_share_memory(self, mem_client, mem_prefix):
        create_resp = await _create_memory(mem_client, mem_prefix)
        memory_id = create_resp.json()["id"]

        resp = await mem_client.post(f"/api/memories/{memory_id}/share")
        assert resp.status_code == 200
        assert resp.json()["shared"] is True

    async def test_unshare_memory(self, mem_client, mem_prefix):
        create_resp = await _create_memory(mem_client, mem_prefix)
        memory_id = create_resp.json()["id"]

        # Share first
        await mem_client.post(f"/api/memories/{memory_id}/share")
        # Then unshare
        resp = await mem_client.post(f"/api/memories/{memory_id}/unshare")
        assert resp.status_code == 200
        assert resp.json()["shared"] is False

    async def test_share_not_found(self, mem_client):
        fake_id = str(uuid4())
        resp = await mem_client.post(f"/api/memories/{fake_id}/share")
        assert resp.status_code == 404

    async def test_unshare_not_found(self, mem_client):
        fake_id = str(uuid4())
        resp = await mem_client.post(f"/api/memories/{fake_id}/unshare")
        assert resp.status_code == 404


# ============================================================================
# Tags
# ============================================================================


class TestMemoryTags:
    """GET /api/memories/tags/list and /api/memories/tags/suggest"""

    async def test_list_tags(self, mem_client, mem_prefix):
        # Create memories with tags
        await _create_memory(mem_client, mem_prefix, tags=["alpha", "beta"])
        await _create_memory(
            mem_client,
            mem_prefix,
            tags=["beta", "gamma"],
            content=f"{mem_prefix}Second memory",
        )

        resp = await mem_client.get("/api/memories/tags/list")
        assert resp.status_code == 200
        data = resp.json()
        assert "tags" in data
        assert "total_count" in data

    async def test_list_tags_with_type_filter(self, mem_client, mem_prefix):
        await _create_memory(mem_client, mem_prefix, tags=["typed-tag"])

        resp = await mem_client.get("/api/memories/tags/list", params={"type": "experience"})
        assert resp.status_code == 200

    async def test_suggest_tags(self, mem_client, mem_prefix):
        await _create_memory(mem_client, mem_prefix, tags=["python-debugging"])

        resp = await mem_client.get("/api/memories/tags/suggest", params={"query": "python"})
        assert resp.status_code == 200
        data = resp.json()
        assert "suggestions" in data
        assert data["query"] == "python"
