"""API endpoints for Projects.

Project reads use the auth-ID-aware pool; mutations use the legacy
explicitly scoped pool until write policies are introduced.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from lucent.auth_providers import SESSION_COOKIE_NAME, validate_session
from lucent.db import get_pool
from lucent.db.projects import (
    ProjectNotFoundError,
    ProjectRepository,
    get_authorized_projects_pool,
)

router = APIRouter(prefix="/projects", tags=["projects"])


async def _get_session_user(request: Request):
    """Authenticate via session cookie (same as the chat router)."""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        raise HTTPException(401, "Not authenticated")
    pool = await get_pool()
    user = await validate_session(pool, session_token)
    if not user:
        raise HTTPException(401, "Session expired")
    return user, pool


def _scope(user) -> tuple[str, str]:
    """The only scoping pair these handlers use: the authenticated identity."""
    return str(user["organization_id"]), str(user["id"])


def _project_dict(row) -> dict:
    item = dict(row)
    for key in ("id", "organization_id", "user_id"):
        if item.get(key) is not None:
            item[key] = str(item[key])
    return item


async def _owned_project_or_404(
    repo: ProjectRepository,
    project_id: UUID,
) -> dict:
    """Load a project visible through the caller's auth context or 404."""
    project = await repo.get_owned(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    instructions: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    instructions: str | None = None


class ProjectSessionIds(BaseModel):
    session_ids: list[UUID] = Field(..., min_length=1, max_length=100)


class ProjectFileIds(BaseModel):
    file_ids: list[UUID] = Field(..., min_length=1, max_length=100)


class ProjectMemoryIds(BaseModel):
    memory_ids: list[UUID] = Field(..., min_length=1, max_length=100)


class ProjectInteractionIds(BaseModel):
    interaction_ids: list[UUID] = Field(..., min_length=1, max_length=100)


@router.post("")
async def create_project(request: Request, body: ProjectCreate):
    """Create a project."""
    user, pool = await _get_session_user(request)
    org_id, user_id = _scope(user)
    project = await ProjectRepository(pool).create(
        org_id=org_id,
        user_id=user_id,
        name=body.name,
        instructions=body.instructions,
    )
    return _project_dict(project)


@router.get("")
async def list_projects(
    request: Request,
    limit: int = 100,
    offset: int = 0,
):
    """List the caller's projects with member counts."""
    user, pool = await _get_session_user(request)
    authorized_pool = await get_authorized_projects_pool(pool, user, required_clearance="read")
    result = await ProjectRepository(authorized_pool).list_owned(
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
    )
    result["items"] = [_project_dict(item) for item in result["items"]]
    return result


@router.get("/{project_id}")
async def get_project(request: Request, project_id: UUID):
    """Get one owned project, with member counts."""
    user, pool = await _get_session_user(request)
    authorized_pool = await get_authorized_projects_pool(pool, user, required_clearance="read")
    project = await ProjectRepository(authorized_pool).get_owned_with_counts(
        project_id
    )
    if not project:
        raise HTTPException(404, "Project not found")
    return _project_dict(project)


@router.patch("/{project_id}")
async def update_project(request: Request, project_id: UUID, body: ProjectUpdate):
    """Rename a project and/or edit its standing instructions."""
    user, pool = await _get_session_user(request)
    authorized_pool = await get_authorized_projects_pool(pool, user, required_clearance="write")
    authorized_repo = ProjectRepository(authorized_pool)
    await _owned_project_or_404(authorized_repo, project_id)
    org_id, user_id = _scope(user)
    provided = body.model_fields_set
    if not provided & {"name", "instructions"}:
        raise HTTPException(422, "Nothing to update: provide name and/or instructions")
    repo = ProjectRepository(pool)
    project = None
    if "name" in provided:
        if body.name is None:
            raise HTTPException(422, "name cannot be null")
        project = await repo.rename(
            project_id, org_id=org_id, user_id=user_id, name=body.name
        )
    if "instructions" in provided:
        updated = await repo.set_instructions(
            project_id, org_id=org_id, user_id=user_id, instructions=body.instructions
        )
        project = updated if project is None or not updated else updated
    if not project:
        raise HTTPException(404, "Project not found")
    return _project_dict(project)


@router.delete("/{project_id}")
async def delete_project(request: Request, project_id: UUID):
    """Delete a project. Member chats and files are un-filed, never deleted."""
    user, pool = await _get_session_user(request)
    authorized_pool = await get_authorized_projects_pool(pool, user, required_clearance="write")
    await _owned_project_or_404(ProjectRepository(authorized_pool), project_id)
    org_id, user_id = _scope(user)
    deleted = await ProjectRepository(pool).delete_owned(
        project_id, org_id=org_id, user_id=user_id
    )
    if not deleted:
        raise HTTPException(404, "Project not found")
    return {"deleted": True, "project_id": str(project_id)}


@router.post("/{project_id}/sessions")
async def add_sessions_to_project(
    request: Request, project_id: UUID, body: ProjectSessionIds
):
    """Move existing chats into a project (bulk).

    Only sessions owned by the caller move; the moved/requested counts report
    the truth without distinguishing other users' ids.
    """
    user, pool = await _get_session_user(request)
    authorized_pool = await get_authorized_projects_pool(pool, user, required_clearance="write")
    authorized_repo = ProjectRepository(authorized_pool)
    await _owned_project_or_404(authorized_repo, project_id)
    org_id, user_id = _scope(user)
    repo = ProjectRepository(pool)
    moved = await repo.set_sessions_project_bulk(
        [str(s) for s in body.session_ids],
        project_id,
        org_id=org_id,
        user_id=user_id,
    )
    return {"project_id": str(project_id), "moved": moved, "requested": len(body.session_ids)}


@router.delete("/{project_id}/sessions")
async def remove_sessions_from_project(
    request: Request, project_id: UUID, body: ProjectSessionIds
):
    """Move chats out of a project (bulk, back to unfiled)."""
    user, pool = await _get_session_user(request)
    authorized_pool = await get_authorized_projects_pool(pool, user, required_clearance="write")
    await _owned_project_or_404(ProjectRepository(authorized_pool), project_id)
    org_id, user_id = _scope(user)
    repo = ProjectRepository(pool)
    moved = await repo.set_sessions_project_bulk(
        [str(s) for s in body.session_ids],
        None,
        org_id=org_id,
        user_id=user_id,
    )
    return {"project_id": str(project_id), "moved": moved, "requested": len(body.session_ids)}


@router.post("/{project_id}/files")
async def add_files_to_project(
    request: Request, project_id: UUID, body: ProjectFileIds
):
    """Move existing durable files into a project (bulk).

    Only files owned by the caller move; moved/requested counts report the
    truth without distinguishing other users' ids.
    """
    user, pool = await _get_session_user(request)
    authorized_pool = await get_authorized_projects_pool(pool, user, required_clearance="write")
    await _owned_project_or_404(ProjectRepository(authorized_pool), project_id)
    org_id, user_id = _scope(user)
    repo = ProjectRepository(pool)
    moved = await repo.set_files_project_bulk(
        [str(f) for f in body.file_ids],
        project_id,
        org_id=org_id,
        user_id=user_id,
    )
    return {"project_id": str(project_id), "moved": moved, "requested": len(body.file_ids)}


@router.delete("/{project_id}/files")
async def remove_files_from_project(
    request: Request, project_id: UUID, body: ProjectFileIds
):
    """Move durable files out of a project (bulk, back to unfiled)."""
    user, pool = await _get_session_user(request)
    org_id, user_id = _scope(user)
    authorized_pool = await get_authorized_projects_pool(pool, user, required_clearance="write")
    await _owned_project_or_404(ProjectRepository(authorized_pool), project_id)
    repo = ProjectRepository(pool)
    moved = await repo.set_files_project_bulk(
        [str(f) for f in body.file_ids],
        None,
        org_id=org_id,
        user_id=user_id,
    )
    return {"project_id": str(project_id), "moved": moved, "requested": len(body.file_ids)}


@router.get("/{project_id}/sessions")
async def list_project_sessions(
    request: Request, project_id: UUID, limit: int = 100, offset: int = 0
):
    """Chats filed into the project, newest-activity first."""
    user, pool = await _get_session_user(request)
    org_id, user_id = _scope(user)
    try:
        return await ProjectRepository(pool).list_sessions_in_project(
            project_id,
            org_id=org_id,
            user_id=user_id,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(404, "Project not found") from exc


@router.get("/{project_id}/files")
async def list_project_files(
    request: Request, project_id: UUID, limit: int = 100, offset: int = 0
):
    """Durable files filed into the project, newest first."""
    user, pool = await _get_session_user(request)
    org_id, user_id = _scope(user)
    try:
        return await ProjectRepository(pool).list_files_in_project(
            project_id,
            org_id=org_id,
            user_id=user_id,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(404, "Project not found") from exc

@router.post("/{project_id}/memories")
async def add_memories_to_project(
    request: Request, project_id: UUID, body: ProjectMemoryIds
):
    """Attach existing memories to a project (bulk).

    Only memories owned by the caller attach; the moved/requested counts
    report the truth without distinguishing other users' ids.
    """
    user, pool = await _get_session_user(request)
    org_id, user_id = _scope(user)
    authorized_pool = await get_authorized_projects_pool(pool, user, required_clearance="write")
    await _owned_project_or_404(ProjectRepository(authorized_pool), project_id)
    repo = ProjectRepository(pool)
    moved = await repo.set_memories_project_bulk(
        [str(m) for m in body.memory_ids],
        project_id,
        org_id=org_id,
        user_id=user_id,
    )
    return {"project_id": str(project_id), "moved": moved, "requested": len(body.memory_ids)}


@router.delete("/{project_id}/memories")
async def remove_memories_from_project(
    request: Request, project_id: UUID, body: ProjectMemoryIds
):
    """Detach memories from a project (bulk, back to unfiled)."""
    user, pool = await _get_session_user(request)
    authorized_pool = await get_authorized_projects_pool(pool, user, required_clearance="write")
    await _owned_project_or_404(ProjectRepository(authorized_pool), project_id)
    org_id, user_id = _scope(user)
    repo = ProjectRepository(pool)
    moved = await repo.set_memories_project_bulk(
        [str(m) for m in body.memory_ids],
        None,
        org_id=org_id,
        user_id=user_id,
    )
    return {"project_id": str(project_id), "moved": moved, "requested": len(body.memory_ids)}


@router.get("/{project_id}/memories")
async def list_project_memories(
    request: Request, project_id: UUID, limit: int = 100, offset: int = 0
):
    """Memories attached to the project, newest update first."""
    user, pool = await _get_session_user(request)
    org_id, user_id = _scope(user)
    try:
        return await ProjectRepository(pool).list_memories_in_project(
            project_id,
            org_id=org_id,
            user_id=user_id,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(404, "Project not found") from exc


@router.post("/{project_id}/interactions")
async def add_interactions_to_project(
    request: Request, project_id: UUID, body: ProjectInteractionIds
):
    """File existing handoffs into a project (bulk).

    Handoffs are strictly per-user private: only handoffs owned by the
    caller move. Filing changes grouping only — visibility rules are
    untouched.
    """
    user, pool = await _get_session_user(request)
    authorized_pool = await get_authorized_projects_pool(pool, user, required_clearance="write")
    await _owned_project_or_404(ProjectRepository(authorized_pool), project_id)
    org_id, user_id = _scope(user)
    repo = ProjectRepository(pool)
    moved = await repo.set_interactions_project_bulk(
        [str(i) for i in body.interaction_ids],
        project_id,
        org_id=org_id,
        user_id=user_id,
    )
    return {"project_id": str(project_id), "moved": moved, "requested": len(body.interaction_ids)}


@router.delete("/{project_id}/interactions")
async def remove_interactions_from_project(
    request: Request, project_id: UUID, body: ProjectInteractionIds
):
    """Move handoffs out of a project (bulk, back to unfiled)."""
    user, pool = await _get_session_user(request)
    authorized_pool = await get_authorized_projects_pool(pool, user, required_clearance="write")
    await _owned_project_or_404(ProjectRepository(authorized_pool), project_id)
    org_id, user_id = _scope(user)
    repo = ProjectRepository(pool)
    moved = await repo.set_interactions_project_bulk(
        [str(i) for i in body.interaction_ids],
        None,
        org_id=org_id,
        user_id=user_id,
    )
    return {"project_id": str(project_id), "moved": moved, "requested": len(body.interaction_ids)}


@router.get("/{project_id}/interactions")
async def list_project_interactions(
    request: Request, project_id: UUID, limit: int = 100, offset: int = 0
):
    """Handoffs filed into the project, newest update first.

    Handoffs are strictly per-user private: only the caller's own handoffs
    appear; other members' handoffs stay invisible.
    """
    user, pool = await _get_session_user(request)
    org_id, user_id = _scope(user)
    try:
        return await ProjectRepository(pool).list_interactions_in_project(
            project_id,
            org_id=org_id,
            user_id=user_id,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(404, "Project not found") from exc
