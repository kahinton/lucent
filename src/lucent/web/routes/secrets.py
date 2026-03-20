"""Secrets management web routes."""

from math import ceil
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.access_control import AccessControlService
from lucent.auth_providers import CSRF_COOKIE_NAME
from lucent.db import get_pool
from lucent.secrets import SecretRegistry, SecretScope

from ._shared import _check_csrf, get_user_context, templates

router = APIRouter()

ALLOWED_PER_PAGE = {10, 25, 50, 100}


@router.get("/secrets", response_class=HTMLResponse)
async def secrets_page(
    request: Request,
    page: int = 1,
    per_page: int = 25,
):
    """Render secrets list page (keys only, never values)."""
    user = await get_user_context(request)
    pool = await get_pool()
    acl = AccessControlService(pool)
    role_value = user.role if isinstance(user.role, str) else user.role.value

    page = max(1, page)
    per_page = per_page if per_page in ALLOWED_PER_PAGE else 25
    offset = (page - 1) * per_page

    group_ids = [UUID(gid) for gid in await acl.get_user_group_ids(str(user.id))]
    org_id = UUID(str(user.organization_id))
    user_id = UUID(str(user.id))

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS total
            FROM secrets s
            WHERE s.organization_id = $1
              AND (
                s.owner_user_id = $2
                OR s.owner_group_id = ANY($3::uuid[])
                OR $4 IN ('admin', 'owner')
              )
            """,
            org_id,
            user_id,
            group_ids,
            role_value,
        )
        total_count = count_row["total"] if count_row else 0
        rows = await conn.fetch(
            """
            SELECT
                s.key,
                s.owner_user_id,
                s.owner_group_id,
                s.created_at,
                COALESCE(u.display_name, u.email, 'Unknown user') AS owner_user_name,
                g.name AS owner_group_name
            FROM secrets s
            LEFT JOIN users u ON u.id = s.owner_user_id
            LEFT JOIN groups g ON g.id = s.owner_group_id
            WHERE s.organization_id = $1
              AND (
                s.owner_user_id = $2
                OR s.owner_group_id = ANY($3::uuid[])
                OR $4 IN ('admin', 'owner')
              )
            ORDER BY s.created_at DESC, s.key ASC
            LIMIT $5 OFFSET $6
            """,
            org_id,
            user_id,
            group_ids,
            role_value,
            per_page,
            offset,
        )

    total_pages = ceil(total_count / per_page) if total_count > 0 else 1
    page = min(page, total_pages)

    return templates.TemplateResponse(
        request,
        "secrets.html",
        {
            "user": user,
            "secrets": [dict(row) for row in rows],
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total_count": total_count,
        },
    )


@router.post("/secrets/create")
async def create_secret_web(request: Request):
    """Create a secret from web form submission."""
    user = await get_user_context(request)
    await _check_csrf(request)
    form = await request.form()

    key = str(form.get("key", "")).strip()
    value = str(form.get("value", ""))
    owner_group_id = str(form.get("owner_group_id", "")).strip() or None

    if not key or not value:
        return RedirectResponse(
            f"/secrets?error={quote('Key and value are required.')}",
            status_code=303,
        )

    if owner_group_id:
        pool = await get_pool()
        acl = AccessControlService(pool)
        role_value = user.role if isinstance(user.role, str) else user.role.value
        if role_value not in ("admin", "owner"):
            user_group_ids = set(await acl.get_user_group_ids(str(user.id)))
            if owner_group_id not in user_group_ids:
                raise HTTPException(status_code=403, detail="Permission denied")
        scope = SecretScope(
            organization_id=str(user.organization_id),
            owner_group_id=owner_group_id,
        )
    else:
        scope = SecretScope(
            organization_id=str(user.organization_id),
            owner_user_id=str(user.id),
        )

    provider = SecretRegistry.get()
    await provider.set(key, value, scope)
    return RedirectResponse("/secrets?success=Secret+created", status_code=303)


@router.post("/secrets/{key}/delete")
async def delete_secret_web(request: Request, key: str):
    """Delete a secret by key and owner scope."""
    user = await get_user_context(request)
    await _check_csrf(request)
    form = await request.form()
    owner_group_id = str(form.get("owner_group_id", "")).strip() or None
    org_id = str(user.organization_id)

    if owner_group_id:
        scope = SecretScope(organization_id=org_id, owner_group_id=owner_group_id)
    else:
        scope = SecretScope(organization_id=org_id, owner_user_id=str(user.id))

    provider = SecretRegistry.get()
    if hasattr(provider, "get_secret_id"):
        secret_id = await provider.get_secret_id(key, scope)
        if secret_id is None:
            raise HTTPException(status_code=404, detail="Secret not found")
        pool = await get_pool()
        acl = AccessControlService(pool)
        allowed = await acl.can_modify(str(user.id), "secret", secret_id, org_id)
        if not allowed:
            raise HTTPException(status_code=403, detail="Access denied")

    deleted = await provider.delete(key, scope)
    if not deleted:
        raise HTTPException(status_code=404, detail="Secret not found")
    return RedirectResponse("/secrets?success=Secret+deleted", status_code=303)
