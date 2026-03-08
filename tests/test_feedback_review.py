"""Tests for M3 Feedback & Review System.

Covers:
1. Feedback storage and retrieval (approve/reject/comment/reset)
2. Daemon task lifecycle (create → claim → complete/cancel)
3. Daemon message lifecycle (send → list → acknowledge)
"""

import pytest
import pytest_asyncio
from uuid import uuid4

from lucent.db import MemoryRepository, AuditRepository


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


class TestFeedbackStorage:
    """Tests for feedback metadata on daemon memories."""

    @pytest.mark.asyncio
    async def test_approve_feedback(self, db_pool, daemon_memory):
        """Approve feedback stores correct metadata."""
        repo = MemoryRepository(db_pool)
        feedback = {
            "status": "approved",
            "reviewed_at": "2026-03-07 23:00 UTC",
            "reviewed_by": "Kyle",
        }
        existing_metadata = daemon_memory.get("metadata") or {}
        updated_metadata = {**existing_metadata, "feedback": feedback}
        result = await repo.update(memory_id=daemon_memory["id"], metadata=updated_metadata)

        assert result["metadata"]["feedback"]["status"] == "approved"
        assert result["metadata"]["feedback"]["reviewed_by"] == "Kyle"

    @pytest.mark.asyncio
    async def test_reject_feedback_with_comment(self, db_pool, daemon_memory):
        """Reject feedback with comment stores both."""
        repo = MemoryRepository(db_pool)
        feedback = {
            "status": "rejected",
            "reviewed_at": "2026-03-07 23:00 UTC",
            "reviewed_by": "Kyle",
            "comment": "Wrong approach, use async instead",
        }
        existing_metadata = daemon_memory.get("metadata") or {}
        updated_metadata = {**existing_metadata, "feedback": feedback}
        result = await repo.update(memory_id=daemon_memory["id"], metadata=updated_metadata)

        assert result["metadata"]["feedback"]["status"] == "rejected"
        assert result["metadata"]["feedback"]["comment"] == "Wrong approach, use async instead"

    @pytest.mark.asyncio
    async def test_comment_without_verdict(self, db_pool, daemon_memory):
        """Comment-only feedback preserves pending status."""
        repo = MemoryRepository(db_pool)
        feedback = {
            "status": "pending",
            "comment": "Looks interesting, still thinking about it",
            "reviewed_at": "2026-03-07 23:00 UTC",
            "reviewed_by": "Kyle",
        }
        existing_metadata = daemon_memory.get("metadata") or {}
        updated_metadata = {**existing_metadata, "feedback": feedback}
        result = await repo.update(memory_id=daemon_memory["id"], metadata=updated_metadata)

        assert result["metadata"]["feedback"]["status"] == "pending"
        assert "still thinking" in result["metadata"]["feedback"]["comment"]

    @pytest.mark.asyncio
    async def test_reset_feedback(self, db_pool, daemon_memory):
        """Reset clears feedback back to pending."""
        repo = MemoryRepository(db_pool)
        # First approve
        meta1 = {**(daemon_memory.get("metadata") or {}), "feedback": {"status": "approved"}}
        await repo.update(memory_id=daemon_memory["id"], metadata=meta1)

        # Then reset
        meta2 = {**(daemon_memory.get("metadata") or {}), "feedback": {"status": "pending"}}
        result = await repo.update(memory_id=daemon_memory["id"], metadata=meta2)

        assert result["metadata"]["feedback"]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_feedback_audit_log(self, db_pool, daemon_memory, fb_user):
        """Feedback action creates an audit log entry."""
        repo = MemoryRepository(db_pool)
        audit_repo = AuditRepository(db_pool)

        feedback = {"status": "approved", "reviewed_at": "2026-03-07 23:00 UTC"}
        meta = {**(daemon_memory.get("metadata") or {}), "feedback": feedback}
        await repo.update(memory_id=daemon_memory["id"], metadata=meta)

        await audit_repo.log(
            memory_id=daemon_memory["id"],
            action_type="update",
            user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
            changed_fields=["metadata.feedback"],
            old_values={"feedback": {}},
            new_values={"feedback": feedback},
            notes="feedback:approve",
        )

        # Verify audit entry exists
        entries = await audit_repo.get_by_memory_id(daemon_memory["id"])
        feedback_entries = [
            e for e in entries["entries"]
            if e["action_type"] == "update" and e.get("notes") == "feedback:approve"
        ]
        assert len(feedback_entries) >= 1
        assert feedback_entries[0]["changed_fields"] == ["metadata.feedback"]


class TestReviewQueue:
    """Tests for finding memories in the review queue."""

    @pytest.mark.asyncio
    async def test_search_needs_review(self, db_pool, fb_user, fb_prefix):
        """Search with needs-review tag finds review items."""
        repo = MemoryRepository(db_pool)

        # Create review and non-review memories
        await repo.create(
            username=f"{fb_prefix}user", type="experience",
            content="Review me", tags=["daemon", "needs-review"],
            importance=5, user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )
        await repo.create(
            username=f"{fb_prefix}user", type="experience",
            content="Routine daemon work", tags=["daemon"],
            importance=5, user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )

        result = await repo.search(
            tags=["daemon", "needs-review"],
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
                username=f"{fb_prefix}user", type="experience",
                content=f"Review item {i}", tags=["daemon", "needs-review"],
                importance=5, user_id=fb_user["id"],
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
    async def test_claim_and_complete_task(self, db_pool, pending_task):
        """Full lifecycle: claim → complete."""
        repo = MemoryRepository(db_pool)

        # Claim
        claimed = await repo.claim_task(pending_task["id"], "test-instance")
        assert claimed is not None
        assert "pending" not in claimed["tags"]
        assert "claimed-by-test-instance" in claimed["tags"]

        # Complete: replace claim tag with completed
        new_tags = [t for t in claimed["tags"] if not t.startswith("claimed-by-")]
        new_tags.append("completed")
        result = await repo.update(memory_id=pending_task["id"], tags=new_tags)

        assert "completed" in result["tags"]
        assert "pending" not in result["tags"]
        assert not any(t.startswith("claimed-by-") for t in result["tags"])

    @pytest.mark.asyncio
    async def test_cancel_pending_task(self, db_pool, pending_task):
        """Pending task can be soft-deleted (cancelled)."""
        repo = MemoryRepository(db_pool)
        await repo.delete(pending_task["id"])

        # Should not be findable
        result = await repo.get(pending_task["id"])
        assert result is None

    @pytest.mark.asyncio
    async def test_task_result_in_metadata(self, db_pool, pending_task):
        """Task result is stored in metadata."""
        repo = MemoryRepository(db_pool)
        meta = dict(pending_task.get("metadata") or {})
        meta["result"] = "Found 3 patterns worth consolidating."
        result = await repo.update(memory_id=pending_task["id"], metadata=meta)

        assert result["metadata"]["result"] == "Found 3 patterns worth consolidating."


# ============================================================================
# Daemon Message Tests
# ============================================================================


class TestDaemonMessages:
    """Tests for human-daemon messaging."""

    @pytest.mark.asyncio
    async def test_send_human_message(self, db_pool, fb_user, fb_prefix):
        """Human message is stored with correct tags."""
        repo = MemoryRepository(db_pool)
        msg = await repo.create(
            username=f"{fb_prefix}user",
            type="experience",
            content="Hey Lucent, focus on the auth module today.",
            tags=["daemon-message", "daemon", "from-human", "pending"],
            importance=5,
            metadata={"source": "web-ui"},
            user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )

        assert "from-human" in msg["tags"]
        assert "pending" in msg["tags"]
        assert "daemon-message" in msg["tags"]

    @pytest.mark.asyncio
    async def test_send_daemon_message(self, db_pool, fb_user, fb_prefix):
        """Daemon message is stored with from-daemon tag."""
        repo = MemoryRepository(db_pool)
        msg = await repo.create(
            username=f"{fb_prefix}user",
            type="experience",
            content="Understood, focusing on auth module.",
            tags=["daemon-message", "daemon", "from-daemon"],
            importance=5,
            metadata={"source": "daemon-api"},
            user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )

        assert "from-daemon" in msg["tags"]
        assert "pending" not in msg["tags"]

    @pytest.mark.asyncio
    async def test_list_messages(self, db_pool, fb_user, fb_prefix):
        """Search for daemon-message tagged memories returns messages."""
        repo = MemoryRepository(db_pool)

        await repo.create(
            username=f"{fb_prefix}user", type="experience",
            content="Human message", tags=["daemon-message", "daemon", "from-human", "pending"],
            importance=5, user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )
        await repo.create(
            username=f"{fb_prefix}user", type="experience",
            content="Daemon reply", tags=["daemon-message", "daemon", "from-daemon"],
            importance=5, user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )

        result = await repo.search(
            tags=["daemon-message"],
            requesting_user_id=fb_user["id"],
            requesting_org_id=fb_user["organization_id"],
        )

        contents = [m["content"] for m in result["memories"]]
        assert "Human message" in contents
        assert "Daemon reply" in contents

    @pytest.mark.asyncio
    async def test_list_pending_messages_only(self, db_pool, fb_user, fb_prefix):
        """Filter to pending human messages only."""
        repo = MemoryRepository(db_pool)

        await repo.create(
            username=f"{fb_prefix}user", type="experience",
            content="Pending msg", tags=["daemon-message", "daemon", "from-human", "pending"],
            importance=5, user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )
        await repo.create(
            username=f"{fb_prefix}user", type="experience",
            content="Daemon reply", tags=["daemon-message", "daemon", "from-daemon"],
            importance=5, user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )

        result = await repo.search(
            tags=["daemon-message", "from-human", "pending"],
            requesting_user_id=fb_user["id"],
            requesting_org_id=fb_user["organization_id"],
        )

        contents = [m["content"] for m in result["memories"]]
        assert "Pending msg" in contents
        assert "Daemon reply" not in contents

    @pytest.mark.asyncio
    async def test_acknowledge_message(self, db_pool, fb_user, fb_prefix):
        """Acknowledging a message removes pending tag and adds acknowledged."""
        repo = MemoryRepository(db_pool)

        msg = await repo.create(
            username=f"{fb_prefix}user", type="experience",
            content="Please review auth changes",
            tags=["daemon-message", "daemon", "from-human", "pending"],
            importance=5, user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )

        # Acknowledge
        tags = list(msg["tags"])
        tags.remove("pending")
        tags.append("acknowledged")
        metadata = dict(msg.get("metadata") or {})
        metadata["acknowledged_at"] = "2026-03-07 23:15 UTC"

        result = await repo.update(memory_id=msg["id"], tags=tags, metadata=metadata)

        assert "acknowledged" in result["tags"]
        assert "pending" not in result["tags"]
        assert result["metadata"]["acknowledged_at"] == "2026-03-07 23:15 UTC"

    @pytest.mark.asyncio
    async def test_message_sender_detection(self, db_pool, fb_user, fb_prefix):
        """Sender can be determined from tags."""
        repo = MemoryRepository(db_pool)

        human_msg = await repo.create(
            username=f"{fb_prefix}user", type="experience",
            content="From human", tags=["daemon-message", "from-human"],
            importance=5, user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )
        daemon_msg = await repo.create(
            username=f"{fb_prefix}user", type="experience",
            content="From daemon", tags=["daemon-message", "from-daemon"],
            importance=5, user_id=fb_user["id"],
            organization_id=fb_user["organization_id"],
        )

        human_tags = human_msg["tags"]
        daemon_tags = daemon_msg["tags"]

        assert "from-human" in human_tags and "from-daemon" not in human_tags
        assert "from-daemon" in daemon_tags and "from-human" not in daemon_tags
