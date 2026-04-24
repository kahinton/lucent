"""Settings, password change, force password change, and API key routes."""

from urllib.parse import quote
from uuid import UUID

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.auth_providers import (
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL_HOURS,
    create_session,
    destroy_session,
    generate_csrf_token,
    get_cookie_params,
    set_user_password,
    sign_value,
    validate_password_complexity,
    verify_signed_value,
)
from lucent.db import ApiKeyRepository, OrganizationRepository, get_pool

from ._shared import _check_csrf, _set_csrf_cookie, get_user_context, templates

router = APIRouter()


# =============================================================================
# Settings
# =============================================================================

# Temporary storage for newly created API keys (shown once after redirect)
# Uses signed values to pass securely through query params.
# The key is encrypted in the redirect URL and verified on the settings page.


def _encode_pending_key(key_id: str, plain_key: str) -> str:
    """Encode an API key ID and value as a signed string for URL transport."""
    return sign_value(f"{key_id}:{plain_key}")


def _decode_pending_key(signed: str) -> tuple[str, str] | None:
    """Decode and verify a signed API key string.

    Returns:
        Tuple of (key_id, plain_key) or None if invalid.
    """
    value = verify_signed_value(signed)
    if not value or ":" not in value:
        return None
    key_id, plain_key = value.split(":", 1)
    return key_id, plain_key


@router.get("/settings", response_class=HTMLResponse)
async def settings(
    request: Request,
    new_key: str | None = Query(default=None),
    error: str | None = Query(default=None),
    password_changed: str | None = Query(default=None),
):
    """User and organization settings."""
    user = await get_user_context(request)
    pool = await get_pool()
    org_repo = OrganizationRepository(pool)
    api_key_repo = ApiKeyRepository(pool)

    org = await org_repo.get_by_id(user.organization_id)
    api_keys = (await api_key_repo.list_by_user(user.id))["items"]

    # Check for newly created key to display (passed via signed query param)
    new_api_key = None
    new_key_name = None
    if new_key:
        decoded = _decode_pending_key(new_key)
        if decoded:
            key_id, new_api_key = decoded
            for key in api_keys:
                if str(key["id"]) == key_id:
                    new_key_name = key["name"]
                    break

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "organization": org,
            "api_keys": api_keys,
            "new_api_key": new_api_key,
            "new_key_name": new_key_name,
            "error": error,
            "password_changed": password_changed is not None,
        },
    )


# =============================================================================
# Password Change
# =============================================================================


@router.post("/settings/password")
async def change_password(request: Request):
    """Change the current user's password."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    form = await request.form()

    current_password = str(form.get("current_password", ""))
    new_password = str(form.get("new_password", ""))
    confirm_password = str(form.get("confirm_password", ""))

    # Validate new password
    if len(new_password) < 8:
        return RedirectResponse(
            f"/settings?error={quote('New password must be at least 8 characters.')}",
            status_code=303,
        )

    complexity_error = validate_password_complexity(new_password)
    if complexity_error:
        return RedirectResponse(
            f"/settings?error={quote(complexity_error)}",
            status_code=303,
        )

    if new_password != confirm_password:
        return RedirectResponse(
            f"/settings?error={quote('New passwords do not match.')}", status_code=303
        )

    # Verify current password
    query = "SELECT password_hash FROM users WHERE id = $1"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, str(user.id))

    if not row or not row["password_hash"]:
        return RedirectResponse(
            f"/settings?error={quote('No password set on this account.')}", status_code=303
        )

    if not bcrypt.checkpw(current_password.encode("utf-8"), row["password_hash"].encode("utf-8")):
        return RedirectResponse(
            f"/settings?error={quote('Current password is incorrect.')}", status_code=303
        )

    # Set new password
    await set_user_password(pool, user.id, new_password)

    # Invalidate all existing sessions and create a fresh one
    await destroy_session(pool, user.id)
    new_token = await create_session(pool, user.id)

    response = RedirectResponse("/settings?password_changed=1", status_code=303)
    params = get_cookie_params()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=new_token,
        max_age=SESSION_TTL_HOURS * 3600,
        **params,
    )
    _set_csrf_cookie(response, generate_csrf_token())
    return response


# =============================================================================
# Force Password Change
# =============================================================================


@router.get("/force-password-change", response_class=HTMLResponse)
async def force_password_change_page(request: Request):
    """Show the forced password change page."""
    user = await get_user_context(request, allow_force_password_change=True)

    # If no force flag set, redirect to dashboard
    pool = await get_pool()
    query = "SELECT force_password_change FROM users WHERE id = $1"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, str(user.id))

    if not row or not row["force_password_change"]:
        return RedirectResponse("/", status_code=303)

    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "force_password_change.html",
        {
            "user": user,
            "csrf_token": csrf_token,
            "csrf_field_name": CSRF_FIELD_NAME,
            "error": request.query_params.get("error"),
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response


@router.post("/force-password-change")
async def force_password_change_submit(request: Request):
    """Handle forced password change submission."""
    user = await get_user_context(request, allow_force_password_change=True)

    form = await request.form()
    csrf_form_token = str(form.get(CSRF_FIELD_NAME, ""))
    await _check_csrf(request, form_token=csrf_form_token)

    pool = await get_pool()
    new_password = str(form.get("new_password", ""))
    confirm_password = str(form.get("confirm_password", ""))

    if len(new_password) < 8:
        return RedirectResponse(
            f"/force-password-change?error={quote('Password must be at least 8 characters.')}",
            status_code=303,
        )

    complexity_error = validate_password_complexity(new_password)
    if complexity_error:
        return RedirectResponse(
            f"/force-password-change?error={quote(complexity_error)}",
            status_code=303,
        )

    if new_password != confirm_password:
        return RedirectResponse(
            f"/force-password-change?error={quote('Passwords do not match.')}",
            status_code=303,
        )

    # Set new password and clear force flag
    await set_user_password(pool, user.id, new_password)
    from lucent.auth_providers import clear_force_password_change

    await clear_force_password_change(pool, user.id)

    # Create fresh session
    await destroy_session(pool, user.id)
    new_token = await create_session(pool, user.id)

    response = RedirectResponse("/", status_code=303)
    params = get_cookie_params()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=new_token,
        max_age=SESSION_TTL_HOURS * 3600,
        **params,
    )
    _set_csrf_cookie(response, generate_csrf_token())
    return response


# =============================================================================
# API Keys
# =============================================================================


@router.post("/settings/api-keys")
async def create_api_key(
    request: Request,
    name: str = Form(...),
):
    """Create a new API key."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    api_key_repo = ApiKeyRepository(pool)

    try:
        # Create the new key
        key_record, plain_key = await api_key_repo.create(
            user_id=user.id,
            organization_id=user.organization_id,
            name=name.strip(),
        )

        # Encode the key in a signed query param for the redirect
        key_id = str(key_record["id"])
        signed = _encode_pending_key(key_id, plain_key)

        # Redirect to settings with signed key (POST-Redirect-GET pattern)
        from urllib.parse import quote

        return RedirectResponse(f"/settings?new_key={quote(signed)}", status_code=303)

    except ValueError as e:
        # Duplicate name error
        from urllib.parse import quote

        return RedirectResponse(f"/settings?error={quote(str(e))}", status_code=303)


@router.post("/settings/api-keys/{key_id}/revoke", response_class=HTMLResponse)
async def revoke_api_key(request: Request, key_id: UUID):
    """Revoke an API key."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    api_key_repo = ApiKeyRepository(pool)

    success = await api_key_repo.revoke(key_id, user.id)

    if not success:
        raise HTTPException(status_code=404, detail="API key not found")

    return RedirectResponse("/settings", status_code=303)
