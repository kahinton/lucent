"""Tests for migration 065 and memory_shadow_scores repository helpers."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from lucent.db import MemoryRepository


def _read_migration(name: str) -> str:
    migration_path = (
        Path(__file__).resolve().parents[1] / "src" / "lucent" / "db" / "migrations" / name
    )
    return migration_path.read_text()


async def _create_pre_065_tables(conn, schema_name: str) -> None:
    await conn.execute(f'CREATE SCHEMA "{schema_name}"')
    await conn.execute(f'SET search_path TO "{schema_name}", public')
    await conn.execute(
        """
        CREATE TABLE memories (
            id UUID PRIMARY KEY,
            username TEXT NOT NULL,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            tags TEXT[] DEFAULT '{}',
            importance INTEGER DEFAULT 5,
            related_memory_ids UUID[] DEFAULT '{}',
            metadata JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            deleted_at TIMESTAMPTZ
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE memory_access_log (
            id UUID PRIMARY KEY,
            memory_id UUID NOT NULL,
            user_id UUID,
            organization_id UUID,
            access_type TEXT NOT NULL,
            accessed_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
            context JSONB DEFAULT '{}'::jsonb
        )
        """
    )


class TestMigration065MemoryShadowScores:
    async def test_upgrade_creates_sidecar_schema(self, db_pool):
        migration_065 = _read_migration("065_memory_shadow_scores.sql")
        schema_name = f"test_shadow_schema_{str(uuid4()).replace('-', '')[:12]}"

        async with db_pool.acquire() as conn:
            try:
                await _create_pre_065_tables(conn, schema_name)
                await conn.execute(migration_065)

                cols = await conn.fetch(
                    """
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = $1 AND table_name = 'memory_shadow_scores'
                    """,
                    schema_name,
                )
                col_map = {c["column_name"]: c for c in cols}

                assert set(col_map.keys()) == {
                    "memory_id",
                    "strategy",
                    "score",
                    "shadow_action",
                    "signals",
                    "computed_at",
                    "divergence_tag",
                }
                assert col_map["score"]["is_nullable"] == "YES"
                assert col_map["signals"]["data_type"] == "jsonb"
                assert col_map["computed_at"]["data_type"] == "timestamp with time zone"

                pk_cols = await conn.fetch(
                    """
                    SELECT a.attname AS column_name
                    FROM pg_index i
                    JOIN pg_class t ON t.oid = i.indrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
                    WHERE n.nspname = $1
                      AND t.relname = 'memory_shadow_scores'
                      AND i.indisprimary = true
                    ORDER BY array_position(i.indkey, a.attnum)
                    """,
                    schema_name,
                )
                assert [r["column_name"] for r in pk_cols] == [
                    "memory_id",
                    "strategy",
                    "computed_at",
                ]

                indexes = await conn.fetch(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = $1
                      AND tablename IN ('memory_shadow_scores', 'memories', 'memory_access_log')
                    """,
                    schema_name,
                )
                index_names = {r["indexname"] for r in indexes}
                assert "ix_msv_strategy_computed" in index_names
                assert "ix_msv_divergence" in index_names
                assert "idx_memories_related_memory_ids_gin" in index_names
                assert "idx_access_memory_user" in index_names
            finally:
                await conn.execute("RESET search_path")
                await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')

    async def test_downgrade_drops_sidecar_table(self, db_pool):
        migration_065 = _read_migration("065_memory_shadow_scores.sql")
        migration_065_down = _read_migration("065_memory_shadow_scores.down.sql")
        schema_name = f"test_shadow_schema_{str(uuid4()).replace('-', '')[:12]}"

        async with db_pool.acquire() as conn:
            try:
                await _create_pre_065_tables(conn, schema_name)
                await conn.execute(migration_065)
                await conn.execute(migration_065_down)

                row = await conn.fetchrow(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = $1
                          AND table_name = 'memory_shadow_scores'
                    ) AS exists
                    """,
                    schema_name,
                )
                assert row is not None
                assert row["exists"] is False
            finally:
                await conn.execute("RESET search_path")
                await conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')


class TestMemoryShadowScoreHelpers:
    async def test_insert_helper_is_gated_off_by_default(
        self, db_pool, test_user, clean_test_data, monkeypatch
    ):
        monkeypatch.delenv("LUCENT_SHADOW_FORGET_ENABLED", raising=False)
        repo = MemoryRepository(db_pool)
        prefix = clean_test_data
        memory = await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} shadow insert disabled",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        inserted = await repo.insert_shadow_score(
            memory_id=memory["id"],
            strategy="gcp-v1",
            score=0.42,
            shadow_action="keep",
            signals={"in_degree": 1},
            divergence_tag="agree",
        )
        assert inserted is None

        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM memory_shadow_scores WHERE memory_id = $1",
                memory["id"],
            )
        assert count == 0

    async def test_insert_and_upsert_shadow_score(
        self,
        db_pool,
        test_user,
        clean_test_data,
        monkeypatch,
    ):
        monkeypatch.setenv("LUCENT_SHADOW_FORGET_ENABLED", "true")
        repo = MemoryRepository(db_pool)
        prefix = clean_test_data
        memory = await repo.create(
            username=f"{prefix}user",
            type="technical",
            content=f"{prefix} shadow insert enabled",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        computed_at = datetime.now(UTC)

        inserted = await repo.insert_shadow_score(
            memory_id=memory["id"],
            strategy="gcp-v1",
            score=0.10,
            shadow_action="forgetting_candidate",
            signals={"in_degree": 0, "out_degree": 0},
            computed_at=computed_at,
            divergence_tag="gcp-forgets-vitality-keeps",
        )
        assert inserted is not None
        assert inserted["strategy"] == "gcp-v1"
        assert inserted["score"] == pytest.approx(0.10, rel=1e-6)
        assert inserted["signals"]["in_degree"] == 0

        upserted = await repo.upsert_shadow_score(
            memory_id=memory["id"],
            strategy="gcp-v1",
            score=0.90,
            shadow_action="protected_hub",
            signals={"in_degree": 9, "out_degree": 3},
            computed_at=computed_at,
            divergence_tag="gcp-protects-vitality-archives",
        )
        assert upserted is not None
        assert upserted["score"] == pytest.approx(0.90, rel=1e-6)
        assert upserted["shadow_action"] == "protected_hub"
        assert upserted["signals"]["in_degree"] == 9

        latest = await repo.get_latest_shadow_score(memory_id=memory["id"], strategy="gcp-v1")
        assert latest is not None
        assert latest["computed_at"] == computed_at
        assert latest["divergence_tag"] == "gcp-protects-vitality-archives"

        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM memory_shadow_scores
                WHERE memory_id = $1 AND strategy = 'gcp-v1' AND computed_at = $2
                """,
                memory["id"],
                computed_at,
            )
        assert count == 1


class TestLdrDeleteObservation:
    async def test_delete_flag_off_writes_no_ldr_observation(
        self, db_pool, test_user, clean_test_data, monkeypatch
    ):
        monkeypatch.delenv("LUCENT_SHADOW_FORGET_ENABLED", raising=False)
        repo = MemoryRepository(db_pool)
        prefix = clean_test_data

        source = await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} source delete target",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} inbound edge",
            related_memory_ids=[source["id"]],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        deleted = await repo.delete(source["id"])
        assert deleted is True
        assert await repo.get(source["id"]) is None

        async with db_pool.acquire() as conn:
            shadow_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM memory_shadow_scores
                WHERE memory_id = $1
                  AND strategy = 'ldr-obs-v1'
                """,
                source["id"],
            )
            deleted_at = await conn.fetchval(
                "SELECT deleted_at FROM memories WHERE id = $1",
                source["id"],
            )
        assert shadow_count == 0
        assert deleted_at is not None

    async def test_delete_flag_on_writes_ldr_observation_with_edges(
        self, db_pool, test_user, clean_test_data, monkeypatch
    ):
        monkeypatch.setenv("LUCENT_SHADOW_FORGET_ENABLED", "true")
        repo = MemoryRepository(db_pool)
        prefix = clean_test_data

        canonical = await repo.create(
            username=f"{prefix}user",
            type="technical",
            content=f"{prefix} canonical",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        source = await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} source delete target",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} inbound edge 1",
            related_memory_ids=[source["id"]],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} inbound edge 2",
            related_memory_ids=[source["id"]],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        deleted = await repo.delete(source["id"], ldr_canonical_id=canonical["id"])
        assert deleted is True
        assert await repo.get(source["id"]) is None

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT strategy, score, shadow_action, signals
                FROM memory_shadow_scores
                WHERE memory_id = $1
                  AND strategy = 'ldr-obs-v1'
                ORDER BY computed_at DESC
                LIMIT 1
                """,
                source["id"],
            )
        assert row is not None
        assert row["strategy"] == "ldr-obs-v1"
        assert row["score"] is None
        assert row["shadow_action"] == "would_demote"
        assert row["signals"]["would_demote_source_id"] == str(source["id"])
        assert row["signals"]["would_link_canonical_id"] == str(canonical["id"])
        assert row["signals"]["would_break_edges"] == 2
        assert row["signals"]["force_delete_compliance"] is False


class TestCandidateAGcpShadowScoring:
    async def test_compute_shadow_forget_scores_writes_gcp_rows(
        self, db_pool, test_user, clean_test_data, monkeypatch
    ):
        monkeypatch.setenv("LUCENT_SHADOW_FORGET_ENABLED", "true")
        repo = MemoryRepository(db_pool)
        prefix = clean_test_data

        for idx in range(3):
            await repo.create(
                username=f"{prefix}user",
                type="technical",
                content=f"{prefix} gcp scoring target {idx}",
                tags=["candidate-a"],
                importance=5,
                user_id=test_user["id"],
                organization_id=test_user["organization_id"],
            )

        result = await repo.compute_shadow_forget_scores(strategy="gcp-v1", batch_size=100)
        assert result["enabled"] is True
        assert result["processed"] >= 3
        assert result["inserted"] >= 3
        metrics = result["comparison_metrics"]
        assert "top_k_agreement" in metrics
        assert "orphan_reclaim_rate" in metrics
        assert "load_bearing_protection_rate" in metrics
        assert "ldr_edges_at_risk_sum" in metrics

        async with db_pool.acquire() as conn:
            gcp_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM memory_shadow_scores
                WHERE strategy = 'gcp-v1'
                """
            )
            divergence_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM memory_shadow_scores
                WHERE strategy = 'gcp-v1'
                  AND divergence_tag IS NOT NULL
                """
            )
        assert gcp_count >= 3
        assert divergence_count >= 3
