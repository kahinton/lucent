"""Chat page route — dedicated full-page conversational interface."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._shared import get_user_context, templates

router = APIRouter()


@router.get("/chat", response_class=HTMLResponse)
@router.get("/chat/{session_id}", response_class=HTMLResponse)
async def chat_page(request: Request, session_id: str | None = None):
    """Dedicated chat page with model/agent selection and tool visibility."""
    user = await get_user_context(request)
    return templates.TemplateResponse(
        request,
        "chat.html",
        {"user": user, "session_id": session_id},
    )
