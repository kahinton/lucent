"""User management and impersonation routes (admin)."""

from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
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


# =============================================================================
# Users (Admin)
# =============================================================================


@router.get("/users", response_class=HTMLResponse)
async def users_list(request: Request):
    """List organization users (team mode only)."""
    if not is_team_mode():
        raise HTTPException(status_code=404, detail="User management requires team mode")
    user = await get_user_context(request)
    pool = await get_pool()
    user_repo = UserRepository(pool)

    users = await user_repo.get_by_organization(user.organization_id)

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
    temp_pw_cookie = request.cookies.get("lucent_temp_pw", "")
    if temp_pw_cookie and ":" in temp_pw_cookie:
        import hashlib
        import hmac

        pw, sig = temp_pw_cookie.rsplit(":", 1)
        session_token = request.cookies.get("lucent_session", "")
        expected_sig = hmac.new(session_token.encode(), pw.encode(), hashlib.sha256).hexdigest()[
            :16
        ]
        if hmac.compare_digest(sig, expected_sig):
            temp_pw_display = pw

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
        },
    )
    # Clear the temp password cookie after reading
    if temp_pw_cookie:
        response.delete_cookie("lucent_temp_pw", path="/users")
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
    import secrets

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

    # Store temp password in session (not the URL) for one-time display
    response = RedirectResponse(url="/users?success=user_created", status_code=303)
    # Use a short-lived signed cookie for the temp password display
    import hashlib
    import hmac

    session_token = request.cookies.get("lucent_session", "")
    sig = hmac.new(session_token.encode(), temp_password.encode(), hashlib.sha256).hexdigest()[:16]
    params = get_cookie_params()
    response.set_cookie(
        key="lucent_temp_pw",
        value=f"{temp_password}:{sig}",
        httponly=True,
        secure=params["secure"],
        samesite="lax",
        max_age=60,  # Expires after 60 seconds
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

    # Use same temp password cookie pattern as user creation
    import hashlib
    import hmac

    session_token = request.cookies.get("lucent_session", "")
    sig = hmac.new(session_token.encode(), temp_password.encode(), hashlib.sha256).hexdigest()[:16]
    params = get_cookie_params()
    response = RedirectResponse(url="/users?success=password_reset", status_code=303)
    response.set_cookie(
        key="lucent_temp_pw",
        value=f"{temp_password}:{sig}",
        httponly=True,
        secure=params["secure"],
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
