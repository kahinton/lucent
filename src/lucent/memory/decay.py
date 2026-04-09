"""Memory decay scoring and reporting.

This module computes *decay scores* for memories to support maintenance cycles.
It does not archive, delete, or mutate memory records.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import log10
from typing import Any
from uuid import UUID

from lucent.db import AccessRepository, MemoryRepository

# Relation statuses considered "active" work where linked memories should be protected.
_ACTIVE_REQUEST_STATUSES = ("pending", "planned", "in_progress", "review", "needs_rework")
_GOAL_STATUS_ACTIVE = ("active", "paused")


@dataclass(slots=True)
class DecayConfig:
    """Configuration for decay scoring and action thresholds."""

    age_window_days: int = 365
    recency_window_days: int = 180
    access_log_scale: int = 20
    new_memory_protection_days: int = 7
    recent_update_protection_days: int = 14
    high_importance_threshold: int = 8
    high_importance_protection_multiplier: float = 0.4
    active_goal_protection_multiplier: float = 0.2
    recent_update_protection_multiplier: float = 0.5
    type_decay_bias: dict[str, float] = field(
        default_factory=lambda: {
            "goal": 0.35,
            "individual": 0.45,
            "procedural": 0.9,
            "technical": 1.0,
            "experience": 1.05,
        }
    )
    suggest_threshold: float = 0.55
    archive_threshold: float = 0.8
    batch_limit: int = 5000

    @classmethod
    def from_env(cls) -> DecayConfig:
        """Build config from environment variables with sane defaults."""
        cfg = cls()
        cfg.age_window_days = _env_int("LUCENT_MEMORY_DECAY_AGE_WINDOW_DAYS", cfg.age_window_days)
        cfg.recency_window_days = _env_int(
            "LUCENT_MEMORY_DECAY_RECENCY_WINDOW_DAYS", cfg.recency_window_days
        )
        cfg.access_log_scale = _env_int(
            "LUCENT_MEMORY_DECAY_ACCESS_LOG_SCALE", cfg.access_log_scale
        )
        cfg.new_memory_protection_days = _env_int(
            "LUCENT_MEMORY_DECAY_NEW_MEMORY_PROTECTION_DAYS",
            cfg.new_memory_protection_days,
        )
        cfg.recent_update_protection_days = _env_int(
            "LUCENT_MEMORY_DECAY_RECENT_UPDATE_PROTECTION_DAYS",
            cfg.recent_update_protection_days,
        )
        cfg.high_importance_threshold = _env_int(
            "LUCENT_MEMORY_DECAY_HIGH_IMPORTANCE_THRESHOLD",
            cfg.high_importance_threshold,
        )
        cfg.high_importance_protection_multiplier = _env_float(
            "LUCENT_MEMORY_DECAY_HIGH_IMPORTANCE_MULTIPLIER",
            cfg.high_importance_protection_multiplier,
        )
        cfg.active_goal_protection_multiplier = _env_float(
            "LUCENT_MEMORY_DECAY_ACTIVE_GOAL_MULTIPLIER",
            cfg.active_goal_protection_multiplier,
        )
        cfg.recent_update_protection_multiplier = _env_float(
            "LUCENT_MEMORY_DECAY_RECENT_UPDATE_MULTIPLIER",
            cfg.recent_update_protection_multiplier,
        )
        cfg.suggest_threshold = _env_float(
            "LUCENT_MEMORY_DECAY_SUGGEST_THRESHOLD", cfg.suggest_threshold
        )
        cfg.archive_threshold = _env_float(
            "LUCENT_MEMORY_DECAY_ARCHIVE_THRESHOLD", cfg.archive_threshold
        )
        cfg.batch_limit = _env_int("LUCENT_MEMORY_DECAY_BATCH_LIMIT", cfg.batch_limit)
        cfg.suggest_threshold = _clamp01(cfg.suggest_threshold)
        cfg.archive_threshold = _clamp01(cfg.archive_threshold)
        if cfg.suggest_threshold > cfg.archive_threshold:
            cfg.suggest_threshold = cfg.archive_threshold
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

    LEAVE = "leave-alone"
    SUGGEST = "suggest-cleanup"
    ARCHIVE_CANDIDATE = "archive-candidate"


def score_memory_decay(
    profile: MemoryDecayInput,
    *,
    config: DecayConfig | None = None,
    now: datetime | None = None,
) -> DecayScoreResult:
    """Score one memory for decay (0.0 = keep, 1.0 = strongest decay signal)."""
    cfg = config or DecayConfig.from_env()
    current = _utc(now)

    age_days = max(0, (current - _utc(profile.created_at)).days)
    updated_days = max(0, (current - _utc(profile.updated_at)).days)
    recency_source = profile.last_accessed_at or profile.updated_at
    recency_days = max(0, (current - _utc(recency_source)).days)

    age_factor = _clamp01(age_days / max(1, cfg.age_window_days))
    recency_factor = _clamp01(recency_days / max(1, cfg.recency_window_days))
    importance_keep_factor = _clamp01(profile.importance / 10.0)
    importance_decay_factor = 1.0 - importance_keep_factor
    access_keep_factor = _clamp01(log10(profile.access_count + 1) / log10(cfg.access_log_scale + 1))
    access_decay_factor = 1.0 - access_keep_factor

    # Weighted base score (higher means stronger decay signal).
    base_score = (
        (0.35 * age_factor)
        + (0.35 * recency_factor)
        + (0.2 * access_decay_factor)
        + (0.1 * importance_decay_factor)
    )

    type_bias = cfg.type_decay_bias.get(profile.memory_type, 1.0)
    score = base_score * type_bias
    reasons: list[str] = []
    protected = False

    if age_days <= cfg.new_memory_protection_days:
        protected = True
        score *= 0.15
        reasons.append("new-memory-protection")

    if updated_days <= cfg.recent_update_protection_days:
        protected = True
        score *= cfg.recent_update_protection_multiplier
        reasons.append("recent-update-protection")

    if profile.importance >= cfg.high_importance_threshold:
        protected = True
        score *= cfg.high_importance_protection_multiplier
        reasons.append("high-importance-protection")

    goal_status = str((profile.metadata or {}).get("status", "")).lower()
    is_active_goal = profile.memory_type == "goal" and goal_status in _GOAL_STATUS_ACTIVE
    if is_active_goal or profile.linked_to_active_goal:
        protected = True
        score *= cfg.active_goal_protection_multiplier
        reasons.append("active-goal-protection")

    score = _clamp01(score)
    action = classify_decay_action(score, cfg)

    return DecayScoreResult(
        memory_id=profile.memory_id,
        score=score,
        action=action,
        protected=protected,
        reasons=reasons,
        factors={
            "age_factor": age_factor,
            "recency_factor": recency_factor,
            "access_decay_factor": access_decay_factor,
            "importance_decay_factor": importance_decay_factor,
            "type_bias": type_bias,
            "base_score": _clamp01(base_score),
        },
        memory_type=profile.memory_type,
        importance=profile.importance,
        access_count=profile.access_count,
        last_accessed_at=profile.last_accessed_at,
        updated_at=profile.updated_at,
        created_at=profile.created_at,
    )


def classify_decay_action(score: float, config: DecayConfig | None = None) -> str:
    """Map score to action label using configurable thresholds."""
    cfg = config or DecayConfig.from_env()
    if score >= cfg.archive_threshold:
        return DecayAction.ARCHIVE_CANDIDATE
    if score >= cfg.suggest_threshold:
        return DecayAction.SUGGEST
    return DecayAction.LEAVE


def score_memories_batch(
    memories: list[MemoryDecayInput],
    *,
    config: DecayConfig | None = None,
    now: datetime | None = None,
) -> list[DecayScoreResult]:
    """Score many memories and return sorted highest decay first."""
    results = [score_memory_decay(profile, config=config, now=now) for profile in memories]
    return sorted(results, key=lambda item: item.score, reverse=True)


def dry_run_decay_report(
    scored: list[DecayScoreResult],
    *,
    config: DecayConfig | None = None,
) -> dict[str, Any]:
    """Summarize what would be flagged or archived without side effects."""
    cfg = config or DecayConfig.from_env()
    sorted_scored = sorted(scored, key=lambda item: item.score, reverse=True)

    archive_candidates = [r for r in sorted_scored if r.score >= cfg.archive_threshold]
    cleanup_suggestions = [
        r for r in sorted_scored if cfg.suggest_threshold <= r.score < cfg.archive_threshold
    ]

    return {
        "total_scored": len(sorted_scored),
        "thresholds": {
            "suggest_threshold": cfg.suggest_threshold,
            "archive_threshold": cfg.archive_threshold,
        },
        "counts": {
            "archive_candidates": len(archive_candidates),
            "cleanup_suggestions": len(cleanup_suggestions),
            "leave_alone": len(sorted_scored) - len(archive_candidates) - len(cleanup_suggestions),
            "protected": sum(1 for r in sorted_scored if r.protected),
        },
        "archive_candidates": [_result_to_dict(r) for r in archive_candidates],
        "cleanup_suggestions": [_result_to_dict(r) for r in cleanup_suggestions],
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

    This function only computes scoring/reporting. No archive/delete operations are performed.
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
    access_counts = await access_repo.get_access_counts(memory_ids)
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
            access_count=access_counts.get(item["id"], 0),
            tags=item.get("tags", []),
            metadata=(full_memories.get(item["id"], {}) or {}).get("metadata", {}),
            linked_to_active_goal=linked_to_active.get(item["id"], False),
        )
        for item in raw_memories
    ]
    scored = score_memories_batch(profiles, config=cfg, now=now)

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
