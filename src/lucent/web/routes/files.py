"""Web UI routes for user-owned files."""

from math import ceil

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from lucent.db import get_pool
from lucent.storage import UserFileService

from ._shared import _check_csrf, _get_csrf_for_request, get_user_context, templates

router = APIRouter()
ALLOWED_PER_PAGE = {10, 25, 50, 100}


@router.get("/files", response_class=HTMLResponse)
async def files_list(request: Request, page: int = 1, per_page: int = 25):
    user = await get_user_context(request)
    page = max(1, page)
    per_page = per_page if per_page in ALLOWED_PER_PAGE else 25
    result = await UserFileService(await get_pool()).repository.list_owned(
        str(user.organization_id),
        str(user.id),
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    total_pages = ceil(result["total_count"] / per_page) if result["total_count"] else 1
    return templates.TemplateResponse(
        request,
        "files.html",
        {
            "user": user,
            "files": result["items"],
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total_count": result["total_count"],
            "unseen_count": result["unseen_count"],
        },
    )


@router.get("/files/{file_id}", response_class=HTMLResponse)
async def file_detail(request: Request, file_id: str):
    user = await get_user_context(request)
    service = UserFileService(await get_pool())
    result = await service.read_owned(file_id, str(user.organization_id), str(user.id))
    if not result:
        raise HTTPException(404, "File not found")
    item, content = result
    await service.repository.mark_current_revision_viewed_owned(
        file_id, str(user.organization_id), str(user.id)
    )
    revisions = await service.repository.list_revisions_owned(
        file_id, str(user.organization_id), str(user.id)
    )
    preview = None
    if item["mime_type"].startswith("text/") or item["mime_type"] in {
        "application/json",
        "application/xml",
    }:
        preview = content.decode("utf-8", errors="replace")
    return templates.TemplateResponse(
        request,
        "file_detail.html",
        {
            "user": user,
            "file": item,
            "preview": preview,
            "revisions": revisions,
            "csrf_token": _get_csrf_for_request(request),
        },
    )


@router.get("/files/{file_id}/revisions/{revision_number}/content")
async def file_revision_content(request: Request, file_id: str, revision_number: int):
    user = await get_user_context(request)
    service = UserFileService(await get_pool())
    result = await service.read_revision_owned(
        file_id, revision_number, str(user.organization_id), str(user.id)
    )
    if not result:
        raise HTTPException(404, "File revision not found")
    revision, content = result
    item = await service.get_owned(file_id, str(user.organization_id), str(user.id))
    if not item:
        raise HTTPException(404, "File not found")
    if revision_number == item["current_revision"]:
        await service.repository.mark_current_revision_viewed_owned(
            file_id, str(user.organization_id), str(user.id)
        )
    return Response(
        content=content,
        media_type=item["mime_type"],
        headers={
            "Content-Disposition": (
                f'attachment; filename="revision-{revision_number}-{item["filename"]}"'
            ),
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/files/{file_id}/content")
async def file_content(request: Request, file_id: str, download: bool = False):
    user = await get_user_context(request)
    result = await UserFileService(await get_pool()).read_owned(
        file_id, str(user.organization_id), str(user.id)
    )
    if not result:
        raise HTTPException(404, "File not found")
    item, content = result
    await UserFileService(await get_pool()).repository.mark_current_revision_viewed_owned(
        file_id, str(user.organization_id), str(user.id)
    )
    active_content = item["mime_type"] in {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
    }
    disposition = "attachment" if download or active_content else "inline"
    return Response(
        content=content,
        media_type=item["mime_type"],
        headers={
            "Content-Disposition": f'{disposition}; filename="{item["filename"]}"',
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/files/{file_id}/delete")
async def delete_file(
    request: Request,
    file_id: str,
    csrf_token: str = Form(alias="csrf_token"),
):
    _check_csrf(request, csrf_token)
    user = await get_user_context(request)
    deleted = await UserFileService(await get_pool()).delete_owned(
        file_id, str(user.organization_id), str(user.id)
    )
    if not deleted:
        raise HTTPException(404, "File not found")
    return RedirectResponse("/files", status_code=303)
