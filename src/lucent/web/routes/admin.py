"""User management and impersonation routes (admin)."""

import logging
import secrets
import time
from math import ceil
from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    SECURE_COOKIES,
    SESSION_COOKIE_NAME,
    SESSION_TTL_HOURS,
    create_session,
    get_cookie_params,
    hash_session_token,
    sign_value,
)
from lucent.db import AdminAuditRepository, UserRepository, get_pool
from lucent.db import admin_audit as audit_actions
from lucent.llm.model_engine_validation import normalize_engine, validate_engine_override

from ._shared import _check_csrf, get_user_context, templates

logger = logging.getLogger(__name__)

router = APIRouter()

ALLOWED_PER_PAGE = {10, 25, 50, 100}

# In-memory store for temp password reference tokens (M6 security fix)
_temp_pw_store: dict[str, dict] = {}
_TEMP_PW_MAX_ENTRIES = 100
_TEMP_PW_TTL = 60


def _cleanup_temp_pw_store():
    now = time.time()
    expired = [k for k, v in _temp_pw_store.items() if v["expires"] < now]
    for k in expired:
        del _temp_pw_store[k]


# =============================================================================
# Users (Admin)
# =============================================================================


@router.get("/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    page: int = 1,
    per_page: int = 25,
):
    """List organization users (team mode only)."""
    user = await get_user_context(request)
    pool = await get_pool()
    user_repo = UserRepository(pool)

    all_users = await user_repo.get_by_organization(user.organization_id)

    # In-memory pagination (get_by_organization returns plain list)
    page = max(1, page)
    per_page = per_page if per_page in ALLOWED_PER_PAGE else 25
    total_count = len(all_users)
    total_pages = ceil(total_count / per_page) if total_count > 0 else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    users = all_users[offset : offset + per_page]

    # Check if user can manage users (admin or owner)
    can_manage = (
        user.role in ("admin", "owner")
        if hasattr(user, "role") and isinstance(user.role, str)
        else user.role.value in ("admin", "owner")
    )
    can_impersonate = (
        user.role == "owner" if isinstance(user.role, str) else user.role.value == "owner"
    )

    # Check for one-time temp password display (from user creation)
    temp_pw_display = None
    ref = request.cookies.get("lucent_temp_pw_ref", "")
    if ref and ref in _temp_pw_store and _temp_pw_store[ref]["expires"] > time.time():
        temp_pw_display = _temp_pw_store.pop(ref)["password"]

    success = request.query_params.get("success")
    error = request.query_params.get("error")

    response = templates.TemplateResponse(
        request,
        "users.html",
        {
            "user": user,
            "users": users,
            "can_manage": can_manage,
            "can_impersonate": can_impersonate,
            "temp_password": temp_pw_display,
            "success": success,
            "error": error,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total_count": total_count,
        },
    )
    # Clear the temp password ref cookie after reading
    if ref:
        response.delete_cookie("lucent_temp_pw_ref", path="/settings/users")
    return response


@router.post("/users/create", response_class=HTMLResponse)
async def create_user(
    request: Request,
    display_name: str = Form(...),
    email: str = Form(...),
    role: str = Form("member"),
):
    """Create a new user in the organization (team mode only)."""
    user = await get_user_context(request)
    await _check_csrf(request)

    # Check permission
    if not (
        hasattr(user, "role")
        and (
            (isinstance(user.role, str) and user.role in ("admin", "owner"))
            or (hasattr(user.role, "value") and user.role.value in ("admin", "owner"))
        )
    ):
        raise HTTPException(status_code=403, detail="Permission denied")

    pool = await get_pool()
    user_repo = UserRepository(pool)

    # Generate a unique external_id for local users
    external_id = f"local_{secrets.token_hex(8)}"

    # Validate role
    valid_roles = ["member", "admin"]
    user_role_value = user.role if isinstance(user.role, str) else user.role.value
    if user_role_value == "owner":
        valid_roles.append("owner")

    if role not in valid_roles:
        role = "member"

    # Check if user with this email already exists
    users = await user_repo.get_by_organization(user.organization_id)
    for u in users:
        if u.get("email") == email:
            # Redirect back with error
            return RedirectResponse(
                url=f"/settings/users?error=User+with+email+{email}+already+exists",
                status_code=303,
            )

    # Create the user
    new_user = await user_repo.create(
        external_id=external_id,
        provider="local",
        organization_id=user.organization_id,
        email=email,
        display_name=display_name,
    )

    # Update role if not member
    if role != "member":
        await user_repo.update_role(new_user["id"], role)

    # Set a temporary password so the user can log in
    from lucent.auth_providers import set_user_password

    temp_password = secrets.token_urlsafe(12)
    await set_user_password(pool, new_user["id"], temp_password)

    # Audit-log creation. Avoids logging password — only metadata.
    audit_repo = AdminAuditRepository(pool)
    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.USER_CREATE,
        entity_type="user",
        entity_id=new_user["id"],
        entity_label=display_name or email,
        new_values={"email": email, "role": role, "display_name": display_name},
        notes="member created via /users/create; temp password issued",
    )

    # Store temp password server-side with opaque reference token (M6)
    response = RedirectResponse(url="/settings/users?success=user_created", status_code=303)
    _cleanup_temp_pw_store()
    if len(_temp_pw_store) >= _TEMP_PW_MAX_ENTRIES:
        _temp_pw_store.clear()
    ref_token = secrets.token_urlsafe(32)
    _temp_pw_store[ref_token] = {"password": temp_password, "expires": time.time() + _TEMP_PW_TTL}
    response.set_cookie(
        key="lucent_temp_pw_ref",
        value=ref_token,
        httponly=True,
        samesite="lax",
        secure=SECURE_COOKIES,
        max_age=60,
        path="/settings/users",
    )
    return response


@router.post("/users/{user_id}/reset-password")
async def reset_user_password_web(request: Request, user_id: UUID):
    """Reset a user's password (admin action, team mode only)."""
    await _check_csrf(request)
    user = await get_user_context(request)

    # Check permission
    user_role_value = user.role if isinstance(user.role, str) else user.role.value
    if user_role_value not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Permission denied")

    # Can't reset own password via this endpoint
    if user_id == user.id:
        return RedirectResponse(
            url="/settings/users?error=Use+Settings+to+change+your+own+password", status_code=303
        )

    pool = await get_pool()
    user_repo = UserRepository(pool)

    target = await user_repo.get_by_id(user_id)
    if target is None:
        return RedirectResponse(url="/settings/users?error=User+not+found", status_code=303)

    if target.get("organization_id") != user.organization_id:
        return RedirectResponse(url="/settings/users?error=User+not+found", status_code=303)

    from lucent.rbac import can_manage_user

    if not can_manage_user(user_role_value, target.get("role", "member")):
        return RedirectResponse(
            url="/settings/users?error=You+cannot+manage+this+user", status_code=303
        )

    from lucent.auth_providers import admin_reset_password

    temp_password = await admin_reset_password(pool, user_id)

    audit_repo = AdminAuditRepository(pool)
    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.USER_PASSWORD_RESET,
        entity_type="user",
        entity_id=user_id,
        entity_label=target.get("display_name") or target.get("email"),
        notes="admin-initiated password reset",
    )

    # Store temp password server-side with opaque reference token (M6)
    response = RedirectResponse(url="/settings/users?success=password_reset", status_code=303)
    _cleanup_temp_pw_store()
    if len(_temp_pw_store) >= _TEMP_PW_MAX_ENTRIES:
        _temp_pw_store.clear()
    ref_token = secrets.token_urlsafe(32)
    _temp_pw_store[ref_token] = {"password": temp_password, "expires": time.time() + _TEMP_PW_TTL}
    response.set_cookie(
        key="lucent_temp_pw_ref",
        value=ref_token,
        httponly=True,
        samesite="lax",
        secure=SECURE_COOKIES,
        max_age=60,
        path="/settings/users",
    )
    return response


@router.post("/users/{user_id}/impersonate")
async def start_impersonation(request: Request, user_id: UUID):
    """Start impersonating a user (team mode only)."""
    await _check_csrf(request)
    user = await get_user_context(request)

    # Only owners can impersonate (admins have limited impersonation in the dep)
    user_role_value = user.role if isinstance(user.role, str) else user.role.value
    if user_role_value not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Permission denied")

    # Can't impersonate yourself
    if user_id == user.id:
        return RedirectResponse(url="/settings/users?error=Cannot+impersonate+yourself", status_code=303)

    pool = await get_pool()
    user_repo = UserRepository(pool)

    target = await user_repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Check same org
    if target.get("organization_id") != user.organization_id:
        raise HTTPException(status_code=404, detail="User not found")

    # Check role restrictions
    target_role = target.get("role", "member")
    if user_role_value == "admin" and target_role != "member":
        return RedirectResponse(
            url="/settings/users?error=Admins+can+only+impersonate+members", status_code=303
        )
    if user_role_value == "owner" and target_role == "owner":
        return RedirectResponse(url="/settings/users?error=Cannot+impersonate+other+owners", status_code=303)

    # Regenerate session to prevent session fixation during impersonation
    try:
        new_token = await create_session(pool, user.id)
    except Exception:
        logger.exception("Error regenerating session for impersonation")
        raise HTTPException(status_code=500, detail="Failed to start impersonation")

    audit_repo = AdminAuditRepository(pool)
    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.IMPERSONATION_START,
        entity_type="user",
        entity_id=user_id,
        entity_label=target.get("display_name") or target.get("email"),
    )

    # Set signed impersonation cookie and new session cookie, then redirect
    # Bind the impersonation cookie to this session to prevent cookie theft
    session_hash = hash_session_token(new_token)
    response = RedirectResponse(url="/?impersonating=true", status_code=303)
    params = get_cookie_params()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=new_token,
        max_age=SESSION_TTL_HOURS * 3600,
        **params,
    )
    response.set_cookie(
        key="lucent_impersonate",
        value=sign_value(f"{user_id}:{session_hash}"),
        max_age=3600,  # 1 hour max
        **params,
    )
    return response


@router.post("/users/stop-impersonation")
async def stop_impersonation(request: Request):
    """Stop impersonating and return to original user (team mode only)."""
    await _check_csrf(request)
    # Best-effort audit. The current user context is the impersonated user; the
    # impersonator is captured via CurrentUser.impersonator_id when available.
    try:
        user = await get_user_context(request)
        pool = await get_pool()
        audit_repo = AdminAuditRepository(pool)
        await audit_repo.log_for_user(
            user, request,
            action=audit_actions.IMPERSONATION_STOP,
            entity_type="user",
            entity_id=user.id,
        )
    except Exception:
        logger.debug("impersonation stop audit log failed", exc_info=True)
    response = RedirectResponse(url="/settings/users", status_code=303)
    params = get_cookie_params()
    response.delete_cookie(key="lucent_impersonate", **params)
    return response


# =============================================================================
# Members — role change / deactivate / reactivate (added M5 settings redesign)
# =============================================================================


async def _load_target_user(
    pool, *, caller, target_user_id: UUID
) -> tuple[dict | None, str | None]:
    """Fetch a target user and validate same-org membership.

    Returns (target, error_message). target is None when validation fails.
    """
    user_repo = UserRepository(pool)
    target = await user_repo.get_by_id(target_user_id)
    if not target or target.get("organization_id") != caller.organization_id:
        return None, "User not found"
    return target, None


@router.post("/users/{user_id}/role")
async def change_user_role(request: Request, user_id: UUID, role: str = Form(...)):
    """Change a user's role. Owners only."""
    await _check_csrf(request)
    user = await get_user_context(request)
    caller_role = user.role if isinstance(user.role, str) else user.role.value
    if caller_role != "owner":
        raise HTTPException(status_code=403, detail="Only owners can change roles")
    if user_id == user.id:
        return RedirectResponse("/settings/users?error=You+cannot+change+your+own+role", status_code=303)
    if role not in ("member", "admin"):
        return RedirectResponse("/settings/users?error=Invalid+role", status_code=303)

    pool = await get_pool()
    target, err = await _load_target_user(pool, caller=user, target_user_id=user_id)
    if not target:
        return RedirectResponse(f"/settings/users?error={quote_plus(err or 'User not found')}", status_code=303)
    if target.get("role") == "owner":
        return RedirectResponse("/settings/users?error=Cannot+change+the+owner+role+here", status_code=303)

    user_repo = UserRepository(pool)
    audit_repo = AdminAuditRepository(pool)
    old_role = target.get("role", "member")
    if old_role == role:
        return RedirectResponse("/settings/users?success=Role+unchanged", status_code=303)

    await user_repo.update_role(user_id, role)
    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.USER_ROLE_CHANGE,
        entity_type="user",
        entity_id=user_id,
        entity_label=target.get("display_name") or target.get("email"),
        changed_fields=["role"],
        old_values={"role": old_role},
        new_values={"role": role},
    )
    return RedirectResponse("/settings/users?success=Role+updated", status_code=303)


@router.post("/users/{user_id}/deactivate")
async def deactivate_user(request: Request, user_id: UUID):
    """Disable a user account (admin/owner). Cannot deactivate self or owner."""
    await _check_csrf(request)
    user = await get_user_context(request)
    caller_role = user.role if isinstance(user.role, str) else user.role.value
    if caller_role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Permission denied")
    if user_id == user.id:
        return RedirectResponse("/settings/users?error=You+cannot+deactivate+yourself", status_code=303)

    pool = await get_pool()
    target, err = await _load_target_user(pool, caller=user, target_user_id=user_id)
    if not target:
        return RedirectResponse(f"/settings/users?error={quote_plus(err or 'User not found')}", status_code=303)

    from lucent.rbac import can_manage_user
    if not can_manage_user(caller_role, target.get("role", "member")):
        return RedirectResponse("/settings/users?error=You+cannot+deactivate+this+user", status_code=303)
    if target.get("role") == "owner":
        return RedirectResponse("/settings/users?error=Cannot+deactivate+the+owner", status_code=303)

    user_repo = UserRepository(pool)
    await user_repo.update(user_id, is_active=False)
    # Also kill any active session for them.
    from lucent.auth_providers import destroy_session
    try:
        await destroy_session(pool, user_id)
    except Exception:
        logger.debug("failed to destroy sessions for deactivated user", exc_info=True)

    audit_repo = AdminAuditRepository(pool)
    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.USER_DEACTIVATE,
        entity_type="user",
        entity_id=user_id,
        entity_label=target.get("display_name") or target.get("email"),
    )
    return RedirectResponse("/settings/users?success=Member+deactivated", status_code=303)


@router.post("/users/{user_id}/reactivate")
async def reactivate_user(request: Request, user_id: UUID):
    """Re-enable a user account (admin/owner)."""
    await _check_csrf(request)
    user = await get_user_context(request)
    caller_role = user.role if isinstance(user.role, str) else user.role.value
    if caller_role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Permission denied")

    pool = await get_pool()
    target, err = await _load_target_user(pool, caller=user, target_user_id=user_id)
    if not target:
        return RedirectResponse(f"/settings/users?error={quote_plus(err or 'User not found')}", status_code=303)

    from lucent.rbac import can_manage_user
    if not can_manage_user(caller_role, target.get("role", "member")):
        return RedirectResponse("/settings/users?error=You+cannot+reactivate+this+user", status_code=303)

    user_repo = UserRepository(pool)
    await user_repo.update(user_id, is_active=True)

    audit_repo = AdminAuditRepository(pool)
    await audit_repo.log_for_user(
        user, request,
        action=audit_actions.USER_REACTIVATE,
        entity_type="user",
        entity_id=user_id,
        entity_label=target.get("display_name") or target.get("email"),
    )
    return RedirectResponse("/settings/users?success=Member+reactivated", status_code=303)


# =============================================================================
# Model Management (Admin)
# =============================================================================

MODEL_CATEGORIES = [
    "general", "fast", "reasoning", "agentic", "frontier", "research", "visual",
]
MODEL_TAGS = [
    "coding", "frontier", "reasoning", "reasoning-effort", "agentic", "fast",
    "research", "tools", "reflection", "general", "writing", "lightweight",
    "preview", "default",
]
MODEL_PROVIDERS = ["anthropic", "copilot", "google", "ollama", "openai", "xai"]


async def _require_admin(request: Request):
    user = await get_user_context(request)
    role_val = user.role.value if hasattr(user.role, "value") else str(user.role)
    if role_val not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _parse_tags(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


def _parse_reasoning_efforts(form) -> list[str]:
    raw_values = form.getlist("reasoning_efforts") if hasattr(form, "getlist") else []
    values: list[str] = []
    for raw in raw_values:
        for part in str(raw).split(","):
            effort = part.strip().lower()
            if effort and len(effort) <= 64 and effort not in values:
                values.append(effort)
    return values


def _validate_engine_form(provider: str, engine: str | None) -> tuple[str | None, list[str], str | None]:
    try:
        normalized = normalize_engine(engine)
        warnings = validate_engine_override(provider, normalized)
        return normalized, warnings, None
    except ValueError as e:
        return None, [], str(e)


def _models_redirect(success: str | None = None, error: str | None = None, warning: str | None = None):
    params: list[str] = []
    if success:
        params.append(f"success={quote_plus(success)}")
    if error:
        params.append(f"error={quote_plus(error)}")
    if warning:
        params.append(f"warning={quote_plus(warning)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(url=f"/settings/models{suffix}", status_code=303)


def _visible_model_providers(models: list[dict], active_providers: set[str]) -> list[str]:
    """Return provider sections that should be shown on the models page.

    Provider-discovered rows are visible only when the provider is configured in
    this deployment. Manual/custom rows remain visible so admins can manage what
    they explicitly added through the UI.
    """
    visible: set[str] = set()
    for model in models:
        provider = model.get("provider")
        if not provider:
            continue
        if (
            provider in active_providers
            or model.get("is_custom")
            or model.get("discovery_source") == "manual"
        ):
            visible.add(provider)
    return sorted(visible)


async def _refresh_runtime_registry(pool) -> None:
    try:
        from lucent.model_registry import load_models_from_db

        await load_models_from_db(pool)
    except Exception:
        logger.debug("Failed to refresh model registry cache", exc_info=True)


@router.get("/models", response_class=HTMLResponse)
async def models_list(request: Request):
    """List all models with admin controls."""
    user = await _require_admin(request)
    pool = await get_pool()
    from lucent.db.models import ModelRepository
    from lucent.model_discovery import ModelDiscoveryService

    repo = ModelRepository(pool)
    all_models = (await repo.list_models(limit=500))["items"]

    discovery_service = ModelDiscoveryService(pool)
    provider_statuses = await discovery_service.provider_configuration_statuses(
        org_id=str(user.organization_id),
    )
    active_providers = set(
        await discovery_service.configured_providers_async(org_id=str(user.organization_id))
    )
    providers = _visible_model_providers(all_models, active_providers)
    models = [m for m in all_models if m["provider"] in providers]
    enabled_count = sum(1 for m in models if m["is_enabled"])

    # Per-model access summary for THIS org (models are a global catalog; access
    # is governed by org-scoped grants in resource_access_grants).
    org_id = str(user.organization_id)
    async with pool.acquire() as conn:
        grant_rows = await conn.fetch(
            "SELECT resource_id, principal_type, COUNT(*) AS n "
            "FROM resource_access_grants "
            "WHERE resource_type = 'model' AND organization_id = $1 "
            "GROUP BY resource_id, principal_type",
            UUID(org_id),
        )
    summary: dict[str, dict] = {}
    for r in grant_rows:
        s = summary.setdefault(
            str(r["resource_id"]), {"org": False, "users": 0, "groups": 0}
        )
        if r["principal_type"] == "org":
            s["org"] = True
        elif r["principal_type"] == "user":
            s["users"] += int(r["n"])
        elif r["principal_type"] == "group":
            s["groups"] += int(r["n"])
    for m in models:
        m["access_summary"] = summary.get(
            str(m["id"]), {"org": False, "users": 0, "groups": 0}
        )

    return templates.TemplateResponse(
        request,
        "models.html",
        {
            "user": user,
            "models": models,
            "providers": providers,
            "enabled_count": enabled_count,
            "total_count": len(models),
            "all_categories": MODEL_CATEGORIES,
            "all_tags": MODEL_TAGS,
            "all_providers": MODEL_PROVIDERS,
            "provider_statuses": provider_statuses,
        },
    )


@router.get("/models/{model_id}/access", response_class=HTMLResponse)
async def model_access_page(request: Request, model_id: str):
    """Per-model access management (who in this org may use the model)."""
    from .access_ui import build_access_context

    user = await _require_admin(request)
    pool = await get_pool()
    from lucent.db.models import ModelRepository

    repo = ModelRepository(pool)
    model = await repo.get_model(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    access = await build_access_context(
        pool,
        resource_type="model",
        resource_id=model_id,
        org_id=str(user.organization_id),
        user=user,
        redirect=request.url.path,
    )
    return templates.TemplateResponse(
        request,
        "model_access.html",
        {
            "user": user,
            "model": model,
            "access": access,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.post("/models/providers/{provider}/credential")
async def configure_model_provider_credential(request: Request, provider: str):
    """Store a workspace model-provider credential via configured secret storage."""
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()
    from lucent.model_discovery import (
        model_provider_credential_definitions,
        model_provider_secret_scope,
    )
    from lucent.secrets import SecretRegistry

    provider = provider.strip().lower()
    definition = next(
        (item for item in model_provider_credential_definitions() if item.provider == provider),
        None,
    )
    if not definition:
        return _models_redirect(error="Unsupported model provider")

    form = await request.form()
    credential = str(form.get("credential", "")).strip()
    if not credential:
        return _models_redirect(error=f"{definition.name} credential is required")

    secret_provider = SecretRegistry.get()
    await secret_provider.set(
        definition.secret_key,
        credential,
        model_provider_secret_scope(str(user.organization_id)),
    )

    audit_repo = AdminAuditRepository(pool)
    await audit_repo.log_for_user(
        user,
        request,
        action=audit_actions.SETTING_UPDATE,
        entity_type="settings",
        entity_label=f"{definition.name} model provider credential",
        changed_fields=[definition.secret_key],
        new_values={
            "provider": definition.provider,
            "secret_key": definition.secret_key,
            "configured": True,
        },
        notes="model provider credential updated from Models UI",
    )
    return _models_redirect(success=f"{definition.name} credential saved")


@router.post("/models/providers/{provider}/credential/clear")
async def clear_model_provider_credential(request: Request, provider: str):
    """Remove a DB-backed model-provider credential so env fallback can apply."""
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()
    from lucent.model_discovery import (
        model_provider_credential_definitions,
        model_provider_secret_scope,
    )
    from lucent.secrets import SecretRegistry

    provider = provider.strip().lower()
    definition = next(
        (item for item in model_provider_credential_definitions() if item.provider == provider),
        None,
    )
    if not definition:
        return _models_redirect(error="Unsupported model provider")

    secret_provider = SecretRegistry.get()
    deleted = await secret_provider.delete(
        definition.secret_key,
        model_provider_secret_scope(str(user.organization_id)),
    )

    audit_repo = AdminAuditRepository(pool)
    await audit_repo.log_for_user(
        user,
        request,
        action=audit_actions.SETTING_RESET,
        entity_type="settings",
        entity_label=f"{definition.name} model provider credential",
        changed_fields=[definition.secret_key],
        old_values={
            "provider": definition.provider,
            "secret_key": definition.secret_key,
            "configured": bool(deleted),
        },
        new_values={"configured": False},
        notes="model provider credential cleared from Models UI",
    )
    if deleted:
        return _models_redirect(success=f"{definition.name} credential cleared")
    return _models_redirect(warning=f"{definition.name} had no saved credential")


@router.post("/models/discover")
async def discover_models_web(request: Request):
    """Discover available models from configured providers and sync the DB."""
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()
    from lucent.model_discovery import ModelDiscoveryService

    form = await request.form()
    raw_providers = form.get("providers", "")
    providers = _parse_tags(raw_providers) if raw_providers else None
    disable_missing = "disable_missing" in form

    service = ModelDiscoveryService(pool)
    result = await service.sync(
        providers=providers,
        org_id=str(user.organization_id),
        disable_missing=disable_missing,
    )
    await _refresh_runtime_registry(pool)

    provider_count = result.get("provider_count", 0)
    discovered = result.get("discovered_count", 0)
    upserted = result.get("upserted_count", 0)
    errors = result.get("errors") or []
    if errors and not discovered:
        return _models_redirect(error=f"Model discovery failed for {len(errors)} provider(s)")
    if errors:
        return _models_redirect(
            success=f"Synced {upserted} models from {provider_count} provider(s)",
            warning=f"{len(errors)} provider(s) failed; check logs for details",
        )
    if provider_count == 0:
        return _models_redirect(
            warning="No model providers are configured. Set provider API keys or OLLAMA_HOST."
        )
    return _models_redirect(success=f"Synced {upserted} models from {provider_count} provider(s)")


@router.post("/models/{model_id:path}/toggle")
async def toggle_model(request: Request, model_id: str):
    """Enable or disable a model."""
    await _check_csrf(request)
    await _require_admin(request)
    pool = await get_pool()
    from lucent.db.models import ModelRepository

    repo = ModelRepository(pool)
    model = await repo.get_model(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    await repo.toggle_model(model_id, not model["is_enabled"])
    await _refresh_runtime_registry(pool)
    return RedirectResponse(url="/settings/models", status_code=303)


@router.post("/models/{model_id:path}/edit")
async def edit_model(request: Request, model_id: str):
    """Edit a model's properties."""
    await _check_csrf(request)
    await _require_admin(request)
    pool = await get_pool()
    from lucent.db.models import ModelRepository

    form = await request.form()
    repo = ModelRepository(pool)
    model = await repo.get_model(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    provider = form.get("provider", model["provider"])
    engine, warnings, error = _validate_engine_form(provider, form.get("engine"))
    if error:
        return _models_redirect(error=error)

    await repo.update_model(
        model_id,
        name=form.get("name", model["name"]),
        provider=provider,
        category=form.get("category", model["category"]),
        api_model_id=form.get("api_model_id", model["api_model_id"]),
        context_window=int(form.get("context_window") or 0),
        notes=form.get("notes", model["notes"]),
        tags=_parse_tags(form.get("tags", "")),
        reasoning_efforts=_parse_reasoning_efforts(form),
        supports_tools="supports_tools" in form,
        supports_vision="supports_vision" in form,
        engine=engine,
    )
    await _refresh_runtime_registry(pool)
    if warnings:
        return _models_redirect(success="Model updated", warning=warnings[0])
    return _models_redirect(success="Model updated")


@router.post("/models/add")
async def add_model(request: Request):
    """Add a new model."""
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()
    from lucent.db.models import ModelRepository

    form = await request.form()
    model_id = form.get("model_id", "").strip()
    if not model_id:
        return RedirectResponse(url="/settings/models?error=Model+ID+is+required", status_code=303)

    repo = ModelRepository(pool)
    existing = await repo.get_model(model_id)
    if existing:
        return RedirectResponse(url="/settings/models?error=Model+ID+already+exists", status_code=303)
    provider = form.get("provider", "openai")
    engine, warnings, error = _validate_engine_form(provider, form.get("engine"))
    if error:
        return _models_redirect(error=error)

    await repo.create_model(
        model_id=model_id,
        provider=provider,
        name=form.get("name", model_id),
        category=form.get("category", "general"),
        api_model_id=form.get("api_model_id", "") or model_id,
        context_window=int(form.get("context_window") or 0),
        notes=form.get("notes", ""),
        tags=_parse_tags(form.get("tags", "")),
        reasoning_efforts=_parse_reasoning_efforts(form),
        supports_tools="supports_tools" in form,
        supports_vision="supports_vision" in form,
        org_id=str(user.organization_id),
        engine=engine,
        discovery_source="manual",
        is_custom=True,
    )
    await _refresh_runtime_registry(pool)
    if warnings:
        return _models_redirect(success="Model added", warning=warnings[0])
    return _models_redirect(success="Model added")


@router.post("/models/{model_id:path}/delete")
async def delete_model(request: Request, model_id: str):
    """Remove a model from the registry."""
    await _check_csrf(request)
    await _require_admin(request)
    pool = await get_pool()
    from lucent.db.models import ModelRepository

    repo = ModelRepository(pool)
    await repo.delete_model(model_id)
    await _refresh_runtime_registry(pool)
    return RedirectResponse(url="/settings/models?success=Model+removed", status_code=303)
