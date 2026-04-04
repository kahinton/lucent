"""Usage analytics routes for memory access insights."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from lucent.db import AccessRepository, MemoryRepository, get_pool
from lucent.mode import is_team_mode

from ._shared import get_user_context, templates

router = APIRouter()


@router.get("/memories/analytics", response_class=HTMLResponse)
async def memory_usage_analytics(request: Request, bucket: str = "day"):
    """Usage analytics page for memory access trends and cleanup candidates."""
    user = await get_user_context(request)
    pool = await get_pool()

    access_repo = AccessRepository(pool)
    memory_repo = MemoryRepository(pool)

    selected_bucket = bucket if bucket in {"hour", "day", "week"} else "day"

    # Match API behavior: in team mode show user-scoped by default.
    least_accessed = await access_repo.get_least_accessed(user_id=user.id, limit=20)
    frequency = await access_repo.get_access_frequency(
        bucket=selected_bucket,
        user_id=user.id,
        limit=30,
    )

    # Attach memory summaries for display
    memory_ids = [item["memory_id"] for item in least_accessed]
    memories_result = await memory_repo.search(
        memory_ids=memory_ids,
        limit=len(memory_ids) or 1,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    memory_map = {m["id"]: m for m in memories_result["memories"]}

    least_accessed_items = []
    for item in least_accessed:
        memory = memory_map.get(item["memory_id"])
        if memory is None:
            continue
        least_accessed_items.append(
            {
                "memory_id": item["memory_id"],
                "access_count": item["access_count"],
                "last_accessed": item["last_accessed"],
                "content": memory.get("content", ""),
                "type": memory.get("type", "unknown"),
                "username": memory.get("username", "unknown"),
            }
        )

    # Build simple bar chart data
    frequency_points = list(reversed(frequency))  # oldest -> newest for chart
    max_count = max((point["access_count"] for point in frequency_points), default=1)
    chart_data = [
        {
            "label": point["bucket_start"].strftime("%Y-%m-%d %H:%M")
            if selected_bucket == "hour"
            else point["bucket_start"].strftime("%Y-%m-%d"),
            "count": point["access_count"],
            "width_pct": max(6, int((point["access_count"] / max_count) * 100)) if max_count else 0,
        }
        for point in frequency_points
    ]

    never_accessed_count = sum(1 for item in least_accessed_items if item["access_count"] == 0)

    return templates.TemplateResponse(
        request,
        "memories_analytics.html",
        {
            "user": user,
            "team_mode": is_team_mode,
            "selected_bucket": selected_bucket,
            "chart_data": chart_data,
            "least_accessed_items": least_accessed_items,
            "never_accessed_count": never_accessed_count,
        },
    )

