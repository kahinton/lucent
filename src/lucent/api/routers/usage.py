"""Organization-scoped LLM token usage reporting."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from lucent.api.deps import AuthenticatedUser, get_pool
from lucent.db.token_usage import TokenUsageRepository
from lucent.rbac import Role

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/tokens")
async def get_token_usage(
    user: AuthenticatedUser,
    starts_at: datetime | None = Query(default=None),
    ends_at: datetime | None = Query(default=None),
    organization_wide: bool = Query(default=False),
):
    """Report provider-recorded token usage grouped by model and user/model."""
    if starts_at and ends_at and ends_at <= starts_at:
        raise HTTPException(422, "ends_at must be after starts_at")
    if organization_wide and user.role < Role.ADMIN:
        raise HTTPException(403, "Organization-wide usage requires admin role or higher")

    report = await TokenUsageRepository(await get_pool()).get_report(
        user.organization_id,
        user_id=None if organization_wide else user.id,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    return {
        "scope": "organization" if organization_wide else "user",
        "organization_id": str(user.organization_id),
        "user_id": None if organization_wide else str(user.id),
        "starts_at": starts_at,
        "ends_at": ends_at,
        **report,
    }
