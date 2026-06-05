"""Candidate-A shadow scoring integration invariants."""

import json

from lucent.db import MemoryRepository


def _snapshot_row(row) -> dict:
    return {
        "id": str(row["id"]),
        "lifecycle_stage": row["lifecycle_stage"],
        "vitality_score": row["vitality_score"],
        "vitality_computed_at": row["vitality_computed_at"].isoformat()
        if row["vitality_computed_at"]
        else None,
        "content": row["content"],
        "tags": list(row["tags"] or []),
        "importance": row["importance"],
        "related_memory_ids": [str(item) for item in (row["related_memory_ids"] or [])],
    }


async def test_shadow_gcp_flag_off_is_noop_and_does_not_change_vitality_or_ranking(
    db_pool,
    test_user,
    clean_test_data,
    monkeypatch,
):
    monkeypatch.delenv("LUCENT_SHADOW_FORGET_ENABLED", raising=False)
    repo = MemoryRepository(db_pool)
    prefix = clean_test_data

    for idx in range(3):
        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} candidate-a ranking corpus {idx}",
            tags=["candidate-a", "shadow"],
            importance=8 - idx,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

    await repo.compute_vitality_scores(batch_size=100)

    search_before = await repo.search(
        query=f"{prefix} candidate-a ranking corpus",
        limit=10,
        requesting_user_id=test_user["id"],
        requesting_org_id=test_user["organization_id"],
    )
    ranking_before = [str(item["id"]) for item in search_before["memories"]]

    async with db_pool.acquire() as conn:
        rows_before = await conn.fetch(
            """
            SELECT id, lifecycle_stage, vitality_score, vitality_computed_at,
                   content, tags, importance, related_memory_ids
            FROM memories
            WHERE organization_id = $1
              AND deleted_at IS NULL
              AND content ILIKE $2
            ORDER BY id
            """,
            test_user["organization_id"],
            f"%{prefix} candidate-a ranking corpus%",
        )
    snapshot_before = json.dumps([_snapshot_row(row) for row in rows_before], sort_keys=True)
    scoped_ids = [row["id"] for row in rows_before]

    result = await repo.compute_shadow_forget_scores(strategy="gcp-v1", batch_size=100)
    assert result["enabled"] is False
    assert result["processed"] == 0
    assert result["inserted"] == 0

    async with db_pool.acquire() as conn:
        sidecar_count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM memory_shadow_scores
            WHERE strategy = 'gcp-v1'
              AND memory_id = ANY($1::uuid[])
            """,
            scoped_ids,
        )
        rows_after = await conn.fetch(
            """
            SELECT id, lifecycle_stage, vitality_score, vitality_computed_at,
                   content, tags, importance, related_memory_ids
            FROM memories
            WHERE organization_id = $1
              AND deleted_at IS NULL
              AND content ILIKE $2
            ORDER BY id
            """,
            test_user["organization_id"],
            f"%{prefix} candidate-a ranking corpus%",
        )
    assert sidecar_count == 0
    snapshot_after = json.dumps([_snapshot_row(row) for row in rows_after], sort_keys=True)
    assert snapshot_after == snapshot_before

    search_after = await repo.search(
        query=f"{prefix} candidate-a ranking corpus",
        limit=10,
        requesting_user_id=test_user["id"],
        requesting_org_id=test_user["organization_id"],
    )
    ranking_after = [str(item["id"]) for item in search_after["memories"]]
    assert ranking_after == ranking_before
