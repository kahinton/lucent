"""Web views for LLM token usage reporting."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.db import get_pool
from lucent.db.token_usage import TokenUsageRepository
from lucent.rbac import Role

from ._shared import get_user_context, templates

router = APIRouter()


@router.get("/settings/usage", response_class=HTMLResponse)
async def token_usage(
    request: Request,
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    scope: str = Query(default="user"),
):
    """Render personal or permitted organization token usage."""
    user = await get_user_context(request)
    today = datetime.now(timezone.utc).date()
    start_date = start or today - timedelta(days=29)
    end_date = end or today
    if end_date < start_date:
        raise HTTPException(422, "End date must not be before start date")
    can_view_organization = user.role >= Role.ADMIN
    organization_wide = scope == "organization" and can_view_organization
    report = await TokenUsageRepository(await get_pool()).get_report(
        user.organization_id,
        user_id=None if organization_wide else user.id,
        starts_at=datetime.combine(start_date, time.min, tzinfo=timezone.utc),
        ends_at=datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc),
    )
    return templates.TemplateResponse(
        request,
        "usage.html",
        {
            "user": user,
            "report": report,
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "scope": "organization" if organization_wide else "user",
            "can_view_organization": can_view_organization,
        },
    )


@router.get("/usage", include_in_schema=False)
async def legacy_token_usage_redirect(request: Request):
    """Redirect the former standalone usage page into Settings."""
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/settings/usage{query}", status_code=303)
