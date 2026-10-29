"""Chat page route — dedicated full-page conversational interface."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ._shared import _check_csrf, get_user_context, templates
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


class _InteractionAnswerEntry(BaseModel):
    """One answer inside a batched clarifying-question response."""

    answer: str = Field(default="", max_length=8000)
    choice: str | None = Field(default=None, max_length=500)


class _InteractionAnswer(BaseModel):
    """The browser's answer to a tool-approval prompt or clarifying questions."""

    session_id: str | None = Field(default=None, max_length=64)
    decision: str | None = Field(default=None, pattern=r"^(approve|deny)$")
    remember: bool = False
    note: str = Field(default="", max_length=2000)
    answer: str = Field(default="", max_length=8000)
    choice: str | None = Field(default=None, max_length=500)
    #: Parallel to the questions the card showed. Batched answers arrive here;
    #: the single `answer`/`choice` fields above stay for one-question cards and
    #: older clients.
    answers: list[_InteractionAnswerEntry] | None = Field(default=None, max_length=10)


@router.post("/chat/interactions/{request_id}/resolve")
async def resolve_chat_interaction(
    request: Request, request_id: str, body: _InteractionAnswer
):
    """Deliver the person's answer to a waiting tool call or questions.

    This is a session-cookie route (not the bearer-key API), because the caller
    is the chat page itself. CSRF travels in the ``X-CSRF-Token`` header, matching
    the other session-cookie JSON routes.

    Scope note: the waiting coroutine and this route share one process because the
    server runs a single uvicorn worker. A multi-replica deployment would need the
    wait moved to a durable channel; that boundary is documented in
    ``lucent.llm.tool_approval``.
    """
    token = (
        request.headers.get("X-CSRF-Token")
        or request.headers.get("X-CSRFToken")
        or ""
    )
    await _check_csrf(request, form_token=token)
    user = await get_user_context(request)

    from lucent.llm.tool_approval import interaction_broker

    if body.decision is not None:
        resolved = interaction_broker.resolve(
            request_id,
            answer={
                "decision": body.decision,
                "remember": bool(body.remember),
                "note": body.note,
            },
            session_id=body.session_id,
            user_id=str(user.id),
        )
        if resolved is None:
            raise HTTPException(
                status_code=409, detail="This tool request is no longer waiting"
            )
        return {"request_id": request_id, "decision": body.decision, "status": "resolved"}

    answers = (
        [entry.model_dump() for entry in body.answers]
        if body.answers is not None
        else None
    )
    resolved = interaction_broker.resolve(
        request_id,
        answer={"answer": body.answer, "choice": body.choice, "answers": answers},
        session_id=body.session_id,
        user_id=str(user.id),
    )
    if resolved is None:
        raise HTTPException(status_code=409, detail="This question is no longer waiting")
    return {"request_id": request_id, "answered": True, "status": "resolved"}
