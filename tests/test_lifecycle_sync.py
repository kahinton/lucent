"""Tests for lifecycle_stage auto-sync with metadata.status on goal memories.

Validates that MemoryRepository.create() and .update() automatically keep
lifecycle_stage in sync with metadata.status for goal-type memories, while
leaving non-goal memories untouched.

Mapping (goal memories only):
    active, paused       → lifecycle_stage = 'active'
    completed, done      → lifecycle_stage = 'archived'
    abandoned, cancelled → lifecycle_stage = 'archived'

See migration 063 for the one-time backfill and MemoryRepository for the
service-layer sync logic.
"""

from pathlib import Path
from uuid import uuid4

import pytest

from lucent.db import MemoryRepository


def _read_migration(name: str) -> str:
    """Read a migration file from the migrations directory."""
    migration_path = (
        Path(__file__).resolve().parents[1] / "src" / "lucent" / "db" / "migrations" / name
    )
    return migration_path.read_text()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _create_goal(repo, prefix, test_user, *, status="active", **kwargs):
    """Create a goal memory with given metadata.status."""
    return await repo.create(
        username=f"{prefix}user",
        type="goal",
        content=f"{prefix} Test goal with status={status}",
        tags=["test", "lifecycle-sync"],
        importance=6,
        metadata={"status": status, **(kwargs.get("extra_metadata") or {})},
        user_id=test_user["id"],
        organization_id=test_user["organization_id"],
    )


async def _create_experience(repo, prefix, test_user, *, status="active"):
    """Create an experience memory with a metadata.status field."""
    return await repo.create(
        username=f"{prefix}user",
        type="experience",
        content=f"{prefix} Test experience with status={status}",
        tags=["test", "lifecycle-sync"],
        importance=5,
        metadata={"status": status},
        user_id=test_user["id"],
        organization_id=test_user["organization_id"],
    )


# ---------------------------------------------------------------------------
# Update-path tests: verify lifecycle_stage transitions on metadata changes
# ---------------------------------------------------------------------------

class TestGoalLifecycleSyncOnUpdate:
    """Test lifecycle_stage auto-sync when updating goal metadata.status."""

    @pytest.mark.asyncio
    async def test_completed_archives_goal(self, db_pool, test_user, clean_test_data):
        """Goal completed → lifecycle_stage archived."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="active")
        assert goal["lifecycle_stage"] == "active"

        updated = await repo.update(goal["id"], metadata={"status": "completed"})

        assert updated is not None
        assert updated["lifecycle_stage"] == "archived"
        assert updated["metadata"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_abandoned_archives_goal(self, db_pool, test_user, clean_test_data):
        """Goal abandoned → lifecycle_stage archived."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="active")
        assert goal["lifecycle_stage"] == "active"

        updated = await repo.update(goal["id"], metadata={"status": "abandoned"})

        assert updated is not None
        assert updated["lifecycle_stage"] == "archived"
        assert updated["metadata"]["status"] == "abandoned"

    @pytest.mark.asyncio
    async def test_cancelled_archives_goal(self, db_pool, test_user, clean_test_data):
        """Goal cancelled → lifecycle_stage archived."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="active")
        assert goal["lifecycle_stage"] == "active"

        updated = await repo.update(goal["id"], metadata={"status": "cancelled"})

        assert updated is not None
        assert updated["lifecycle_stage"] == "archived"
        assert updated["metadata"]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_done_archives_goal(self, db_pool, test_user, clean_test_data):
        """Goal done (synonym for completed) → lifecycle_stage archived."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="active")
        updated = await repo.update(goal["id"], metadata={"status": "done"})

        assert updated is not None
        assert updated["lifecycle_stage"] == "archived"

    @pytest.mark.asyncio
    async def test_paused_keeps_active(self, db_pool, test_user, clean_test_data):
        """Goal paused → lifecycle_stage stays active.

        Paused goals are filtered by the planner via metadata.status,
        not lifecycle_stage, so they must remain 'active' in lifecycle.
        """
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="active")
        assert goal["lifecycle_stage"] == "active"

        updated = await repo.update(goal["id"], metadata={"status": "paused"})

        assert updated is not None
        assert updated["lifecycle_stage"] == "active"
        assert updated["metadata"]["status"] == "paused"

    @pytest.mark.asyncio
    async def test_reactivated_goal_becomes_active(self, db_pool, test_user, clean_test_data):
        """Goal reactivated (completed → active) → lifecycle_stage back to active."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        # Create and archive the goal
        goal = await _create_goal(repo, prefix, test_user, status="active")
        archived = await repo.update(goal["id"], metadata={"status": "completed"})
        assert archived["lifecycle_stage"] == "archived"

        # Reactivate it
        reactivated = await repo.update(goal["id"], metadata={"status": "active"})

        assert reactivated is not None
        assert reactivated["lifecycle_stage"] == "active"
        assert reactivated["metadata"]["status"] == "active"

    @pytest.mark.asyncio
    async def test_no_metadata_status_no_lifecycle_change(
        self, db_pool, test_user, clean_test_data
    ):
        """Updating a goal's content without metadata.status leaves lifecycle unchanged."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="active")
        assert goal["lifecycle_stage"] == "active"

        # Update content only — no metadata
        updated = await repo.update(goal["id"], content="Updated goal description")

        assert updated is not None
        assert updated["lifecycle_stage"] == "active"  # unchanged

    @pytest.mark.asyncio
    async def test_metadata_without_status_key_no_lifecycle_change(
        self, db_pool, test_user, clean_test_data
    ):
        """Updating metadata without a 'status' key doesn't change lifecycle."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="active")

        # Update metadata with unrelated fields — no 'status' key
        updated = await repo.update(
            goal["id"],
            metadata={"priority": "high", "notes": "important goal"},
        )

        assert updated is not None
        assert updated["lifecycle_stage"] == "active"  # unchanged

    @pytest.mark.asyncio
    async def test_unknown_status_no_lifecycle_change(
        self, db_pool, test_user, clean_test_data
    ):
        """Unknown metadata.status value doesn't change lifecycle_stage."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="active")

        updated = await repo.update(
            goal["id"],
            metadata={"status": "some-unknown-status"},
        )

        assert updated is not None
        assert updated["lifecycle_stage"] == "active"  # unchanged — unknown status


# ---------------------------------------------------------------------------
# Non-goal isolation: verify experience/technical/etc. are never affected
# ---------------------------------------------------------------------------

class TestNonGoalLifecycleIsolation:
    """Verify lifecycle sync only applies to goal-type memories."""

    @pytest.mark.asyncio
    async def test_experience_not_affected_on_update(
        self, db_pool, test_user, clean_test_data
    ):
        """Updating an experience memory's metadata.status to 'completed'
        should NOT change lifecycle_stage."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        exp = await _create_experience(repo, prefix, test_user, status="active")
        original_stage = exp["lifecycle_stage"]

        updated = await repo.update(exp["id"], metadata={"status": "completed"})

        assert updated is not None
        assert updated["lifecycle_stage"] == original_stage  # unchanged
        assert updated["metadata"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_technical_not_affected_on_update(
        self, db_pool, test_user, clean_test_data
    ):
        """Technical memory with metadata.status='completed' stays unchanged."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        tech = await repo.create(
            username=f"{prefix}user",
            type="technical",
            content=f"{prefix} Technical doc",
            tags=["test"],
            importance=5,
            metadata={"status": "active", "language": "python"},
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        original_stage = tech["lifecycle_stage"]

        updated = await repo.update(tech["id"], metadata={"status": "completed"})

        assert updated is not None
        assert updated["lifecycle_stage"] == original_stage

    @pytest.mark.asyncio
    async def test_experience_not_affected_on_create(
        self, db_pool, test_user, clean_test_data
    ):
        """Creating an experience with metadata.status='completed' should NOT
        set lifecycle_stage to 'archived' — sync only applies to goals."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        exp = await _create_experience(repo, prefix, test_user, status="completed")

        assert exp["lifecycle_stage"] == "active"  # DB default, not 'archived'


# ---------------------------------------------------------------------------
# Create-path tests: verify initial lifecycle_stage on goal creation
# ---------------------------------------------------------------------------

class TestGoalLifecycleSyncOnCreate:
    """Test lifecycle_stage is set correctly when creating goal memories."""

    @pytest.mark.asyncio
    async def test_create_active_goal(self, db_pool, test_user, clean_test_data):
        """Creating a goal with status=active → lifecycle_stage active (default)."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="active")

        assert goal["lifecycle_stage"] == "active"

    @pytest.mark.asyncio
    async def test_create_completed_goal(self, db_pool, test_user, clean_test_data):
        """Creating a goal with status=completed → lifecycle_stage archived.

        This handles imported or backfilled goals that arrive already completed.
        """
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="completed")

        assert goal["lifecycle_stage"] == "archived"

    @pytest.mark.asyncio
    async def test_create_abandoned_goal(self, db_pool, test_user, clean_test_data):
        """Creating a goal with status=abandoned → lifecycle_stage archived."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="abandoned")

        assert goal["lifecycle_stage"] == "archived"

    @pytest.mark.asyncio
    async def test_create_cancelled_goal(self, db_pool, test_user, clean_test_data):
        """Creating a goal with status=cancelled → lifecycle_stage archived."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="cancelled")

        assert goal["lifecycle_stage"] == "archived"

    @pytest.mark.asyncio
    async def test_create_paused_goal(self, db_pool, test_user, clean_test_data):
        """Creating a goal with status=paused → lifecycle_stage active."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="paused")

        assert goal["lifecycle_stage"] == "active"

    @pytest.mark.asyncio
    async def test_create_goal_no_metadata(self, db_pool, test_user, clean_test_data):
        """Creating a goal without metadata → lifecycle_stage defaults to active."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await repo.create(
            username=f"{prefix}user",
            type="goal",
            content=f"{prefix} Goal without metadata",
            tags=["test"],
            importance=5,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        assert goal["lifecycle_stage"] == "active"


# ---------------------------------------------------------------------------
# Case insensitivity and whitespace handling
# ---------------------------------------------------------------------------

class TestStatusNormalization:
    """Test that status matching is case-insensitive and trims whitespace."""

    @pytest.mark.asyncio
    async def test_uppercase_completed(self, db_pool, test_user, clean_test_data):
        """COMPLETED (uppercase) → lifecycle_stage archived."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="active")
        updated = await repo.update(goal["id"], metadata={"status": "COMPLETED"})

        assert updated["lifecycle_stage"] == "archived"

    @pytest.mark.asyncio
    async def test_mixed_case_abandoned(self, db_pool, test_user, clean_test_data):
        """Abandoned (mixed case) → lifecycle_stage archived."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="active")
        updated = await repo.update(goal["id"], metadata={"status": "Abandoned"})

        assert updated["lifecycle_stage"] == "archived"

    @pytest.mark.asyncio
    async def test_whitespace_trimmed(self, db_pool, test_user, clean_test_data):
        """Status with leading/trailing whitespace is trimmed before matching."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        goal = await _create_goal(repo, prefix, test_user, status="active")
        updated = await repo.update(goal["id"], metadata={"status": "  completed  "})

        assert updated["lifecycle_stage"] == "archived"


# ---------------------------------------------------------------------------
# Full lifecycle round-trip
# ---------------------------------------------------------------------------

class TestFullLifecycleRoundTrip:
    """Test a goal through its full lifecycle: active → completed → reactivated → cancelled."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, db_pool, test_user, clean_test_data):
        """Walk a goal through every transition and verify lifecycle_stage at each step."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        # 1. Create active goal
        goal = await _create_goal(repo, prefix, test_user, status="active")
        assert goal["lifecycle_stage"] == "active"

        # 2. Pause it — should stay active
        paused = await repo.update(goal["id"], metadata={"status": "paused"})
        assert paused["lifecycle_stage"] == "active"

        # 3. Complete it — should archive
        completed = await repo.update(goal["id"], metadata={"status": "completed"})
        assert completed["lifecycle_stage"] == "archived"

        # 4. Reactivate it — should go back to active
        reactivated = await repo.update(goal["id"], metadata={"status": "active"})
        assert reactivated["lifecycle_stage"] == "active"

        # 5. Cancel it — should archive again
        cancelled = await repo.update(goal["id"], metadata={"status": "cancelled"})
        assert cancelled["lifecycle_stage"] == "archived"

        # 6. Reactivate again — should work
        reactivated2 = await repo.update(goal["id"], metadata={"status": "active"})
        assert reactivated2["lifecycle_stage"] == "active"

        # 7. Abandon it — should archive
        abandoned = await repo.update(goal["id"], metadata={"status": "abandoned"})
        assert abandoned["lifecycle_stage"] == "archived"


# ---------------------------------------------------------------------------
# Backfill migration 063
# ---------------------------------------------------------------------------

class TestBackfillMigration063:
    """Verify the backfill migration SQL correctness against test data."""

    @pytest.mark.asyncio
    async def test_backfill_archives_stale_active_goals(self, db_pool, test_user, clean_test_data):
        """Migration 063 should archive goals where metadata.status indicates
        completion but lifecycle_stage was stuck at 'active'."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)
        migration_sql = _read_migration("063_backfill_goal_lifecycle_stage.sql")

        # Create goals that simulate pre-sync drift:
        # These have terminal statuses but lifecycle_stage will be forced to
        # 'active' via direct SQL (bypassing the sync logic).
        goal_completed = await _create_goal(repo, prefix, test_user, status="completed")
        goal_abandoned = await _create_goal(repo, prefix, test_user, status="abandoned")
        goal_cancelled = await _create_goal(repo, prefix, test_user, status="cancelled")
        goal_done = await _create_goal(repo, prefix, test_user, status="done")

        # Also create goals that should NOT be changed by the migration
        goal_active = await _create_goal(repo, prefix, test_user, status="active")
        goal_paused = await _create_goal(repo, prefix, test_user, status="paused")

        # Force lifecycle_stage back to 'active' to simulate the pre-sync state
        async with db_pool.acquire() as conn:
            for goal in [goal_completed, goal_abandoned, goal_cancelled, goal_done]:
                await conn.execute(
                    "UPDATE memories SET lifecycle_stage = 'active' WHERE id = $1",
                    str(goal["id"]),
                )

        # Run the backfill migration
        async with db_pool.acquire() as conn:
            await conn.execute(migration_sql)

        # Verify: terminal goals should now be archived
        for goal_id, expected_status in [
            (goal_completed["id"], "archived"),
            (goal_abandoned["id"], "archived"),
            (goal_cancelled["id"], "archived"),
            (goal_done["id"], "archived"),
        ]:
            row = await repo.get(goal_id)
            assert row["lifecycle_stage"] == expected_status, (
                f"Expected lifecycle_stage={expected_status} for goal {goal_id} "
                f"(status={row['metadata']['status']}), got {row['lifecycle_stage']}"
            )

        # Verify: active and paused goals are still active
        for goal_id in [goal_active["id"], goal_paused["id"]]:
            row = await repo.get(goal_id)
            assert row["lifecycle_stage"] == "active", (
                f"Expected lifecycle_stage=active for goal {goal_id} "
                f"(status={row['metadata']['status']}), got {row['lifecycle_stage']}"
            )

    @pytest.mark.asyncio
    async def test_backfill_is_idempotent(self, db_pool, test_user, clean_test_data):
        """Running the backfill migration twice should not error or change results."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)
        migration_sql = _read_migration("063_backfill_goal_lifecycle_stage.sql")

        goal = await _create_goal(repo, prefix, test_user, status="completed")

        # Force it back to active to simulate drift
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories SET lifecycle_stage = 'active' WHERE id = $1",
                str(goal["id"]),
            )

        # Run migration twice
        async with db_pool.acquire() as conn:
            await conn.execute(migration_sql)
            await conn.execute(migration_sql)  # second run should be a no-op

        row = await repo.get(goal["id"])
        assert row["lifecycle_stage"] == "archived"

    @pytest.mark.asyncio
    async def test_backfill_ignores_non_goal_types(self, db_pool, test_user, clean_test_data):
        """The backfill should only affect goal-type memories."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)
        migration_sql = _read_migration("063_backfill_goal_lifecycle_stage.sql")

        exp = await _create_experience(repo, prefix, test_user, status="completed")

        # Run the migration
        async with db_pool.acquire() as conn:
            await conn.execute(migration_sql)

        row = await repo.get(exp["id"])
        assert row["lifecycle_stage"] == "active"  # experience should not be archived

    @pytest.mark.asyncio
    async def test_backfill_ignores_deleted_memories(self, db_pool, test_user, clean_test_data):
        """Soft-deleted goals should not be affected by the backfill."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)
        migration_sql = _read_migration("063_backfill_goal_lifecycle_stage.sql")

        goal = await _create_goal(repo, prefix, test_user, status="completed")

        # Force to active and soft-delete
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories SET lifecycle_stage = 'active', deleted_at = NOW() WHERE id = $1",
                str(goal["id"]),
            )

        # Run the migration
        async with db_pool.acquire() as conn:
            await conn.execute(migration_sql)

        # Fetch directly (repo.get filters out deleted rows)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT lifecycle_stage FROM memories WHERE id = $1",
                str(goal["id"]),
            )
        assert row["lifecycle_stage"] == "active"  # deleted → not touched


# ---------------------------------------------------------------------------
# _resolve_goal_lifecycle_stage unit tests
# ---------------------------------------------------------------------------

class TestResolveGoalLifecycleStage:
    """Unit tests for the mapping helper (no DB required)."""

    def test_goal_active(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": "active"}) == "active"

    def test_goal_paused(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": "paused"}) == "active"

    def test_goal_completed(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": "completed"}) == "archived"

    def test_goal_done(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": "done"}) == "archived"

    def test_goal_abandoned(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": "abandoned"}) == "archived"

    def test_goal_cancelled(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": "cancelled"}) == "archived"

    def test_non_goal_type_returns_none(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("experience", {"status": "completed"}) is None

    def test_no_metadata_returns_none(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", None) is None

    def test_empty_metadata_returns_none(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {}) is None

    def test_no_status_key_returns_none(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"priority": "high"}) is None

    def test_unknown_status_returns_none(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": "custom-status"}) is None

    def test_case_insensitive(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": "COMPLETED"}) == "archived"
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": "Active"}) == "active"

    def test_whitespace_trimmed(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": "  completed  "}) == "archived"

    def test_non_string_status_returns_none(self):
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": 123}) is None
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": True}) is None
        assert MemoryRepository._resolve_goal_lifecycle_stage("goal", {"status": None}) is None
