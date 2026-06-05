"""Tests for memory lifecycle vitality scoring and maintenance reporting."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from lucent.db import AccessRepository, MemoryRepository
from lucent.db.requests import RequestRepository
from lucent.memory.decay import (
    DecayAction,
    DecayConfig,
    GcpConfig,
    GraphConnectednessInput,
    MemoryDecayInput,
    ShadowGcpAction,
    classify_decay_action,
    classify_gcp_action,
    compute_graph_connectedness,
    compute_memory_vitality,
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


def test_frequently_accessed_scores_higher_than_never_accessed():
    old_frequent = _profile(access_count=20, age_days=400, updated_days=300, last_accessed_days=3)
    old_never = _profile(access_count=0, age_days=400, updated_days=300, last_accessed_days=None)

    frequent_result = score_memory_decay(old_frequent)
    never_result = score_memory_decay(old_never)
    assert frequent_result.score > never_result.score


def test_very_old_never_accessed_low_importance_is_low_vitality():
    profile = _profile(
        importance=1,
        access_count=0,
        age_days=900,
        updated_days=900,
        last_accessed_days=None,
    )
    result = score_memory_decay(profile)
    assert result.action in (DecayAction.SUGGEST, DecayAction.ARCHIVE_CANDIDATE)
    assert result.score < 0.35


def test_higher_importance_increases_vitality():
    high = _profile(importance=10, age_days=300, updated_days=300, last_accessed_days=100, access_count=1)
    low = _profile(importance=1, age_days=300, updated_days=300, last_accessed_days=100, access_count=1)

    high_result = compute_memory_vitality(high)
    low_result = compute_memory_vitality(low)
    assert high_result.score > low_result.score


def test_individual_memory_hard_exempt_even_if_stale():
    profile = _profile(
        memory_type="individual",
        importance=1,
        age_days=2000,
        updated_days=2000,
        last_accessed_days=2000,
        access_count=0,
    )
    result = score_memory_decay(profile)
    assert result.protected is True
    assert result.action == DecayAction.EXEMPT
    assert "hard-exempt-individual" in result.reasons


def test_active_goal_memory_hard_exempt_even_if_stale():
    profile = _profile(
        memory_type="goal",
        importance=1,
        age_days=1200,
        updated_days=1200,
        last_accessed_days=1200,
        access_count=0,
        metadata={"status": "active"},
    )
    result = score_memory_decay(profile)
    assert result.protected is True
    assert result.action == DecayAction.EXEMPT
    assert "hard-exempt-active-goal" in result.reasons


def test_pinned_memory_hard_exempt_even_if_stale():
    """A memory tagged 'pinned' must short-circuit out of consolidation/forgetting
    candidate selection, parallel to individual + active-goal exemptions."""
    profile = _profile(
        memory_type="experience",
        importance=1,
        age_days=2000,
        updated_days=2000,
        last_accessed_days=2000,
        access_count=0,
    )
    profile.tags = ["pinned"]
    result = score_memory_decay(profile)
    assert result.protected is True
    assert result.action == DecayAction.EXEMPT
    assert "hard-exempt-pinned" in result.reasons


def test_pinned_memory_excluded_from_consolidation_candidates():
    """A pinned memory must not appear in archive/cleanup candidate sets even
    when its raw vitality would otherwise classify it as a candidate."""
    pinned = _profile(
        memory_type="experience",
        importance=1,
        age_days=2000,
        updated_days=2000,
        last_accessed_days=2000,
        access_count=0,
    )
    pinned.tags = ["pinned"]
    stale_unpinned = _profile(
        memory_type="experience",
        importance=1,
        age_days=2000,
        updated_days=2000,
        last_accessed_days=2000,
        access_count=0,
    )

    scored = score_memories_batch([pinned, stale_unpinned])
    report = dry_run_decay_report(scored)

    pinned_id = str(pinned.memory_id)
    candidate_ids = {
        c["memory_id"] for c in report["archive_candidates"] + report["cleanup_suggestions"]
    }
    assert pinned_id not in candidate_ids
    exempt_ids = {c["memory_id"] for c in report["exempt"]}
    assert pinned_id in exempt_ids


def test_classify_decay_action_short_circuits_for_exempt_profile():
    profile = _profile(memory_type="individual", age_days=999, last_accessed_days=999, importance=1)
    # Even with a score that would otherwise archive, exemption should win.
    assert classify_decay_action(0.0, profile=profile) == DecayAction.EXEMPT


def test_batch_results_sorted_by_score_asc():
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
    assert scores == sorted(scores)


def test_dry_run_report_is_reporting_only():
    config = DecayConfig(suggest_threshold=0.4, archive_threshold=0.2)
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


def _gcp_profile(
    *,
    in_degree: int = 0,
    out_degree: int = 0,
    active_request_links: int = 0,
    version_depth: int = 1,
    distinct_readers_90d: int = 0,
    importance: int = 5,
    tags: list[str] | None = None,
) -> GraphConnectednessInput:
    return GraphConnectednessInput(
        memory_id=uuid4(),
        importance=importance,
        age_days=30,
        in_degree=in_degree,
        out_degree=out_degree,
        active_request_links=active_request_links,
        version_depth=version_depth,
        distinct_readers_90d=distinct_readers_90d,
        tags=tags or [],
    )


def test_gcp_connected_hub_scores_as_protected():
    profile = _gcp_profile(
        in_degree=12,
        out_degree=5,
        active_request_links=3,
        version_depth=8,
        distinct_readers_90d=6,
    )
    result = compute_graph_connectedness(profile)
    assert result.score > 1.0
    assert result.action == ShadowGcpAction.PROTECTED_HUB


def test_gcp_low_connectedness_is_forgetting_candidate():
    profile = _gcp_profile(
        in_degree=0,
        out_degree=0,
        active_request_links=0,
        version_depth=1,
        distinct_readers_90d=0,
        importance=2,
    )
    result = compute_graph_connectedness(profile, config=GcpConfig(forgetting_threshold=0.4))
    assert result.action == ShadowGcpAction.FORGETTING_CANDIDATE


def test_gcp_hard_protection_tags_override_thresholds():
    profile = _gcp_profile(
        in_degree=0,
        out_degree=0,
        active_request_links=0,
        version_depth=1,
        distinct_readers_90d=0,
        importance=1,
        tags=["pinned"],
    )
    action = classify_gcp_action(0.0, profile=profile)
    assert action == ShadowGcpAction.PROTECTED_HUB


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

    # Backdate one memory to make it a stronger low-vitality candidate.
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories
            SET created_at = NOW() - INTERVAL '800 days',
                updated_at = NOW() - INTERVAL '800 days',
                last_accessed_at = NOW() - INTERVAL '800 days'
            WHERE id = $1
            """,
            old_memory["id"],
        )

    # Link goal memory to an active request to verify hard exemption path.
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

    # Goal memory should be present as exempt in report output.
    exempt_ids = {entry["memory_id"] for entry in report.get("exempt", [])}
    assert str(goal_memory["id"]) in exempt_ids

    # Explicit cleanup for request rows (fixture cleanup does not currently remove requests).
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM request_memories WHERE request_id = $1", req["id"])
        await conn.execute("DELETE FROM tasks WHERE request_id = $1", req["id"])
        await conn.execute("DELETE FROM requests WHERE id = $1", req["id"])
