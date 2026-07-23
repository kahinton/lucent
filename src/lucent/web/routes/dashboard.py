"""Dashboard routes."""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from lucent.db import MemoryRepository, get_pool
from lucent.integrations.github_repo_access_service import GitHubRepoAccessService
from lucent.rbac import Role
from lucent.services.memory_access_service import MemoryAccessService

from ._shared import get_user_context, templates

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
        return explicit.strip()
    for line in (goal.get("content") or "").splitlines():
        cleaned = line.strip().lstrip("# ").strip()
        if cleaned:
            return cleaned[:120]
    return "Untitled goal"


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


async def _load_goal_requests(conn, org_id: UUID, goal_ids: list[UUID]) -> dict[str, list[dict]]:
    """Load requests linked to visible goal memories."""
    requests_by_goal = {str(goal_id): [] for goal_id in goal_ids}
    if not goal_ids:
        return requests_by_goal

    rows = await conn.fetch(
        """SELECT DISTINCT ON (goal_link.linked_goal_id, r.id)
                  goal_link.linked_goal_id,
                  r.id,
                  r.title,
                  r.status,
                  r.approval_status,
                  r.priority,
                  r.source,
                  r.goal_milestone_index,
                  r.created_at,
                  r.updated_at,
                  r.completed_at
           FROM requests r
           LEFT JOIN request_memories rm
             ON rm.request_id = r.id AND rm.relation = 'goal'
           CROSS JOIN LATERAL (
             SELECT COALESCE(r.goal_memory_id, rm.memory_id) AS linked_goal_id
           ) goal_link
           WHERE r.organization_id = $1
             AND goal_link.linked_goal_id = ANY($2::uuid[])
           ORDER BY goal_link.linked_goal_id, r.id, r.updated_at DESC""",
        org_id,
        goal_ids,
    )

    for row in rows:
        requests_by_goal.setdefault(str(row["linked_goal_id"]), []).append(dict(row))
    return requests_by_goal


# =============================================================================
# Dashboard
# =============================================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    user = await get_user_context(request)
    pool = await get_pool()
    org_id = str(user.organization_id)
    role_value = _role_value(user.role)
    is_admin_or_owner = _is_admin_or_owner(user.role)

    memory_repo = MemoryRepository(pool)
    memory_access = MemoryAccessService(
        memory_repo,
        GitHubRepoAccessService(pool),
        is_admin=is_admin_or_owner,
    )

    recent = await memory_access.search(
        user_id=user.id,
        limit=5,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    goal_result = await memory_access.search(
        user_id=user.id,
        type="goal",
        limit=100,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
        include_archived=True,
    )
    accessible_goals = goal_result["memories"]
    goal_ids = [UUID(str(goal["id"])) for goal in accessible_goals]

    from lucent.db.definitions import DefinitionRepository

    def_repo = DefinitionRepository(pool)
    agents_result = await def_repo.list_agents(
        org_id,
        status="active",
        requester_user_id=str(user.id),
        requester_role=role_value,
    )
    skills_result = await def_repo.list_skills(
        org_id,
        status="active",
        requester_user_id=str(user.id),
        requester_role=role_value,
    )
    active_mcp_servers = (
        await def_repo.list_mcp_servers(
            org_id,
            status="active",
            requester_user_id=str(user.id),
            requester_role=role_value,
        )
    )["total_count"]

    from lucent.db.requests import RequestRepository

    req_repo = RequestRepository(pool)
    ready_tasks = await req_repo.list_pending_tasks(org_id, limit=1)
    recent_events = await req_repo.get_recent_events(org_id, limit=8)
    recently_completed = await req_repo.list_recently_completed(org_id, hours=24, limit=5)
    requests_in_review = await req_repo.get_requests_in_review(org_id, limit=5)

    activity_summary: dict = {"requests": {}, "tasks": {}}
    active_work: dict = {"items": [], "total_count": 0}
    pending_approvals: dict = {"items": [], "total_count": 0}
    completed_24h_count = 0
    admin_summary: dict = {}
    pending_proposals: dict = {"agents": [], "skills": [], "mcp_servers": [], "total": 0}
    proposed_sandbox_templates: list[dict] = []

    async with pool.acquire() as conn:
        goal_requests_by_id = await _load_goal_requests(conn, user.organization_id, goal_ids)

        summary_row = await conn.fetchrow(
            """WITH current_requests AS (
                   SELECT id, status
                   FROM requests
                   WHERE organization_id = $1
                     AND status = ANY($2::text[])
                     AND approval_status = ANY($3::text[])
                 )
                 SELECT
                   (SELECT COUNT(*) FROM current_requests) AS open_requests,
                   (SELECT COUNT(*) FROM current_requests
                    WHERE status IN ('pending', 'planned')) AS pending_requests,
                   (SELECT COUNT(*) FROM current_requests
                    WHERE status IN ('in_progress', 'review', 'needs_rework'))
                    AS active_requests,
                   COUNT(t.id) FILTER (WHERE t.status IN ('claimed', 'running'))
                    AS running_tasks,
                   COUNT(t.id) FILTER (WHERE t.status IN ('pending', 'planned'))
                    AS queued_tasks,
                   COUNT(t.id) FILTER (WHERE t.status = 'completed')
                    AS completed_tasks,
                   COUNT(t.id) FILTER (WHERE t.status = 'failed') AS failed_tasks
                 FROM current_requests cr
                 LEFT JOIN tasks t ON t.request_id = cr.id""",
            user.organization_id,
            list(CURRENT_WORK_STATUSES),
            list(APPROVED_APPROVAL_STATUSES),
        )
        if summary_row:
            activity_summary = {
                "requests": {
                    "open": summary_row["open_requests"] or 0,
                    "pending": summary_row["pending_requests"] or 0,
                    "active": summary_row["active_requests"] or 0,
                },
                "tasks": {
                    "running": summary_row["running_tasks"] or 0,
                    "queued": summary_row["queued_tasks"] or 0,
                    "ready": ready_tasks["total_count"],
                    "completed": summary_row["completed_tasks"] or 0,
                    "failed": summary_row["failed_tasks"] or 0,
                },
            }

        active_count_row = await conn.fetchrow(
            """SELECT COUNT(*) AS total
               FROM requests
               WHERE organization_id = $1
                 AND status = ANY($2::text[])
                 AND approval_status = ANY($3::text[])""",
            user.organization_id,
            list(CURRENT_WORK_STATUSES),
            list(APPROVED_APPROVAL_STATUSES),
        )
        active_rows = await conn.fetch(
            """SELECT r.id, r.title, r.description, r.status, r.priority,
                      r.source, r.created_at, r.updated_at,
                      COUNT(t.id) FILTER (WHERE t.status = 'pending')
                        AS tasks_pending,
                      COUNT(t.id) FILTER (WHERE t.status = 'planned')
                        AS tasks_planned,
                      COUNT(t.id) FILTER (WHERE t.status IN ('claimed', 'running'))
                        AS tasks_running,
                      COUNT(t.id) FILTER (WHERE t.status = 'completed')
                        AS tasks_completed,
                      COUNT(t.id) FILTER (WHERE t.status = 'failed') AS tasks_failed,
                      COUNT(t.id) AS tasks_total
               FROM requests r
               LEFT JOIN tasks t ON t.request_id = r.id
               WHERE r.organization_id = $1
                 AND r.status = ANY($2::text[])
                 AND r.approval_status = ANY($3::text[])
               GROUP BY r.id
               ORDER BY
                 CASE r.status
                   WHEN 'in_progress' THEN 0
                   WHEN 'needs_rework' THEN 1
                   WHEN 'review' THEN 2
                   WHEN 'pending' THEN 3
                   WHEN 'planned' THEN 4
                   ELSE 5
                 END,
                 CASE r.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                 WHEN 'medium' THEN 2 ELSE 3 END,
                 r.updated_at DESC
               LIMIT 5""",
            user.organization_id,
            list(CURRENT_WORK_STATUSES),
            list(APPROVED_APPROVAL_STATUSES),
        )
        active_work = {
            "items": [dict(row) for row in active_rows],
            "total_count": active_count_row["total"] if active_count_row else 0,
        }

        pending_approval_count = await conn.fetchval(
            """SELECT COUNT(*)
               FROM requests
               WHERE organization_id = $1
                 AND approval_status = 'pending_approval'
                 AND status NOT IN ('cancelled', 'rejection_processing')""",
            user.organization_id,
        ) or 0
        approval_rows = await conn.fetch(
            """SELECT r.id, r.title, r.description, r.source, r.priority,
                      r.created_at, r.updated_at,
                      (SELECT COUNT(*) FROM tasks t WHERE t.request_id = r.id)
                        AS task_count
               FROM requests r
               WHERE r.organization_id = $1
                 AND r.approval_status = 'pending_approval'
                 AND r.status NOT IN ('cancelled', 'rejection_processing')
               ORDER BY
                 CASE r.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                 WHEN 'medium' THEN 2 ELSE 3 END,
                 r.created_at
               LIMIT 5""",
            user.organization_id,
        )
        pending_approvals = {
            "items": [dict(row) for row in approval_rows],
            "total_count": pending_approval_count,
        }

        completed_24h_count = await conn.fetchval(
            """SELECT COUNT(*) FROM requests
               WHERE organization_id = $1
                 AND status = 'completed'
                 AND completed_at > NOW() - INTERVAL '24 hours'""",
            user.organization_id,
        ) or 0

        if is_admin_or_owner:
            admin_row = await conn.fetchrow(
                """SELECT
                     (SELECT COUNT(*) FROM users
                      WHERE organization_id = $1 AND is_active = true)
                        AS active_users,
                     (SELECT COUNT(*) FROM requests
                      WHERE organization_id = $1
                        AND status = 'failed'
                        AND updated_at > NOW() - INTERVAL '7 days')
                        AS failed_requests_7d,
                     (SELECT COUNT(*) FROM tasks
                      WHERE organization_id = $1
                        AND status = 'failed'
                        AND updated_at > NOW() - INTERVAL '7 days')
                        AS failed_tasks_7d,
                     (SELECT COUNT(*) FROM schedules
                      WHERE organization_id = $1
                        AND enabled = true
                        AND status = 'active') AS active_schedules,
                     (SELECT COUNT(*) FROM sandboxes
                      WHERE organization_id = $1
                        AND status NOT IN ('destroyed', 'stopped'))
                        AS live_sandboxes""",
                user.organization_id,
            )
            admin_summary = dict(admin_row) if admin_row else {}

    goal_summaries = [
        _goal_summary(goal, goal_requests_by_id.get(str(goal["id"]), []))
        for goal in accessible_goals
    ]
    active_goal_cards = [goal for goal in goal_summaries if goal["status"] == "active"]
    paused_goal_cards = [goal for goal in goal_summaries if goal["status"] == "paused"]
    completed_goal_count = sum(
        1 for goal in goal_summaries if goal["status"] in DONE_MILESTONE_STATUSES
    )
    milestone_total = sum(goal["milestone_total"] for goal in active_goal_cards)
    milestone_completed = sum(goal["milestone_completed"] for goal in active_goal_cards)
    min_datetime = datetime.min.replace(tzinfo=timezone.utc)
    active_goal_cards.sort(
        key=lambda goal: (1 if goal["current_request"] else 0, goal["updated_at"] or min_datetime),
        reverse=True,
    )
    paused_goal_cards.sort(key=lambda goal: goal["updated_at"] or min_datetime, reverse=True)

    active_request_count = active_work["total_count"]
    pending_approval_count = pending_approvals["total_count"]
    review_count = requests_in_review["total_count"]

    if is_admin_or_owner:
        pending_proposals = await def_repo.get_pending_proposals(
            org_id,
            requester_user_id=str(user.id),
            requester_role=role_value,
        )
        try:
            from lucent.db.sandbox_template import SandboxTemplateRepository

            sandbox_template_repo = SandboxTemplateRepository(pool)
            proposed_sandbox_templates = await sandbox_template_repo.list_proposed(org_id)
        except Exception:
            proposed_sandbox_templates = []

    heartbeat_row = None
    if user.organization_id:
        async with pool.acquire() as conn:
            heartbeat_row = await conn.fetchrow(
                """SELECT instance_id, hostname, pid, roles, status,
                          last_seen_at, metadata
                   FROM daemon_instances
                   WHERE organization_id = $1::uuid
                   ORDER BY last_seen_at DESC
                   LIMIT 1""",
                user.organization_id,
            )

    daemon_state = await memory_access.search(
        user_id=user.id,
        tags=["daemon", "daemon-state"],
        limit=1,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    state_memory = daemon_state.get("memories", [])[0] if daemon_state.get("memories") else None

    heartbeat_data = {}
    heartbeat_timestamp = None
    if heartbeat_row:
        heartbeat_data = _metadata_dict(heartbeat_row["metadata"])
        heartbeat_data.update(
            {
                "instance_id": heartbeat_row["instance_id"],
                "hostname": heartbeat_row["hostname"],
                "pid": heartbeat_row["pid"],
                "roles": heartbeat_row["roles"],
                "status": heartbeat_row["status"],
            }
        )
        heartbeat_timestamp = heartbeat_row["last_seen_at"]
        if heartbeat_timestamp and heartbeat_timestamp.tzinfo is None:
            heartbeat_timestamp = heartbeat_timestamp.replace(tzinfo=timezone.utc)

    status_level = "offline"
    status_label = "Offline"
    status_detail = "No heartbeat detected"
    if heartbeat_timestamp:
        age = datetime.now(timezone.utc) - heartbeat_timestamp
        minutes = int(age.total_seconds() // 60)
        status_detail = f"Last heartbeat {minutes} min ago"
        if age <= timedelta(minutes=20):
            status_level = "online"
            status_label = "Online"
        elif age <= timedelta(minutes=60):
            status_level = "stale"
            status_label = "Stale"

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
            "total_memories": recent["total_count"],
            "active_agents": agents_result["total_count"],
            "active_skills": skills_result["total_count"],
            "active_mcp_servers": active_mcp_servers,
            "active_goal_count": len(active_goal_cards),
            "paused_goal_count": len(paused_goal_cards),
            "completed_goal_count": completed_goal_count,
            "goal_milestone_total": milestone_total,
            "goal_milestone_completed": milestone_completed,
            "goal_cards": active_goal_cards[:5],
            "paused_goal_cards": paused_goal_cards[:3],
            "activity_summary": activity_summary,
            "active_work": active_work["items"],
            "active_request_count": active_request_count,
            "recent_events": recent_events,
            "recently_completed": recently_completed,
            "completed_24h_count": completed_24h_count,
            "requests_in_review": requests_in_review["items"],
            "review_count": review_count,
            "pending_approval_count": pending_approval_count,
            "pending_approvals": pending_approvals["items"],
            "daemon_status_level": status_level,
            "daemon_status_label": status_label,
            "daemon_status_detail": status_detail,
            "daemon_cycle_count": heartbeat_data.get("cycle_count"),
            "daemon_instance_id": heartbeat_data.get("instance_id"),
            "daemon_state_summary": state_summary,
            "is_admin_or_owner": is_admin_or_owner,
            "admin_summary": admin_summary,
            "pending_proposals": pending_proposals,
            "proposed_sandbox_templates": proposed_sandbox_templates,
        },
    )


# End of dashboard routes.
