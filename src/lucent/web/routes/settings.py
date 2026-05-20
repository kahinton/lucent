"""Settings, password change, force password change, and API key routes.

After the M5 settings redesign, this module hosts the unified settings hub:
    /settings                   -> redirect to /settings/account
    /settings/account           -> profile + change password
    /settings/api-keys          -> list/create/revoke API keys
    /settings/api-access        -> developer reference
    /settings/organization      -> view (everyone) / edit (owner)
    /settings/danger            -> owner-only destructive actions

POST endpoints under /settings/* are CSRF-protected and audit-logged via
:class:`AdminAuditRepository` for security-sensitive actions.
"""

import logging
from urllib.parse import quote
from uuid import UUID

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from lucent import settings as runtime_settings
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
from lucent.db import (
    AdminAuditRepository,
    ApiKeyRepository,
    OrganizationRepository,
    RuntimeSettingsRepository,
    UserRepository,
    get_pool,
)
from lucent.db import admin_audit as audit_actions

from ._shared import _check_csrf, _set_csrf_cookie, get_user_context, templates

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _role_value(user) -> str:
    return user.role.value if hasattr(user.role, "value") else str(user.role)


async def _require_admin_or_owner(request: Request):
    user = await get_user_context(request)
    if _role_value(user) not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


def _refresh_runtime_setting_dependents(setting_key: str) -> None:
    """Best-effort refresh for services whose singletons cache settings."""
    if setting_key.startswith("server."):
        try:
            from lucent.rate_limit import reset_rate_limiters

            reset_rate_limiters()
        except Exception:
            logger.debug("failed to reset rate limiters after settings update", exc_info=True)


def _runtime_settings_wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "").lower()
    requested_with = request.headers.get("x-requested-with", "").lower()
    return "application/json" in accept or requested_with in {"fetch", "xmlhttprequest"}


def _runtime_source_badge(source: str) -> dict[str, str]:
    base = "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium"
    if source == "database":
        return {"label": "Saved in DB", "class_name": f"{base} bg-green-100 text-green-700"}
    if source == "environment":
        return {"label": "From env", "class_name": f"{base} bg-amber-100 text-amber-800"}
    return {"label": "Default", "class_name": f"{base} bg-gray-100 text-gray-600"}


def _runtime_source_note(source: str) -> str:
    if source == "database":
        return "This saved value takes precedence over the environment fallback."
    if source == "environment":
        return "No DB value is saved, so Lucent is using the environment variable."
    return "No DB value or environment variable is set, so Lucent is using the built-in default."


def _runtime_setting_payload(organization_id, setting_key: str, message: str) -> dict:
    snapshots = runtime_settings.runtime_setting_snapshots(organization_id)
    snapshot = next(
        item for item in snapshots if item["definition"].key == setting_key
    )
    definition = snapshot["definition"]
    badge = _runtime_source_badge(snapshot["source"])
    return {
        "ok": True,
        "message": message,
        "setting": {
            "key": definition.key,
            "title": definition.title,
            "source": snapshot["source"],
            "source_label": badge["label"],
            "source_badge_class": badge["class_name"],
            "source_note": _runtime_source_note(snapshot["source"]),
            "display_value": snapshot["display_value"],
            "form_value": snapshot["form_value"],
        },
    }


def _runtime_settings_error_response(
    request: Request,
    message: str,
    *,
    status_code: int = 400,
):
    if _runtime_settings_wants_json(request):
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)
    return RedirectResponse(
        f"/settings/runtime?error={quote(message)}",
        status_code=303,
    )


def _encode_pending_key(key_id: str, plain_key: str) -> str:
    return sign_value(f"{key_id}:{plain_key}")


def _decode_pending_key(signed: str) -> tuple[str, str] | None:
    value = verify_signed_value(signed)
    if not value or ":" not in value:
        return None
    key_id, plain_key = value.split(":", 1)
    return key_id, plain_key


# ---------------------------------------------------------------------------
# /settings — landing redirect
# ---------------------------------------------------------------------------


@router.get("/settings")
async def settings_root(request: Request):
    """Settings root — redirect to the Account section."""
    return RedirectResponse("/settings/account", status_code=303)


# ---------------------------------------------------------------------------
# /settings/account — profile + change password
# ---------------------------------------------------------------------------


@router.get("/settings/account", response_class=HTMLResponse)
async def settings_account(
    request: Request,
    error: str | None = Query(default=None),
    success: str | None = Query(default=None),
    password_changed: str | None = Query(default=None),
):
    user = await get_user_context(request)
    if password_changed and not success:
        success = "Password changed successfully."
    return templates.TemplateResponse(
        request,
        "settings/account.html",
        {
            "user": user,
            "error": error,
            "success": success,
        },
    )


@router.post("/settings/profile")
async def settings_update_profile(
    request: Request,
    display_name: str = Form(""),
):
    """Update the current user's display name."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    user_repo = UserRepository(pool)
    audit_repo = AdminAuditRepository(pool)

    new_name = (display_name or "").strip()[:120]
    old_name = user.display_name or ""

    if new_name == old_name:
        return RedirectResponse("/settings/account", status_code=303)

    await user_repo.update(user.id, display_name=new_name or None)
    await audit_repo.log_for_user(
        user,
        request,
        action=audit_actions.USER_UPDATE,
        entity_type="user",
        entity_id=user.id,
        entity_label=new_name or old_name,
        changed_fields=["display_name"],
        old_values={"display_name": old_name},
        new_values={"display_name": new_name},
        notes="self profile update",
    )
    return RedirectResponse(
        f"/settings/account?success={quote('Profile updated.')}", status_code=303
    )


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------


@router.post("/settings/password")
async def change_password(request: Request):
    """Change the current user's password."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    audit_repo = AdminAuditRepository(pool)
    form = await request.form()

    current_password = str(form.get("current_password", ""))
    new_password = str(form.get("new_password", ""))
    confirm_password = str(form.get("confirm_password", ""))

    def _err(msg: str) -> RedirectResponse:
        return RedirectResponse(
            f"/settings/account?error={quote(msg)}", status_code=303
        )

    if len(new_password) < 8:
        return _err("New password must be at least 8 characters.")
    complexity_error = validate_password_complexity(new_password)
    if complexity_error:
        return _err(complexity_error)
    if new_password != confirm_password:
        return _err("New passwords do not match.")

    query = "SELECT password_hash FROM users WHERE id = $1"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, str(user.id))

    if not row or not row["password_hash"]:
        await audit_repo.log_for_user(
            user, request,
            action=audit_actions.PASSWORD_CHANGE,
            entity_type="user",
            entity_id=user.id,
            outcome="failed",
            notes="no password set",
        )
        return _err("No password set on this account.")

    if not bcrypt.checkpw(
        current_password.encode("utf-8"), row["password_hash"].encode("utf-8")
    ):
        await audit_repo.log_for_user(
            user, request,
            action=audit_actions.PASSWORD_CHANGE,
            entity_type="user",
            entity_id=user.id,
            outcome="denied",
            notes="incorrect current password",
        )
        return _err("Current password is incorrect.")

    await set_user_password(pool, user.id, new_password)
    await destroy_session(pool, user.id)
    new_token = await create_session(pool, user.id)

    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.PASSWORD_CHANGE,
        entity_type="user",
        entity_id=user.id,
        notes="self-service password change; all other sessions revoked",
    )

    response = RedirectResponse(
        f"/settings/account?success={quote('Password updated. Other sessions were signed out.')}",
        status_code=303,
    )
    params = get_cookie_params()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=new_token,
        max_age=SESSION_TTL_HOURS * 3600,
        **params,
    )
    _set_csrf_cookie(response, generate_csrf_token())
    return response


# ---------------------------------------------------------------------------
# Force password change (unchanged URL — referenced by login flow)
# ---------------------------------------------------------------------------


@router.get("/force-password-change", response_class=HTMLResponse)
async def force_password_change_page(request: Request):
    """Show the forced password change page."""
    user = await get_user_context(request, allow_force_password_change=True)

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
    user = await get_user_context(request, allow_force_password_change=True)

    form = await request.form()
    csrf_form_token = str(form.get(CSRF_FIELD_NAME, ""))
    await _check_csrf(request, form_token=csrf_form_token)

    pool = await get_pool()
    audit_repo = AdminAuditRepository(pool)
    new_password = str(form.get("new_password", ""))
    confirm_password = str(form.get("confirm_password", ""))

    def _err(msg: str) -> RedirectResponse:
        return RedirectResponse(
            f"/force-password-change?error={quote(msg)}", status_code=303
        )

    if len(new_password) < 8:
        return _err("Password must be at least 8 characters.")
    complexity_error = validate_password_complexity(new_password)
    if complexity_error:
        return _err(complexity_error)
    if new_password != confirm_password:
        return _err("Passwords do not match.")

    await set_user_password(pool, user.id, new_password)
    from lucent.auth_providers import clear_force_password_change
    await clear_force_password_change(pool, user.id)

    await destroy_session(pool, user.id)
    new_token = await create_session(pool, user.id)

    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.PASSWORD_CHANGE,
        entity_type="user",
        entity_id=user.id,
        notes="forced password change at first login",
    )

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


# ---------------------------------------------------------------------------
# /settings/api-keys
# ---------------------------------------------------------------------------


@router.get("/settings/api-keys", response_class=HTMLResponse)
async def settings_api_keys(
    request: Request,
    new_key: str | None = Query(default=None),
    error: str | None = Query(default=None),
    success: str | None = Query(default=None),
):
    user = await get_user_context(request)
    pool = await get_pool()
    api_key_repo = ApiKeyRepository(pool)
    api_keys = (await api_key_repo.list_by_user(user.id))["items"]

    new_api_key = None
    new_key_name = None
    if new_key:
        decoded = _decode_pending_key(new_key)
        if decoded:
            key_id, new_api_key = decoded
            for k in api_keys:
                if str(k["id"]) == key_id:
                    new_key_name = k["name"]
                    break

    return templates.TemplateResponse(
        request,
        "settings/api_keys.html",
        {
            "user": user,
            "api_keys": api_keys,
            "new_api_key": new_api_key,
            "new_key_name": new_key_name,
            "error": error,
            "success": success,
        },
    )


@router.post("/settings/api-keys")
async def create_api_key(request: Request, name: str = Form(...)):
    """Create a new API key for the current user."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    api_key_repo = ApiKeyRepository(pool)
    audit_repo = AdminAuditRepository(pool)

    try:
        key_record, plain_key = await api_key_repo.create(
            user_id=user.id,
            organization_id=user.organization_id,
            name=name.strip(),
        )
        key_id = str(key_record["id"])
        await audit_repo.log_for_user(
            user, request,
            action=audit_actions.API_KEY_CREATE,
            entity_type="api_key",
            entity_id=key_id,
            entity_label=key_record["name"],
            new_values={
                "name": key_record["name"],
                "key_prefix": key_record["key_prefix"],
                "scopes": list(key_record.get("scopes") or []),
            },
        )
        signed = _encode_pending_key(key_id, plain_key)
        return RedirectResponse(
            f"/settings/api-keys?new_key={quote(signed)}", status_code=303
        )
    except ValueError as e:
        return RedirectResponse(
            f"/settings/api-keys?error={quote(str(e))}", status_code=303
        )


@router.post("/settings/api-keys/{key_id}/revoke", response_class=HTMLResponse)
async def revoke_api_key(request: Request, key_id: UUID):
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    api_key_repo = ApiKeyRepository(pool)
    audit_repo = AdminAuditRepository(pool)

    existing = await api_key_repo.get_by_id(key_id, user.id)
    success = await api_key_repo.revoke(key_id, user.id)
    if not success:
        raise HTTPException(status_code=404, detail="API key not found")

    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.API_KEY_REVOKE,
        entity_type="api_key",
        entity_id=key_id,
        entity_label=(existing or {}).get("name"),
    )
    return RedirectResponse(
        f"/settings/api-keys?success={quote('API key revoked.')}", status_code=303
    )


# ---------------------------------------------------------------------------
# /settings/api-access
# ---------------------------------------------------------------------------


@router.get("/settings/api-access", response_class=HTMLResponse)
async def settings_api_access(request: Request):
    user = await get_user_context(request)
    # Build a base URL that matches the host that served this request so users
    # can copy/paste straight into clients without editing.
    scheme = request.url.scheme
    host = request.headers.get("host") or "localhost:8766"
    base_url = f"{scheme}://{host}"
    return templates.TemplateResponse(
        request,
        "settings/api_access.html",
        {"user": user, "base_url": base_url},
    )


# ---------------------------------------------------------------------------
# /settings/organization — visible to all; editable by owner
# ---------------------------------------------------------------------------


@router.get("/settings/organization", response_class=HTMLResponse)
async def settings_organization(
    request: Request,
    error: str | None = Query(default=None),
    success: str | None = Query(default=None),
):
    user = await get_user_context(request)
    pool = await get_pool()
    org_repo = OrganizationRepository(pool)
    user_repo = UserRepository(pool)
    api_key_repo = ApiKeyRepository(pool)

    org = await org_repo.get_by_id(user.organization_id)
    members = await user_repo.get_by_organization(user.organization_id)
    admin_count = sum(1 for m in members if m.get("role") in ("admin", "owner"))
    api_keys_total = 0
    try:
        # Count total active keys for the org. ApiKeyRepository doesn't have an
        # org-level method, so we sum across users. This is fine for typical org
        # sizes during launch; a dedicated method can come later.
        for m in members:
            keys = await api_key_repo.list_by_user(m["id"], limit=1000)
            api_keys_total += sum(1 for k in keys["items"] if k.get("is_active"))
    except Exception:
        pass

    return templates.TemplateResponse(
        request,
        "settings/organization.html",
        {
            "user": user,
            "organization": org,
            "stats": {
                "total_users": len(members),
                "admin_count": admin_count,
                "api_key_count": api_keys_total,
            },
            "error": error,
            "success": success,
        },
    )


@router.post("/settings/organization")
async def settings_organization_update(request: Request, name: str = Form(...)):
    """Owner-only: rename the workspace."""
    user = await get_user_context(request)
    await _check_csrf(request)
    if _role_value(user) != "owner":
        raise HTTPException(status_code=403, detail="Only the workspace owner can change this.")

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)
    audit_repo = AdminAuditRepository(pool)

    new_name = name.strip()[:120]
    if not new_name:
        return RedirectResponse(
            f"/settings/organization?error={quote('Name is required.')}", status_code=303
        )

    org = await org_repo.get_by_id(user.organization_id)
    old_name = (org or {}).get("name", "")
    if new_name == old_name:
        return RedirectResponse("/settings/organization", status_code=303)

    await org_repo.update(organization_id=user.organization_id, name=new_name)
    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.ORG_UPDATE,
        entity_type="organization",
        entity_id=user.organization_id,
        entity_label=new_name,
        changed_fields=["name"],
        old_values={"name": old_name},
        new_values={"name": new_name},
    )
    return RedirectResponse(
        f"/settings/organization?success={quote('Workspace updated.')}", status_code=303
    )


# ---------------------------------------------------------------------------
# /settings/runtime — admin/owner editable runtime settings
# ---------------------------------------------------------------------------


@router.get("/settings/runtime", response_class=HTMLResponse)
async def settings_runtime(
    request: Request,
    error: str | None = Query(default=None),
    success: str | None = Query(default=None),
):
    """Admin/owner view for safe DB-backed runtime settings."""
    user = await _require_admin_or_owner(request)
    pool = await get_pool()
    await runtime_settings.load_runtime_settings_from_db(
        pool,
        organization_id=user.organization_id,
    )
    return templates.TemplateResponse(
        request,
        "settings/runtime.html",
        {
            "user": user,
            "settings_by_section": runtime_settings.runtime_settings_by_section(
                user.organization_id
            ),
            "error": error,
            "success": success,
        },
    )


@router.post("/settings/runtime/{setting_key:path}/reset")
async def settings_runtime_reset(request: Request, setting_key: str):
    """Delete a DB setting so env/default fallback is used again."""
    user = await _require_admin_or_owner(request)
    await _check_csrf(request)

    definition = runtime_settings.get_runtime_setting_definition(setting_key)
    if not definition or not definition.editable:
        return _runtime_settings_error_response(
            request,
            "Unknown or read-only setting.",
            status_code=404,
        )

    old_value = runtime_settings.get_runtime_setting(
        setting_key,
        organization_id=user.organization_id,
    )
    old_source = runtime_settings.get_runtime_setting_source(
        setting_key,
        organization_id=user.organization_id,
    )

    pool = await get_pool()
    repo = RuntimeSettingsRepository(pool)
    deleted = await repo.delete_setting(user.organization_id, setting_key)
    runtime_settings.clear_runtime_setting_cache(user.organization_id, setting_key)
    _refresh_runtime_setting_dependents(setting_key)

    new_value = runtime_settings.get_runtime_setting(
        setting_key,
        organization_id=user.organization_id,
    )
    new_source = runtime_settings.get_runtime_setting_source(
        setting_key,
        organization_id=user.organization_id,
    )

    audit_repo = AdminAuditRepository(pool)
    await audit_repo.log_for_user(
        user,
        request,
        action=audit_actions.SETTING_RESET,
        entity_type="settings",
        entity_label=definition.title,
        changed_fields=[setting_key],
        old_values={"value": old_value, "source": old_source},
        new_values={"value": new_value, "source": new_source},
        notes=(
            "runtime setting reset to fallback"
            if deleted
            else "runtime setting already using fallback"
        ),
        extra_context={"setting_key": setting_key, "env_var": definition.env_var},
    )
    message = f"{definition.title} reset to fallback."
    if _runtime_settings_wants_json(request):
        return JSONResponse(
            _runtime_setting_payload(user.organization_id, setting_key, message)
        )
    return RedirectResponse(
        f"/settings/runtime?success={quote(message)}",
        status_code=303,
    )


@router.post("/settings/runtime/{setting_key:path}")
async def settings_runtime_update(request: Request, setting_key: str):
    """Create/update a DB-backed runtime setting value."""
    user = await _require_admin_or_owner(request)
    await _check_csrf(request)

    definition = runtime_settings.get_runtime_setting_definition(setting_key)
    if not definition or not definition.editable:
        return _runtime_settings_error_response(
            request,
            "Unknown or read-only setting.",
            status_code=404,
        )

    form = await request.form()
    raw_value = form.get("value", "")
    try:
        value = runtime_settings.validate_runtime_setting_value(setting_key, raw_value)
    except ValueError as exc:
        return _runtime_settings_error_response(
            request,
            f"{definition.title}: {exc}",
        )

    old_value = runtime_settings.get_runtime_setting(
        setting_key,
        organization_id=user.organization_id,
    )
    old_source = runtime_settings.get_runtime_setting_source(
        setting_key,
        organization_id=user.organization_id,
    )

    pool = await get_pool()
    repo = RuntimeSettingsRepository(pool)
    await repo.upsert_setting(
        organization_id=user.organization_id,
        key=setting_key,
        value=value,
        value_type=definition.value_type,
        user_id=user.id,
    )
    runtime_settings.set_runtime_setting_cache(user.organization_id, setting_key, value)
    _refresh_runtime_setting_dependents(setting_key)

    audit_repo = AdminAuditRepository(pool)
    await audit_repo.log_for_user(
        user,
        request,
        action=audit_actions.SETTING_UPDATE,
        entity_type="settings",
        entity_label=definition.title,
        changed_fields=[setting_key],
        old_values={"value": old_value, "source": old_source},
        new_values={"value": value, "source": "database"},
        notes="runtime setting updated from Settings UI",
        extra_context={"setting_key": setting_key, "env_var": definition.env_var},
    )
    message = f"{definition.title} updated."
    if _runtime_settings_wants_json(request):
        return JSONResponse(
            _runtime_setting_payload(user.organization_id, setting_key, message)
        )
    return RedirectResponse(
        f"/settings/runtime?success={quote(message)}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# /settings/danger — owner-only destructive actions
# ---------------------------------------------------------------------------


@router.get("/settings/danger", response_class=HTMLResponse)
async def settings_danger(
    request: Request,
    error: str | None = Query(default=None),
    success: str | None = Query(default=None),
):
    user = await get_user_context(request)
    if _role_value(user) != "owner":
        raise HTTPException(status_code=403, detail="Owner access required.")
    pool = await get_pool()
    org_repo = OrganizationRepository(pool)
    user_repo = UserRepository(pool)

    org = await org_repo.get_by_id(user.organization_id)
    members = await user_repo.get_by_organization(user.organization_id)
    eligible_owners = [
        m for m in members
        if m.get("role") == "admin" and m.get("is_active") and m["id"] != user.id
    ]
    return templates.TemplateResponse(
        request,
        "settings/danger.html",
        {
            "user": user,
            "organization": org,
            "eligible_owners": eligible_owners,
            "error": error,
            "success": success,
        },
    )


@router.post("/settings/sessions/revoke-all")
async def settings_revoke_all_sessions(request: Request):
    """Sign every device out for the current user."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    audit_repo = AdminAuditRepository(pool)

    await destroy_session(pool, user.id)
    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.SESSION_REVOKE_ALL,
        entity_type="user",
        entity_id=user.id,
        notes="self-service revoke-all-sessions",
    )

    response = RedirectResponse("/login", status_code=303)
    params = get_cookie_params()
    response.delete_cookie(SESSION_COOKIE_NAME, **params)
    response.delete_cookie("lucent_impersonate", **params)
    return response


@router.post("/settings/organization/transfer-ownership")
async def settings_transfer_ownership(
    request: Request, new_owner_id: UUID = Form(...)
):
    """Owner-only: transfer the owner role to another admin."""
    user = await get_user_context(request)
    await _check_csrf(request)
    if _role_value(user) != "owner":
        raise HTTPException(status_code=403, detail="Owner access required.")
    if new_owner_id == user.id:
        return RedirectResponse(
            f"/settings/danger?error={quote('You are already the owner.')}", status_code=303
        )

    pool = await get_pool()
    user_repo = UserRepository(pool)
    audit_repo = AdminAuditRepository(pool)

    target = await user_repo.get_by_id(new_owner_id)
    if not target or target.get("organization_id") != user.organization_id:
        return RedirectResponse(
            f"/settings/danger?error={quote('User not found in this workspace.')}",
            status_code=303,
        )
    if target.get("role") != "admin":
        return RedirectResponse(
            f"/settings/danger?error={quote('You can only transfer ownership to an admin.')}",
            status_code=303,
        )

    # Promote target, demote self. The unique-owner constraint requires we do
    # the demotion first to avoid a partial-index conflict.
    await user_repo.update_role(user.id, "admin")
    try:
        await user_repo.update_role(new_owner_id, "owner")
    except Exception:
        # Rollback: restore self as owner.
        await user_repo.update_role(user.id, "owner")
        return RedirectResponse(
            f"/settings/danger?error={quote('Transfer failed; ownership unchanged.')}",
            status_code=303,
        )

    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.ORG_TRANSFER_OWNERSHIP,
        entity_type="organization",
        entity_id=user.organization_id,
        old_values={"owner_id": str(user.id)},
        new_values={"owner_id": str(new_owner_id)},
        notes="ownership transferred",
    )
    return RedirectResponse(
        f"/settings/account?success={quote('Ownership transferred. You are now an admin.')}",
        status_code=303,
    )


@router.post("/settings/organization/delete")
async def settings_delete_org(request: Request, confirm_name: str = Form(...)):
    """Owner-only: permanently delete the workspace (irreversible)."""
    user = await get_user_context(request)
    await _check_csrf(request)
    if _role_value(user) != "owner":
        raise HTTPException(status_code=403, detail="Owner access required.")

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)
    audit_repo = AdminAuditRepository(pool)

    org = await org_repo.get_by_id(user.organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if confirm_name.strip() != org["name"]:
        return RedirectResponse(
            f"/settings/danger?error={quote('Confirmation name did not match.')}",
            status_code=303,
        )

    # Log BEFORE deleting — audit row gets dropped on cascade, but we log so
    # the action is at least visible until the cascade fires. For real
    # multi-org deployments the audit log should live in a separate db.
    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.ORG_DELETE,
        entity_type="organization",
        entity_id=user.organization_id,
        entity_label=org["name"],
        notes="workspace permanently deleted by owner",
    )
    await org_repo.delete(user.organization_id)

    response = RedirectResponse("/login?deleted=1", status_code=303)
    params = get_cookie_params()
    response.delete_cookie(SESSION_COOKIE_NAME, **params)
    return response
