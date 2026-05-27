"""Comprehensive tests for Phase 1 memory lifecycle shadow mode components."""

from pathlib import Path
from uuid import uuid4

from lucent.db import MemoryRepository


def _read_migration(name: str) -> str:
    migration_path = (
        Path(__file__).resolve().parents[1] / "src" / "lucent" / "db" / "migrations" / name
    )
    return migration_path.read_text()


async def _create_pre_phase1_memories_table(conn, schema_name: str) -> None:
    """Create a pre-Phase-1 shaped memories table in an isolated schema."""
    await conn.execute(f'CREATE SCHEMA "{schema_name}"')
    await conn.execute(f'SET search_path TO "{schema_name}", public')
    await conn.execute(
        """
        CREATE TABLE memories (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            username TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('experience', 'technical', 'procedural', 'goal', 'individual')),
            content TEXT NOT NULL,
            tags TEXT[] DEFAULT '{}',
            importance INTEGER DEFAULT 5 CHECK (importance >= 1 AND importance <= 10),
            related_memory_ids UUID[] DEFAULT '{}',
            metadata JSONB DEFAULT '{}',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL
        )
        """
    )


class TestPhase1SchemaMigrations:
    async def test_migrations_apply_and_add_expected_columns(self, db_pool):
        migration_055 = _read_migration("055_add_lifecycle_columns.sql")
        migration_056 = _read_migration("056_add_consolidation_metadata_index.sql")
        schema_name = f"test_lifecycle_schema_{str(uuid4()).replace('-', '')[:12]}"

        async with db_pool.acquire() as conn:
            try:
                await _create_pre_phase1_memories_table(conn, schema_name)
                await conn.execute(migration_055)
                await conn.execute(migration_056)

                cols = await conn.fetch(
                    """
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = $1 AND table_name = 'memories'
                    """,
                    schema_name,
                )
                col_map = {c["column_name"]: c for c in cols}

                assert "lifecycle_stage" in col_map
                assert "vitality_score" in col_map
                assert "vitality_computed_at" in col_map
                assert col_map["vitality_score"]["is_nullable"] == "YES"
                assert col_map["vitality_computed_at"]["data_type"] == "timestamp with time zone"
            finally:
                await conn.execute("RESET search_path")
                await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')

    async def test_existing_rows_default_to_active_and_scores_null(self, db_pool):
        migration_055 = _read_migration("055_add_lifecycle_columns.sql")
        schema_name = f"test_lifecycle_schema_{str(uuid4()).replace('-', '')[:12]}"

        async with db_pool.acquire() as conn:
            try:
                await _create_pre_phase1_memories_table(conn, schema_name)
                await conn.execute(
                    """
                    INSERT INTO memories (username, type, content, tags, importance)
                    VALUES ('legacy_user', 'experience', 'legacy row', ARRAY['legacy'], 5)
                    """
                )
                await conn.execute(migration_055)

                row = await conn.fetchrow(
                    """
                    SELECT lifecycle_stage, vitality_score, vitality_computed_at
                    FROM memories
                    WHERE username = 'legacy_user'
                    """
                )
                assert row["lifecycle_stage"] == "active"
                assert row["vitality_score"] is None
                assert row["vitality_computed_at"] is None
            finally:
                await conn.execute("RESET search_path")
                await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')


class TestPhase1VitalityComputation:
    async def test_scores_computed_and_persisted(self, db_pool, test_user, clean_test_data):
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        fresh = await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} freshness memory",
            tags=["phase1"],
            importance=9,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        stale = await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} stale memory",
            tags=["phase1"],
            importance=1,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE memories
                SET created_at = NOW() - INTERVAL '1000 days',
                    updated_at = NOW() - INTERVAL '1000 days',
                    last_accessed_at = NOW() - INTERVAL '1000 days'
                WHERE id = $1
                """,
                stale["id"],
            )

        result = await repo.compute_vitality_scores(batch_size=100)
        assert result["processed"] >= 2
        assert result["updated"] >= 2

        fresh_after = await repo.get(fresh["id"])
        stale_after = await repo.get(stale["id"])
        assert fresh_after is not None
        assert stale_after is not None
        assert fresh_after["vitality_score"] is not None
        assert stale_after["vitality_score"] is not None
        assert fresh_after["vitality_computed_at"] is not None
        assert stale_after["vitality_computed_at"] is not None
        assert fresh_after["vitality_score"] > stale_after["vitality_score"]

    async def test_handles_no_access_history_edge_case(self, db_pool, test_user, clean_test_data):
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)
        memory = await repo.create(
            username=f"{prefix}user",
            type="technical",
            content=f"{prefix} no access history memory",
            tags=["phase1"],
            importance=5,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories SET last_accessed_at = NULL WHERE id = $1",
                memory["id"],
            )

        await repo.compute_vitality_scores(batch_size=100)
        persisted = await repo.get(memory["id"])
        assert persisted is not None
        assert persisted["vitality_score"] is not None
        assert 0.0 <= persisted["vitality_score"] <= 1.0
        assert persisted["vitality_computed_at"] is not None

    async def test_hard_exemptions_remain_active(self, db_pool, test_user, clean_test_data):
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        individual = await repo.create(
            username=f"{prefix}user",
            type="individual",
            content=f"{prefix} individual profile memory",
            tags=["phase1"],
            importance=1,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        active_goal = await repo.create(
            username=f"{prefix}user",
            type="goal",
            content=f"{prefix} active goal memory",
            tags=["phase1"],
            importance=1,
            metadata={"status": "active"},
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE memories
                SET created_at = NOW() - INTERVAL '1400 days',
                    updated_at = NOW() - INTERVAL '1400 days',
                    last_accessed_at = NOW() - INTERVAL '1400 days'
                WHERE id = ANY($1::uuid[])
                """,
                [individual["id"], active_goal["id"]],
            )

        await repo.compute_vitality_scores(batch_size=100)
        individual_after = await repo.get(individual["id"])
        goal_after = await repo.get(active_goal["id"])
        assert individual_after is not None
        assert goal_after is not None
        assert individual_after["lifecycle_stage"] == "active"
        assert goal_after["lifecycle_stage"] == "active"
        assert individual_after["vitality_score"] is not None
        assert goal_after["vitality_score"] is not None

    async def test_compute_has_no_search_side_effects(self, db_pool, test_user, clean_test_data):
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        for idx in range(3):
            await repo.create(
                username=f"{prefix}user",
                type="experience",
                content=f"{prefix} shadow invariant search corpus item {idx}",
                tags=["phase1", "search"],
                importance=10 - idx,
                user_id=test_user["id"],
                organization_id=test_user["organization_id"],
            )

        before = await repo.search(
            query=f"{prefix} shadow invariant search corpus",
            limit=10,
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )
        before_ids = [item["id"] for item in before["memories"]]

        await repo.compute_vitality_scores(batch_size=100)

        after = await repo.search(
            query=f"{prefix} shadow invariant search corpus",
            limit=10,
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )
        after_ids = [item["id"] for item in after["memories"]]

        assert after_ids == before_ids
