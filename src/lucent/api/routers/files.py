"""API endpoints for user-owned files."""

from __future__ import annotations

import base64
import binascii
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from lucent.api.deps import AuthenticatedUser, get_pool
from lucent.storage import UserFileService

router = APIRouter(prefix="/files", tags=["files"])


class UserFileCreate(BaseModel):
    filename: str = Field(..., min_length=1, max_length=512)
    content: str
    content_encoding: str = Field(default="utf-8", pattern=r"^(utf-8|base64)$")
    display_name: str | None = Field(default=None, max_length=256)
    mime_type: str | None = Field(default=None, max_length=128)
    request_id: UUID | None = None
    task_id: UUID | None = None
    metadata: dict = Field(default_factory=dict)
    is_primary: bool = False


class UserFileUpdate(BaseModel):
    content: str
    content_encoding: str = Field(default="utf-8", pattern=r"^(utf-8|base64)$")
    change_summary: str | None = Field(default=None, max_length=500)


def _owner_id(user: AuthenticatedUser) -> str:
    return str(user.effective_memory_user_id)


def _decode_content(content: str, encoding: str) -> bytes:
    try:
        return (
            base64.b64decode(content, validate=True)
            if encoding == "base64"
            else content.encode("utf-8")
        )
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "Invalid base64 content") from exc


@router.post("")
async def create_file(
    body: UserFileCreate,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    content = _decode_content(body.content, body.content_encoding)

    try:
        return await UserFileService(pool).create(
            org_id=str(user.organization_id),
            user_id=_owner_id(user),
            created_by=str(user.id),
            filename=body.filename,
            content=content,
            display_name=body.display_name,
            mime_type=body.mime_type,
            request_id=str(body.request_id) if body.request_id else None,
            task_id=str(body.task_id) if body.task_id else None,
            metadata=body.metadata,
            is_primary=body.is_primary,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("")
async def list_files(
    user: AuthenticatedUser,
    limit: int = 50,
    offset: int = 0,
    pool=Depends(get_pool),
):
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    return await UserFileService(pool).repository.list_owned(
        str(user.organization_id), _owner_id(user), limit=limit, offset=offset
    )


@router.get("/{file_id}")
async def get_file(file_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    item = await UserFileService(pool).get_owned(
        str(file_id), str(user.organization_id), _owner_id(user)
    )
    if not item:
        raise HTTPException(404, "File not found")
    return item


@router.patch("/{file_id}")
async def update_file(
    file_id: UUID,
    body: UserFileUpdate,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    try:
        return await UserFileService(pool).update(
            file_id=str(file_id),
            org_id=str(user.organization_id),
            user_id=_owner_id(user),
            edited_by=str(user.id),
            content=_decode_content(body.content, body.content_encoding),
            change_summary=body.change_summary,
        )
    except ValueError as exc:
        if str(exc) == "File not found":
            raise HTTPException(404, "File not found") from exc
        raise HTTPException(422, str(exc)) from exc


@router.get("/{file_id}/revisions")
async def list_file_revisions(
    file_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)
):
    revisions = await UserFileService(pool).repository.list_revisions_owned(
        str(file_id), str(user.organization_id), _owner_id(user)
    )
    if not revisions:
        raise HTTPException(404, "File not found")
    return {"items": revisions, "total_count": len(revisions)}


@router.get("/{file_id}/revisions/{revision_number}/content")
async def get_file_revision_content(
    file_id: UUID,
    revision_number: int,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    result = await UserFileService(pool).read_revision_owned(
        str(file_id), revision_number, str(user.organization_id), _owner_id(user)
    )
    if not result:
        raise HTTPException(404, "File revision not found")
    revision, content = result
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (
                f'attachment; filename="revision-{revision["revision_number"]}"'
            ),
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{file_id}/content")
async def get_file_content(
    file_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)
):
    result = await UserFileService(pool).read_owned(
        str(file_id), str(user.organization_id), _owner_id(user)
    )
    if not result:
        raise HTTPException(404, "File not found")
    item, content = result
    active_content = item["mime_type"] in {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
    }
    return Response(
        content=content,
        media_type=item["mime_type"],
        headers={
            "Content-Disposition": (
                f'attachment; filename="{item["filename"]}"'
                if active_content
                else f'inline; filename="{item["filename"]}"'
            ),
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{file_id}", status_code=204)
async def delete_file(file_id: UUID, user: AuthenticatedUser, pool=Depends(get_pool)):
    deleted = await UserFileService(pool).delete_owned(
        str(file_id), str(user.organization_id), _owner_id(user)
    )
    if not deleted:
        raise HTTPException(404, "File not found")
    return Response(status_code=204)
