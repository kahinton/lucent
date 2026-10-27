"""Dashboard routes."""

import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.db import DashboardRepository, MemoryRepository, get_pool
from lucent.integrations.github_repo_access_service import GitHubRepoAccessService
from lucent.rbac import Role
from lucent.services.memory_access_service import MemoryAccessService

from ._shared import get_user_context

router = APIRouter()

CURRENT_WORK_STATUSES = ("pending", "planned", "in_progress", "review", "needs_rework")
APPROVED_APPROVAL_STATUSES = ("approved", "auto_approved")
DONE_MILESTONE_STATUSES = {"completed", "done"}
SKIPPED_MILESTONE_STATUSES = {"abandoned", "cancelled", "skipped"}


def _role_value(role: Role | str) -> str:
    """Return a stable string value for user roles."""
    return role if isinstance(role, str) else role.value


def _is_admin_or_owner(role: Role | str) -> bool:
    """Whether the current user should see operational admin panels."""
    return Role.from_string(_role_value(role)) >= Role.ADMIN


def _metadata_dict(value) -> dict:
    """Return metadata as a dict even when asyncpg hands back a JSON string."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _goal_title(goal: dict, metadata: dict) -> str:
    """Short, human-readable title for a goal memory."""
    explicit = metadata.get("title") or metadata.get("name")
    if isinstance(explicit, str) and explicit.strip():
        return _plain_markdown_label(explicit)
    for line in (goal.get("content") or "").splitlines():
        cleaned = _plain_markdown_label(line)
        if cleaned:
            return cleaned[:120]
    return "Untitled goal"


def _plain_markdown_label(value: str) -> str:
    """Collapse common Markdown syntax for compact, single-line labels."""
    cleaned = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", value.strip())
    cleaned = re.sub(r"^\s{0,3}(?:#{1,6}|[-*+]|\d+\.)\s+", "", cleaned)
    cleaned = re.sub(r"[*_`~]", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _milestone_label(milestone: dict | None) -> str | None:
    if not isinstance(milestone, dict):
        return None
    for key in ("description", "title", "name"):
        value = milestone.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _request_sort_value(req: dict):
    return req.get("completed_at") or req.get("updated_at") or req.get("created_at")


def _goal_summary(goal: dict, linked_requests: list[dict]) -> dict:
    """Summarize a goal for the collaborative dashboard overview."""
    metadata = _metadata_dict(goal.get("metadata"))
    milestones = metadata.get("milestones") or []
    if not isinstance(milestones, list):
        milestones = []

    completed = 0
    current_index = None
    current_label = None
    for idx, milestone in enumerate(milestones, start=1):
        if not isinstance(milestone, dict):
            continue
        status = str(milestone.get("status") or "active").lower()
        if status in DONE_MILESTONE_STATUSES:
            completed += 1
            continue
        if status not in SKIPPED_MILESTONE_STATUSES and current_index is None:
            current_index = idx
            current_label = _milestone_label(milestone)

    total = len(milestones)
    progress_pct = int((completed / total) * 100) if total else None
    status = str(metadata.get("status") or "active").lower()

    current_requests = [
        req
        for req in linked_requests
        if req.get("status") in CURRENT_WORK_STATUSES
        and req.get("approval_status") in APPROVED_APPROVAL_STATUSES
    ]
    completed_requests = [req for req in linked_requests if req.get("status") == "completed"]
    current_requests.sort(key=_request_sort_value, reverse=True)
    completed_requests.sort(key=_request_sort_value, reverse=True)

    return {
        "id": str(goal["id"]),
        "title": _goal_title(goal, metadata),
        "content": goal.get("content") or "",
        "status": status,
        "updated_at": goal.get("updated_at"),
        "milestone_total": total,
        "milestone_completed": completed,
        "progress_pct": progress_pct,
        "current_milestone_index": current_index,
        "current_milestone_label": current_label,
        "current_request": current_requests[0] if current_requests else None,
        "current_request_count": len(current_requests),
        "latest_completed_request": completed_requests[0] if completed_requests else None,
    }


async def _load_goal_requests(repo: DashboardRepository, org_id: UUID, goal_ids: list[UUID]) -> dict[str, list[dict]]:
    """Load requests linked to visible goal memories."""
    requests_by_goal = {str(goal_id): [] for goal_id in goal_ids}
    if not goal_ids:
        return requests_by_goal

    return await repo.list_goal_requests(org_id, goal_ids)


async def load_chat_overview(user) -> dict:
    """Load the small operational overview shown on the chat start screen."""
    pool = await get_pool()
    is_admin_or_owner = _is_admin_or_owner(user.role)
    memory_repo = MemoryRepository(pool)
    memory_access = MemoryAccessService(
        memory_repo,
        GitHubRepoAccessService(pool),
        organization_id=user.organization_id,
        is_admin=is_admin_or_owner,
    )
    goal_result = await memory_access.search(
        user_id=user.id,
        type="goal",
        limit=100,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
        include_archived=False,
    )
    goals = goal_result["memories"]
    goal_ids = [UUID(str(goal["id"])) for goal in goals]

    from lucent.db.requests import RequestRepository

    request_repo = RequestRepository(pool)
    pending_approval_count = await request_repo.count_pending_approvals(
        org_id=str(user.organization_id),
        requester_user_id=str(user.id),
        include_system=is_admin_or_owner,
    )
    request_result = await request_repo.list_requests(
        org_id=str(user.organization_id),
        limit=5,
        requester_user_id=str(user.id),
        include_system=is_admin_or_owner,
    )
    pending_approvals = [
        request
        for request in request_result["items"]
        if request.get("approval_status") == "pending_approval"
        and request.get("status") not in {"cancelled", "rejection_processing"}
    ][:3]

    heartbeat_row = None
    dashboard_repo = DashboardRepository(pool)
    goal_requests = await _load_goal_requests(
        dashboard_repo,
        user.organization_id,
        goal_ids,
    )
    heartbeat_row = await dashboard_repo.get_latest_heartbeat(user.organization_id)

    active_goals = [
        summary
        for goal in goals
        if (summary := _goal_summary(goal, goal_requests.get(str(goal["id"]), [])))[
            "status"
        ]
        == "active"
    ]
    min_datetime = datetime.min.replace(tzinfo=timezone.utc)
    active_goals.sort(
        key=lambda goal: (
            1 if goal["current_request"] else 0,
            goal["updated_at"] or min_datetime,
        ),
        reverse=True,
    )

    status_level = "offline"
    status_label = "Offline"
    if heartbeat_row and heartbeat_row["last_seen_at"]:
        heartbeat_at = heartbeat_row["last_seen_at"]
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
        heartbeat_age = datetime.now(timezone.utc) - heartbeat_at
        if heartbeat_age <= timedelta(minutes=20):
            status_level = "online"
            status_label = "Online"
        elif heartbeat_age <= timedelta(minutes=60):
            status_level = "stale"
            status_label = "Stale"

    return {
        "chat_goal_cards": active_goals[:3],
        "chat_active_goal_count": len(active_goals),
        "pending_approval_count": pending_approval_count,
        "pending_approvals": pending_approvals,
        "daemon_status_level": status_level,
        "daemon_status_label": status_label,
    }


# =============================================================================
# Dashboard
# =============================================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    await get_user_context(request)
    return RedirectResponse(url="/chat", status_code=303)

# End of dashboard routes.
