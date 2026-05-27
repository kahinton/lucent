"""Reconsolidation-on-access tests for the memory lifecycle (M9 Phase 2).

When a memory in lifecycle_stage 'consolidating' or 'archived' is touched by
a read (which already updates last_accessed_at via AccessRepository) or by an
update via MemoryRepository.update, the stage must be promoted back to
'active' in the same UPDATE statement. 'forgotten' rows must NOT be
reactivated — they are queued for hard delete.

Reference: goal 82b41acd (M9 Phase 2).
"""

import pytest

from lucent.db import AccessRepository, MemoryRepository


async def _set_stage(db_pool, memory_id, stage: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET lifecycle_stage = $1 WHERE id = $2",
            stage,
            str(memory_id),
        )


async def _read_stage(db_pool, memory_id) -> str | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT lifecycle_stage FROM memories WHERE id = $1",
            str(memory_id),
        )
    return row["lifecycle_stage"] if row else None


class TestReconsolidationOnRead:
    """Reads via AccessRepository.log_access reactivate qualifying stages."""

    async def test_consolidating_memory_reactivates_on_get(
        self, db_pool, test_memory, test_user
    ):
        await _set_stage(db_pool, test_memory["id"], "consolidating")
        assert await _read_stage(db_pool, test_memory["id"]) == "consolidating"

        # Simulate the get_memory MCP tool path: fetch + log_access.
        mem_repo = MemoryRepository(db_pool)
        access_repo = AccessRepository(db_pool)

        result = await mem_repo.get_accessible(
            test_memory["id"],
            test_user["id"],
            test_user["organization_id"],
        )
        assert result is not None

        await access_repo.log_access(
            memory_id=test_memory["id"],
            access_type="view",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        assert await _read_stage(db_pool, test_memory["id"]) == "active"

    async def test_archived_memory_reactivates_on_get(
        self, db_pool, test_memory, test_user
    ):
        await _set_stage(db_pool, test_memory["id"], "archived")

        access_repo = AccessRepository(db_pool)
        await access_repo.log_access(
            memory_id=test_memory["id"],
            access_type="view",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        assert await _read_stage(db_pool, test_memory["id"]) == "active"

    async def test_forgotten_memory_does_not_reactivate(
        self, db_pool, test_memory, test_user
    ):
        await _set_stage(db_pool, test_memory["id"], "forgotten")

        access_repo = AccessRepository(db_pool)
        await access_repo.log_access(
            memory_id=test_memory["id"],
            access_type="view",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        # Forgotten rows are queued for hard delete and must stay forgotten.
        assert await _read_stage(db_pool, test_memory["id"]) == "forgotten"

    async def test_active_memory_stage_unchanged_on_get(
        self, db_pool, test_memory, test_user
    ):
        # Sanity check: baseline reactivation is a no-op for already-active rows.
        assert await _read_stage(db_pool, test_memory["id"]) == "active"

        access_repo = AccessRepository(db_pool)
        await access_repo.log_access(
            memory_id=test_memory["id"],
            access_type="view",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        assert await _read_stage(db_pool, test_memory["id"]) == "active"


class TestReconsolidationOnBatchRead:
    """Batched reads (get_memories) reactivate ONLY qualifying rows in a
    single UPDATE statement."""

    async def test_batch_reactivates_only_qualifying_rows(
        self, db_pool, test_user, clean_test_data
    ):
        prefix = clean_test_data
        mem_repo = MemoryRepository(db_pool)
        access_repo = AccessRepository(db_pool)

        # Create 4 memories: consolidating, archived, forgotten, active.
        stages = ["consolidating", "archived", "forgotten", "active"]
        memories = []
        for i, stage in enumerate(stages):
            m = await mem_repo.create(
                username=f"{prefix}user",
                type="experience",
                content=f"{prefix} batch reconsolidation #{i}",
                user_id=test_user["id"],
                organization_id=test_user["organization_id"],
            )
            await _set_stage(db_pool, m["id"], stage)
            memories.append(m)

        memory_ids = [m["id"] for m in memories]

        await access_repo.log_batch_access(
            memory_ids=memory_ids,
            access_type="view",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        # consolidating + archived → active. forgotten + active → unchanged.
        assert await _read_stage(db_pool, memories[0]["id"]) == "active"
        assert await _read_stage(db_pool, memories[1]["id"]) == "active"
        assert await _read_stage(db_pool, memories[2]["id"]) == "forgotten"
        assert await _read_stage(db_pool, memories[3]["id"]) == "active"


class TestReconsolidationOnUpdate:
    """update_memory reactivates as a side effect of any real field change."""

    async def test_update_archived_memory_reactivates(
        self, db_pool, test_memory
    ):
        await _set_stage(db_pool, test_memory["id"], "archived")

        mem_repo = MemoryRepository(db_pool)
        result = await mem_repo.update(
            memory_id=test_memory["id"],
            content="updated content for reactivation test",
        )
        assert result is not None
        assert result["lifecycle_stage"] == "active"
        assert await _read_stage(db_pool, test_memory["id"]) == "active"

    async def test_update_consolidating_memory_reactivates(
        self, db_pool, test_memory
    ):
        await _set_stage(db_pool, test_memory["id"], "consolidating")

        mem_repo = MemoryRepository(db_pool)
        result = await mem_repo.update(
            memory_id=test_memory["id"],
            tags=["touched"],
        )
        assert result is not None
        assert result["lifecycle_stage"] == "active"

    async def test_update_forgotten_memory_does_not_reactivate(
        self, db_pool, test_memory
    ):
        await _set_stage(db_pool, test_memory["id"], "forgotten")

        mem_repo = MemoryRepository(db_pool)
        result = await mem_repo.update(
            memory_id=test_memory["id"],
            content="touch a forgotten memory",
        )
        assert result is not None
        assert result["lifecycle_stage"] == "forgotten"

    async def test_update_noop_does_not_run_update(
        self, db_pool, test_memory
    ):
        # No fields supplied — the repo short-circuits to get(), so
        # no reactivation logic should fire and version stays the same.
        await _set_stage(db_pool, test_memory["id"], "archived")

        mem_repo = MemoryRepository(db_pool)
        before = await mem_repo.get(test_memory["id"])
        result = await mem_repo.update(memory_id=test_memory["id"])

        assert result is not None
        assert result["version"] == before["version"]
        # No-op update doesn't reactivate (intentional — stage transitions
        # require an explicit touch).
        assert await _read_stage(db_pool, test_memory["id"]) == "archived"


class TestReconsolidationGoalSyncCoexistence:
    """Goal-driven lifecycle sync still wins over the reactivation branch."""

    async def test_goal_sync_takes_precedence_over_reactivation(
        self, db_pool, test_user, clean_test_data
    ):
        prefix = clean_test_data
        mem_repo = MemoryRepository(db_pool)

        goal = await mem_repo.create(
            username=f"{prefix}user",
            type="goal",
            content=f"{prefix} goal lifecycle precedence",
            metadata={"status": "active"},
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        # Force the goal into 'consolidating' (a reactivation-eligible stage).
        await _set_stage(db_pool, goal["id"], "consolidating")

        # metadata.status='completed' maps goal rows to lifecycle_stage='archived'.
        # Goal-sync wins; reactivation must NOT silently flip it to 'active'.
        result = await mem_repo.update(
            memory_id=goal["id"],
            metadata={"status": "completed"},
        )
        assert result is not None
        assert await _read_stage(db_pool, goal["id"]) == "archived"
