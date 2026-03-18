"""Authentication routes — login, logout, setup."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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
    is_first_run,
    validate_password_complexity,
)
from lucent.db import get_pool
from lucent.logging import get_logger

from ._shared import (
    _check_csrf,
    _get_csrf_for_request,
    _set_csrf_cookie,
    get_user_context,
    templates,
)

logger = get_logger("web.routes.auth")

router = APIRouter()


# =============================================================================
# Authentication Routes (unauthenticated)
# =============================================================================


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    """Render the login page."""
    pool = await get_pool()
    first_run = await is_first_run(pool)
    if first_run:
        return RedirectResponse(url="/setup", status_code=303)

    # If already logged in, redirect to dashboard
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        from lucent.auth_providers import validate_session

        user = await validate_session(pool, session_token)
        if user:
            return RedirectResponse("/", status_code=303)

    csrf_token = _get_csrf_for_request(request)

    provider = await get_auth_provider()
    response = templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": error,
            "csrf_token": csrf_token,
            "csrf_field_name": CSRF_FIELD_NAME,
            "fields": provider.get_login_fields(),
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
    csrf = generate_csrf_token()
    _set_csrf_cookie(response, csrf)
    return response


@router.post("/logout")
async def logout(request: Request):
    """Log the user out."""
    await _check_csrf(request)
    pool = await get_pool()

    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        from lucent.auth_providers import validate_session

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
    """Render the initial setup page."""
    pool = await get_pool()
    first_run = await is_first_run(pool)
    if not first_run:
        return RedirectResponse(url="/login", status_code=303)

    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "setup.html",
        {
            "error": error,
            "csrf_token": csrf_token,
            "csrf_field_name": CSRF_FIELD_NAME,
        },
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
