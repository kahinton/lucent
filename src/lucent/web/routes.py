"""Web routes for Lucent admin dashboard using Jinja2 + HTMX."""

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


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


async def get_user_context(request: Request) -> CurrentUser:
    """Get the current user for web routes via session cookie.

    Validates the session cookie and returns a CurrentUser.
    Raises HTTPException(303) to redirect to login if not authenticated.
    Also handles impersonation via cookie (team mode only).
    """
    pool = await get_pool()

    # Check session cookie
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    user = await validate_session(pool, session_token)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})

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
                    pass  # Invalid UUID or other error, skip impersonation

    return current_user


# =============================================================================
# Authentication Routes (unauthenticated)
# =============================================================================


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    """Show the login page."""
    pool = await get_pool()

    # If first run, redirect to setup
    if await is_first_run(pool):
        return RedirectResponse("/setup", status_code=303)

    # If already logged in, redirect to dashboard
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        user = await validate_session(pool, session_token)
        if user:
            return RedirectResponse("/", status_code=303)

    provider = await get_auth_provider()
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "fields": provider.get_login_fields(),
            "error": error,
            "csrf_token": csrf_token,
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response


@router.post("/login")
async def login_submit(request: Request):
    """Handle login form submission."""
    form = await request.form()
    csrf_form_token = str(form.get(CSRF_FIELD_NAME, ""))
    await _check_csrf(request, form_token=csrf_form_token)
    pool = await get_pool()
    credentials = {key: str(value) for key, value in form.items()}

    provider = await get_auth_provider()

    try:
        user = await provider.authenticate(credentials)
    except Exception:
        logger.exception("Error during authentication")
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "fields": provider.get_login_fields(),
                "error": "An unexpected error occurred. Please try again later.",
            },
            status_code=500,
        )

    if user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "fields": provider.get_login_fields(),
                "error": "Invalid credentials. Please try again.",
            },
            status_code=401,
        )

    # Create session
    try:
        token = await create_session(pool, user["id"])
    except Exception:
        logger.exception("Error creating session")
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "fields": provider.get_login_fields(),
                "error": "An unexpected error occurred. Please try again later.",
            },
            status_code=500,
        )

    params = get_cookie_params()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_HOURS * 3600,
        **params,
    )
    # Set CSRF cookie for authenticated pages
    _set_csrf_cookie(response, generate_csrf_token())
    return response


@router.get("/logout")
async def logout(request: Request):
    """Log the user out."""
    pool = await get_pool()

    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        user = await validate_session(pool, session_token)
        if user:
            await destroy_session(pool, user["id"])

    response = RedirectResponse("/login", status_code=303)
    params = get_cookie_params()
    response.delete_cookie(key=SESSION_COOKIE_NAME, **params)
    response.delete_cookie(key="lucent_impersonate", **params)
    response.delete_cookie(key=CSRF_COOKIE_NAME, **params)
    return response


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, error: str | None = None):
    """Show the first-run setup page."""
    pool = await get_pool()

    if not await is_first_run(pool):
        return RedirectResponse("/login", status_code=303)

    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        "setup.html",
        {"request": request, "error": error, "csrf_token": csrf_token},
    )
    _set_csrf_cookie(response, csrf_token)
    return response


@router.post("/setup")
async def setup_submit(request: Request):
    """Handle first-run setup form submission."""
    await _check_csrf(request)
    pool = await get_pool()

    if not await is_first_run(pool):
        return RedirectResponse("/login", status_code=303)

    form = await request.form()
    display_name = str(form.get("display_name", "")).strip()
    email = str(form.get("email", "")).strip() or None
    password = str(form.get("password", ""))
    password_confirm = str(form.get("password_confirm", ""))

    # Validate
    if not display_name:
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "error": "Display name is required."},
            status_code=400,
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "error": "Password must be at least 8 characters."},
            status_code=400,
        )

    if password != password_confirm:
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "error": "Passwords do not match."},
            status_code=400,
        )

    try:
        user, api_key = await create_initial_user(pool, display_name, email, password)
    except Exception:
        logger.exception("Error during initial user setup")
        return templates.TemplateResponse(
            "setup.html",
            {
                "request": request,
                "error": "Setup failed due to an unexpected error. Please try again.",
            },
            status_code=500,
        )

    # Create session and log the user in
    try:
        token = await create_session(pool, user["id"])
    except Exception:
        logger.exception("Error creating session after setup")
        return templates.TemplateResponse(
            "setup.html",
            {
                "request": request,
                "error": "Account created but session failed. Please log in manually.",
            },
            status_code=500,
        )

    response = templates.TemplateResponse(
        "setup_complete.html",
        {"request": request, "display_name": display_name, "api_key": api_key},
    )
    params = get_cookie_params()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_HOURS * 3600,
        **params,
    )
    _set_csrf_cookie(response, generate_csrf_token())
    return response


# =============================================================================
# Dashboard
# =============================================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    user = await get_user_context(request)
    pool = await get_pool()

    # Get stats
    memory_repo = MemoryRepository(pool)

    # Recent memories
    recent = await memory_repo.search(
        limit=5,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    # Most accessed (team mode only)
    most_accessed = []
    if is_team_mode():
        access_repo = AccessRepository(pool)
        most_accessed = await access_repo.get_most_accessed(
            user_id=user.id,
            limit=5,
        )

    # Get tag stats (with access control)
    tags = await memory_repo.get_existing_tags(
        limit=10,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "recent_memories": recent["memories"],
            "most_accessed": most_accessed,
            "top_tags": tags,
            "total_memories": recent["total_count"],
        },
    )


# =============================================================================
# Daemon Activity
# =============================================================================
# Definitions Management
# =============================================================================

@router.get("/definitions", response_class=HTMLResponse)
async def definitions_page(request: Request, tab: str = "agents"):
    """Manage instance-specific agent, skill, and MCP server definitions."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository
    repo = DefinitionRepository(pool)

    org_id = str(user.organization_id)
    agents = await repo.list_agents(org_id)
    skills = await repo.list_skills(org_id)
    mcp_servers = await repo.list_mcp_servers(org_id)
    proposals = await repo.get_pending_proposals(org_id)

    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        "definitions.html",
        {
            "request": request,
            "user": user,
            "tab": tab,
            "agents": agents,
            "skills": skills,
            "mcp_servers": mcp_servers,
            "proposals": proposals,
            "csrf_token": csrf_token,
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response

@router.post("/definitions/agents/{agent_id}/approve")
async def approve_agent_web(request: Request, agent_id: str):
    """Approve an agent definition from the web UI."""
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository
    repo = DefinitionRepository(pool)
    await repo.approve_agent(agent_id, str(user.organization_id), str(user.id))
    return RedirectResponse("/definitions?tab=agents", status_code=303)

@router.post("/definitions/agents/{agent_id}/reject")
async def reject_agent_web(request: Request, agent_id: str):
    """Reject an agent definition from the web UI."""
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository
    repo = DefinitionRepository(pool)
    await repo.reject_agent(agent_id, str(user.organization_id), str(user.id))
    return RedirectResponse("/definitions?tab=agents", status_code=303)

@router.post("/definitions/skills/{skill_id}/approve")
async def approve_skill_web(request: Request, skill_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository
    repo = DefinitionRepository(pool)
    await repo.approve_skill(skill_id, str(user.organization_id), str(user.id))
    return RedirectResponse("/definitions?tab=skills", status_code=303)

@router.post("/definitions/skills/{skill_id}/reject")
async def reject_skill_web(request: Request, skill_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository
    repo = DefinitionRepository(pool)
    await repo.reject_skill(skill_id, str(user.organization_id), str(user.id))
    return RedirectResponse("/definitions?tab=skills", status_code=303)

@router.post("/definitions/mcp-servers/{server_id}/approve")
async def approve_mcp_web(request: Request, server_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository
    repo = DefinitionRepository(pool)
    await repo.approve_mcp_server(server_id, str(user.organization_id), str(user.id))
    return RedirectResponse("/definitions?tab=mcp", status_code=303)

@router.post("/definitions/mcp-servers/{server_id}/reject")
async def reject_mcp_web(request: Request, server_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository
    repo = DefinitionRepository(pool)
    await repo.reject_mcp_server(server_id, str(user.organization_id), str(user.id))
    return RedirectResponse("/definitions?tab=mcp", status_code=303)


# =============================================================================


@router.get("/daemon", response_class=HTMLResponse)
async def daemon_activity(request: Request):
    """Show daemon autonomous activity — memories tagged 'daemon'."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)

    # Search for daemon-tagged memories, most recent first
    result = await repo.search(
        tags=["daemon"],
        limit=50,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    # Count needs-review items for the badge
    review_result = await repo.search(
        tags=["daemon", "needs-review"],
        limit=1,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    needs_review_count = review_result.get("total_count", 0)

    # Fetch daemon messages for the conversation thread
    messages_result = await repo.search(
        tags=["daemon-message"],
        limit=50,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    # Build message list with sender info, sorted oldest-first for chat display
    daemon_messages = []
    for mem in messages_result.get("memories", []):
        tags = mem.get("tags") or []
        metadata = mem.get("metadata") or {}
        daemon_messages.append(
            {
                "id": mem["id"],
                "content": mem["content"],
                "sender": "daemon" if "from-daemon" in tags else "human",
                "acknowledged": "acknowledged" in tags,
                "created_at": mem["created_at"],
                "in_reply_to": metadata.get("in_reply_to"),
            }
        )
    daemon_messages.reverse()  # oldest first for chat order

    return templates.TemplateResponse(
        "daemon.html",
        {
            "request": request,
            "user": user,
            "daemon_memories": result["memories"],
            "needs_review_count": needs_review_count,
            "daemon_messages": daemon_messages,
        },
    )


@router.post("/daemon/messages", response_class=HTMLResponse)
async def send_daemon_message(request: Request):
    """Send a message from the human to the daemon."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)

    form = await request.form()
    content = form.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message content is required")

    username = user.display_name or user.email or str(user.id)
    await repo.create(
        username=username,
        type="experience",
        content=content,
        tags=["daemon-message", "daemon", "from-human", "pending"],
        importance=5,
        metadata={"source": "web-ui"},
        user_id=user.id,
        organization_id=user.organization_id,
    )

    # Re-fetch messages and return the partial for HTMX swap
    messages_result = await repo.search(
        tags=["daemon-message"],
        limit=50,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    daemon_messages = []
    for mem in messages_result.get("memories", []):
        tags = mem.get("tags") or []
        metadata = mem.get("metadata") or {}
        daemon_messages.append(
            {
                "id": mem["id"],
                "content": mem["content"],
                "sender": "daemon" if "from-daemon" in tags else "human",
                "acknowledged": "acknowledged" in tags,
                "created_at": mem["created_at"],
                "in_reply_to": metadata.get("in_reply_to"),
            }
        )
    daemon_messages.reverse()

    return templates.TemplateResponse(
        "partials/message_thread.html",
        {"request": request, "daemon_messages": daemon_messages},
    )


@router.get("/daemon/review", response_class=HTMLResponse)
async def daemon_review_queue(request: Request):
    """Show memories tagged 'needs-review' that need human approval."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)

    result = await repo.search(
        tags=["daemon", "needs-review"],
        limit=50,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    return templates.TemplateResponse(
        "daemon_review.html",
        {
            "request": request,
            "user": user,
            "review_memories": result["memories"],
        },
    )


@router.post("/daemon/feedback/{memory_id}", response_class=HTMLResponse)
async def daemon_feedback(
    request: Request,
    memory_id: UUID,
    action: str = Form(...),
    comment: str = Form(""),
):
    """Handle feedback on daemon work (approve/reject/comment/reset)."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    existing_metadata = memory.get("metadata") or {}
    existing_feedback = existing_metadata.get("feedback", {})
    existing_tags = list(memory.get("tags") or [])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if action == "approve":
        feedback = {
            "status": "approved",
            "reviewed_at": now,
            "reviewed_by": user.display_name or user.email,
        }
        if comment:
            feedback["comment"] = comment
    elif action == "reject":
        feedback = {
            "status": "rejected",
            "reviewed_at": now,
            "reviewed_by": user.display_name or user.email,
        }
        if comment:
            feedback["comment"] = comment
    elif action == "comment":
        feedback = {
            **existing_feedback,
            "comment": comment,
            "reviewed_at": now,
            "reviewed_by": user.display_name or user.email,
        }
        if "status" not in feedback:
            feedback["status"] = "pending"
    elif action == "reset":
        feedback = {"status": "pending"}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    # Update tags to make feedback discoverable by the daemon's tag-based search.
    # Remove any existing feedback tags first.
    feedback_tag_prefixes = ("feedback-approved", "feedback-rejected")
    updated_tags = [t for t in existing_tags if t not in feedback_tag_prefixes]
    if action == "approve":
        updated_tags.append("feedback-approved")
        if "needs-review" in updated_tags:
            updated_tags.remove("needs-review")
    elif action == "reject":
        updated_tags.append("feedback-rejected")
        if "needs-review" in updated_tags:
            updated_tags.remove("needs-review")
    elif action == "reset":
        # Restore needs-review if this is daemon work
        if "daemon" in updated_tags and "needs-review" not in updated_tags:
            updated_tags.append("needs-review")
        # Remove feedback-processed if re-opening
        if "feedback-processed" in updated_tags:
            updated_tags.remove("feedback-processed")

    updated_metadata = {**existing_metadata, "feedback": feedback}
    await repo.update(memory_id=memory_id, metadata=updated_metadata, tags=updated_tags)

    await audit_repo.log(
        memory_id=memory_id,
        action_type="update",
        user_id=user.id,
        organization_id=user.organization_id,
        changed_fields=["metadata.feedback", "tags"],
        old_values={"feedback": existing_feedback, "tags": existing_tags},
        new_values={"feedback": feedback, "tags": updated_tags},
        notes=f"feedback:{action}",
    )

    # Return the partial HTML for HTMX swap
    # Re-fetch memory to get updated state
    updated_memory = await repo.get(memory_id)
    return templates.TemplateResponse(
        "partials/feedback_actions.html",
        {"request": request, "memory": updated_memory},
    )


# =============================================================================
# Daemon Tasks
# =============================================================================

# Valid values (matching the API router)
_TASK_AGENT_TYPES = {"research", "code", "memory", "reflection", "documentation", "planning"}
_TASK_PRIORITIES = {"low", "medium", "high"}


def _memory_to_task_view(memory: dict) -> dict:
    """Convert a daemon-task memory to a view-friendly dict."""
    tags = memory.get("tags") or []
    metadata = memory.get("metadata") or {}

    # Derive status
    if "completed" in tags:
        status = "completed"
    elif any(t.startswith("claimed-by-") for t in tags):
        status = "claimed"
    elif "pending" in tags:
        status = "pending"
    else:
        status = "unknown"

    # Extract agent type, priority, claimed_by
    agent_type = next((t for t in tags if t in _TASK_AGENT_TYPES), "unknown")
    priority = next((t for t in tags if t in _TASK_PRIORITIES), "medium")
    claimed_by = next((t[len("claimed-by-") :] for t in tags if t.startswith("claimed-by-")), None)

    internal_tags = (
        {"daemon-task", "pending", "completed", "daemon"} | _TASK_AGENT_TYPES | _TASK_PRIORITIES
    )
    display_tags = [t for t in tags if t not in internal_tags and not t.startswith("claimed-by-")]

    return {
        "id": memory["id"],
        "description": memory["content"],
        "agent_type": agent_type,
        "priority": priority,
        "status": status,
        "tags": display_tags,
        "created_at": memory["created_at"],
        "updated_at": memory["updated_at"],
        "result": metadata.get("result"),
        "claimed_by": claimed_by,
    }


@router.get("/daemon/tasks", response_class=HTMLResponse)
async def daemon_tasks_list(
    request: Request,
    status: str | None = None,
):
    """List daemon tasks with optional status filter."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)

    # Fetch all daemon-task memories
    tags_filter = ["daemon-task"]
    if status in ("pending", "completed"):
        tags_filter.append(status)

    result = await repo.search(
        tags=tags_filter,
        limit=100,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    all_tasks = [_memory_to_task_view(m) for m in result["memories"]]

    # Post-filter for "claimed" status (prefix tag)
    if status == "claimed":
        all_tasks = [t for t in all_tasks if t["status"] == "claimed"]

    # Compute status counts from unfiltered set
    all_result = (
        await repo.search(
            tags=["daemon-task"],
            limit=100,
            requesting_user_id=user.id,
            requesting_org_id=user.organization_id,
        )
        if status
        else result
    )
    all_for_counts = (
        [_memory_to_task_view(m) for m in all_result["memories"]] if status else all_tasks
    )
    status_counts = {}
    for t in all_for_counts:
        status_counts[t["status"]] = status_counts.get(t["status"], 0) + 1

    return templates.TemplateResponse(
        "daemon_tasks.html",
        {
            "request": request,
            "user": user,
            "tasks": all_tasks,
            "current_status": status,
            "total_count": len(all_for_counts),
            "status_counts": status_counts,
        },
    )


@router.get("/daemon/tasks/new", response_class=HTMLResponse)
async def daemon_tasks_new_form(request: Request):
    """Show the task submission form."""
    user = await get_user_context(request)
    return templates.TemplateResponse(
        "daemon_tasks_new.html",
        {"request": request, "user": user},
    )


@router.post("/daemon/tasks/new", response_class=HTMLResponse)
async def daemon_tasks_create(
    request: Request,
    description: str = Form(...),
    agent_type: str = Form("code"),
    priority: str = Form("medium"),
    context: str = Form(""),
    tags: str = Form(""),
):
    """Handle task submission form POST."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)

    if agent_type not in _TASK_AGENT_TYPES:
        agent_type = "code"
    if priority not in _TASK_PRIORITIES:
        priority = "medium"

    # Build tags
    memory_tags = ["daemon-task", "daemon", "pending", agent_type, priority]
    if tags.strip():
        extra = [t.strip() for t in tags.split(",") if t.strip()]
        memory_tags.extend(extra)

    # Build metadata
    metadata: dict = {"submitted_by": str(user.id), "source": "web"}
    if context.strip():
        metadata["context"] = context.strip()

    username = user.display_name or user.email or str(user.id)

    await repo.create(
        username=username,
        type="technical",
        content=description,
        tags=memory_tags,
        importance={"low": 3, "medium": 5, "high": 8}.get(priority, 5),
        metadata=metadata,
        user_id=user.id,
        organization_id=user.organization_id,
    )

    return RedirectResponse(url="/daemon/tasks", status_code=303)


@router.get("/daemon/tasks/{task_id}", response_class=HTMLResponse)
async def daemon_task_detail(request: Request, task_id: UUID):
    """Show task detail with execution history."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    memory = await repo.get_accessible(task_id, user.id, user.organization_id)
    if memory is None or "daemon-task" not in (memory.get("tags") or []):
        raise HTTPException(status_code=404, detail="Task not found")

    task = _memory_to_task_view(memory)

    # Get version history
    version_data = await audit_repo.get_versions(task_id, limit=50)
    versions = version_data.get("versions", [])

    return templates.TemplateResponse(
        "daemon_task_detail.html",
        {
            "request": request,
            "user": user,
            "task": task,
            "versions": versions,
        },
    )


@router.post("/daemon/tasks/{task_id}/cancel", response_class=HTMLResponse)
async def daemon_task_cancel(request: Request, task_id: UUID):
    """Cancel a pending daemon task."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)

    memory = await repo.get_accessible(task_id, user.id, user.organization_id)
    if memory is None or "daemon-task" not in (memory.get("tags") or []):
        raise HTTPException(status_code=404, detail="Task not found")

    tags = memory.get("tags") or []
    if "pending" not in tags:
        raise HTTPException(status_code=400, detail="Only pending tasks can be cancelled")

    await repo.delete(task_id)
    return RedirectResponse(url="/daemon/tasks", status_code=303)


# =============================================================================
# Memories
# =============================================================================


@router.get("/memories", response_class=HTMLResponse)
async def memories_list(
    request: Request,
    q: str | None = None,
    type: str | None = None,
    tag: str | None = None,
    page: int = 1,
):
    """List and search memories."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)

    # Treat empty strings as None
    q = q if q else None
    type = type if type else None
    tag = tag if tag else None

    # Convert single tag to list for the search
    tag_list = [tag] if tag else None

    limit = 20
    offset = (page - 1) * limit

    result = await repo.search(
        query=q,
        type=type,
        tags=tag_list,
        offset=offset,
        limit=limit,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    # Get tags for filter (with access control)
    tags = await repo.get_existing_tags(
        limit=20,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    total_pages = (result["total_count"] + limit - 1) // limit

    # For HTMX partial updates
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "partials/memory_list.html",
            {
                "request": request,
                "memories": result["memories"],
                "total_count": result["total_count"],
                "page": page,
                "total_pages": total_pages,
                "query": q,
                "type_filter": type,
                "tag_filter": tag,
            },
        )

    return templates.TemplateResponse(
        "memories.html",
        {
            "request": request,
            "user": user,
            "memories": result["memories"],
            "total_count": result["total_count"],
            "page": page,
            "total_pages": total_pages,
            "query": q,
            "type_filter": type,
            "tag_filter": tag,
            "tags": tags,
            "memory_types": ["experience", "technical", "procedural", "goal", "individual"],
        },
    )


# New memory routes - MUST be before /memories/{memory_id} to avoid route conflicts
@router.get("/memories/new", response_class=HTMLResponse)
async def memory_new_form(request: Request):
    """New memory form."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)
    user_repo = UserRepository(pool)

    tags = await repo.get_existing_tags(
        limit=30,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    # Get users in the organization for linking individual memories
    org_users = (
        await user_repo.get_by_organization(user.organization_id) if user.organization_id else []
    )

    return templates.TemplateResponse(
        "memory_new.html",
        {
            "request": request,
            "user": user,
            "memory_types": ["experience", "technical", "procedural", "goal", "individual"],
            "existing_tags": tags,
            "org_users": org_users,
        },
    )


@router.post("/memories/new", response_class=HTMLResponse)
async def memory_new_submit(
    request: Request,
    type: str = Form(...),
    content: str = Form(...),
    tags: str = Form(""),
    importance: int = Form(5),
    # Experience metadata
    meta_context: str = Form(""),
    meta_outcome: str = Form(""),
    meta_lessons_learned: str = Form(""),
    meta_related_entities: str = Form(""),
    # Technical metadata
    meta_category: str = Form(""),
    meta_language: str = Form(""),
    meta_version_info: str = Form(""),
    meta_repo: str = Form(""),
    meta_filename: str = Form(""),
    meta_code_snippet: str = Form(""),
    meta_references: str = Form(""),
    # Procedural metadata
    meta_estimated_time: str = Form(""),
    meta_success_criteria: str = Form(""),
    meta_prerequisites: str = Form(""),
    meta_common_pitfalls: str = Form(""),
    meta_steps: str = Form(""),
    # Goal metadata
    meta_status: str = Form("active"),
    meta_priority: int = Form(3),
    meta_deadline: str = Form(""),
    meta_blockers: str = Form(""),
    meta_milestones: str = Form(""),
    # Individual metadata
    meta_user_id: str = Form(""),
    meta_name: str = Form(""),
    meta_relationship: str = Form(""),
    meta_organization: str = Form(""),
    meta_role: str = Form(""),
    meta_email: str = Form(""),
    meta_phone: str = Form(""),
    meta_linkedin: str = Form(""),
    meta_github: str = Form(""),
    meta_preferences: str = Form(""),
):
    """Handle new memory form submission."""
    user = await get_user_context(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    # Use the logged-in user's display name as username
    username = user.display_name or "unknown"

    # Parse tags
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]

    # Individual memories cannot be created via web interface
    # - they are auto-created when users join
    if type == "individual":
        raise HTTPException(
            status_code=400,
            detail=(
                "Individual memories cannot be created directly."
                " They are automatically created when users are"
                " added to the system."
            ),
        )

    # Build type-specific metadata
    metadata = _build_metadata_from_form(
        type,
        meta_context=meta_context,
        meta_outcome=meta_outcome,
        meta_lessons_learned=meta_lessons_learned,
        meta_related_entities=meta_related_entities,
        meta_category=meta_category,
        meta_language=meta_language,
        meta_version_info=meta_version_info,
        meta_repo=meta_repo,
        meta_filename=meta_filename,
        meta_code_snippet=meta_code_snippet,
        meta_references=meta_references,
        meta_estimated_time=meta_estimated_time,
        meta_success_criteria=meta_success_criteria,
        meta_prerequisites=meta_prerequisites,
        meta_common_pitfalls=meta_common_pitfalls,
        meta_steps=meta_steps,
        meta_status=meta_status,
        meta_priority=meta_priority,
        meta_deadline=meta_deadline,
        meta_blockers=meta_blockers,
        meta_milestones=meta_milestones,
    )

    # Create memory
    result = await repo.create(
        username=username,
        type=type,
        content=content,
        tags=tag_list if tag_list else None,
        importance=importance,
        metadata=metadata if metadata else None,
        user_id=user.id,
        organization_id=user.organization_id,
    )

    # Log creation
    await audit_repo.log(
        memory_id=result["id"],
        action_type="create",
        user_id=user.id,
        organization_id=user.organization_id,
        new_values={
            "username": username,
            "type": type,
            "content": content,
            "tags": tag_list,
            "importance": importance,
            "metadata": metadata,
        },
    )

    return RedirectResponse(f"/memories/{result['id']}", status_code=303)


# Memory by ID routes
@router.get("/memories/{memory_id}", response_class=HTMLResponse)
async def memory_detail(request: Request, memory_id: UUID):
    """View memory details."""
    user = await get_user_context(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)
    access_repo = AccessRepository(pool)

    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    # Log access
    await access_repo.log_access(
        memory_id=memory_id,
        access_type="view",
        user_id=user.id,
        organization_id=user.organization_id,
    )

    # Get audit history
    audit = await audit_repo.get_by_memory_id(memory_id, limit=10)

    # Get version history
    versions = await audit_repo.get_versions(memory_id, limit=20)

    # Get access history
    access = await access_repo.get_access_history(memory_id, limit=10)

    is_owner = memory.get("user_id") == user.id

    return templates.TemplateResponse(
        "memory_detail.html",
        {
            "request": request,
            "user": user,
            "memory": memory,
            "audit_entries": audit["entries"],
            "version_entries": versions["versions"],
            "access_entries": access["entries"],
            "is_owner": is_owner,
        },
    )


@router.get("/memories/{memory_id}/edit", response_class=HTMLResponse)
async def memory_edit_form(request: Request, memory_id: UUID):
    """Edit memory form."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)
    user_repo = UserRepository(pool)

    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    if memory.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own memories")

    # Get users in the organization for linking individual memories
    org_users = (
        await user_repo.get_by_organization(user.organization_id) if user.organization_id else []
    )

    return templates.TemplateResponse(
        "memory_edit.html",
        {
            "request": request,
            "user": user,
            "memory": memory,
            "memory_types": ["experience", "technical", "procedural", "goal", "individual"],
            "org_users": org_users,
        },
    )


@router.post("/memories/{memory_id}/edit", response_class=HTMLResponse)
async def memory_edit_submit(
    request: Request,
    memory_id: UUID,
    content: str = Form(...),
    tags: str = Form(""),
    importance: int = Form(5),
    # Experience metadata
    meta_context: str = Form(""),
    meta_outcome: str = Form(""),
    meta_lessons_learned: str = Form(""),
    meta_related_entities: str = Form(""),
    # Technical metadata
    meta_category: str = Form(""),
    meta_language: str = Form(""),
    meta_version_info: str = Form(""),
    meta_repo: str = Form(""),
    meta_filename: str = Form(""),
    meta_code_snippet: str = Form(""),
    meta_references: str = Form(""),
    # Procedural metadata
    meta_estimated_time: str = Form(""),
    meta_success_criteria: str = Form(""),
    meta_prerequisites: str = Form(""),
    meta_common_pitfalls: str = Form(""),
    meta_steps: str = Form(""),
    # Goal metadata
    meta_status: str = Form("active"),
    meta_priority: int = Form(3),
    meta_deadline: str = Form(""),
    meta_blockers: str = Form(""),
    meta_milestones: str = Form(""),
    # Individual metadata
    meta_user_id: str = Form(""),
    meta_name: str = Form(""),
    meta_relationship: str = Form(""),
    meta_organization: str = Form(""),
    meta_role: str = Form(""),
    meta_email: str = Form(""),
    meta_phone: str = Form(""),
    meta_linkedin: str = Form(""),
    meta_github: str = Form(""),
    meta_preferences: str = Form(""),
):
    """Handle memory edit form submission."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    # Get existing to check ownership
    existing = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    if existing.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own memories")

    # Parse tags
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]

    # Build type-specific metadata based on the memory's type
    memory_type = existing.get("type")
    metadata = _build_metadata_from_form(
        memory_type,
        meta_context=meta_context,
        meta_outcome=meta_outcome,
        meta_lessons_learned=meta_lessons_learned,
        meta_related_entities=meta_related_entities,
        meta_category=meta_category,
        meta_language=meta_language,
        meta_version_info=meta_version_info,
        meta_repo=meta_repo,
        meta_filename=meta_filename,
        meta_code_snippet=meta_code_snippet,
        meta_references=meta_references,
        meta_estimated_time=meta_estimated_time,
        meta_success_criteria=meta_success_criteria,
        meta_prerequisites=meta_prerequisites,
        meta_common_pitfalls=meta_common_pitfalls,
        meta_steps=meta_steps,
        meta_status=meta_status,
        meta_priority=meta_priority,
        meta_deadline=meta_deadline,
        meta_blockers=meta_blockers,
        meta_milestones=meta_milestones,
        meta_user_id=meta_user_id,
        meta_name=meta_name,
        meta_relationship=meta_relationship,
        meta_organization=meta_organization,
        meta_role=meta_role,
        meta_email=meta_email,
        meta_phone=meta_phone,
        meta_linkedin=meta_linkedin,
        meta_github=meta_github,
        meta_preferences=meta_preferences,
    )

    # Update
    result = await repo.update(
        memory_id=memory_id,
        content=content,
        tags=tag_list if tag_list else None,
        importance=importance,
        metadata=metadata if metadata else None,
    )

    # Log the update with version snapshot
    await audit_repo.log(
        memory_id=memory_id,
        action_type="update",
        user_id=user.id,
        organization_id=user.organization_id,
        changed_fields=["content", "tags", "importance", "metadata"],
        old_values={
            "content": existing["content"],
            "tags": existing["tags"],
            "importance": existing["importance"],
            "metadata": existing.get("metadata"),
        },
        new_values={
            "content": content,
            "tags": tag_list,
            "importance": importance,
            "metadata": metadata,
        },
        version=result["version"] if result else None,
        snapshot={
            "content": result["content"],
            "tags": result["tags"],
            "importance": result["importance"],
            "metadata": result["metadata"],
            "related_memory_ids": [str(uid) for uid in result.get("related_memory_ids", [])],
            "shared": result.get("shared", False),
        }
        if result
        else None,
    )

    return RedirectResponse(f"/memories/{memory_id}", status_code=303)


@router.post("/memories/{memory_id}/share", response_class=HTMLResponse)
async def memory_share(request: Request, memory_id: UUID):
    """Toggle memory sharing (team mode only)."""
    if not is_team_mode():
        raise HTTPException(status_code=404, detail="Sharing requires team mode")
    user = await get_user_context(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    if memory.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="You can only share your own memories")

    new_shared = not memory.get("shared", False)
    await repo.set_shared(memory_id, user.id, new_shared)

    await audit_repo.log(
        memory_id=memory_id,
        action_type="share" if new_shared else "unshare",
        user_id=user.id,
        organization_id=user.organization_id,
        changed_fields=["shared"],
        old_values={"shared": not new_shared},
        new_values={"shared": new_shared},
    )

    # Return updated button for HTMX
    if request.headers.get("HX-Request"):
        return HTMLResponse(
            f"""<button
                hx-post="/memories/{memory_id}/share"
                hx-swap="outerHTML"
                class="btn {"btn-warning" if new_shared else "btn-primary"}">
                {"Unshare" if new_shared else "Share"}
            </button>"""
        )

    return RedirectResponse(f"/memories/{memory_id}", status_code=303)


@router.post("/memories/{memory_id}/delete", response_class=HTMLResponse)
async def memory_delete(request: Request, memory_id: UUID):
    """Delete a memory."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    # Individual memories cannot be deleted via web interface
    # - they are deleted when users are removed
    if memory.get("type") == "individual":
        raise HTTPException(
            status_code=400,
            detail=(
                "Individual memories cannot be deleted directly."
                " They are automatically deleted when users are"
                " removed from the system."
            ),
        )

    if memory.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own memories")

    await repo.delete(memory_id)

    await audit_repo.log(
        memory_id=memory_id,
        action_type="delete",
        user_id=user.id,
        organization_id=user.organization_id,
        old_values={
            "content": memory["content"],
            "tags": memory["tags"],
        },
        snapshot={
            "content": memory["content"],
            "tags": memory["tags"],
            "importance": memory["importance"],
            "metadata": memory["metadata"],
            "related_memory_ids": [str(uid) for uid in memory.get("related_memory_ids", [])],
            "shared": memory.get("shared", False),
        },
    )

    return RedirectResponse("/memories", status_code=303)


@router.post("/memories/{memory_id}/restore/{version}", response_class=HTMLResponse)
async def memory_restore(request: Request, memory_id: UUID, version: int):
    """Restore a memory to a previous version."""
    user = await get_user_context(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    if memory.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="You can only restore your own memories")

    if memory["version"] == version:
        return RedirectResponse(f"/memories/{memory_id}", status_code=303)

    # Get the snapshot for the target version
    version_entry = await audit_repo.get_version_snapshot(memory_id, version)
    if version_entry is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")

    snapshot = version_entry.get("snapshot")
    if snapshot is None:
        raise HTTPException(
            status_code=400,
            detail=f"Version {version} does not have a restorable snapshot",
        )

    # Build old snapshot for audit
    old_snapshot = {
        "content": memory["content"],
        "tags": memory["tags"],
        "importance": memory["importance"],
        "metadata": memory["metadata"],
        "related_memory_ids": [str(uid) for uid in memory.get("related_memory_ids", [])],
        "shared": memory.get("shared", False),
    }

    # Apply the restore
    result = await repo.update(
        memory_id=memory_id,
        content=snapshot.get("content"),
        tags=snapshot.get("tags"),
        importance=snapshot.get("importance"),
        metadata=snapshot.get("metadata"),
        related_memory_ids=[UUID(uid) for uid in snapshot.get("related_memory_ids", [])],
    )

    if result is None:
        raise HTTPException(status_code=500, detail="Failed to apply restore")

    # Log the restore
    await audit_repo.log(
        memory_id=memory_id,
        action_type="restore",
        user_id=user.id,
        organization_id=user.organization_id,
        old_values=old_snapshot,
        new_values=snapshot,
        notes=f"Restored to version {version}",
        version=result["version"],
        snapshot={
            "content": result["content"],
            "tags": result["tags"],
            "importance": result["importance"],
            "metadata": result["metadata"],
            "related_memory_ids": [str(uid) for uid in result.get("related_memory_ids", [])],
            "shared": result.get("shared", False),
        },
    )

    return RedirectResponse(f"/memories/{memory_id}", status_code=303)


# =============================================================================
# Audit Logs
# =============================================================================


@router.get("/audit", response_class=HTMLResponse)
async def audit_logs(
    request: Request,
    page: int = 1,
    action_type: str | None = None,
):
    """View audit logs (team mode only)."""
    if not is_team_mode():
        raise HTTPException(status_code=404, detail="Audit logs require team mode")
    user = await get_user_context(request)
    pool = await get_pool()
    audit_repo = AuditRepository(pool)

    limit = 50
    offset = (page - 1) * limit

    result = await audit_repo.get_by_organization_id(
        organization_id=user.organization_id,
        action_type=action_type,
        offset=offset,
        limit=limit,
    )

    total_pages = (result["total_count"] + limit - 1) // limit

    return templates.TemplateResponse(
        "audit.html",
        {
            "request": request,
            "user": user,
            "entries": result["entries"],
            "total_count": result["total_count"],
            "page": page,
            "total_pages": total_pages,
            "action_type": action_type,
            "action_types": ["create", "update", "delete", "share", "unshare"],
        },
    )


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

    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "can_manage": can_manage,
            "can_impersonate": can_impersonate,
        },
    )


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
    # TODO: Implement invite/password-set flow instead of temp passwords
    from lucent.auth_providers import set_user_password

    temp_password = secrets.token_urlsafe(12)
    await set_user_password(pool, new_user["id"], temp_password)

    from urllib.parse import quote

    return RedirectResponse(
        url=f"/users?success=User+created.+Temporary+password:+{quote(temp_password)}",
        status_code=303,
    )


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
    api_keys = await api_key_repo.list_by_user(user.id)

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
        "settings.html",
        {
            "request": request,
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

    return RedirectResponse("/settings?password_changed=1", status_code=303)


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
    pool = await get_pool()
    api_key_repo = ApiKeyRepository(pool)

    success = await api_key_repo.revoke(key_id, user.id)

    if not success:
        raise HTTPException(status_code=404, detail="API key not found")

    return RedirectResponse("/settings", status_code=303)
