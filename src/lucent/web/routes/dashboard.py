"""Dashboard routes."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from lucent.db import AccessRepository, MemoryRepository, get_pool
from lucent.mode import is_team_mode

from ._shared import get_user_context, templates

router = APIRouter()


# =============================================================================
# Dashboard
# =============================================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    user = await get_user_context(request)
    pool = await get_pool()

    # Get stats
    memory_repo = MemoryRepository(pool)

    # Recent memories
    recent = await memory_repo.search(
        limit=5,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    # Most accessed (team mode only)
    most_accessed = []
    if is_team_mode():
        access_repo = AccessRepository(pool)
        most_accessed = await access_repo.get_most_accessed(
            user_id=user.id,
            limit=5,
        )

    # Get tag stats (with access control)
    tags = await memory_repo.get_existing_tags(
        limit=10,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    # Agent/skill stats
    from lucent.db.definitions import DefinitionRepository

    def_repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)
    role_value = user.role if isinstance(user.role, str) else user.role.value
    agents = (
        await def_repo.list_agents(
            org_id,
            status="active",
            requester_user_id=str(user.id),
            requester_role=role_value,
        )
    )["items"]
    skills = (
        await def_repo.list_skills(
            org_id,
            status="active",
            requester_user_id=str(user.id),
            requester_role=role_value,
        )
    )["items"]
    active_agents = len(agents)
    active_skills = len(skills)

    # Active requests count (from request tracking system)
    from lucent.db.requests import RequestRepository

    req_repo = RequestRepository(pool)
    active_requests = await req_repo.list_requests(
        org_id=str(user.organization_id),
        status="in_progress",
    )
    pending_requests = await req_repo.list_requests(
        org_id=str(user.organization_id),
        status="pending",
    )
    active_request_count = active_requests["total_count"] + pending_requests["total_count"]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "recent_memories": recent["memories"],
            "most_accessed": most_accessed,
            "top_tags": tags,
            "total_memories": recent["total_count"],
            "active_agents": active_agents,
            "active_skills": active_skills,
            "active_request_count": active_request_count,
        },
    )
