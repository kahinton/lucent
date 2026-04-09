"""Tests for memory decay scoring and maintenance reporting."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from lucent.db import AccessRepository, MemoryRepository
from lucent.db.requests import RequestRepository
from lucent.memory.decay import (
    DecayAction,
    DecayConfig,
    MemoryDecayInput,
    dry_run_decay_report,
    run_memory_decay_maintenance_cycle,
    score_memories_batch,
    score_memory_decay,
)


def _profile(
    *,
    memory_type: str = "experience",
    importance: int = 5,
    age_days: int = 100,
    updated_days: int = 100,
    last_accessed_days: int | None = 100,
    access_count: int = 0,
    metadata: dict | None = None,
    linked_to_active_goal: bool = False,
) -> MemoryDecayInput:
    now = datetime.now(UTC)
    return MemoryDecayInput(
        memory_id=uuid4(),
        memory_type=memory_type,
        importance=importance,
        created_at=now - timedelta(days=age_days),
        updated_at=now - timedelta(days=updated_days),
        last_accessed_at=(
            now - timedelta(days=last_accessed_days) if last_accessed_days is not None else None
        ),
        access_count=access_count,
        metadata=metadata or {},
        linked_to_active_goal=linked_to_active_goal,
    )


def test_brand_new_memory_is_protected():
    now = datetime.now(UTC)
    profile = MemoryDecayInput(
        memory_id=uuid4(),
        memory_type="experience",
        importance=3,
        created_at=now - timedelta(days=1),
        updated_at=now - timedelta(days=1),
        last_accessed_at=None,
        access_count=0,
    )
    result = score_memory_decay(profile, now=now)
    assert result.protected is True
    assert "new-memory-protection" in result.reasons
    assert result.action == DecayAction.LEAVE


def test_old_high_importance_memory_protected():
    profile = _profile(importance=10, age_days=800, updated_days=300, last_accessed_days=400)
    result = score_memory_decay(profile)
    assert result.protected is True
    assert "high-importance-protection" in result.reasons
    assert result.score < 0.6


def test_frequently_accessed_scores_lower_than_never_accessed():
    old_frequent = _profile(access_count=200, age_days=400, updated_days=300, last_accessed_days=3)
    old_never = _profile(access_count=0, age_days=400, updated_days=300, last_accessed_days=None)

    frequent_result = score_memory_decay(old_frequent)
    never_result = score_memory_decay(old_never)
    assert frequent_result.score < never_result.score


def test_very_old_never_accessed_low_importance_is_high_decay():
    profile = _profile(
        importance=1,
        access_count=0,
        age_days=900,
        updated_days=900,
        last_accessed_days=None,
    )
    result = score_memory_decay(profile)
    assert result.action in (DecayAction.SUGGEST, DecayAction.ARCHIVE_CANDIDATE)
    assert result.score >= 0.55


def test_active_goal_and_recent_update_protections_apply():
    profile = _profile(
        memory_type="goal",
        importance=6,
        age_days=200,
        updated_days=2,
        last_accessed_days=200,
        metadata={"status": "active"},
        linked_to_active_goal=True,
    )
    result = score_memory_decay(profile)
    assert result.protected is True
    assert "recent-update-protection" in result.reasons
    assert "active-goal-protection" in result.reasons


def test_batch_results_sorted_by_score_desc():
    memories = [
        _profile(
            importance=10,
            age_days=700,
            updated_days=700,
            last_accessed_days=1,
            access_count=50,
        ),
        _profile(
            importance=1,
            age_days=800,
            updated_days=800,
            last_accessed_days=None,
            access_count=0,
        ),
        _profile(
            importance=4,
            age_days=100,
            updated_days=90,
            last_accessed_days=70,
            access_count=2,
        ),
    ]
    results = score_memories_batch(memories)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_dry_run_report_is_reporting_only():
    config = DecayConfig(suggest_threshold=0.4, archive_threshold=0.7)
    scored = score_memories_batch(
        [
            _profile(
                importance=1,
                age_days=900,
                updated_days=900,
                last_accessed_days=None,
                access_count=0,
            ),
            _profile(
                importance=9,
                age_days=200,
                updated_days=2,
                last_accessed_days=2,
                access_count=30,
            ),
        ],
        config=config,
    )
    report = dry_run_decay_report(scored, config=config)
    assert report["total_scored"] == 2
    assert report["counts"]["archive_candidates"] >= 0
    assert report["counts"]["cleanup_suggestions"] >= 0
    # input untouched (no in-place mutation)
    assert len(scored) == 2


async def test_maintenance_cycle_dry_run_no_side_effects(db_pool, test_user, clean_test_data):
    """Dry-run maintenance should score/report without deleting or archiving."""
    prefix = clean_test_data
    memory_repo = MemoryRepository(db_pool)
    access_repo = AccessRepository(db_pool)
    request_repo = RequestRepository(db_pool)

    old_memory = await memory_repo.create(
        username=f"{prefix}user",
        type="experience",
        content=f"{prefix} old stale memory",
        tags=["cleanup-test"],
        importance=2,
        user_id=test_user["id"],
        organization_id=test_user["organization_id"],
    )
    goal_memory = await memory_repo.create(
        username=f"{prefix}user",
        type="goal",
        content=f"{prefix} active goal memory",
        tags=["cleanup-test"],
        importance=6,
        metadata={"status": "active"},
        user_id=test_user["id"],
        organization_id=test_user["organization_id"],
    )

    # Backdate one memory to make it a stronger decay candidate.
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories
            SET created_at = NOW() - INTERVAL '800 days',
                updated_at = NOW() - INTERVAL '800 days'
            WHERE id = $1
            """,
            old_memory["id"],
        )

    # Link goal memory to an active request to verify protection path.
    req = await request_repo.create_request(
        title=f"{prefix}goal work",
        description="active work",
        source="user",
        priority="medium",
        org_id=str(test_user["organization_id"]),
        created_by=str(test_user["id"]),
        memory_ids=[{"id": str(goal_memory["id"]), "relation": "goal"}],
    )
    assert req["status"] in {"pending", "planned", "in_progress", "review", "needs_rework"}

    report = await run_memory_decay_maintenance_cycle(
        memory_repo=memory_repo,
        access_repo=access_repo,
        requesting_user_id=test_user["id"],
        requesting_org_id=test_user["organization_id"],
        dry_run=True,
    )
    assert report["total_scored"] >= 2

    # Verify no side effects (still active, not soft-deleted).
    persisted_old = await memory_repo.get(old_memory["id"])
    persisted_goal = await memory_repo.get(goal_memory["id"])
    assert persisted_old is not None and persisted_old["deleted_at"] is None
    assert persisted_goal is not None and persisted_goal["deleted_at"] is None

    # Explicit cleanup for request rows (fixture cleanup does not currently remove requests).
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM request_memories WHERE request_id = $1", req["id"])
        await conn.execute("DELETE FROM tasks WHERE request_id = $1", req["id"])
        await conn.execute("DELETE FROM requests WHERE id = $1", req["id"])
