"""Web UI routes for Projects — server-rendered pages over ProjectRepository.

Every handler authenticates the caller via the session cookie and passes the
authenticated (organization_id, id) pair into ProjectRepository, where tenant
scoping is enforced by construction. There is no unscoped code path.

Pages follow the files.html conventions: base.html layout, form POSTs with
CSRF double-submit, RedirectResponse after mutations.
"""

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.db import get_pool
from lucent.db.projects import (
    ProjectNotFoundError,
    ProjectRepository,
    get_authorized_projects_pool,
)

from ._shared import _check_csrf, _get_csrf_for_request, get_user_context, templates

router = APIRouter()


async def _authorized_repo(pool, user) -> ProjectRepository:
    """Build a ProjectRepository using auth-ID-aware project reads."""
    authorized_pool = await get_authorized_projects_pool(
        pool,
        {
            "id": str(user.id),
            "organization_id": str(user.organization_id),
        },
    )
    return ProjectRepository(authorized_pool)


@router.get("/projects", response_class=HTMLResponse)
async def projects_list(request: Request):
    user = await get_user_context(request)
    repo = await _authorized_repo(await get_pool(), user)
    result = await repo.list_owned()
    return templates.TemplateResponse(
        request,
        "projects.html",
        {
            "user": user,
            "projects": result["items"],
            "total_count": result["total_count"],
            "csrf_token": _get_csrf_for_request(request),
        },
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: str):
    user = await get_user_context(request)
    pool = await get_pool()
    authorized_repo = await _authorized_repo(pool, user)
    project = await authorized_repo.get_owned_with_counts(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    repo = ProjectRepository(pool)
    sessions = (await repo.list_sessions_in_project(
        project_id, org_id=str(user.organization_id), user_id=str(user.id)
    ))["items"]
    files = (await repo.list_files_in_project(
        project_id, org_id=str(user.organization_id), user_id=str(user.id)
    ))["items"]
    memories = (await repo.list_memories_in_project(
        project_id, org_id=str(user.organization_id), user_id=str(user.id)
    ))["items"]
    interactions = (await repo.list_interactions_in_project(
        project_id, org_id=str(user.organization_id), user_id=str(user.id)
    ))["items"]
    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "user": user,
            "project": project,
            "sessions": sessions,
            "files": files,
            "memories": memories,
            "interactions": interactions,
            "csrf_token": _get_csrf_for_request(request),
        },
    )


@router.post("/projects/create")
async def project_create(
    request: Request,
    name: str = Form(""),
    csrf_token: str = Form(alias="csrf_token"),
):
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    if not name.strip():
        raise HTTPException(422, "Project name is required")
    project = await ProjectRepository(await get_pool()).create(
        org_id=str(user.organization_id),
        user_id=str(user.id),
        name=name.strip(),
    )
    return RedirectResponse(f"/projects/{project['id']}", status_code=303)


@router.post("/projects/{project_id}/rename")
async def project_rename(
    request: Request,
    project_id: str,
    name: str = Form(""),
    csrf_token: str = Form(alias="csrf_token"),
):
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    if not name.strip():
        raise HTTPException(422, "Project name is required")
    project = await ProjectRepository(await get_pool()).rename(
        project_id,
        org_id=str(user.organization_id),
        user_id=str(user.id),
        name=name.strip(),
    )
    if not project:
        raise HTTPException(404, "Project not found")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/instructions")
async def project_instructions(
    request: Request,
    project_id: str,
    instructions: str = Form(""),
    csrf_token: str = Form(alias="csrf_token"),
):
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    try:
        updated = await ProjectRepository(await get_pool()).set_instructions(
            project_id,
            org_id=str(user.organization_id),
            user_id=str(user.id),
            instructions=instructions,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(404, "Project not found") from exc
    if not updated:
        raise HTTPException(404, "Project not found")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/delete")
async def project_delete(
    request: Request,
    project_id: str,
    csrf_token: str = Form(alias="csrf_token"),
):
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    deleted = await ProjectRepository(await get_pool()).delete_owned(
        project_id, org_id=str(user.organization_id), user_id=str(user.id)
    )
    if not deleted:
        raise HTTPException(404, "Project not found")
    return RedirectResponse("/projects", status_code=303)


@router.post("/projects/{project_id}/remove-session")
async def project_remove_session(
    request: Request,
    project_id: str,
    session_id: str = Form(alias="session_id"),
    csrf_token: str = Form(alias="csrf_token"),
):
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    try:
        await ProjectRepository(await get_pool()).set_sessions_project_bulk(
            [session_id],
            None,
            org_id=str(user.organization_id),
            user_id=str(user.id),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(404, "Project not found") from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/remove-file")
async def project_remove_file(
    request: Request,
    project_id: str,
    file_id: str = Form(alias="file_id"),
    csrf_token: str = Form(alias="csrf_token"),
):
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    try:
        await ProjectRepository(await get_pool()).set_files_project_bulk(
            [file_id],
            None,
            org_id=str(user.organization_id),
            user_id=str(user.id),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(404, "Project not found") from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/new-chat")
async def project_new_chat(
    request: Request,
    project_id: str,
    csrf_token: str = Form(alias="csrf_token"),
):
    """File a new chat into this project BEFORE any message is sent.

    Creates the session server-side with project_id set — stronger than the
    composer's lazy createSession-on-first-send, which stays as the fallback
    path for chats started from the chat page itself.
    """
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    pool = await get_pool()
    authorized_repo = await _authorized_repo(pool, user)
    project = await authorized_repo.get_owned(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    from lucent.db.llm_sessions import LLMSessionRepository

    session = await LLMSessionRepository(pool).create_session(
        org_id=str(user.organization_id),
        user_id=str(user.id),
        kind="chat",
    )
    await ProjectRepository(pool).set_session_project(
        session["id"],
        project["id"],
        org_id=str(user.organization_id),
        user_id=str(user.id),
    )
    return RedirectResponse(f"/chat/{session['id']}", status_code=303)


@router.get("/projects/{project_id}/add-chats", response_class=HTMLResponse)
async def project_add_chats_page(request: Request, project_id: str):
    user = await get_user_context(request)
    pool = await get_pool()
    authorized_repo = await _authorized_repo(pool, user)
    project = await authorized_repo.get_owned(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    repo = ProjectRepository(pool)
    sessions = (await repo.list_unfiled_sessions(
        org_id=str(user.organization_id), user_id=str(user.id)
    ))["items"]
    return templates.TemplateResponse(
        request,
        "project_add_chats.html",
        {
            "user": user,
            "project": project,
            "sessions": sessions,
            "csrf_token": _get_csrf_for_request(request),
        },
    )


@router.post("/projects/{project_id}/add-chats")
async def project_add_chats(
    request: Request,
    project_id: str,
    csrf_token: str = Form(alias="csrf_token"),
):
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    form = await request.form()
    session_ids = [str(v) for v in form.getlist("session_ids") if str(v).strip()]
    if session_ids:
        try:
            await ProjectRepository(await get_pool()).set_sessions_project_bulk(
                session_ids,
                project_id,
                org_id=str(user.organization_id),
                user_id=str(user.id),
            )
        except ProjectNotFoundError as exc:
            raise HTTPException(404, "Project not found") from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}/add-files", response_class=HTMLResponse)
async def project_add_files_page(request: Request, project_id: str):
    user = await get_user_context(request)
    pool = await get_pool()
    authorized_repo = await _authorized_repo(pool, user)
    project = await authorized_repo.get_owned(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    repo = ProjectRepository(pool)
    files = (await repo.list_unfiled_files(
        org_id=str(user.organization_id), user_id=str(user.id)
    ))["items"]
    return templates.TemplateResponse(
        request,
        "project_add_files.html",
        {
            "user": user,
            "project": project,
            "files": files,
            "csrf_token": _get_csrf_for_request(request),
        },
    )


@router.post("/projects/{project_id}/add-files")
async def project_add_files(
    request: Request,
    project_id: str,
    csrf_token: str = Form(alias="csrf_token"),
):
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    form = await request.form()
    file_ids = [str(v) for v in form.getlist("file_ids") if str(v).strip()]
    if file_ids:
        try:
            await ProjectRepository(await get_pool()).set_files_project_bulk(
                file_ids,
                project_id,
                org_id=str(user.organization_id),
                user_id=str(user.id),
            )
        except ProjectNotFoundError as exc:
            raise HTTPException(404, "Project not found") from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)

@router.post("/projects/{project_id}/remove-memory")
async def project_remove_memory(
    request: Request,
    project_id: str,
    memory_id: str = Form(alias="memory_id"),
    csrf_token: str = Form(alias="csrf_token"),
):
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    try:
        await ProjectRepository(await get_pool()).set_memories_project_bulk(
            [memory_id],
            None,
            org_id=str(user.organization_id),
            user_id=str(user.id),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(404, "Project not found") from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/remove-interaction")
async def project_remove_interaction(
    request: Request,
    project_id: str,
    interaction_id: str = Form(alias="interaction_id"),
    csrf_token: str = Form(alias="csrf_token"),
):
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    try:
        await ProjectRepository(await get_pool()).set_interactions_project_bulk(
            [interaction_id],
            None,
            org_id=str(user.organization_id),
            user_id=str(user.id),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(404, "Project not found") from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


MEMORY_TYPES = ("goal", "technical", "experience", "individual")


@router.get("/projects/{project_id}/add-memories", response_class=HTMLResponse)
async def project_add_memories_page(
    request: Request, project_id: str, type: str = ""
):
    user = await get_user_context(request)
    pool = await get_pool()
    authorized_repo = await _authorized_repo(pool, user)
    project = await authorized_repo.get_owned(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    repo = ProjectRepository(pool)
    type_filter = type if type in MEMORY_TYPES else ""
    unfiled = await repo.list_unfiled_memories(
        org_id=str(user.organization_id), user_id=str(user.id), limit=200
    )
    if type_filter:
        memories = [m for m in unfiled["items"] if m.get("type") == type_filter]
    else:
        memories = unfiled["items"]
    return templates.TemplateResponse(
        request,
        "project_add_memories.html",
        {
            "user": user,
            "project": project,
            "memories": memories,
            "unfiled_total": unfiled["total_count"],
            "type_filter": type_filter,
            "memory_types": MEMORY_TYPES,
            "csrf_token": _get_csrf_for_request(request),
        },
    )


@router.post("/projects/{project_id}/add-memories")
async def project_add_memories(
    request: Request,
    project_id: str,
    csrf_token: str = Form(alias="csrf_token"),
):
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    form = await request.form()
    memory_ids = [str(v) for v in form.getlist("memory_ids") if str(v).strip()]
    if memory_ids:
        try:
            await ProjectRepository(await get_pool()).set_memories_project_bulk(
                memory_ids,
                project_id,
                org_id=str(user.organization_id),
                user_id=str(user.id),
            )
        except ProjectNotFoundError as exc:
            raise HTTPException(404, "Project not found") from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}/add-interactions", response_class=HTMLResponse)
async def project_add_interactions_page(request: Request, project_id: str):
    user = await get_user_context(request)
    pool = await get_pool()
    authorized_repo = await _authorized_repo(pool, user)
    project = await authorized_repo.get_owned(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    repo = ProjectRepository(pool)
    interactions = (await repo.list_unfiled_interactions(
        org_id=str(user.organization_id), user_id=str(user.id)
    ))["items"]
    return templates.TemplateResponse(
        request,
        "project_add_interactions.html",
        {
            "user": user,
            "project": project,
            "interactions": interactions,
            "csrf_token": _get_csrf_for_request(request),
        },
    )


@router.post("/projects/{project_id}/add-interactions")
async def project_add_interactions(
    request: Request,
    project_id: str,
    csrf_token: str = Form(alias="csrf_token"),
):
    await _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    form = await request.form()
    interaction_ids = [str(v) for v in form.getlist("interaction_ids") if str(v).strip()]
    if interaction_ids:
        try:
            await ProjectRepository(await get_pool()).set_interactions_project_bulk(
                interaction_ids,
                project_id,
                org_id=str(user.organization_id),
                user_id=str(user.id),
            )
        except ProjectNotFoundError as exc:
            raise HTTPException(404, "Project not found") from exc
    return RedirectResponse(f"/projects/{project_id}", status_code=303)
