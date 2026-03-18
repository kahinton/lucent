"""Web routes for Lucent admin dashboard using Jinja2 + HTMX."""

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
                    logger.debug("Impersonation failed for header value", exc_info=True)

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
        request,
        "login.html",
        {
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

    # Rate limit login attempts by IP
    from lucent.rate_limit import get_login_limiter

    client_ip = request.client.host if request.client else "unknown"
    limiter = get_login_limiter()
    allowed, retry_after = limiter.check(client_ip)
    if not allowed:
        provider = await get_auth_provider()
        logger.warning("Login rate limit exceeded for IP %s", client_ip)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "fields": provider.get_login_fields(),
                "error": f"Too many login attempts. Please try again in {retry_after} seconds.",
            },
            status_code=429,
        )

    pool = await get_pool()
    credentials = {key: str(value) for key, value in form.items()}

    provider = await get_auth_provider()

    try:
        user = await provider.authenticate(credentials)
    except Exception:
        logger.exception("Error during authentication")
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "fields": provider.get_login_fields(),
                "error": "An unexpected error occurred. Please try again later.",
            },
            status_code=500,
        )

    if user is None:
        logger.warning(
            "Failed login attempt from IP %s for user '%s'",
            client_ip,
            credentials.get("username", credentials.get("email", "unknown")),
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {
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
            request,
            "login.html",
            {
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
    logger.info("Successful login for user %s from IP %s", user.get("email", user["id"]), client_ip)
    return response


@router.post("/logout")
async def logout(request: Request):
    """Log the user out."""
    await _check_csrf(request)
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
        request,
        "setup.html",
        {"error": error, "csrf_token": csrf_token},
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
            request,
            "setup.html",
            {"error": "Display name is required."},
            status_code=400,
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": "Password must be at least 8 characters."},
            status_code=400,
        )

    complexity_error = validate_password_complexity(password)
    if complexity_error:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": complexity_error},
            status_code=400,
        )

    if password != password_confirm:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": "Passwords do not match."},
            status_code=400,
        )

    try:
        user, api_key = await create_initial_user(pool, display_name, email, password)
    except Exception:
        logger.exception("Error during initial user setup")
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
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
            request,
            "setup.html",
            {
                "error": "Account created but session failed. Please log in manually.",
            },
            status_code=500,
        )

    response = templates.TemplateResponse(
        request,
        "setup_complete.html",
        {"display_name": display_name, "api_key": api_key},
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

    # Agent/skill stats
    from lucent.db.definitions import DefinitionRepository

    def_repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)
    agents = await def_repo.list_agents(org_id, status="active")
    skills = await def_repo.list_skills(org_id, status="active")
    active_agents = len(agents)
    active_skills = len(skills)

    # Active requests count (from request tracking system)
    from lucent.db.requests import RequestRepository

    req_repo = RequestRepository(pool)
    active_requests = await req_repo.list_requests(
        org_id=str(user.organization_id),
        status="in_progress",
    )
    pending_requests = await req_repo.list_requests(
        org_id=str(user.organization_id),
        status="pending",
    )
    active_request_count = len(active_requests) + len(pending_requests)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "recent_memories": recent["memories"],
            "most_accessed": most_accessed,
            "top_tags": tags,
            "total_memories": recent["total_count"],
            "active_agents": active_agents,
            "active_skills": active_skills,
            "active_request_count": active_request_count,
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
    agents = await repo.list_agents_with_grants(org_id)
    skills = await repo.list_skills(org_id)
    mcp_servers = await repo.list_mcp_servers(org_id)
    proposals = await repo.get_pending_proposals(org_id)

    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "definitions.html",
        {
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


@router.get("/definitions/agents/{agent_id}", response_class=HTMLResponse)
async def agent_detail_page(request: Request, agent_id: str):
    """View full agent definition with content, skills, and MCP servers."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)
    agent = await repo.get_agent(agent_id, org_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    skills = await repo.get_agent_skills(agent_id)
    mcp_servers = await repo.get_agent_mcp_servers(agent_id)
    # Get all available skills and MCP servers for grant dropdowns
    all_skills = await repo.list_skills(org_id, status="active")
    all_mcp = await repo.list_mcp_servers(org_id, status="active")
    granted_skill_ids = {str(s["id"]) for s in skills}
    granted_mcp_ids = {str(m["id"]) for m in mcp_servers}
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition_type": "agent",
            "definition": agent,
            "skills": skills,
            "mcp_servers": mcp_servers,
            "all_skills": all_skills,
            "all_mcp": all_mcp,
            "granted_skill_ids": granted_skill_ids,
            "granted_mcp_ids": granted_mcp_ids,
            "csrf_token": csrf_token,
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response


@router.get("/definitions/skills/{skill_id}", response_class=HTMLResponse)
async def skill_detail_page(request: Request, skill_id: str):
    """View full skill definition with content."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)
    skill = await repo.get_skill(skill_id, org_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition_type": "skill",
            "definition": skill,
            "skills": [],
            "mcp_servers": [],
            "csrf_token": csrf_token,
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response


@router.get("/definitions/mcp-servers/{server_id}", response_class=HTMLResponse)
async def mcp_server_detail_page(request: Request, server_id: str):
    """View full MCP server definition with config."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    org_id = str(user.organization_id)
    server = await repo.get_mcp_server(server_id, org_id)
    if not server:
        raise HTTPException(404, "MCP server not found")
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "definition_detail.html",
        {
            "user": user,
            "definition_type": "mcp-server",
            "definition": server,
            "skills": [],
            "mcp_servers": [],
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


# ── Definition CRUD (create, update, delete) ──────────────────────────────


@router.post("/definitions/agents/create")
async def create_agent_web(request: Request):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.create_agent(
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        content=str(form.get("content", "")).strip(),
        org_id=str(user.organization_id),
        created_by=str(user.id),
    )
    return RedirectResponse("/definitions?tab=agents", status_code=303)


@router.post("/definitions/agents/{agent_id}/update")
async def update_agent_web(request: Request, agent_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.update_agent(
        agent_id,
        str(user.organization_id),
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        content=str(form.get("content", "")).strip(),
    )
    return RedirectResponse(f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/delete")
async def delete_agent_web(request: Request, agent_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.delete_agent(agent_id, str(user.organization_id))
    return RedirectResponse("/definitions?tab=agents", status_code=303)


@router.post("/definitions/skills/create")
async def create_skill_web(request: Request):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.create_skill(
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        content=str(form.get("content", "")).strip(),
        org_id=str(user.organization_id),
        created_by=str(user.id),
    )
    return RedirectResponse("/definitions?tab=skills", status_code=303)


@router.post("/definitions/skills/{skill_id}/update")
async def update_skill_web(request: Request, skill_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.update_skill(
        skill_id,
        str(user.organization_id),
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        content=str(form.get("content", "")).strip(),
    )
    return RedirectResponse(f"/definitions/skills/{skill_id}", status_code=303)


@router.post("/definitions/skills/{skill_id}/delete")
async def delete_skill_web(request: Request, skill_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.delete_skill(skill_id, str(user.organization_id))
    return RedirectResponse("/definitions?tab=skills", status_code=303)


@router.post("/definitions/mcp-servers/create")
async def create_mcp_web(request: Request):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    import json

    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    headers_raw = str(form.get("headers", "{}")).strip()
    try:
        headers = json.loads(headers_raw) if headers_raw else {}
    except json.JSONDecodeError:
        headers = {}
    await repo.create_mcp_server(
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        server_type=str(form.get("server_type", "http")).strip(),
        url=str(form.get("url", "")).strip() or None,
        org_id=str(user.organization_id),
        created_by=str(user.id),
        headers=headers,
        command=str(form.get("command", "")).strip() or None,
    )
    return RedirectResponse("/definitions?tab=mcp", status_code=303)


@router.post("/definitions/mcp-servers/{server_id}/update")
async def update_mcp_web(request: Request, server_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    import json

    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    headers_raw = str(form.get("headers", "{}")).strip()
    try:
        headers = json.loads(headers_raw) if headers_raw else {}
    except json.JSONDecodeError:
        headers = {}
    await repo.update_mcp_server(
        server_id,
        str(user.organization_id),
        name=str(form.get("name", "")).strip(),
        description=str(form.get("description", "")).strip(),
        server_type=str(form.get("server_type", "http")).strip(),
        url=str(form.get("url", "")).strip() or None,
        headers=headers,
        command=str(form.get("command", "")).strip() or None,
    )
    return RedirectResponse(f"/definitions/mcp-servers/{server_id}", status_code=303)


@router.post("/definitions/mcp-servers/{server_id}/delete")
async def delete_mcp_web(request: Request, server_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.delete_mcp_server(server_id, str(user.organization_id))
    return RedirectResponse("/definitions?tab=mcp", status_code=303)


# ── Grant management (skill/MCP ↔ agent) ─────────────────────────────────


@router.post("/definitions/agents/{agent_id}/grant-skill")
async def grant_skill_web(request: Request, agent_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    skill_id = str(form.get("skill_id", ""))
    if skill_id:
        await repo.grant_skill(agent_id, skill_id)
    return RedirectResponse(f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/revoke-skill/{skill_id}")
async def revoke_skill_web(request: Request, agent_id: str, skill_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.revoke_skill(agent_id, skill_id)
    return RedirectResponse(f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/grant-mcp")
async def grant_mcp_web(request: Request, agent_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    mcp_id = str(form.get("mcp_server_id", ""))
    if mcp_id:
        await repo.grant_mcp_server(agent_id, mcp_id)
    return RedirectResponse(f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/revoke-mcp/{server_id}")
async def revoke_mcp_web(request: Request, agent_id: str, server_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    await repo.revoke_mcp_server(agent_id, server_id)
    return RedirectResponse(f"/definitions/agents/{agent_id}", status_code=303)


@router.post("/definitions/agents/{agent_id}/mcp-tools/{server_id}")
async def update_mcp_tools_web(request: Request, agent_id: str, server_id: str):
    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))
    await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    tools_raw = str(form.get("allowed_tools", "")).strip()
    if tools_raw:
        tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
        await repo.update_mcp_tool_grants(agent_id, server_id, tools if tools else None)
    else:
        await repo.update_mcp_tool_grants(agent_id, server_id, None)
    return RedirectResponse(f"/definitions/agents/{agent_id}", status_code=303)


# =============================================================================


@router.get("/daemon", response_class=HTMLResponse)
async def daemon_activity(request: Request):
    """Redirect old /daemon URL to /activity filtered to Lucent's work."""
    return RedirectResponse(url="/activity?source=cognitive", status_code=301)


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
        request,
        "partials/message_thread.html",
        {"daemon_messages": daemon_messages},
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
        request,
        "daemon_review.html",
        {
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
        request,
        "partials/feedback_actions.html",
        {"memory": updated_memory},
    )


# =============================================================================
# Daemon Tasks
# =============================================================================

# Valid priorities for tasks
_TASK_PRIORITIES = {"low", "medium", "high"}
# Legacy agent type names used for classifying memory-based tasks in the UI
_TASK_AGENT_TYPES = {"research", "code", "memory", "reflection", "documentation", "planning"}


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


# Legacy Task Queue routes — redirect to Activity
@router.get("/daemon/tasks", response_class=HTMLResponse)
async def daemon_tasks_redirect(request: Request):
    """Redirect legacy task queue to activity page."""
    return RedirectResponse(url="/activity", status_code=301)


@router.get("/daemon/tasks/new", response_class=HTMLResponse)
async def daemon_tasks_new_redirect(request: Request):
    """Redirect legacy new task form to activity page."""
    return RedirectResponse(url="/activity", status_code=301)


@router.get("/daemon/tasks/{task_id}", response_class=HTMLResponse)
async def daemon_task_detail_redirect(request: Request, task_id: UUID):
    """Redirect legacy task detail to activity page."""
    return RedirectResponse(url="/activity", status_code=301)


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
            request,
            "partials/memory_list.html",
            {
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
        request,
        "memories.html",
        {
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
        request,
        "memory_new.html",
        {
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
    await _check_csrf(request)
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
        request,
        "memory_detail.html",
        {
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
        request,
        "memory_edit.html",
        {
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
    await _check_csrf(request)
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
    await _check_csrf(request)
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
        request,
        "audit.html",
        {
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


# ── Sandboxes ──────────────────────────────────────────────────────


@router.get("/sandboxes", response_class=HTMLResponse)
async def sandboxes_page(
    request: Request,
    tab: str | None = Query(default=None),
    show: str | None = Query(default=None),
):
    """Sandbox templates and instances."""
    user = await get_user_context(request)
    org_id = str(user.organization_id) if user.organization_id else None

    # Always load templates (needed for both tabs and launch modal)
    pool = await get_pool()
    from lucent.db.sandbox_template import SandboxTemplateRepository

    tpl_repo = SandboxTemplateRepository(pool)
    try:
        template_list = await tpl_repo.list_all(org_id) if org_id else []
    except Exception:
        logger.debug("Failed to load sandbox templates", exc_info=True)
        template_list = []

    # Load instances only when on instances tab
    sandbox_list = []
    active_tab = tab or "templates"
    if active_tab == "instances":
        from lucent.sandbox.manager import get_sandbox_manager

        manager = get_sandbox_manager()
        try:
            if show == "active":
                sandbox_list = await manager.list_active(org_id)
            else:
                sandbox_list = await manager.list_all(org_id)
        except Exception:
            logger.debug("Failed to load sandbox list", exc_info=True)
            sandbox_list = []

        # Enrich with template name
        tpl_names = {str(t["id"]): t["name"] for t in template_list}
        for sb in sandbox_list:
            sb["template_name"] = tpl_names.get(str(sb.get("template_id", "")))

    return templates.TemplateResponse(
        request,
        "sandboxes.html",
        {
            "tab": active_tab,
            "templates": template_list,
            "sandboxes": sandbox_list,
            "show_filter": show or "all",
            "user": user,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.post("/sandboxes/templates/create")
async def create_template_web(
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    image: str = Form(default="python:3.12-slim"),
    repo_url: str = Form(default=""),
    branch: str = Form(default="main"),
    setup_commands: str = Form(default=""),
    env_vars: str = Form(default=""),
    memory_limit: str = Form(default="2g"),
    cpu_limit: float = Form(default=2.0),
    disk_limit: str = Form(default="10g"),
    network_mode: str = Form(default="none"),
    timeout_seconds: int = Form(default=1800),
    csrf_token: str = Form(default=""),
):
    """Create a new sandbox template."""
    user = await get_user_context(request)
    await _check_csrf(request, form_token=csrf_token)
    pool = await get_pool()
    from lucent.db.sandbox_template import SandboxTemplateRepository

    repo = SandboxTemplateRepository(pool)

    await repo.create(
        name=name.strip(),
        organization_id=str(user.organization_id),
        description=description.strip(),
        image=image,
        repo_url=repo_url.strip() or None,
        branch=branch.strip() or None,
        setup_commands=[c.strip() for c in setup_commands.splitlines() if c.strip()],
        env_vars=_parse_env_vars(env_vars),
        memory_limit=memory_limit,
        cpu_limit=cpu_limit,
        disk_limit=disk_limit,
        network_mode=network_mode,
        timeout_seconds=timeout_seconds,
        created_by=str(user.id),
    )
    return RedirectResponse("/sandboxes", status_code=303)


@router.get("/sandboxes/templates/{template_id}/edit", response_class=HTMLResponse)
async def edit_template_page(request: Request, template_id: str):
    """Edit a sandbox template."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.sandbox_template import SandboxTemplateRepository

    repo = SandboxTemplateRepository(pool)
    tpl = await repo.get(template_id, str(user.organization_id))
    if not tpl:
        raise HTTPException(404, "Template not found")

    return templates.TemplateResponse(
        request,
        "sandbox_template_edit.html",
        {
            "template": tpl,
            "user": user,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.post("/sandboxes/templates/{template_id}/edit")
async def update_template_web(
    request: Request,
    template_id: str,
    name: str = Form(...),
    description: str = Form(default=""),
    image: str = Form(default="python:3.12-slim"),
    repo_url: str = Form(default=""),
    branch: str = Form(default="main"),
    setup_commands: str = Form(default=""),
    env_vars: str = Form(default=""),
    memory_limit: str = Form(default="2g"),
    cpu_limit: float = Form(default=2.0),
    disk_limit: str = Form(default="10g"),
    network_mode: str = Form(default="none"),
    timeout_seconds: int = Form(default=1800),
    csrf_token: str = Form(default=""),
):
    """Update a sandbox template."""
    user = await get_user_context(request)
    await _check_csrf(request, form_token=csrf_token)
    pool = await get_pool()
    from lucent.db.sandbox_template import SandboxTemplateRepository

    repo = SandboxTemplateRepository(pool)

    await repo.update(
        template_id,
        str(user.organization_id),
        name=name.strip(),
        description=description.strip(),
        image=image,
        repo_url=repo_url.strip() or None,
        branch=branch.strip() or None,
        setup_commands=[c.strip() for c in setup_commands.splitlines() if c.strip()],
        env_vars=_parse_env_vars(env_vars),
        memory_limit=memory_limit,
        cpu_limit=cpu_limit,
        disk_limit=disk_limit,
        network_mode=network_mode,
        timeout_seconds=timeout_seconds,
    )
    return RedirectResponse("/sandboxes", status_code=303)


@router.post("/sandboxes/templates/{template_id}/delete")
async def delete_template_web(request: Request, template_id: str):
    """Delete a sandbox template."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.sandbox_template import SandboxTemplateRepository

    repo = SandboxTemplateRepository(pool)
    await repo.delete(template_id, str(user.organization_id))
    return RedirectResponse("/sandboxes", status_code=303)


@router.post("/sandboxes/launch")
async def launch_sandbox_web(
    request: Request,
    template_id: str = Form(...),
    name: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    """Launch a sandbox instance from a template."""
    user = await get_user_context(request)
    await _check_csrf(request, form_token=csrf_token)
    pool = await get_pool()
    from lucent.db.sandbox_template import SandboxTemplateRepository
    from lucent.sandbox.manager import get_sandbox_manager
    from lucent.sandbox.models import SandboxConfig

    tpl_repo = SandboxTemplateRepository(pool)
    tpl = await tpl_repo.get(template_id, str(user.organization_id))
    if not tpl:
        raise HTTPException(404, "Template not found")

    config = SandboxConfig(
        name=name.strip() or f"{tpl['name']}-instance",
        image=tpl["image"],
        repo_url=tpl.get("repo_url"),
        branch=tpl.get("branch"),
        setup_commands=tpl.get("setup_commands") or [],
        env_vars=tpl.get("env_vars") or {},
        working_dir=tpl.get("working_dir", "/workspace"),
        memory_limit=tpl.get("memory_limit", "2g"),
        cpu_limit=float(tpl.get("cpu_limit", 2.0)),
        network_mode=tpl.get("network_mode", "none"),
        allowed_hosts=tpl.get("allowed_hosts") or [],
        timeout_seconds=tpl.get("timeout_seconds", 1800),
        organization_id=str(user.organization_id),
    )
    manager = get_sandbox_manager()
    await manager.create(config)
    return RedirectResponse("/sandboxes?tab=instances", status_code=303)


@router.post("/sandboxes/{sandbox_id}/stop")
async def stop_sandbox_web(request: Request, sandbox_id: str):
    """Stop a sandbox from the web UI."""
    await get_user_context(request)
    await _check_csrf(request)
    from lucent.sandbox.manager import get_sandbox_manager

    manager = get_sandbox_manager()
    await manager.stop(sandbox_id)
    return RedirectResponse("/sandboxes?tab=instances", status_code=303)


@router.post("/sandboxes/{sandbox_id}/destroy")
async def destroy_sandbox_web(request: Request, sandbox_id: str):
    """Destroy a sandbox from the web UI."""
    await get_user_context(request)
    await _check_csrf(request)
    from lucent.sandbox.manager import get_sandbox_manager

    manager = get_sandbox_manager()
    await manager.destroy(sandbox_id)
    return RedirectResponse("/sandboxes?tab=instances", status_code=303)


@router.post("/sandboxes/{sandbox_id}/exec")
async def exec_sandbox_web(request: Request, sandbox_id: str):
    """Execute a command in a sandbox from the web UI.

    Uses session cookie auth + CSRF validation so the frontend
    never needs to handle bearer tokens.
    """
    await get_user_context(request)
    csrf_token = request.headers.get("X-CSRF-Token", "")
    await _check_csrf(request, form_token=csrf_token)

    from lucent.sandbox.manager import get_sandbox_manager

    body = await request.json()
    command = body.get("command", "")
    timeout = min(body.get("timeout", 30), 300)

    if not command:
        return JSONResponse({"error": "No command provided"}, status_code=400)

    manager = get_sandbox_manager()
    result = await manager.exec(sandbox_id, command, timeout=timeout)
    return JSONResponse({
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    })


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


# =============================================================================
# Request Tracking
# =============================================================================


@router.get("/activity", response_class=HTMLResponse)
async def activity_list(
    request: Request,
    status: str | None = None,
    source: str | None = None,
):
    """Unified activity page — all requests from users and the daemon."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)

    org_id = str(user.organization_id)
    requests_data = await repo.list_requests(org_id, status=status, source=source, limit=100)
    summary = await repo.get_active_summary(org_id)

    # Load task counts for each request
    for req in requests_data:
        tasks = await repo.list_tasks(str(req["id"]))
        statuses = [t["status"] for t in tasks]
        req["task_count"] = len(tasks)
        req["tasks_completed"] = sum(1 for s in statuses if s == "completed")
        req["tasks_running"] = sum(1 for s in statuses if s in ("claimed", "running"))
        req["tasks_failed"] = sum(1 for s in statuses if s == "failed")

    # Count needs-review items for the badge
    memory_repo = MemoryRepository(pool)
    review_result = await memory_repo.search(
        tags=["daemon", "needs-review"],
        limit=1,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    needs_review_count = review_result.get("total_count", 0)

    return templates.TemplateResponse(
        request,
        "requests_list.html",
        {
            "user": user,
            "requests": requests_data,
            "summary": summary,
            "filter_status": status,
            "filter_source": source,
            "needs_review_count": needs_review_count,
        },
    )


@router.get("/requests", response_class=HTMLResponse)
async def requests_redirect(request: Request):
    """Redirect old /requests URL to /activity."""
    qs = str(request.url.query)
    url = "/activity" + ("?" + qs if qs else "")
    return RedirectResponse(url=url, status_code=301)


@router.get("/activity/{request_id}", response_class=HTMLResponse)
@router.get("/requests/{request_id}", response_class=HTMLResponse)
async def request_detail(request: Request, request_id: str):
    """Full request detail with task tree, events, and memory links."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)

    req = await repo.get_request_with_tasks(request_id, str(user.organization_id))
    if not req:
        raise HTTPException(404, "Request not found")

    # Get recent events for the activity feed
    recent_events = []
    for task in req.get("tasks", []):
        for event in task.get("events", []):
            event["task_title"] = task["title"]
            event["agent_type"] = task.get("agent_type")
            recent_events.append(event)
    recent_events.sort(key=lambda e: e["created_at"], reverse=True)

    return templates.TemplateResponse(
        request,
        "request_detail.html",
        {
            "user": user,
            "req": req,
            "recent_events": recent_events[:50],
        },
    )


@router.post("/requests/tasks/{task_id}/retry", response_class=HTMLResponse)
async def retry_task(request: Request, task_id: str):
    """Retry a failed task — resets it to pending for the daemon to pick up."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(pool)

    task = await repo.retry_task(task_id, org_id=str(user.organization_id))
    if not task:
        raise HTTPException(409, "Task not in failed state")

    request_id = str(task["request_id"])
    return RedirectResponse(f"/requests/{request_id}", status_code=303)


# =============================================================================
# Schedules
# =============================================================================


@router.get("/schedules", response_class=HTMLResponse)
async def schedules_list(request: Request, status: str | None = None, enabled: str | None = None):
    """List all scheduled tasks with filtering."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)

    org_id = str(user.organization_id)
    enabled_filter = True if enabled == "true" else (False if enabled == "false" else None)
    schedules = await repo.list_schedules(org_id, status=status, enabled=enabled_filter)
    summary = await repo.get_summary(org_id)

    return templates.TemplateResponse(
        request,
        "schedules_list.html",
        {
            "user": user,
            "schedules": schedules,
            "summary": summary,
            "filter_status": status,
            "filter_enabled": enabled,
        },
    )


@router.get("/schedules/{schedule_id}", response_class=HTMLResponse)
async def schedule_detail(request: Request, schedule_id: str):
    """Schedule detail page with run history."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.definitions import DefinitionRepository
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)

    sched = await repo.get_schedule_with_runs(schedule_id, str(user.organization_id))
    if not sched:
        raise HTTPException(404, "Schedule not found")

    def_repo = DefinitionRepository(pool)
    active_agents = await def_repo.list_agents(str(user.organization_id), status="active")

    # Resolve sandbox template name if linked
    sandbox_template = None
    if sched.get("sandbox_template_id"):
        from lucent.db.sandbox_template import SandboxTemplateRepository

        tmpl_repo = SandboxTemplateRepository(pool)
        sandbox_template = await tmpl_repo.get(
            str(sched["sandbox_template_id"]), str(user.organization_id)
        )

    return templates.TemplateResponse(
        request,
        "schedule_detail.html",
        {
            "user": user,
            "sched": sched,
            "active_agents": active_agents,
            "sandbox_template": sandbox_template,
        },
    )


@router.post("/schedules/{schedule_id}/toggle", response_class=HTMLResponse)
async def schedule_toggle(request: Request, schedule_id: str):
    """Toggle a schedule between enabled and paused."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    org_id = str(user.organization_id)

    sched = await repo.get_schedule(schedule_id, org_id)
    if not sched:
        raise HTTPException(404, "Schedule not found")

    new_enabled = not sched["enabled"]
    await repo.toggle_schedule(schedule_id, org_id, new_enabled)

    # Redirect back to referrer or detail page
    referer = request.headers.get("referer", "")
    if "/schedules/" in referer and schedule_id in referer:
        return RedirectResponse(url=f"/schedules/{schedule_id}", status_code=303)
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/delete", response_class=HTMLResponse)
async def schedule_delete(request: Request, schedule_id: str):
    """Delete a schedule."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    org_id = str(user.organization_id)

    deleted = await repo.delete_schedule(schedule_id, org_id)
    if not deleted:
        raise HTTPException(404, "Schedule not found")

    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/edit", response_class=HTMLResponse)
async def schedule_edit(request: Request, schedule_id: str):
    """Update editable schedule fields."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.schedules import ScheduleRepository

    repo = ScheduleRepository(pool)
    org_id = str(user.organization_id)

    sched = await repo.get_schedule(schedule_id, org_id)
    if not sched:
        raise HTTPException(404, "Schedule not found")

    form = await request.form()
    updates: dict[str, Any] = {}

    # Text fields
    title = form.get("title", "").strip()
    if title and title != sched["title"]:
        updates["title"] = title

    description = form.get("description", "").strip()
    if description != (sched["description"] or ""):
        updates["description"] = description

    agent_type = form.get("agent_type", "").strip()
    if agent_type and agent_type != sched["agent_type"]:
        updates["agent_type"] = agent_type

    # Schedule-type-specific fields
    cron_expression = form.get("cron_expression", "").strip()
    if cron_expression and cron_expression != (sched.get("cron_expression") or ""):
        updates["cron_expression"] = cron_expression

    interval_str = form.get("interval_seconds", "").strip()
    if interval_str:
        try:
            interval_val = int(interval_str)
            if interval_val != (sched.get("interval_seconds") or 0):
                updates["interval_seconds"] = interval_val
        except ValueError:
            pass

    # Prompt (free-form text sent to the agent)
    prompt = form.get("prompt", "").strip()
    if prompt != (sched.get("prompt") or ""):
        updates["prompt"] = prompt

    if updates:
        await repo.update_schedule(schedule_id, org_id, **updates)

    return RedirectResponse(url=f"/schedules/{schedule_id}", status_code=303)
