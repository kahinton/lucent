"""Tests for audit log repository and API endpoints.

Covers:
- AuditRepository: log(), get_by_memory_id(), get_by_user_id(),
  get_by_organization_id(), get_recent(), get_versions(), get_version_snapshot()
- API: GET /api/audit/memory/{id}, /api/audit/user/{id},
  /api/audit/organization, /api/audit/recent
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import AuditRepository, MemoryRepository, OrganizationRepository, UserRepository

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def audit_prefix(db_pool):
    """Create and clean up test data for audit tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_audit_{test_id}_"
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
async def audit_org(db_pool, audit_prefix):
    """Create a test organization for audit tests."""
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{audit_prefix}org")


@pytest_asyncio.fixture
async def audit_user(db_pool, audit_org, audit_prefix):
    """Create a test user for audit tests."""
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{audit_prefix}user",
        provider="local",
        organization_id=audit_org["id"],
        email=f"{audit_prefix}user@test.com",
        display_name=f"{audit_prefix}User",
    )


@pytest_asyncio.fixture
async def audit_admin(db_pool, audit_org, audit_prefix):
    """Create an admin user for audit tests."""
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{audit_prefix}admin",
        provider="local",
        organization_id=audit_org["id"],
        email=f"{audit_prefix}admin@test.com",
        display_name=f"{audit_prefix}Admin",
        role="admin",
    )


@pytest_asyncio.fixture
async def audit_memory(db_pool, audit_user, audit_prefix):
    """Create a test memory for audit tests."""
    repo = MemoryRepository(db_pool)
    return await repo.create(
        username=f"{audit_prefix}user",
        type="experience",
        content=f"{audit_prefix} Test memory for audit",
        tags=["test", "audit"],
        importance=5,
        user_id=audit_user["id"],
        organization_id=audit_user["organization_id"],
    )


@pytest_asyncio.fixture
async def member_client(db_pool, audit_user):
    """httpx AsyncClient authenticated as a regular member."""
    with patch("lucent.api.app.is_team_mode", return_value=True):
        app = create_app()
    fake_user = CurrentUser(
        id=audit_user["id"],
        organization_id=audit_user["organization_id"],
        role="member",
        email=audit_user.get("email"),
        display_name=audit_user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )
    app.dependency_overrides[get_current_user] = lambda: fake_user
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_client(db_pool, audit_admin):
    """httpx AsyncClient authenticated as an admin."""
    with patch("lucent.api.app.is_team_mode", return_value=True):
        app = create_app()
    fake_user = CurrentUser(
        id=audit_admin["id"],
        organization_id=audit_admin["organization_id"],
        role="admin",
        email=audit_admin.get("email"),
        display_name=audit_admin.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )
    app.dependency_overrides[get_current_user] = lambda: fake_user
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ============================================================================
# AuditRepository Tests
# ============================================================================


class TestAuditRepositoryLog:
    """Tests for AuditRepository.log()."""

    async def test_log_basic_entry(self, db_pool, audit_user, audit_memory):
        """Test creating a basic audit log entry."""
        repo = AuditRepository(db_pool)
        entry = await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
            organization_id=audit_user["organization_id"],
        )

        assert entry["id"] is not None
        assert entry["memory_id"] == audit_memory["id"]
        assert entry["user_id"] == audit_user["id"]
        assert entry["action_type"] == "create"
        assert entry["created_at"] is not None

    async def test_log_with_changed_fields(self, db_pool, audit_user, audit_memory):
        """Test audit entry with field change tracking."""
        repo = AuditRepository(db_pool)
        entry = await repo.log(
            memory_id=audit_memory["id"],
            action_type="update",
            user_id=audit_user["id"],
            organization_id=audit_user["organization_id"],
            changed_fields=["content", "tags"],
            old_values={"content": "old content", "tags": ["old"]},
            new_values={"content": "new content", "tags": ["new"]},
        )

        assert entry["changed_fields"] == ["content", "tags"]
        assert entry["old_values"]["content"] == "old content"
        assert entry["new_values"]["content"] == "new content"

    async def test_log_with_version_and_snapshot(self, db_pool, audit_user, audit_memory):
        """Test audit entry with version number and snapshot."""
        repo = AuditRepository(db_pool)
        snapshot = {"content": "snapshot content", "tags": ["v1"]}
        entry = await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
            organization_id=audit_user["organization_id"],
            version=1,
            snapshot=snapshot,
        )

        assert entry["version"] == 1
        assert entry["snapshot"] == snapshot

    async def test_log_with_notes(self, db_pool, audit_user, audit_memory):
        """Test audit entry with notes."""
        repo = AuditRepository(db_pool)
        entry = await repo.log(
            memory_id=audit_memory["id"],
            action_type="delete",
            user_id=audit_user["id"],
            notes="Soft deleted by user request",
        )

        assert entry["notes"] == "Soft deleted by user request"
        assert entry["action_type"] == "delete"

    async def test_log_with_context(self, db_pool, audit_user, audit_memory):
        """Test audit entry with context metadata."""
        repo = AuditRepository(db_pool)
        ctx = {"ip": "127.0.0.1", "user_agent": "test-client"}
        entry = await repo.log(
            memory_id=audit_memory["id"],
            action_type="update",
            user_id=audit_user["id"],
            context=ctx,
        )

        assert entry["context"]["ip"] == "127.0.0.1"

    async def test_log_without_user_id(self, db_pool, audit_memory):
        """Test audit entry without user_id (system action)."""
        repo = AuditRepository(db_pool)
        entry = await repo.log(
            memory_id=audit_memory["id"],
            action_type="system_cleanup",
        )

        assert entry["user_id"] is None
        assert entry["action_type"] == "system_cleanup"


class TestAuditRepositoryGetByMemoryId:
    """Tests for AuditRepository.get_by_memory_id()."""

    async def test_get_entries_for_memory(self, db_pool, audit_user, audit_memory):
        """Test retrieving audit entries for a specific memory."""
        repo = AuditRepository(db_pool)

        # Create several entries
        for action in ["create", "update", "update"]:
            await repo.log(
                memory_id=audit_memory["id"],
                action_type=action,
                user_id=audit_user["id"],
                organization_id=audit_user["organization_id"],
            )

        result = await repo.get_by_memory_id(audit_memory["id"])

        assert result["total_count"] == 3
        assert len(result["entries"]) == 3
        assert result["offset"] == 0
        assert result["limit"] == 50
        assert result["has_more"] is False

    async def test_pagination(self, db_pool, audit_user, audit_memory):
        """Test pagination of audit entries."""
        repo = AuditRepository(db_pool)

        for i in range(5):
            await repo.log(
                memory_id=audit_memory["id"],
                action_type="update",
                user_id=audit_user["id"],
            )

        page1 = await repo.get_by_memory_id(audit_memory["id"], limit=2, offset=0)
        assert len(page1["entries"]) == 2
        assert page1["total_count"] == 5
        assert page1["has_more"] is True

        page2 = await repo.get_by_memory_id(audit_memory["id"], limit=2, offset=2)
        assert len(page2["entries"]) == 2
        assert page2["has_more"] is True

        page3 = await repo.get_by_memory_id(audit_memory["id"], limit=2, offset=4)
        assert len(page3["entries"]) == 1
        assert page3["has_more"] is False

    async def test_empty_result(self, db_pool):
        """Test querying a memory with no audit entries."""
        repo = AuditRepository(db_pool)
        result = await repo.get_by_memory_id(uuid4())

        assert result["total_count"] == 0
        assert len(result["entries"]) == 0
        assert result["has_more"] is False

    async def test_entries_ordered_by_created_at_desc(self, db_pool, audit_user, audit_memory):
        """Test that entries are returned newest first."""
        repo = AuditRepository(db_pool)

        await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
        )
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="update",
            user_id=audit_user["id"],
        )

        result = await repo.get_by_memory_id(audit_memory["id"])
        entries = result["entries"]
        assert entries[0]["created_at"] >= entries[1]["created_at"]


class TestAuditRepositoryGetByUserId:
    """Tests for AuditRepository.get_by_user_id()."""

    async def test_get_entries_for_user(self, db_pool, audit_user, audit_memory):
        """Test retrieving audit entries by user."""
        repo = AuditRepository(db_pool)

        await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
        )
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="update",
            user_id=audit_user["id"],
        )

        result = await repo.get_by_user_id(audit_user["id"])
        assert result["total_count"] == 2
        assert len(result["entries"]) == 2

    async def test_filter_by_action_type(self, db_pool, audit_user, audit_memory):
        """Test filtering audit entries by action type."""
        repo = AuditRepository(db_pool)

        await repo.log(memory_id=audit_memory["id"], action_type="create", user_id=audit_user["id"])
        await repo.log(memory_id=audit_memory["id"], action_type="update", user_id=audit_user["id"])
        await repo.log(memory_id=audit_memory["id"], action_type="update", user_id=audit_user["id"])

        result = await repo.get_by_user_id(audit_user["id"], action_type="update")
        assert result["total_count"] == 2

    async def test_filter_by_since(self, db_pool, audit_user, audit_memory):
        """Test filtering audit entries by timestamp."""
        repo = AuditRepository(db_pool)

        await repo.log(memory_id=audit_memory["id"], action_type="create", user_id=audit_user["id"])

        # Search for entries since a past time (should find the entry)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        result = await repo.get_by_user_id(audit_user["id"], since=past)
        assert result["total_count"] >= 1

        # Search for entries since the future (should find nothing)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        result = await repo.get_by_user_id(audit_user["id"], since=future)
        assert result["total_count"] == 0


class TestAuditRepositoryGetByOrganizationId:
    """Tests for AuditRepository.get_by_organization_id()."""

    async def test_get_entries_for_org(self, db_pool, audit_user, audit_org, audit_memory):
        """Test retrieving audit entries by organization."""
        repo = AuditRepository(db_pool)

        await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
            organization_id=audit_org["id"],
        )

        result = await repo.get_by_organization_id(audit_org["id"])
        assert result["total_count"] == 1
        assert result["entries"][0]["organization_id"] == audit_org["id"]

    async def test_filter_action_type_and_since(self, db_pool, audit_user, audit_org, audit_memory):
        """Test combined action_type and since filters."""
        repo = AuditRepository(db_pool)

        await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
            organization_id=audit_org["id"],
        )
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="delete",
            user_id=audit_user["id"],
            organization_id=audit_org["id"],
        )

        past = datetime.now(timezone.utc) - timedelta(hours=1)
        result = await repo.get_by_organization_id(
            audit_org["id"], action_type="create", since=past
        )
        assert result["total_count"] == 1
        assert result["entries"][0]["action_type"] == "create"


class TestAuditRepositoryGetRecent:
    """Tests for AuditRepository.get_recent()."""

    async def test_get_recent_no_filters(self, db_pool, audit_user, audit_memory):
        """Test getting recent entries without filters."""
        repo = AuditRepository(db_pool)

        await repo.log(memory_id=audit_memory["id"], action_type="create", user_id=audit_user["id"])

        entries = await repo.get_recent()
        assert len(entries) >= 1

    async def test_get_recent_with_org_filter(self, db_pool, audit_user, audit_org, audit_memory):
        """Test filtering recent entries by organization."""
        repo = AuditRepository(db_pool)

        await repo.log(
            memory_id=audit_memory["id"],
            action_type="update",
            user_id=audit_user["id"],
            organization_id=audit_org["id"],
        )

        entries = await repo.get_recent(organization_id=audit_org["id"])
        assert len(entries) >= 1
        assert all(e["organization_id"] == audit_org["id"] for e in entries)

    async def test_get_recent_with_action_types(self, db_pool, audit_user, audit_memory):
        """Test filtering recent entries by action types list."""
        repo = AuditRepository(db_pool)

        await repo.log(memory_id=audit_memory["id"], action_type="create", user_id=audit_user["id"])
        await repo.log(memory_id=audit_memory["id"], action_type="delete", user_id=audit_user["id"])

        entries = await repo.get_recent(action_types=["create"])
        assert all(e["action_type"] == "create" for e in entries)

    async def test_get_recent_with_limit(self, db_pool, audit_user, audit_memory):
        """Test limiting recent entries."""
        repo = AuditRepository(db_pool)

        for _ in range(5):
            await repo.log(
                memory_id=audit_memory["id"], action_type="update", user_id=audit_user["id"]
            )

        entries = await repo.get_recent(limit=2)
        assert len(entries) == 2


class TestAuditRepositoryVersions:
    """Tests for get_versions() and get_version_snapshot()."""

    async def test_get_versions(self, db_pool, audit_user, audit_memory):
        """Test retrieving version history for a memory."""
        repo = AuditRepository(db_pool)

        for v in range(1, 4):
            await repo.log(
                memory_id=audit_memory["id"],
                action_type="create" if v == 1 else "update",
                user_id=audit_user["id"],
                version=v,
                snapshot={"content": f"version {v}"},
            )

        result = await repo.get_versions(audit_memory["id"])
        assert result["total_count"] == 3
        # Newest version first
        assert result["versions"][0]["version"] == 3
        assert result["versions"][-1]["version"] == 1

    async def test_get_versions_excludes_unversioned(self, db_pool, audit_user, audit_memory):
        """Test that entries without version numbers are excluded."""
        repo = AuditRepository(db_pool)

        # One versioned, one not
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
            version=1,
            snapshot={"content": "v1"},
        )
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="update",
            user_id=audit_user["id"],
        )

        result = await repo.get_versions(audit_memory["id"])
        assert result["total_count"] == 1

    async def test_get_version_snapshot(self, db_pool, audit_user, audit_memory):
        """Test retrieving a specific version snapshot."""
        repo = AuditRepository(db_pool)

        snapshot_data = {"content": "snapshot at v2", "tags": ["a", "b"]}
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="update",
            user_id=audit_user["id"],
            version=2,
            snapshot=snapshot_data,
        )

        entry = await repo.get_version_snapshot(audit_memory["id"], version=2)
        assert entry is not None
        assert entry["version"] == 2
        assert entry["snapshot"] == snapshot_data

    async def test_get_version_snapshot_not_found(self, db_pool, audit_memory):
        """Test snapshot retrieval for nonexistent version."""
        repo = AuditRepository(db_pool)
        entry = await repo.get_version_snapshot(audit_memory["id"], version=999)
        assert entry is None


class TestAuditRepositoryFilterColumnValidation:
    """Test the _FILTERABLE_COLUMNS guard."""

    async def test_invalid_filter_column_raises(self, db_pool):
        """Test that invalid filter columns raise ValueError."""
        repo = AuditRepository(db_pool)
        with pytest.raises(ValueError, match="Invalid filter column"):
            await repo._get_filtered_entries("injected_column", uuid4())


# ============================================================================
# Audit API Endpoint Tests
# ============================================================================


class TestAuditMemoryEndpoint:
    """Tests for GET /api/audit/memory/{memory_id}."""

    async def test_member_sees_own_entries(self, member_client, db_pool, audit_user, audit_memory):
        """Member can see their own audit entries for a memory."""
        repo = AuditRepository(db_pool)
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
            organization_id=audit_user["organization_id"],
        )

        resp = await member_client.get(f"/api/audit/memory/{audit_memory['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] >= 1
        for entry in data["entries"]:
            assert entry["user_id"] == str(audit_user["id"])

    async def test_admin_sees_org_entries(
        self, admin_client, db_pool, audit_user, audit_admin, audit_memory
    ):
        """Admin can see all org entries for a memory."""
        repo = AuditRepository(db_pool)
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
            organization_id=audit_admin["organization_id"],
        )

        resp = await admin_client.get(f"/api/audit/memory/{audit_memory['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] >= 1


class TestAuditUserEndpoint:
    """Tests for GET /api/audit/user/{user_id}."""

    async def test_member_sees_own_audit(self, member_client, db_pool, audit_user, audit_memory):
        """Member can view their own audit log."""
        repo = AuditRepository(db_pool)
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
        )

        resp = await member_client.get(f"/api/audit/user/{audit_user['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] >= 1

    async def test_member_cannot_see_other_user(self, member_client, audit_admin):
        """Member cannot view another user's audit log."""
        resp = await member_client.get(f"/api/audit/user/{audit_admin['id']}")
        assert resp.status_code == 403

    async def test_admin_sees_other_user(self, admin_client, db_pool, audit_user, audit_memory):
        """Admin can view any user's audit log in their org."""
        repo = AuditRepository(db_pool)
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="update",
            user_id=audit_user["id"],
        )

        resp = await admin_client.get(f"/api/audit/user/{audit_user['id']}")
        assert resp.status_code == 200

    async def test_filter_params(self, member_client, db_pool, audit_user, audit_memory):
        """Test action_type and since query parameters."""
        repo = AuditRepository(db_pool)
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
        )

        resp = await member_client.get(
            f"/api/audit/user/{audit_user['id']}",
            params={"action_type": "create"},
        )
        assert resp.status_code == 200
        for entry in resp.json()["entries"]:
            assert entry["action_type"] == "create"


class TestAuditOrganizationEndpoint:
    """Tests for GET /api/audit/organization."""

    async def test_admin_can_access(self, admin_client, db_pool, audit_user, audit_memory):
        """Admin can view organization audit log."""
        repo = AuditRepository(db_pool)
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
            organization_id=audit_user["organization_id"],
        )

        resp = await admin_client.get("/api/audit/organization")
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "total_count" in data

    async def test_member_forbidden(self, member_client):
        """Regular member cannot access organization audit log."""
        resp = await member_client.get("/api/audit/organization")
        assert resp.status_code == 403


class TestAuditRecentEndpoint:
    """Tests for GET /api/audit/recent."""

    async def test_admin_can_access(self, admin_client, db_pool, audit_user, audit_memory):
        """Admin can view recent audit entries."""
        repo = AuditRepository(db_pool)
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
        )

        resp = await admin_client.get("/api/audit/recent")
        assert resp.status_code == 200
        entries = resp.json()
        assert isinstance(entries, list)

    async def test_member_forbidden(self, member_client):
        """Regular member cannot access recent audit entries."""
        resp = await member_client.get("/api/audit/recent")
        assert resp.status_code == 403

    async def test_filter_params(self, admin_client, db_pool, audit_user, audit_memory):
        """Test action_types and limit query parameters."""
        repo = AuditRepository(db_pool)
        await repo.log(
            memory_id=audit_memory["id"],
            action_type="create",
            user_id=audit_user["id"],
        )

        resp = await admin_client.get(
            "/api/audit/recent",
            params={"action_types": ["create"], "limit": 5},
        )
        assert resp.status_code == 200
