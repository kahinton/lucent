"""Shared helpers for web routes — templates, CSRF, user context, form utilities."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from lucent.api.deps import CurrentUser
from lucent.auth import set_current_user
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL_HOURS,
    create_initial_user,
    create_session,
    destroy_session,
    generate_csrf_token,
    get_auth_provider,
    get_cookie_params,
    hash_session_token,
    is_first_run,
    set_user_password,
    sign_value,
    validate_csrf_token,
    validate_password_complexity,
    validate_session,
    verify_signed_value,
)
from lucent.db import (
    AccessRepository,
    ApiKeyRepository,
    AuditRepository,
    MemoryRepository,
    OrganizationRepository,
    UserRepository,
    get_pool,
)
from lucent.logging import get_logger
from lucent.mode import is_team_mode
from lucent.rbac import Role

logger = get_logger("web.routes")


# Set up templates
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Custom template filters
def format_datetime(value: datetime | None) -> str:
    """Format datetime for display."""
    if value is None:
        return "Never"
    return value.strftime("%Y-%m-%d %H:%M")


def truncate(value: str, length: int = 100) -> str:
    """Truncate string to length."""
    if len(value) <= length:
        return value
    return value[:length] + "..."


# Register filters
templates.env.filters["datetime"] = format_datetime
templates.env.filters["truncate"] = truncate

# Make deployment mode and CSRF available to all templates
templates.env.globals["team_mode"] = is_team_mode
templates.env.globals["csrf_field_name"] = CSRF_FIELD_NAME


def _get_csrf_for_request(request: Request) -> str:
    """Get or generate a CSRF token for a request.

    Reuses the token from the cookie if valid, otherwise generates a new one.
    """
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing and validate_csrf_token(existing):
        return existing
    return generate_csrf_token()


def _set_csrf_cookie(response, token: str) -> None:
    """Set the CSRF cookie on a response."""
    params = get_cookie_params()
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=False,  # Must be readable by JS for dynamic forms
        samesite=params["samesite"],
        secure=params["secure"],
        path=params["path"],
        max_age=SESSION_TTL_HOURS * 3600,
    )


def _parse_env_vars(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines into a dict, skipping blank/invalid lines."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            key = k.strip()
            if key:
                result[key] = v.strip()
    return result


def _build_metadata_from_form(
    memory_type: str,
    *,
    meta_context: str = "",
    meta_outcome: str = "",
    meta_lessons_learned: str = "",
    meta_related_entities: str = "",
    meta_category: str = "",
    meta_language: str = "",
    meta_version_info: str = "",
    meta_repo: str = "",
    meta_filename: str = "",
    meta_code_snippet: str = "",
    meta_references: str = "",
    meta_estimated_time: str = "",
    meta_success_criteria: str = "",
    meta_prerequisites: str = "",
    meta_common_pitfalls: str = "",
    meta_steps: str = "",
    meta_status: str = "active",
    meta_priority: int = 3,
    meta_deadline: str = "",
    meta_blockers: str = "",
    meta_milestones: str = "",
    meta_user_id: str = "",
    meta_name: str = "",
    meta_relationship: str = "",
    meta_organization: str = "",
    meta_role: str = "",
    meta_email: str = "",
    meta_phone: str = "",
    meta_linkedin: str = "",
    meta_github: str = "",
    meta_preferences: str = "",
) -> dict:
    """Build and validate type-specific metadata from web form fields."""
    from lucent.models.validation import validate_metadata

    metadata: dict = {}

    if memory_type == "experience":
        if meta_context:
            metadata["context"] = meta_context
        if meta_outcome:
            metadata["outcome"] = meta_outcome
        if meta_lessons_learned:
            metadata["lessons_learned"] = [
                item.strip() for item in meta_lessons_learned.split(",") if item.strip()
            ]
        if meta_related_entities:
            metadata["related_entities"] = [
                e.strip() for e in meta_related_entities.split(",") if e.strip()
            ]

    elif memory_type == "technical":
        if meta_category:
            metadata["category"] = meta_category
        if meta_language:
            metadata["language"] = meta_language
        if meta_version_info:
            metadata["version_info"] = meta_version_info
        if meta_repo:
            metadata["repo"] = meta_repo
        if meta_filename:
            metadata["filename"] = meta_filename
        if meta_code_snippet:
            metadata["code_snippet"] = meta_code_snippet
        if meta_references:
            metadata["references"] = [r.strip() for r in meta_references.split(",") if r.strip()]

    elif memory_type == "procedural":
        if meta_estimated_time:
            metadata["estimated_time"] = meta_estimated_time
        if meta_success_criteria:
            metadata["success_criteria"] = meta_success_criteria
        if meta_prerequisites:
            metadata["prerequisites"] = [
                p.strip() for p in meta_prerequisites.split(",") if p.strip()
            ]
        if meta_common_pitfalls:
            metadata["common_pitfalls"] = [
                p.strip() for p in meta_common_pitfalls.split(",") if p.strip()
            ]
        if meta_steps:
            steps = []
            for i, line in enumerate(meta_steps.strip().split("\n"), 1):
                if line.strip():
                    parts = line.split("|", 1)
                    step = {"order": i, "description": parts[0].strip()}
                    if len(parts) > 1 and parts[1].strip():
                        step["notes"] = parts[1].strip()
                    steps.append(step)
            if steps:
                metadata["steps"] = steps

    elif memory_type == "goal":
        metadata["status"] = meta_status
        metadata["priority"] = meta_priority
        if meta_deadline:
            metadata["deadline"] = meta_deadline
        if meta_blockers:
            metadata["blockers"] = [b.strip() for b in meta_blockers.split(",") if b.strip()]
        if meta_milestones:
            metadata["milestones"] = [
                {"description": m.strip(), "status": "active"}
                for m in meta_milestones.split(",")
                if m.strip()
            ]

    elif memory_type == "individual":
        if meta_user_id:
            metadata["user_id"] = meta_user_id
        if meta_name:
            metadata["name"] = meta_name
        if meta_relationship:
            metadata["relationship"] = meta_relationship
        if meta_organization:
            metadata["organization"] = meta_organization
        if meta_role:
            metadata["role"] = meta_role
        contact_info = {}
        if meta_email:
            contact_info["email"] = meta_email
        if meta_phone:
            contact_info["phone"] = meta_phone
        if meta_linkedin:
            contact_info["linkedin"] = meta_linkedin
        if meta_github:
            contact_info["github"] = meta_github
        if contact_info:
            metadata["contact_info"] = contact_info
        if meta_preferences:
            metadata["preferences"] = [p.strip() for p in meta_preferences.split(",") if p.strip()]

    # Validate against the Pydantic model
    try:
        return validate_metadata(memory_type, metadata)
    except ValueError:
        return metadata  # Return unvalidated if validation fails (graceful degradation)


async def _check_csrf(request: Request, form_token: str | None = None) -> None:
    """Verify CSRF token: form field must match cookie.

    Uses the double-submit cookie pattern: the cookie value must match
    the form field value. No signing or secrets involved.
    """
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)

    if form_token is None:
        form = await request.form()
        form_token = str(form.get(CSRF_FIELD_NAME, ""))

    # Log for debugging
    from lucent.logging import get_logger

    _csrf_logger = get_logger("csrf")
    _csrf_logger.debug(
        "CSRF check: cookie=%s form=%s",
        "present" if cookie_token else "NONE",
        "present" if form_token else "NONE",
    )

    if not cookie_token:
        _csrf_logger.warning("CSRF failed: no cookie")
        raise HTTPException(status_code=403, detail="CSRF validation failed - no cookie")

    if not form_token:
        _csrf_logger.warning("CSRF failed: no form token")
        raise HTTPException(status_code=403, detail="CSRF validation failed - no form token")

    if form_token != cookie_token:
        _csrf_logger.warning("CSRF failed: token mismatch")
        raise HTTPException(status_code=403, detail="CSRF validation failed - token mismatch")


async def get_user_context(
    request: Request, *, allow_force_password_change: bool = False
) -> CurrentUser:
    """Get the current user for web routes via session cookie.

    Validates the session cookie and returns a CurrentUser.
    Raises HTTPException(303) to redirect to login if not authenticated.
    Also handles impersonation via cookie (team mode only).

    If force_password_change is set on the user and allow_force_password_change
    is False, redirects to /force-password-change.
    """
    pool = await get_pool()

    # Check session cookie
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    user = await validate_session(pool, session_token)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    # Check force password change before building user context
    if not allow_force_password_change and user.get("force_password_change"):
        raise HTTPException(
            status_code=303, headers={"Location": "/force-password-change"}
        )

    # Set context var for downstream code
    set_current_user(user)

    # Build CurrentUser
    current_user = CurrentUser(
        id=user["id"],
        organization_id=user.get("organization_id"),
        role=user.get("role", "member"),
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="session",
    )

    # Handle impersonation (team mode only)
    impersonate_cookie = request.cookies.get("lucent_impersonate")
    if impersonate_cookie and is_team_mode():
        from lucent.auth import set_impersonating_user

        # Verify the cookie signature
        impersonate_value = verify_signed_value(impersonate_cookie)
        if (
            impersonate_value
            and ":" in impersonate_value
            and current_user.role in (Role.ADMIN, Role.OWNER)
        ):
            impersonate_user_id_str, cookie_session_hash = impersonate_value.split(":", 1)
            # Verify the impersonation cookie is bound to this session
            current_session_hash = hash_session_token(session_token)
            if cookie_session_hash != current_session_hash:
                pass  # Session mismatch — impersonation cookie not valid for this session
            else:
                try:
                    target_user_id = UUID(impersonate_user_id_str)
                    if target_user_id != current_user.id:
                        user_repo = UserRepository(pool)
                        target_user = await user_repo.get_by_id(target_user_id)

                        if (
                            target_user
                            and target_user.get("organization_id") == current_user.organization_id
                        ):
                            target_role = target_user.get("role", "member")
                            can_impersonate = (
                                current_user.role == Role.ADMIN and target_role == "member"
                            ) or (current_user.role == Role.OWNER and target_role != "owner")

                            if can_impersonate:
                                set_current_user(target_user)
                                set_impersonating_user(
                                    {
                                        "id": current_user.id,
                                        "display_name": current_user.display_name,
                                        "role": current_user.role.value,
                                    }
                                )
                                return CurrentUser(
                                    id=target_user["id"],
                                    organization_id=target_user.get("organization_id"),
                                    role=target_user.get("role", "member"),
                                    email=target_user.get("email"),
                                    display_name=target_user.get("display_name"),
                                    auth_method="impersonation",
                                    impersonator_id=current_user.id,
                                    impersonator_display_name=current_user.display_name,
                                )
                except (ValueError, Exception):
                    logger.debug("Impersonation failed for header value", exc_info=True)

    return current_user
