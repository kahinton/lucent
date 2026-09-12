"""Chat page route — dedicated full-page conversational interface."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._shared import get_user_context, templates
from .dashboard import load_chat_overview

router = APIRouter()


@router.get("/chat", response_class=HTMLResponse)
@router.get("/chat/{session_id}", response_class=HTMLResponse)
async def chat_page(request: Request, session_id: str | None = None):
    """Dedicated chat page with model/agent selection and tool visibility."""
    user = await get_user_context(request)
    overview = await load_chat_overview(user)

    # Active-project workspace context (?project=<id>): shows a project chip
    # so new chats land in the workspace. Fail-closed: unknown/foreign project
    # ids are ignored (chip absent, unfiled behavior) — never a wrong project.
    from lucent.db.projects import ProjectRepository
    from lucent.db import get_pool

    active_project = None
    project_param = request.query_params.get("project")
    if project_param:
        project = await ProjectRepository(await get_pool()).get_owned(
            project_param, org_id=str(user.organization_id), user_id=str(user.id)
        )
        if project:
            active_project = {"id": str(project["id"]), "name": project["name"]}

    return templates.TemplateResponse(
        request,
        "chat.html",
        {"user": user, "session_id": session_id, "active_project": active_project, **overview},
    )
