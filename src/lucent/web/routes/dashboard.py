"""Dashboard routes."""

import json
from datetime import datetime, timedelta, timezone

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

    # Daemon status (heartbeat + state summary)
    daemon_heartbeat = await memory_repo.search(
        tags=["daemon-heartbeat"],
        limit=1,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    heartbeat_memory = (
        daemon_heartbeat.get("memories", [])[0] if daemon_heartbeat.get("memories") else None
    )

    daemon_state = await memory_repo.search(
        tags=["daemon", "daemon-state"],
        limit=1,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    state_memory = daemon_state.get("memories", [])[0] if daemon_state.get("memories") else None

    heartbeat_data = {}
    heartbeat_timestamp = None
    if heartbeat_memory:
        try:
            heartbeat_data = json.loads(heartbeat_memory.get("content") or "{}")
        except (TypeError, ValueError):
            heartbeat_data = {}
        raw_ts = heartbeat_data.get("timestamp")
        if isinstance(raw_ts, str):
            try:
                heartbeat_timestamp = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if heartbeat_timestamp.tzinfo is None:
                    heartbeat_timestamp = heartbeat_timestamp.replace(tzinfo=timezone.utc)
            except ValueError:
                heartbeat_timestamp = None

    status_level = "offline"
    status_label = "Offline"
    status_detail = "No heartbeat detected"
    if heartbeat_timestamp:
        age = datetime.now(timezone.utc) - heartbeat_timestamp
        if age <= timedelta(minutes=20):
            status_level = "online"
            status_label = "Online"
            status_detail = f"Last heartbeat {int(age.total_seconds() // 60)} min ago"
        elif age <= timedelta(minutes=60):
            status_level = "stale"
            status_label = "Stale"
            status_detail = f"Last heartbeat {int(age.total_seconds() // 60)} min ago"
        else:
            status_level = "offline"
            status_label = "Offline"
            status_detail = f"Last heartbeat {int(age.total_seconds() // 60)} min ago"

    state_summary = ""
    if state_memory:
        state_lines = [
            line.strip()
            for line in (state_memory.get("content") or "").splitlines()
            if line.strip()
        ]
        for line in state_lines:
            if line.startswith("### ") or line.startswith("- "):
                state_summary = line.removeprefix("### ").removeprefix("- ").strip()
                break
        if not state_summary and state_lines:
            state_summary = state_lines[0]

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
            "daemon_status_level": status_level,
            "daemon_status_label": status_label,
            "daemon_status_detail": status_detail,
            "daemon_cycle_count": heartbeat_data.get("cycle_count"),
            "daemon_instance_id": heartbeat_data.get("instance_id"),
            "daemon_state_summary": state_summary,
        },
    )
