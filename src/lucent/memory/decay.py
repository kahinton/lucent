"""Memory lifecycle vitality scoring and reporting.

This module computes vitality scores for memories to support lifecycle maintenance
cycles. It currently reports candidates only; it does not archive, delete, or
mutate memory records.

Backward-compatibility note:
- Historical names (`score_memory_decay`, `classify_decay_action`) are retained
  as wrappers around vitality-first implementations.
- Existing `LUCENT_MEMORY_DECAY_*` environment variable prefixes are preserved.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import ceil, exp
from typing import Any
from uuid import UUID

from lucent.db import AccessRepository, MemoryRepository

# Relation statuses considered "active" work where linked memories should be protected.
_ACTIVE_REQUEST_STATUSES = ("pending", "planned", "in_progress", "review", "needs_rework")
_GOAL_STATUS_ACTIVE = ("active", "paused")


@dataclass(slots=True)
class DecayConfig:
    """Configuration for vitality scoring and lifecycle thresholds."""

    recency_lambda: float = 0.03
    frequency_baseline: int = 10

    weight_recency: float = 0.35
    weight_frequency: float = 0.25
    weight_importance: float = 0.25
    weight_type: float = 0.15

    type_bonus: dict[str, float] = field(
        default_factory=lambda: {
            "individual": 0.30,
            "technical": 0.15,
            "goal_active": 0.10,
            "goal_inactive": 0.00,
            "experience": 0.00,
        }
    )

    # Kept on historical field names for compatibility, but semantics are vitality-based:
    # - suggest_threshold == consolidation threshold
    # - archive_threshold == archive threshold
    suggest_threshold: float = 0.35
    archive_threshold: float = 0.15
    forget_threshold: float = 0.05
    batch_limit: int = 5000

    @classmethod
    def from_env(cls) -> DecayConfig:
        """Build config from environment variables with sane defaults."""
        cfg = cls()

        cfg.recency_lambda = _env_float(
            "LUCENT_MEMORY_DECAY_RECENCY_LAMBDA",
            cfg.recency_lambda,
        )
        cfg.frequency_baseline = _env_int(
            "LUCENT_MEMORY_DECAY_FREQUENCY_BASELINE",
            cfg.frequency_baseline,
        )

        cfg.weight_recency = _env_float(
            "LUCENT_MEMORY_DECAY_WEIGHT_RECENCY",
            cfg.weight_recency,
        )
        cfg.weight_frequency = _env_float(
            "LUCENT_MEMORY_DECAY_WEIGHT_FREQUENCY",
            cfg.weight_frequency,
        )
        cfg.weight_importance = _env_float(
            "LUCENT_MEMORY_DECAY_WEIGHT_IMPORTANCE",
            cfg.weight_importance,
        )
        cfg.weight_type = _env_float(
            "LUCENT_MEMORY_DECAY_WEIGHT_TYPE",
            cfg.weight_type,
        )

        cfg.type_bonus["individual"] = _env_float(
            "LUCENT_MEMORY_DECAY_TYPE_BONUS_INDIVIDUAL",
            cfg.type_bonus["individual"],
        )
        cfg.type_bonus["technical"] = _env_float(
            "LUCENT_MEMORY_DECAY_TYPE_BONUS_TECHNICAL",
            cfg.type_bonus["technical"],
        )
        cfg.type_bonus["goal_active"] = _env_float(
            "LUCENT_MEMORY_DECAY_TYPE_BONUS_GOAL_ACTIVE",
            cfg.type_bonus["goal_active"],
        )
        cfg.type_bonus["goal_inactive"] = _env_float(
            "LUCENT_MEMORY_DECAY_TYPE_BONUS_GOAL_INACTIVE",
            cfg.type_bonus["goal_inactive"],
        )
        cfg.type_bonus["experience"] = _env_float(
            "LUCENT_MEMORY_DECAY_TYPE_BONUS_EXPERIENCE",
            cfg.type_bonus["experience"],
        )

        cfg.suggest_threshold = _clamp01(
            _env_float("LUCENT_MEMORY_DECAY_SUGGEST_THRESHOLD", cfg.suggest_threshold)
        )
        cfg.archive_threshold = _clamp01(
            _env_float("LUCENT_MEMORY_DECAY_ARCHIVE_THRESHOLD", cfg.archive_threshold)
        )
        cfg.forget_threshold = _clamp01(
            _env_float("LUCENT_MEMORY_DECAY_FORGET_THRESHOLD", cfg.forget_threshold)
        )
        cfg.batch_limit = _env_int("LUCENT_MEMORY_DECAY_BATCH_LIMIT", cfg.batch_limit)

        # With vitality semantics: consolidation threshold must be >= archive threshold.
        if cfg.suggest_threshold < cfg.archive_threshold:
            cfg.suggest_threshold = cfg.archive_threshold
        if cfg.archive_threshold < cfg.forget_threshold:
            cfg.archive_threshold = cfg.forget_threshold

        return cfg


@dataclass(slots=True)
class MemoryDecayInput:
    """Input profile for scoring one memory."""

    memory_id: UUID
    memory_type: str
    importance: int
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None
    # Interpreted as access_count_last_90_days for vitality scoring.
    access_count: int
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    linked_to_active_goal: bool = False


@dataclass(slots=True)
class DecayScoreResult:
    """Scoring output for one memory."""

    memory_id: UUID
    score: float
    action: str
    protected: bool
    reasons: list[str]
    factors: dict[str, float]
    memory_type: str
    importance: int
    access_count: int
    last_accessed_at: datetime | None
    updated_at: datetime
    created_at: datetime


class DecayAction:
    """Action labels produced by classification."""

    EXEMPT = "exempt"
    LEAVE = "leave-alone"
    SUGGEST = "suggest-cleanup"
    ARCHIVE_CANDIDATE = "archive-candidate"


class ShadowGcpAction:
    """Shadow-only action labels for Candidate-A graph connectedness scoring."""

    KEEP = "keep"
    FORGETTING_CANDIDATE = "forgetting_candidate"
    PROTECTED_HUB = "protected_hub"


@dataclass(slots=True)
class GraphConnectednessInput:
    """Input profile for one-memory graph connectedness scoring."""

    memory_id: UUID
    importance: int
    age_days: int
    in_degree: int
    out_degree: int
    active_request_links: int
    version_depth: int
    distinct_readers_90d: int
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ShadowScoreResult:
    """Output for one-memory shadow graph connectedness scoring."""

    memory_id: UUID
    score: float
    action: str
    protected: bool
    reasons: list[str]
    signals: dict[str, float | int | bool]


@dataclass(slots=True)
class GcpConfig:
    """Configuration for Candidate-A graph connectedness scoring."""

    weight_edges: float = 0.35
    weight_active_links: float = 0.30
    weight_version: float = 0.15
    weight_distinct_readers: float = 0.20
    forgetting_threshold: float = 0.70
    protected_hub_threshold: float = 1.30
    protected_importance_floor: int = 8


def compute_graph_connectedness(
    profile: GraphConnectednessInput,
    *,
    config: GcpConfig | None = None,
) -> ShadowScoreResult:
    """Compute Candidate-A connectedness score and shadow-only classification."""
    cfg = config or GcpConfig()
    hard_protection_reason = _hard_gcp_protection_reason(profile, config=cfg)

    edges = max(0, profile.in_degree) + max(0, profile.out_degree)
    active_links = max(0, profile.active_request_links)
    version_depth = max(1, profile.version_depth)
    distinct_readers = max(0, profile.distinct_readers_90d)

    connectedness = (
        cfg.weight_edges * _ln1p(edges)
        + cfg.weight_active_links * _ln1p(2 * active_links)
        + cfg.weight_version * _ln1p(version_depth)
        + cfg.weight_distinct_readers * _ln1p(distinct_readers)
    )
    action = classify_gcp_action(
        connectedness,
        profile=profile,
        config=cfg,
    )

    return ShadowScoreResult(
        memory_id=profile.memory_id,
        score=connectedness,
        action=action,
        protected=hard_protection_reason is not None,
        reasons=[hard_protection_reason] if hard_protection_reason else [],
        signals={
            "in_degree": profile.in_degree,
            "out_degree": profile.out_degree,
            "active_request_links": profile.active_request_links,
            "version_depth": profile.version_depth,
            "distinct_readers_90d": profile.distinct_readers_90d,
            "age_days": max(0, profile.age_days),
            "importance": profile.importance,
            "connectedness": connectedness,
            "pinned": "pinned" in (profile.tags or []),
            "do_not_consolidate": "do_not_consolidate" in (profile.tags or []),
        },
    )


def classify_gcp_action(
    score: float,
    *,
    profile: GraphConnectednessInput,
    config: GcpConfig | None = None,
) -> str:
    """Map Candidate-A connectedness score to a shadow-only action."""
    cfg = config or GcpConfig()
    if _hard_gcp_protection_reason(profile, config=cfg):
        return ShadowGcpAction.PROTECTED_HUB
    if score >= cfg.protected_hub_threshold:
        return ShadowGcpAction.PROTECTED_HUB
    if score >= cfg.forgetting_threshold:
        return ShadowGcpAction.KEEP
    return ShadowGcpAction.FORGETTING_CANDIDATE


def compute_memory_vitality(
    profile: MemoryDecayInput,
    *,
    config: DecayConfig | None = None,
    now: datetime | None = None,
    frequency_baseline: int | None = None,
) -> DecayScoreResult:
    """Compute one memory vitality score (0.0 = least vital, 1.0 = most vital)."""
    cfg = config or DecayConfig.from_env()

    hard_exemption_reason = _hard_exemption_reason(profile)
    if hard_exemption_reason:
        return _build_hard_exempt_result(
            profile=profile,
            reason=hard_exemption_reason,
            config=cfg,
        )

    current = _utc(now)
    recency_source = profile.last_accessed_at or profile.created_at
    recency_days = max(0, (current - _utc(recency_source)).days)

    recency_score = _clamp01(exp(-cfg.recency_lambda * recency_days))

    effective_frequency_baseline = max(
        1,
        frequency_baseline if frequency_baseline is not None else cfg.frequency_baseline,
    )
    frequency_score = _clamp01(profile.access_count / effective_frequency_baseline)
    importance_score = _clamp01(profile.importance / 10.0)
    type_bonus = _type_bonus(profile, cfg)

    score = _clamp01(
        (cfg.weight_recency * recency_score)
        + (cfg.weight_frequency * frequency_score)
        + (cfg.weight_importance * importance_score)
        + (cfg.weight_type * type_bonus)
    )

    action = classify_vitality_action(score, config=cfg, profile=profile)

    return DecayScoreResult(
        memory_id=profile.memory_id,
        score=score,
        action=action,
        protected=False,
        reasons=[],
        factors={
            "recency_score": recency_score,
            "frequency_score": frequency_score,
            "importance_score": importance_score,
            "type_bonus": type_bonus,
            "frequency_baseline": float(effective_frequency_baseline),
        },
        memory_type=profile.memory_type,
        importance=profile.importance,
        access_count=profile.access_count,
        last_accessed_at=profile.last_accessed_at,
        updated_at=profile.updated_at,
        created_at=profile.created_at,
    )


def score_memory_decay(
    profile: MemoryDecayInput,
    *,
    config: DecayConfig | None = None,
    now: datetime | None = None,
    frequency_baseline: int | None = None,
) -> DecayScoreResult:
    """Backward-compatible wrapper for vitality scoring."""
    return compute_memory_vitality(
        profile,
        config=config,
        now=now,
        frequency_baseline=frequency_baseline,
    )


def classify_vitality_action(
    score: float,
    config: DecayConfig | None = None,
    *,
    profile: MemoryDecayInput | None = None,
) -> str:
    """Map vitality score to action label using configurable thresholds."""
    if profile is not None and _hard_exemption_reason(profile):
        return DecayAction.EXEMPT

    cfg = config or DecayConfig.from_env()
    if score < cfg.archive_threshold:
        return DecayAction.ARCHIVE_CANDIDATE
    if score < cfg.suggest_threshold:
        return DecayAction.SUGGEST
    return DecayAction.LEAVE


def classify_decay_action(
    score: float,
    config: DecayConfig | None = None,
    *,
    profile: MemoryDecayInput | None = None,
) -> str:
    """Backward-compatible wrapper for vitality-based classification."""
    return classify_vitality_action(score, config=config, profile=profile)


def score_memories_batch(
    memories: list[MemoryDecayInput],
    *,
    config: DecayConfig | None = None,
    now: datetime | None = None,
    frequency_baseline: int | None = None,
) -> list[DecayScoreResult]:
    """Score many memories and return sorted lowest vitality first."""
    cfg = config or DecayConfig.from_env()
    results = [
        compute_memory_vitality(
            profile,
            config=cfg,
            now=now,
            frequency_baseline=frequency_baseline,
        )
        for profile in memories
    ]
    return sorted(results, key=lambda item: item.score)


def dry_run_decay_report(
    scored: list[DecayScoreResult],
    *,
    config: DecayConfig | None = None,
) -> dict[str, Any]:
    """Summarize what would be flagged or archived without side effects."""
    cfg = config or DecayConfig.from_env()
    sorted_scored = sorted(scored, key=lambda item: item.score)

    exempt = [r for r in sorted_scored if r.action == DecayAction.EXEMPT]
    archive_candidates = [
        r
        for r in sorted_scored
        if r.action != DecayAction.EXEMPT and r.score < cfg.archive_threshold
    ]
    cleanup_suggestions = [
        r
        for r in sorted_scored
        if r.action != DecayAction.EXEMPT
        and cfg.archive_threshold <= r.score < cfg.suggest_threshold
    ]

    return {
        "total_scored": len(sorted_scored),
        "thresholds": {
            "suggest_threshold": cfg.suggest_threshold,
            "archive_threshold": cfg.archive_threshold,
            "forget_threshold": cfg.forget_threshold,
        },
        "counts": {
            "archive_candidates": len(archive_candidates),
            "cleanup_suggestions": len(cleanup_suggestions),
            "leave_alone": len(sorted_scored)
            - len(archive_candidates)
            - len(cleanup_suggestions)
            - len(exempt),
            "protected": sum(1 for r in sorted_scored if r.protected),
            "exempt": len(exempt),
        },
        "archive_candidates": [_result_to_dict(r) for r in archive_candidates],
        "cleanup_suggestions": [_result_to_dict(r) for r in cleanup_suggestions],
        "exempt": [_result_to_dict(r) for r in exempt],
        "top_10": [_result_to_dict(r) for r in sorted_scored[:10]],
    }


async def run_memory_decay_maintenance_cycle(
    memory_repo: MemoryRepository,
    access_repo: AccessRepository,
    *,
    requesting_user_id: UUID | None = None,
    requesting_org_id: UUID | None = None,
    config: DecayConfig | None = None,
    now: datetime | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Integration entrypoint for memory-management maintenance cycles.

    This function computes vitality scoring/reporting only. No archive/delete
    operations are performed.
    """
    cfg = config or DecayConfig.from_env()
    result = await memory_repo.search(
        limit=cfg.batch_limit,
        offset=0,
        requesting_user_id=requesting_user_id,
        requesting_org_id=requesting_org_id,
    )
    raw_memories = result["memories"]
    memory_ids = [m["id"] for m in raw_memories]
    full_memories = await _fetch_full_memories(memory_repo, memory_ids)

    # Match design: frequency is based on accesses in the last 90 days and
    # normalized by a run-level p75 baseline.
    recent_access_counts = await _get_recent_access_counts(
        access_repo=access_repo,
        memory_ids=memory_ids,
        days=90,
        now=_utc(now),
    )
    frequency_baseline = _compute_p75_baseline(
        counts=[recent_access_counts.get(memory_id, 0) for memory_id in memory_ids],
        default=cfg.frequency_baseline,
    )

    linked_to_active = await _get_active_goal_linked_map(
        memory_repo=memory_repo,
        memory_ids=memory_ids,
    )

    profiles = [
        MemoryDecayInput(
            memory_id=item["id"],
            memory_type=item["type"],
            importance=item["importance"],
            created_at=item["created_at"],
            updated_at=item["updated_at"],
            last_accessed_at=item.get("last_accessed_at"),
            access_count=recent_access_counts.get(item["id"], 0),
            tags=item.get("tags", []),
            metadata=(full_memories.get(item["id"], {}) or {}).get("metadata", {}),
            linked_to_active_goal=linked_to_active.get(item["id"], False),
        )
        for item in raw_memories
    ]

    scored = score_memories_batch(
        profiles,
        config=cfg,
        now=now,
        frequency_baseline=frequency_baseline,
    )

    # For now both branches are report-only by design.
    if dry_run:
        return dry_run_decay_report(scored, config=cfg)

    return {
        "mode": "report-only",
        "note": (
            "Decay maintenance is currently scoring/reporting only; "
            "no archival actions executed."
        ),
        "report": dry_run_decay_report(scored, config=cfg),
    }


async def _get_recent_access_counts(
    access_repo: AccessRepository,
    memory_ids: list[UUID],
    *,
    days: int,
    now: datetime,
) -> dict[UUID, int]:
    """Get access counts for specific memories over the last N days."""
    if not memory_ids:
        return {}

    cutoff = now - timedelta(days=days)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(memory_ids)))
    cutoff_param = len(memory_ids) + 1
    query = f"""
        SELECT memory_id, COUNT(*) AS access_count
        FROM memory_access_log
        WHERE memory_id IN ({placeholders})
          AND accessed_at >= ${cutoff_param}
        GROUP BY memory_id
    """

    async with access_repo.pool.acquire() as conn:
        rows = await conn.fetch(query, *[str(mid) for mid in memory_ids], cutoff)

    counts: dict[UUID, int] = {memory_id: 0 for memory_id in memory_ids}
    for row in rows:
        memory_id = (
            row["memory_id"] if isinstance(row["memory_id"], UUID) else UUID(row["memory_id"])
        )
        counts[memory_id] = row["access_count"]
    return counts


def _compute_p75_baseline(*, counts: list[int], default: int) -> int:
    """Compute p75 baseline using nearest-rank, with safe fallback."""
    non_negative = sorted(max(0, c) for c in counts)
    if not non_negative:
        return max(1, default)

    rank = max(1, ceil(0.75 * len(non_negative)))
    p75 = non_negative[rank - 1]
    return max(1, p75 if p75 > 0 else default)


async def _get_active_goal_linked_map(
    memory_repo: MemoryRepository,
    memory_ids: list[UUID],
) -> dict[UUID, bool]:
    """Return whether each memory is linked to active request work on a goal relation."""
    if not memory_ids:
        return {}

    placeholders = ", ".join(f"${i + 1}" for i in range(len(memory_ids)))
    query = f"""
        SELECT DISTINCT rm.memory_id
        FROM request_memories rm
        JOIN requests r ON r.id = rm.request_id
        WHERE rm.memory_id IN ({placeholders})
          AND rm.relation = 'goal'
          AND r.status = ANY(${"%d" % (len(memory_ids) + 1)})
    """
    # Uses repository pool, keeping all DB access behind repository objects.
    async with memory_repo.pool.acquire() as conn:
        rows = await conn.fetch(query, *memory_ids, list(_ACTIVE_REQUEST_STATUSES))

    linked: dict[UUID, bool] = {memory_id: False for memory_id in memory_ids}
    for row in rows:
        linked[row["memory_id"]] = True
    return linked


async def _fetch_full_memories(
    memory_repo: MemoryRepository,
    memory_ids: list[UUID],
) -> dict[UUID, dict[str, Any]]:
    """Fetch full memory records for IDs already ACL-filtered by caller."""
    if not memory_ids:
        return {}

    placeholders = ", ".join(f"${i + 1}" for i in range(len(memory_ids)))
    query = f"""
        SELECT {memory_repo._FULL_COLUMNS}
        FROM memories
        WHERE id IN ({placeholders})
          AND deleted_at IS NULL
    """
    async with memory_repo.pool.acquire() as conn:
        rows = await conn.fetch(query, *memory_ids)
    return {
        row["id"]: memory_repo._row_to_dict(row)  # noqa: SLF001 - intentional repository reuse
        for row in rows
    }


def _build_hard_exempt_result(
    *,
    profile: MemoryDecayInput,
    reason: str,
    config: DecayConfig,
) -> DecayScoreResult:
    """Create an exempt result without running scoring math."""
    # Assign a maximal vitality score for exempt records to keep all reports safe.
    score = 1.0
    action = classify_vitality_action(score, config=config, profile=profile)
    return DecayScoreResult(
        memory_id=profile.memory_id,
        score=score,
        action=action,
        protected=True,
        reasons=[reason],
        factors={
            "recency_score": 1.0,
            "frequency_score": 1.0,
            "importance_score": _clamp01(profile.importance / 10.0),
            "type_bonus": _type_bonus(profile, config),
            "hard_exempt": 1.0,
        },
        memory_type=profile.memory_type,
        importance=profile.importance,
        access_count=profile.access_count,
        last_accessed_at=profile.last_accessed_at,
        updated_at=profile.updated_at,
        created_at=profile.created_at,
    )


def _hard_exemption_reason(profile: MemoryDecayInput) -> str | None:
    # Pinned tag is a manual user-override exemption that short-circuits all
    # consolidation/forgetting candidate selection, parallel to the
    # individual + active-goal exemptions.
    if "pinned" in (profile.tags or []):
        return "hard-exempt-pinned"

    if profile.memory_type == "individual":
        return "hard-exempt-individual"

    goal_status = str((profile.metadata or {}).get("status", "")).lower()
    is_active_goal = profile.memory_type == "goal" and goal_status in _GOAL_STATUS_ACTIVE
    if is_active_goal or profile.linked_to_active_goal:
        return "hard-exempt-active-goal"

    return None


def _type_bonus(profile: MemoryDecayInput, cfg: DecayConfig) -> float:
    if profile.memory_type == "goal":
        goal_status = str((profile.metadata or {}).get("status", "")).lower()
        key = "goal_active" if goal_status in _GOAL_STATUS_ACTIVE else "goal_inactive"
        return _clamp01(cfg.type_bonus.get(key, 0.0))
    return _clamp01(cfg.type_bonus.get(profile.memory_type, 0.0))


def _result_to_dict(result: DecayScoreResult) -> dict[str, Any]:
    return {
        "memory_id": str(result.memory_id),
        "score": round(result.score, 4),
        "action": result.action,
        "protected": result.protected,
        "reasons": result.reasons,
        "memory_type": result.memory_type,
        "importance": result.importance,
        "access_count": result.access_count,
        "last_accessed_at": (
            result.last_accessed_at.isoformat() if result.last_accessed_at else None
        ),
        "updated_at": result.updated_at.isoformat(),
        "created_at": result.created_at.isoformat(),
        "factors": {k: round(v, 4) for k, v in result.factors.items()},
    }


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _ln1p(value: int) -> float:
    # Keep this helper local to avoid importing math.log1p for one callsite.
    from math import log1p

    return log1p(max(0, value))


def _hard_gcp_protection_reason(
    profile: GraphConnectednessInput,
    *,
    config: GcpConfig,
) -> str | None:
    tags = set(profile.tags or [])
    if "pinned" in tags:
        return "hard-protect-pinned"
    if "do_not_consolidate" in tags:
        return "hard-protect-do-not-consolidate"
    if profile.importance >= config.protected_importance_floor:
        return "hard-protect-high-importance"
    return None
