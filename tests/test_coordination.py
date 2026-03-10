"""Tests for distributed coordination — atomic claiming and optimistic locking."""

import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio

from lucent.db import MemoryRepository, VersionConflictError


@pytest_asyncio.fixture
async def coord_prefix(db_pool):
    """Create and clean up test data for coordination tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_coord_{test_id}_"
    yield prefix
    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memory_audit_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM memory_access_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%"
        )
        await conn.execute("DELETE FROM memories WHERE username LIKE $1", f"{prefix}%")
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%"
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def coord_user(db_pool, coord_prefix):
    """Create a test user for coordination tests."""
    from lucent.db import OrganizationRepository, UserRepository

    prefix = coord_prefix
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}user@test.com",
        display_name=f"{prefix}User",
    )
    return user


@pytest_asyncio.fixture
async def pending_task(db_pool, coord_user, coord_prefix):
    """Create a pending daemon task memory."""
    repo = MemoryRepository(db_pool)
    memory = await repo.create(
        username=f"{coord_prefix}user",
        type="procedural",
        content="Test daemon task for coordination testing",
        tags=["daemon-task", "pending", "code"],
        importance=7,
        user_id=coord_user["id"],
        organization_id=coord_user["organization_id"],
    )
    return memory


# ============================================================================
# Atomic Claim Tests
# ============================================================================


class TestClaimTask:
    """Tests for atomic task claiming."""

    async def test_claim_pending_task(self, db_pool, pending_task):
        """Successfully claim a pending task."""
        repo = MemoryRepository(db_pool)
        result = await repo.claim_task(pending_task["id"], "instance-a")

        assert result is not None
        assert "pending" not in result["tags"]
        assert "claimed-by-instance-a" in result["tags"]
        assert "daemon-task" in result["tags"]
        assert result["version"] == pending_task["version"] + 1

    async def test_claim_already_claimed_task(self, db_pool, pending_task):
        """Cannot claim a task that's already claimed."""
        repo = MemoryRepository(db_pool)
        # First claim succeeds
        result = await repo.claim_task(pending_task["id"], "instance-a")
        assert result is not None

        # Second claim fails
        result2 = await repo.claim_task(pending_task["id"], "instance-b")
        assert result2 is None

    async def test_claim_nonexistent_task(self, db_pool):
        """Cannot claim a task that doesn't exist."""
        repo = MemoryRepository(db_pool)
        result = await repo.claim_task(uuid4(), "instance-a")
        assert result is None

    async def test_concurrent_claims(self, db_pool, coord_user, coord_prefix):
        """Only one of N concurrent claims succeeds (race condition test)."""
        repo = MemoryRepository(db_pool)

        # Create a task
        task = await repo.create(
            username=f"{coord_prefix}user",
            type="procedural",
            content="Concurrent claim test task",
            tags=["daemon-task", "pending", "code"],
            importance=7,
            user_id=coord_user["id"],
            organization_id=coord_user["organization_id"],
        )

        # Launch 5 concurrent claims
        results = await asyncio.gather(
            repo.claim_task(task["id"], "instance-1"),
            repo.claim_task(task["id"], "instance-2"),
            repo.claim_task(task["id"], "instance-3"),
            repo.claim_task(task["id"], "instance-4"),
            repo.claim_task(task["id"], "instance-5"),
        )

        # Exactly one should succeed
        successful = [r for r in results if r is not None]
        assert len(successful) == 1
        winner = successful[0]
        assert any(t.startswith("claimed-by-instance-") for t in winner["tags"])
        assert "pending" not in winner["tags"]


# ============================================================================
# Release Claim Tests
# ============================================================================


class TestReleaseClaim:
    """Tests for releasing claimed tasks."""

    async def test_release_own_claim(self, db_pool, pending_task):
        """Release a task claimed by this instance."""
        repo = MemoryRepository(db_pool)
        await repo.claim_task(pending_task["id"], "instance-a")

        result = await repo.release_claim(pending_task["id"], "instance-a")
        assert result is not None
        assert "pending" in result["tags"]
        assert "claimed-by-instance-a" not in result["tags"]

    async def test_cannot_release_other_instance_claim(self, db_pool, pending_task):
        """Cannot release a task claimed by another instance when specifying instance_id."""
        repo = MemoryRepository(db_pool)
        await repo.claim_task(pending_task["id"], "instance-a")

        result = await repo.release_claim(pending_task["id"], "instance-b")
        assert result is None

    async def test_release_any_claim(self, db_pool, pending_task):
        """Release any claim without specifying instance_id (for stale detection)."""
        repo = MemoryRepository(db_pool)
        await repo.claim_task(pending_task["id"], "instance-a")

        result = await repo.release_claim(pending_task["id"], instance_id=None)
        assert result is not None
        assert "pending" in result["tags"]

    async def test_release_unclaimed_task(self, db_pool, pending_task):
        """Cannot release a task that isn't claimed."""
        repo = MemoryRepository(db_pool)
        result = await repo.release_claim(pending_task["id"], "instance-a")
        assert result is None


# ============================================================================
# Optimistic Locking Tests
# ============================================================================


class TestOptimisticLocking:
    """Tests for version-based optimistic locking."""

    async def test_update_with_correct_version(self, db_pool, pending_task):
        """Update succeeds when expected_version matches."""
        repo = MemoryRepository(db_pool)
        result = await repo.update(
            memory_id=pending_task["id"],
            content="Updated content",
            expected_version=pending_task["version"],
        )
        assert result is not None
        assert result["content"] == "Updated content"
        assert result["version"] == pending_task["version"] + 1

    async def test_update_with_wrong_version(self, db_pool, pending_task):
        """Update raises VersionConflictError when expected_version is stale."""
        repo = MemoryRepository(db_pool)

        # First update succeeds
        await repo.update(
            memory_id=pending_task["id"],
            content="First update",
            expected_version=pending_task["version"],
        )

        # Second update with stale version fails
        with pytest.raises(VersionConflictError) as exc_info:
            await repo.update(
                memory_id=pending_task["id"],
                content="Stale update",
                expected_version=pending_task["version"],
            )

        assert exc_info.value.expected_version == pending_task["version"]
        assert exc_info.value.actual_version == pending_task["version"] + 1

    async def test_update_without_version_always_succeeds(self, db_pool, pending_task):
        """Update without expected_version always succeeds (backward compatible)."""
        repo = MemoryRepository(db_pool)
        result = await repo.update(
            memory_id=pending_task["id"],
            content="No version check",
        )
        assert result is not None
        assert result["content"] == "No version check"
