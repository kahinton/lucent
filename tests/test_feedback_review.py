"""Tests for M3 Feedback & Review System.

Covers:
1. Review-queue memory search
2. Daemon task lifecycle (create → claim → complete/cancel)
"""

from uuid import uuid4

import pytest
import pytest_asyncio

from lucent.db import AuditRepository, MemoryRepository


@pytest_asyncio.fixture
async def fb_prefix(db_pool):
    """Create and clean up test data for feedback tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_fb_{test_id}_"
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
async def fb_user(db_pool, fb_prefix):
    """Create a test user for feedback tests."""
    from lucent.db import OrganizationRepository, UserRepository

    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{fb_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{fb_prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{fb_prefix}user@test.com",
        display_name=f"{fb_prefix}User",
    )
    return user


@pytest_asyncio.fixture
async def daemon_memory(db_pool, fb_user, fb_prefix):
    """Create a daemon memory tagged needs-review."""
    repo = MemoryRepository(db_pool)
    return await repo.create(
        username=f"{fb_prefix}user",
        type="experience",
        content="Daemon performed code review of auth module.",
        tags=["daemon", "needs-review", "code-review"],
        importance=6,
        user_id=fb_user["id"],
        organization_id=fb_user["organization_id"],
    )


@pytest_asyncio.fixture
async def pending_task(db_pool, fb_user, fb_prefix):
    """Create a pending daemon task memory."""
    repo = MemoryRepository(db_pool)
    return await repo.create(
        username=f"{fb_prefix}user",
        type="procedural",
        content="Research async patterns in Python",
        tags=["daemon-task", "daemon", "pending", "research", "medium"],
        importance=5,
        metadata={"submitted_by": str(fb_user["id"]), "source": "test"},
        user_id=fb_user["id"],
        organization_id=fb_user["organization_id"],
    )


# ============================================================================
# Feedback Tests
# ============================================================================


class TestReviewQueue:
    """Tests for finding memories in the review queue."""

    @pytest.mark.asyncio
    async def test_search_needs_review(self, db_pool, fb_user, fb_prefix):
        """Search with needs-review tag finds review items."""
        repo = MemoryRepository(db_pool)

        # Create review and non-review memories
        await repo.create(
            username=f"{fb_prefix}user",
            type="experience",
            content="Review me",
            tags=["daemon", "needs-review"],
            importance=5,
            user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )
        await repo.create(
            username=f"{fb_prefix}user",
            type="experience",
            content="Routine daemon work",
            tags=["daemon"],
            importance=5,
            user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )

        result = await repo.search(
            tags=["needs-review"],
            requesting_user_id=fb_user["id"],
            requesting_org_id=fb_user["organization_id"],
        )

        contents = [m["content"] for m in result["memories"]]
        assert "Review me" in contents
        assert "Routine daemon work" not in contents

    @pytest.mark.asyncio
    async def test_review_count(self, db_pool, fb_user, fb_prefix):
        """Can count pending review items."""
        repo = MemoryRepository(db_pool)

        for i in range(3):
            await repo.create(
                username=f"{fb_prefix}user",
                type="experience",
                content=f"Review item {i}",
                tags=["daemon", "needs-review"],
                importance=5,
                user_id=fb_user["id"],
                organization_id=fb_user["organization_id"],
            )

        result = await repo.search(
            tags=["daemon", "needs-review"],
            requesting_user_id=fb_user["id"],
            requesting_org_id=fb_user["organization_id"],
        )

        assert result["total_count"] >= 3


# ============================================================================
# Daemon Task Lifecycle Tests
# ============================================================================


class TestDaemonTaskLifecycle:
    """Tests for the full daemon task lifecycle."""

    @pytest.mark.asyncio
    async def test_create_task_with_tags(self, db_pool, fb_user, fb_prefix):
        """Creating a task stores correct tags and metadata."""
        repo = MemoryRepository(db_pool)
        task = await repo.create(
            username=f"{fb_prefix}user",
            type="procedural",
            content="Investigate memory consolidation approach",
            tags=["daemon-task", "daemon", "pending", "research", "high"],
            importance=8,
            metadata={"submitted_by": str(fb_user["id"]), "source": "api"},
            user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )

        assert "daemon-task" in task["tags"]
        assert "pending" in task["tags"]
        assert "research" in task["tags"]
        assert task["metadata"]["source"] == "api"

    @pytest.mark.asyncio
    async def test_list_pending_tasks(self, db_pool, fb_user, fb_prefix, pending_task):
        """Search for pending tasks returns them."""
        repo = MemoryRepository(db_pool)
        result = await repo.search(
            tags=["daemon-task", "pending"],
            requesting_user_id=fb_user["id"],
            requesting_org_id=fb_user["organization_id"],
        )

        task_ids = [m["id"] for m in result["memories"]]
        assert pending_task["id"] in task_ids

    @pytest.mark.asyncio
    async def test_claim_and_complete_task(self, db_pool, fb_user, pending_task):
        """Full lifecycle: claim → complete."""
        repo = MemoryRepository(db_pool)
        org_id = fb_user["organization_id"]

        # Claim
        claimed = await repo.claim_task(pending_task["id"], "test-instance", org_id=org_id)
        assert claimed is not None
        assert "pending" not in claimed["tags"]
        assert "claimed-by-test-instance" in claimed["tags"]

        # Complete: replace claim tag with completed
        new_tags = [t for t in claimed["tags"] if not t.startswith("claimed-by-")]
        new_tags.append("completed")
        result = await repo.update(
            memory_id=pending_task["id"], tags=new_tags, organization_id=org_id
        )

        assert "completed" in result["tags"]
        assert "pending" not in result["tags"]
        assert not any(t.startswith("claimed-by-") for t in result["tags"])

    @pytest.mark.asyncio
    async def test_cancel_pending_task(self, db_pool, fb_user, pending_task):
        """Pending task can be soft-deleted (cancelled)."""
        repo = MemoryRepository(db_pool)
        org_id = fb_user["organization_id"]
        await repo.delete(pending_task["id"], organization_id=org_id)

        # Should not be findable
        result = await repo.get(pending_task["id"], organization_id=org_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_task_result_in_metadata(self, db_pool, fb_user, pending_task):
        """Task result is stored in metadata."""
        repo = MemoryRepository(db_pool)
        meta = dict(pending_task.get("metadata") or {})
        meta["result"] = "Found 3 patterns worth consolidating."
        result = await repo.update(
            memory_id=pending_task["id"],
            metadata=meta,
            organization_id=fb_user["organization_id"],
        )

        assert result["metadata"]["result"] == "Found 3 patterns worth consolidating."


# ============================================================================
# Daemon Message Tests
# ============================================================================


