"""User management and impersonation routes (admin)."""

import secrets
import time
from math import ceil
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.auth_providers import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_HOURS,
    create_session,
    get_cookie_params,
    hash_session_token,
    sign_value,
)
from lucent.db import UserRepository, get_pool
from lucent.logging import get_logger
from lucent.mode import is_team_mode

from ._shared import _check_csrf, get_user_context, templates

logger = get_logger("web.routes.admin")

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
    if not is_team_mode():
        raise HTTPException(status_code=404, detail="User management requires team mode")
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
        response.delete_cookie("lucent_temp_pw_ref", path="/users")
    return response


@router.post("/users/create", response_class=HTMLResponse)
async def create_user(
    request: Request,
    display_name: str = Form(...),
    email: str = Form(...),
    role: str = Form("member"),
):
    """Create a new user in the organization (team mode only)."""
    if not is_team_mode():
        raise HTTPException(status_code=404, detail="User management requires team mode")
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
                url=f"/users?error=User+with+email+{email}+already+exists",
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

    # Store temp password server-side with opaque reference token (M6)
    response = RedirectResponse(url="/users?success=user_created", status_code=303)
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
        max_age=60,
        path="/users",
    )
    return response


@router.post("/users/{user_id}/reset-password")
async def reset_user_password_web(request: Request, user_id: UUID):
    """Reset a user's password (admin action, team mode only)."""
    if not is_team_mode():
        raise HTTPException(status_code=404, detail="User management requires team mode")
    await _check_csrf(request)
    user = await get_user_context(request)

    # Check permission
    user_role_value = user.role if isinstance(user.role, str) else user.role.value
    if user_role_value not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Permission denied")

    # Can't reset own password via this endpoint
    if user_id == user.id:
        return RedirectResponse(
            url="/users?error=Use+Settings+to+change+your+own+password", status_code=303
        )

    pool = await get_pool()
    user_repo = UserRepository(pool)

    target = await user_repo.get_by_id(user_id)
    if target is None:
        return RedirectResponse(url="/users?error=User+not+found", status_code=303)

    if target.get("organization_id") != user.organization_id:
        return RedirectResponse(url="/users?error=User+not+found", status_code=303)

    from lucent.rbac import can_manage_user

    if not can_manage_user(user_role_value, target.get("role", "member")):
        return RedirectResponse(
            url="/users?error=You+cannot+manage+this+user", status_code=303
        )

    from lucent.auth_providers import admin_reset_password

    temp_password = await admin_reset_password(pool, user_id)

    # Store temp password server-side with opaque reference token (M6)
    response = RedirectResponse(url="/users?success=password_reset", status_code=303)
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
        max_age=60,
        path="/users",
    )
    return response


@router.post("/users/{user_id}/impersonate")
async def start_impersonation(request: Request, user_id: UUID):
    """Start impersonating a user (team mode only)."""
    if not is_team_mode():
        raise HTTPException(status_code=404, detail="Impersonation requires team mode")
    await _check_csrf(request)
    user = await get_user_context(request)

    # Only owners can impersonate (admins have limited impersonation in the dep)
    user_role_value = user.role if isinstance(user.role, str) else user.role.value
    if user_role_value not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Permission denied")

    # Can't impersonate yourself
    if user_id == user.id:
        return RedirectResponse(url="/users?error=Cannot+impersonate+yourself", status_code=303)

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
            url="/users?error=Admins+can+only+impersonate+members", status_code=303
        )
    if user_role_value == "owner" and target_role == "owner":
        return RedirectResponse(url="/users?error=Cannot+impersonate+other+owners", status_code=303)

    # Regenerate session to prevent session fixation during impersonation
    try:
        new_token = await create_session(pool, user.id)
    except Exception:
        logger.exception("Error regenerating session for impersonation")
        raise HTTPException(status_code=500, detail="Failed to start impersonation")

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
    if not is_team_mode():
        raise HTTPException(status_code=404, detail="Impersonation requires team mode")
    await _check_csrf(request)
    response = RedirectResponse(url="/users", status_code=303)
    params = get_cookie_params()
    response.delete_cookie(key="lucent_impersonate", **params)
    return response


# =============================================================================
# Model Management (Admin)
# =============================================================================

MODEL_CATEGORIES = [
    "general", "fast", "reasoning", "agentic", "frontier", "research", "visual",
]
MODEL_TAGS = [
    "coding", "frontier", "reasoning", "agentic", "fast", "research", "tools",
    "reflection", "general", "writing", "lightweight", "preview", "default",
]
MODEL_PROVIDERS = ["anthropic", "google", "ollama", "openai"]


async def _require_admin(request: Request):
    user = await get_user_context(request)
    role_val = user.role.value if hasattr(user.role, "value") else str(user.role)
    if role_val not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _parse_tags(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


@router.get("/models", response_class=HTMLResponse)
async def models_list(request: Request):
    """List all models with admin controls."""
    user = await _require_admin(request)
    pool = await get_pool()
    from lucent.db.models import ModelRepository

    repo = ModelRepository(pool)
    models = (await repo.list_models())["items"]

    providers = sorted({m["provider"] for m in models})
    enabled_count = sum(1 for m in models if m["is_enabled"])

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
        },
    )


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
    return RedirectResponse(url="/models", status_code=303)


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

    await repo.update_model(
        model_id,
        name=form.get("name", model["name"]),
        provider=form.get("provider", model["provider"]),
        category=form.get("category", model["category"]),
        api_model_id=form.get("api_model_id", model["api_model_id"]),
        context_window=int(form.get("context_window") or 0),
        notes=form.get("notes", model["notes"]),
        tags=_parse_tags(form.get("tags", "")),
        supports_tools="supports_tools" in form,
        supports_vision="supports_vision" in form,
    )
    return RedirectResponse(url="/models?success=Model+updated", status_code=303)


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
        return RedirectResponse(url="/models?error=Model+ID+is+required", status_code=303)

    repo = ModelRepository(pool)
    existing = await repo.get_model(model_id)
    if existing:
        return RedirectResponse(url="/models?error=Model+ID+already+exists", status_code=303)

    await repo.create_model(
        model_id=model_id,
        provider=form.get("provider", "openai"),
        name=form.get("name", model_id),
        category=form.get("category", "general"),
        api_model_id=form.get("api_model_id", "") or model_id,
        context_window=int(form.get("context_window") or 0),
        notes=form.get("notes", ""),
        tags=_parse_tags(form.get("tags", "")),
        supports_tools="supports_tools" in form,
        supports_vision="supports_vision" in form,
        org_id=str(user.organization_id),
    )
    return RedirectResponse(url="/models?success=Model+added", status_code=303)


@router.post("/models/{model_id:path}/delete")
async def delete_model(request: Request, model_id: str):
    """Remove a model from the registry."""
    await _check_csrf(request)
    await _require_admin(request)
    pool = await get_pool()
    from lucent.db.models import ModelRepository

    repo = ModelRepository(pool)
    await repo.delete_model(model_id)
    return RedirectResponse(url="/models?success=Model+removed", status_code=303)
