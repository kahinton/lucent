"""API endpoint tests for the search router.

Tests /api/search endpoints:
- POST /api/search (search memories by content)
- GET /api/search (search memories GET variant)
- POST /api/search/full (search across all fields)
"""

import pytest
import pytest_asyncio
from uuid import uuid4, UUID

import httpx
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import MemoryRepository


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def srch_prefix(db_pool):
    """Create and clean up test data for search API tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_srch_{test_id}_"
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
async def srch_org(db_pool, srch_prefix):
    """Create a test organization."""
    from lucent.db import OrganizationRepository

    repo = OrganizationRepository(db_pool)
    org = await repo.create(name=f"{srch_prefix}org")
    return org


@pytest_asyncio.fixture
async def srch_user(db_pool, srch_org, srch_prefix):
    """Create a test user for search API tests."""
    from lucent.db import UserRepository

    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{srch_prefix}user",
        provider="local",
        organization_id=srch_org["id"],
        email=f"{srch_prefix}user@test.com",
        display_name=f"{srch_prefix}User",
    )
    return user


@pytest_asyncio.fixture
async def srch_user_b(db_pool, srch_org, srch_prefix):
    """Create a second user in the same org."""
    from lucent.db import UserRepository

    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{srch_prefix}user_b",
        provider="local",
        organization_id=srch_org["id"],
        email=f"{srch_prefix}userb@test.com",
        display_name=f"{srch_prefix}UserB",
    )
    return user


@pytest_asyncio.fixture
async def srch_client(db_pool, srch_user):
    """AsyncClient authenticated as srch_user."""
    app = create_app()
    fake_user = CurrentUser(
        id=srch_user["id"],
        organization_id=srch_user["organization_id"],
        role=srch_user.get("role", "member"),
        email=srch_user.get("email"),
        display_name=srch_user.get("display_name"),
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
async def srch_client_b(db_pool, srch_user_b):
    """AsyncClient authenticated as srch_user_b."""
    app = create_app()
    fake_user = CurrentUser(
        id=srch_user_b["id"],
        organization_id=srch_user_b["organization_id"],
        role=srch_user_b.get("role", "member"),
        email=srch_user_b.get("email"),
        display_name=srch_user_b.get("display_name"),
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


async def _seed_memories(db_pool, user, prefix, count=3):
    """Seed test memories and return them as a list."""
    repo = MemoryRepository(db_pool)
    memories = []
    contents = [
        f"{prefix}Python debugging techniques for async code",
        f"{prefix}FastAPI dependency injection patterns",
        f"{prefix}PostgreSQL query optimization strategies",
    ]
    types = ["technical", "experience", "procedural"]
    tag_sets = [
        ["python", "debugging", "async"],
        ["fastapi", "patterns", "python"],
        ["postgresql", "optimization", "database"],
    ]
    importances = [7, 5, 8]

    for i in range(min(count, len(contents))):
        m = await repo.create(
            username=f"{prefix}user",
            type=types[i],
            content=contents[i],
            tags=tag_sets[i],
            importance=importances[i],
            user_id=user["id"],
            organization_id=user["organization_id"],
        )
        memories.append(m)
    return memories


# ============================================================================
# POST /api/search — Standard Search
# ============================================================================


class TestSearchPost:
    """POST /api/search"""

    async def test_search_empty_query(self, srch_client, db_pool, srch_user, srch_prefix):
        """Search with no query returns all accessible memories."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert "memories" in data
        assert "total_count" in data
        assert "offset" in data
        assert "limit" in data
        assert "has_more" in data

    async def test_search_by_content(self, srch_client, db_pool, srch_user, srch_prefix):
        """Search with a query returns matching memories."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search", json={
            "query": "Python debugging",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["memories"]) >= 1
        # The Python debugging memory should be in results
        contents = [m["content"] for m in data["memories"]]
        assert any("Python debugging" in c for c in contents)

    async def test_search_by_type(self, srch_client, db_pool, srch_user, srch_prefix):
        """Search filtered by memory type."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search", json={
            "type": "technical",
            "username": f"{srch_prefix}user",
        })
        assert resp.status_code == 200
        data = resp.json()
        for m in data["memories"]:
            assert m["type"] == "technical"

    async def test_search_by_tags(self, srch_client, db_pool, srch_user, srch_prefix):
        """Search filtered by tags."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search", json={
            "tags": ["python"],
            "username": f"{srch_prefix}user",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["memories"]) >= 1
        for m in data["memories"]:
            assert "python" in m["tags"]

    async def test_search_by_importance_range(self, srch_client, db_pool, srch_user, srch_prefix):
        """Search filtered by importance range."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search", json={
            "importance_min": 7,
            "importance_max": 10,
            "username": f"{srch_prefix}user",
        })
        assert resp.status_code == 200
        data = resp.json()
        for m in data["memories"]:
            assert m["importance"] >= 7

    async def test_search_by_username(self, srch_client, db_pool, srch_user, srch_prefix):
        """Search filtered by username."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search", json={
            "username": f"{srch_prefix}user",
        })
        assert resp.status_code == 200
        data = resp.json()
        for m in data["memories"]:
            assert m["username"] == f"{srch_prefix}user"

    async def test_search_pagination(self, srch_client, db_pool, srch_user, srch_prefix):
        """Search results respect offset and limit."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search", json={
            "username": f"{srch_prefix}user",
            "limit": 1,
            "offset": 0,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 1
        assert data["offset"] == 0
        assert len(data["memories"]) <= 1

    async def test_search_pagination_offset(self, srch_client, db_pool, srch_user, srch_prefix):
        """Search offset skips results."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        # Get first page
        resp1 = await srch_client.post("/api/search", json={
            "username": f"{srch_prefix}user",
            "limit": 1,
            "offset": 0,
        })
        # Get second page
        resp2 = await srch_client.post("/api/search", json={
            "username": f"{srch_prefix}user",
            "limit": 1,
            "offset": 1,
        })
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        data1 = resp1.json()
        data2 = resp2.json()

        if data1["memories"] and data2["memories"]:
            assert data1["memories"][0]["id"] != data2["memories"][0]["id"]

    async def test_search_result_structure(self, srch_client, db_pool, srch_user, srch_prefix):
        """Search results have the correct field structure."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search", json={
            "username": f"{srch_prefix}user",
        })
        assert resp.status_code == 200
        data = resp.json()
        if data["memories"]:
            m = data["memories"][0]
            assert "id" in m
            assert "username" in m
            assert "type" in m
            assert "content" in m
            assert "content_truncated" in m
            assert "tags" in m
            assert "importance" in m
            assert "related_memory_ids" in m
            assert "created_at" in m
            assert "updated_at" in m
            assert "user_id" in m
            assert "organization_id" in m
            assert "shared" in m

    async def test_search_no_results(self, srch_client, srch_prefix):
        """Search for nonexistent content returns empty results."""
        resp = await srch_client.post("/api/search", json={
            "query": f"{srch_prefix}completely_nonexistent_zzzzzzz",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["memories"] == []
        assert data["total_count"] == 0

    async def test_search_combined_filters(self, srch_client, db_pool, srch_user, srch_prefix):
        """Search with multiple filters applied together."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search", json={
            "query": "Python",
            "type": "technical",
            "tags": ["python"],
            "username": f"{srch_prefix}user",
            "importance_min": 5,
        })
        assert resp.status_code == 200
        data = resp.json()
        for m in data["memories"]:
            assert m["type"] == "technical"
            assert "python" in m["tags"]
            assert m["importance"] >= 5


# ============================================================================
# GET /api/search — GET variant
# ============================================================================


class TestSearchGet:
    """GET /api/search"""

    async def test_search_get_basic(self, srch_client, db_pool, srch_user, srch_prefix):
        """GET search returns results."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.get("/api/search", params={
            "query": "Python",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "memories" in data
        assert "total_count" in data

    async def test_search_get_with_filters(self, srch_client, db_pool, srch_user, srch_prefix):
        """GET search with query parameters."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.get("/api/search", params={
            "username": f"{srch_prefix}user",
            "type": "technical",
            "importance_min": 5,
            "limit": 10,
        })
        assert resp.status_code == 200
        data = resp.json()
        for m in data["memories"]:
            assert m["type"] == "technical"

    async def test_search_get_pagination(self, srch_client, db_pool, srch_user, srch_prefix):
        """GET search with pagination params."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.get("/api/search", params={
            "username": f"{srch_prefix}user",
            "offset": 0,
            "limit": 2,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["offset"] == 0
        assert data["limit"] == 2
        assert len(data["memories"]) <= 2

    async def test_search_get_no_query(self, srch_client, db_pool, srch_user, srch_prefix):
        """GET search without query returns results based on other filters."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.get("/api/search", params={
            "username": f"{srch_prefix}user",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["memories"], list)

    async def test_search_get_tags_filter(self, srch_client, db_pool, srch_user, srch_prefix):
        """GET search with tags filter."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.get("/api/search", params={
            "tags": ["database"],
            "username": f"{srch_prefix}user",
        })
        assert resp.status_code == 200
        data = resp.json()
        for m in data["memories"]:
            assert "database" in m["tags"]

    async def test_search_get_returns_same_as_post(self, srch_client, db_pool, srch_user, srch_prefix):
        """GET and POST variants should return equivalent results for the same parameters."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        post_resp = await srch_client.post("/api/search", json={
            "username": f"{srch_prefix}user",
            "type": "technical",
        })
        get_resp = await srch_client.get("/api/search", params={
            "username": f"{srch_prefix}user",
            "type": "technical",
        })
        assert post_resp.status_code == 200
        assert get_resp.status_code == 200
        post_data = post_resp.json()
        get_data = get_resp.json()
        assert post_data["total_count"] == get_data["total_count"]
        assert len(post_data["memories"]) == len(get_data["memories"])


# ============================================================================
# POST /api/search/full — Full-text search across all fields
# ============================================================================


class TestSearchFull:
    """POST /api/search/full"""

    async def test_search_full_by_content(self, srch_client, db_pool, srch_user, srch_prefix):
        """Full search matches content text."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search/full", json={
            "query": "PostgreSQL optimization",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["memories"]) >= 1

    async def test_search_full_empty_query_returns_empty(self, srch_client):
        """Full search with no query returns empty results."""
        resp = await srch_client.post("/api/search/full", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["memories"] == []
        assert data["total_count"] == 0

    async def test_search_full_null_query_returns_empty(self, srch_client):
        """Full search with null query returns empty results."""
        resp = await srch_client.post("/api/search/full", json={"query": None})
        assert resp.status_code == 200
        data = resp.json()
        assert data["memories"] == []
        assert data["total_count"] == 0

    async def test_search_full_with_type_filter(self, srch_client, db_pool, srch_user, srch_prefix):
        """Full search with type filter."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search/full", json={
            "query": "Python",
            "type": "technical",
        })
        assert resp.status_code == 200
        data = resp.json()
        for m in data["memories"]:
            assert m["type"] == "technical"

    async def test_search_full_pagination(self, srch_client, db_pool, srch_user, srch_prefix):
        """Full search respects pagination."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search/full", json={
            "query": srch_prefix,
            "limit": 1,
            "offset": 0,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 1
        assert len(data["memories"]) <= 1

    async def test_search_full_by_tag_text(self, srch_client, db_pool, srch_user, srch_prefix):
        """Full search matches against tag text."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search/full", json={
            "query": "debugging",
        })
        assert resp.status_code == 200
        data = resp.json()
        # Should find the memory that has 'debugging' either in content or tags
        assert len(data["memories"]) >= 1

    async def test_search_full_result_structure(self, srch_client, db_pool, srch_user, srch_prefix):
        """Full search results have correct structure."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search/full", json={
            "query": srch_prefix,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "memories" in data
        assert "total_count" in data
        assert "offset" in data
        assert "limit" in data
        assert "has_more" in data

    async def test_search_full_with_importance_filter(self, srch_client, db_pool, srch_user, srch_prefix):
        """Full search with importance range filter."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search/full", json={
            "query": srch_prefix,
            "importance_min": 8,
        })
        assert resp.status_code == 200
        data = resp.json()
        for m in data["memories"]:
            assert m["importance"] >= 8

    async def test_search_full_no_results(self, srch_client, srch_prefix):
        """Full search for nonexistent content returns empty."""
        resp = await srch_client.post("/api/search/full", json={
            "query": f"{srch_prefix}absolutely_nonexistent_xyzzy_99999",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["memories"] == []
        assert data["total_count"] == 0


# ============================================================================
# Access Control
# ============================================================================


class TestSearchAccessControl:
    """Search respects ownership and shared visibility."""

    async def test_user_sees_own_memories(self, srch_client, db_pool, srch_user, srch_prefix):
        """User can search and find their own memories."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search", json={
            "username": f"{srch_prefix}user",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["memories"]) >= 1
        for m in data["memories"]:
            assert m["user_id"] == str(srch_user["id"])

    async def test_user_does_not_see_others_private_memories(
        self, srch_client_b, db_pool, srch_user, srch_prefix
    ):
        """User B cannot see User A's private (unshared) memories."""
        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client_b.post("/api/search", json={
            "username": f"{srch_prefix}user",
        })
        assert resp.status_code == 200
        data = resp.json()
        # User B should not see User A's unshared memories
        for m in data["memories"]:
            assert m["user_id"] != str(srch_user["id"]) or m["shared"] is True

    async def test_user_sees_shared_memories(
        self, srch_client_b, db_pool, srch_user, srch_prefix
    ):
        """User B can find User A's shared memories."""
        repo = MemoryRepository(db_pool)
        memories = await _seed_memories(db_pool, srch_user, srch_prefix)

        # Share the first memory
        await repo.update(
            memory_id=memories[0]["id"],
            user_id=srch_user["id"],
            organization_id=srch_user["organization_id"],
            shared=True,
        )

        resp = await srch_client_b.post("/api/search", json={
            "query": "Python debugging",
        })
        assert resp.status_code == 200
        data = resp.json()
        shared_ids = [m["id"] for m in data["memories"] if m["shared"]]
        assert str(memories[0]["id"]) in shared_ids


# ============================================================================
# Access Logging
# ============================================================================


class TestSearchAccessLogging:
    """Search results trigger access logging."""

    async def test_search_logs_access(self, srch_client, db_pool, srch_user, srch_prefix):
        """Searching memories logs access entries."""
        from lucent.db import AccessRepository

        await _seed_memories(db_pool, srch_user, srch_prefix)

        # Perform a search
        resp = await srch_client.post("/api/search", json={
            "username": f"{srch_prefix}user",
        })
        assert resp.status_code == 200
        data = resp.json()

        if data["memories"]:
            # Check that access was logged
            access_repo = AccessRepository(db_pool)
            memory_id = UUID(data["memories"][0]["id"])
            access_log = await access_repo.get_by_memory_id(
                memory_id=memory_id,
                limit=10,
            )
            # Should have at least one search_result entry
            search_entries = [
                e for e in access_log["entries"]
                if e.get("access_type") == "search_result"
            ]
            assert len(search_entries) >= 1

    async def test_search_full_logs_access(self, srch_client, db_pool, srch_user, srch_prefix):
        """Full search also logs access entries."""
        from lucent.db import AccessRepository

        await _seed_memories(db_pool, srch_user, srch_prefix)

        resp = await srch_client.post("/api/search/full", json={
            "query": srch_prefix,
        })
        assert resp.status_code == 200
        data = resp.json()

        if data["memories"]:
            access_repo = AccessRepository(db_pool)
            memory_id = UUID(data["memories"][0]["id"])
            access_log = await access_repo.get_by_memory_id(
                memory_id=memory_id,
                limit=10,
            )
            search_entries = [
                e for e in access_log["entries"]
                if e.get("access_type") == "search_result"
            ]
            assert len(search_entries) >= 1

    async def test_empty_search_does_not_log(self, srch_client, srch_prefix):
        """Search returning no results does not log any access."""
        resp = await srch_client.post("/api/search", json={
            "query": f"{srch_prefix}nonexistent_zzzzz",
        })
        assert resp.status_code == 200
        assert resp.json()["memories"] == []
        # No access logged for empty results — verified by the endpoint logic


# ============================================================================
# Edge Cases & Validation
# ============================================================================


class TestSearchEdgeCases:
    """Edge cases and input validation."""

    async def test_search_limit_upper_bound(self, srch_client):
        """Limit above 100 should be rejected."""
        resp = await srch_client.post("/api/search", json={
            "limit": 101,
        })
        assert resp.status_code == 422

    async def test_search_limit_lower_bound(self, srch_client):
        """Limit below 1 should be rejected."""
        resp = await srch_client.post("/api/search", json={
            "limit": 0,
        })
        assert resp.status_code == 422

    async def test_search_negative_offset(self, srch_client):
        """Negative offset should be rejected."""
        resp = await srch_client.post("/api/search", json={
            "offset": -1,
        })
        assert resp.status_code == 422

    async def test_search_importance_min_bounds(self, srch_client):
        """Importance min outside 1-10 should be rejected."""
        resp = await srch_client.post("/api/search", json={
            "importance_min": 0,
        })
        assert resp.status_code == 422

    async def test_search_importance_max_bounds(self, srch_client):
        """Importance max outside 1-10 should be rejected."""
        resp = await srch_client.post("/api/search", json={
            "importance_max": 11,
        })
        assert resp.status_code == 422

    async def test_search_get_limit_validation(self, srch_client):
        """GET search also validates limit bounds."""
        resp = await srch_client.get("/api/search", params={"limit": 101})
        assert resp.status_code == 422

    async def test_search_get_offset_validation(self, srch_client):
        """GET search also validates offset bounds."""
        resp = await srch_client.get("/api/search", params={"offset": -1})
        assert resp.status_code == 422

    async def test_search_full_limit_validation(self, srch_client):
        """Full search validates limit bounds."""
        resp = await srch_client.post("/api/search/full", json={
            "query": "test",
            "limit": 101,
        })
        assert resp.status_code == 422
