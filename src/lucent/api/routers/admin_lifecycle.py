"""Admin endpoint exposing memory lifecycle / vitality observability data.

Phase-2 of the memory-lifecycle work introduced shadow-mode vitality scoring
and an opt-in vitality-boosted search ranking. To evaluate the effect before
flipping the default, operators need visibility into the vitality score
distribution and lifecycle stage breakdown for the org. This router exposes
``GET /api/admin/lifecycle/vitality-stats`` for that purpose.

The endpoint is read-only and admin-gated. Owners (a strictly higher role)
may also call it via ``require_role(Role.ADMIN)``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from lucent.api.deps import CurrentUser, Role, require_role
from lucent.db import MemoryRepository, get_pool
from lucent.settings import (
    search_vitality_boost_alpha,
    search_vitality_boost_enabled,
    search_vitality_boost_log_sample_rate,
    search_vitality_boost_log_top_n,
)

router = APIRouter()


@router.get("/vitality-stats")
async def get_vitality_stats(
    user: CurrentUser = Depends(require_role(Role.ADMIN)),
) -> dict[str, Any]:
    """Return lifecycle stage distribution + vitality histogram for the org.

    Scoped to the caller's organization. Returns both the raw stats payload
    (delegated to ``MemoryRepository.get_lifecycle_stats``) and a snapshot of
    the current Phase-2 feature flag values so the caller can correlate
    distribution shifts with configuration changes.
    """
    pool = await get_pool()
    repo = MemoryRepository(pool)
    stats = await repo.get_lifecycle_stats(
        organization_id=user.organization_id,
    )
    return {
        **stats,
        "organization_id": str(user.organization_id) if user.organization_id else None,
        "flags": {
            "vitality_boost_enabled": search_vitality_boost_enabled(),
            "vitality_boost_alpha": search_vitality_boost_alpha(),
            "vitality_boost_log_sample_rate": search_vitality_boost_log_sample_rate(),
            "vitality_boost_log_top_n": search_vitality_boost_log_top_n(),
        },
    }
