"""FastAPI application for the Lucent Admin API and Web Interface.

This module provides:
- REST API for memory management
- Web-based admin dashboard using Jinja2 + HTMX

The API and web interface run alongside the MCP server.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from lucent.api.routers import daemon_messages as daemon_messages_router
from lucent.api.routers import daemon_tasks as daemon_tasks_router
from lucent.api.routers import export, memories, search
from lucent.db import close_db, init_db
from lucent.logging import get_correlation_id, get_logger, set_correlation_id
from lucent.mode import is_team_mode
from lucent.rate_limit import get_rate_limiter
from lucent.secrets import initialize_secret_provider
from lucent.web.routes import router as web_router

# Get logger for this module
logger = get_logger("api.app")

# Path to static files directory
STATIC_DIR = Path(__file__).parent.parent / "web" / "static"

# MCP session manager - set by server.py before creating the app
_mcp_session_manager = None


def set_mcp_session_manager(session_manager):
    """Set the MCP session manager for lifecycle integration."""
    global _mcp_session_manager
    _mcp_session_manager = session_manager


def _instrument_otel(app: FastAPI) -> None:
    """Apply OTEL auto-instrumentation when enabled.

    Instruments FastAPI for automatic HTTP span creation. DB query tracing
    (asyncpg) is handled by the DB layer in pool.py during init_db().
    Safe to call when OTEL is disabled (no-op).
    """
    from lucent.telemetry import is_enabled

    if not is_enabled():
        return

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="health",  # skip /api/health from tracing
        )
        logger.info("OTEL: FastAPI instrumentation enabled")
    except Exception as e:
        logger.warning(f"OTEL: Failed to instrument FastAPI: {e}")


async def _sync_built_in_definitions():
    """Sync built-in skills and agents from .github/ into the database."""
    try:
        from lucent.db import get_pool

        pool = await get_pool()
        from lucent.db.definitions import DefinitionRepository

        repo = DefinitionRepository(pool)
        # Get any org to sync into (there's typically one)
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM organizations LIMIT 1")
        if not row:
            return
        org_id = str(row["id"])
        # Sync skills from .github/skills/
        for candidate in [
            Path("/app/.github/skills"),
            Path(__file__).resolve().parents[3] / ".github" / "skills",
        ]:
            if candidate.is_dir():
                count = await repo.sync_built_in_skills(org_id, str(candidate))
                if count:
                    logger.info(f"Synced {count} built-in skill definitions")
                break
        # Sync agents from .github/agents/definitions/
        for candidate in [
            Path("/app/.github/agents/definitions"),
            Path(__file__).resolve().parents[3] / ".github" / "agents" / "definitions",
        ]:
            if candidate.is_dir():
                count = await repo.sync_built_in_agents(org_id, str(candidate))
                if count:
                    logger.info(f"Synced {count} built-in agent definitions")
                break
    except Exception as e:
        logger.warning(f"Failed to sync built-in definitions: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - startup and shutdown."""
    # Startup: Initialize telemetry (no-op if OTEL_ENABLED is false)
    from lucent.telemetry import init_telemetry, shutdown_telemetry

    init_telemetry(service_name="lucent-api")
    _instrument_otel(app)

    # Startup: Initialize database pool
    import os

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        await init_db(database_url)
        from lucent.db import get_pool as _get_pool_for_secrets

        _secret_pool = await _get_pool_for_secrets()
        initialize_secret_provider(_secret_pool)

    # Load model registry from database
    try:
        from lucent.db import get_pool as _get_pool

        _pool = await _get_pool()
        if _pool:
            from lucent.model_registry import load_models_from_db

            await load_models_from_db(_pool)
    except Exception:
        pass  # Fall back to hardcoded registry

    # Sync built-in skills from .github/skills/ into the DB
    await _sync_built_in_definitions()

    # Start MCP session manager if configured
    if _mcp_session_manager:
        async with _mcp_session_manager.run():
            yield
    else:
        yield

    # Shutdown: Close database pool, then telemetry
    await close_db()
    shutdown_telemetry()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    from lucent import __version__

    app = FastAPI(
        title="Lucent Admin API",
        description="REST API for managing the Lucent memory system",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # Configure CORS — default is no origins (safe); set LUCENT_CORS_ORIGINS explicitly
    cors_env = os.environ.get("LUCENT_CORS_ORIGINS", "")
    if cors_env == "*":
        logger.warning("=" * 72)
        logger.warning(
            "SECURITY WARNING: LUCENT_CORS_ORIGINS is set to '*' — "
            "this allows ANY origin to make cross-origin requests."
        )
        if is_team_mode():
            logger.warning(
                "CRITICAL: Wildcard CORS in team mode exposes multi-user data "
                "to cross-origin attacks. Set explicit origins immediately."
            )
        else:
            logger.warning(
                "Set explicit origins (e.g. 'http://localhost:8766') for safe operation."
            )
        logger.warning("=" * 72)
    allowed_origins = [o for o in cors_env.split(",") if o] if cors_env and cors_env != "*" else []
    # Never allow credentials with wildcard — only when explicit origins are set
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=bool(allowed_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def otel_http_metrics_middleware(request: Request, call_next):
        """Record HTTP request metrics when OTEL is enabled."""
        from lucent.telemetry import is_enabled

        if not is_enabled():
            return await call_next(request)

        import time

        from lucent.metrics import metrics

        start = time.monotonic()
        response = await call_next(request)
        duration = time.monotonic() - start

        attrs = {
            "method": request.method,
            "route": request.url.path,
            "status_code": response.status_code,
        }
        metrics.http_request_duration.record(duration, attrs)
        metrics.http_requests_total.add(1, attrs)

        if response.status_code >= 400:
            error_attrs = {
                "status_code": response.status_code,
                "error_category": "5xx" if response.status_code >= 500 else "4xx",
            }
            metrics.http_errors_total.add(1, error_attrs)

        return response

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        """Add security headers including CSP to all responses."""
        response = await call_next(request)
        # Content-Security-Policy: restrict resource loading to same origin.
        # unsafe-inline needed for inline event handlers / <script> blocks in templates.
        # unsafe-eval needed for Tailwind CSS JIT which uses eval() at runtime.
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    @app.middleware("http")
    async def correlation_id_middleware(request: Request, call_next):
        """Generate or propagate a correlation ID for every request.

        When OTEL is active, extracts trace_id from the OTEL span context and
        uses it as the correlation ID for log-trace correlation. Falls back to
        the X-Request-ID header or generates a 12-char hex ID when OTEL is
        inactive.
        """
        from lucent.telemetry import (
            bridge_correlation_id,
            is_enabled,
            sync_trace_to_correlation_id,
            unbind_correlation_id,
        )

        cid = None
        otel_token = None

        # When OTEL is active, extract trace_id as correlation ID
        if is_enabled():
            sync_trace_to_correlation_id()
            cid = get_correlation_id()

        # Fall back to X-Request-ID header or generate a new one
        if not cid:
            header_cid = request.headers.get("X-Request-ID")
            if header_cid:
                set_correlation_id(header_cid)
                cid = header_cid
            else:
                cid = set_correlation_id()

        # Bridge correlation ID into OTEL baggage and span attributes
        if is_enabled():
            otel_token = bridge_correlation_id()

        response = await call_next(request)
        response.headers["X-Request-ID"] = cid

        # Clean up OTEL baggage context
        if otel_token is not None:
            unbind_correlation_id(otel_token)

        return response

    @app.middleware("http")
    async def api_rate_limit_middleware(request: Request, call_next):
        """Apply per-key rate limiting to all /api/* endpoints.

        Uses the same RateLimiter singleton and LUCENT_RATE_LIMIT_PER_MINUTE
        config as the MCP rate limiting in server.py.
        """
        path = request.url.path
        if not path.startswith("/api/") or path == "/api/health":
            return await call_next(request)

        rate_limiter = get_rate_limiter()

        # Determine rate limit key: prefer API key, fall back to client IP
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            rate_key = f"api:{auth_header[7:]}"
        elif auth_header:
            rate_key = f"api:{auth_header}"
        else:
            from lucent.rate_limit import get_client_ip

            client_ip = get_client_ip(request)
            rate_key = f"api:ip:{client_ip}"

        rate_result = rate_limiter.check_rate_limit(rate_key)

        if not rate_result.allowed:
            logger.warning(
                "API rate limit exceeded: key=%s, path=%s, retry_after=%s",
                rate_key[:20] + "...",
                path,
                rate_result.headers.get("Retry-After"),
            )
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded. Please slow down your requests."},
                headers=rate_result.headers,
            )

        response = await call_next(request)
        for name, value in rate_result.headers.items():
            response.headers[name] = value
        return response

    # Include core API routers
    # Export router must be registered before memories router to avoid
    # path conflicts with /{memory_id} routes
    app.include_router(export.router, prefix="/api/memories/export", tags=["Export"])
    app.include_router(memories.router, prefix="/api/memories", tags=["Memories"])
    app.include_router(search.router, prefix="/api/search", tags=["Search"])
    app.include_router(
        daemon_tasks_router.router,
        prefix="/api/daemon/tasks",
        tags=["Daemon Tasks"],
    )
    app.include_router(
        daemon_messages_router.router,
        prefix="/api/daemon/messages",
        tags=["Daemon Messages"],
    )

    # Include team-only API routers
    if is_team_mode():
        from lucent.api.routers import access, audit, groups, organizations, users

        app.include_router(audit.router, prefix="/api/audit", tags=["Audit"])
        app.include_router(access.router, prefix="/api/access", tags=["Access"])
        app.include_router(users.router, prefix="/api/users", tags=["Users"])
        app.include_router(groups.router, prefix="/api", tags=["Groups"])
        app.include_router(
            organizations.router, prefix="/api/organizations", tags=["Organizations"]
        )

    # Include definitions management router
    from lucent.api.routers import definitions

    app.include_router(definitions.router, prefix="/api", tags=["Definitions"])

    # Include request tracking router
    from lucent.api.routers import requests as requests_router

    app.include_router(requests_router.router, prefix="/api", tags=["Requests"])

    # Include schedule management router
    from lucent.api.routers import schedules as schedules_router

    app.include_router(schedules_router.router, prefix="/api", tags=["Schedules"])

    # Include chat router
    from lucent.api.routers import chat as chat_router

    app.include_router(chat_router.router, prefix="/api", tags=["Chat"])

    # Include integrations routers
    from lucent.integrations.router import admin_router as integrations_admin_router
    from lucent.integrations.router import webhook_router as integrations_webhook_router

    app.include_router(
        integrations_admin_router,
        prefix="/api/v1/integrations",
        tags=["Integrations"],
    )
    app.include_router(
        integrations_webhook_router,
        prefix="/integrations",
        tags=["Integrations - Webhooks"],
    )

    # Include sandbox management router
    from lucent.api.routers import sandboxes as sandboxes_router

    app.include_router(sandboxes_router.router, prefix="/api/sandboxes", tags=["Sandboxes"])

    # Include secret storage router
    from lucent.api.routers import secrets as secrets_router

    app.include_router(secrets_router.router, prefix="/api", tags=["Secrets"])

    # Include web interface routes (excluded from API docs)
    app.include_router(web_router, include_in_schema=False)

    # Mount static files (logo, images, etc.)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/api/health", include_in_schema=False)
    async def health_check() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "healthy"}

    def _is_web_request(request: Request) -> bool:
        """Check if the request is for the web UI (not API)."""
        path = request.url.path
        accept = request.headers.get("accept", "")
        if path.startswith("/api/") or path.startswith("/mcp"):
            return False
        return "text/html" in accept or not path.startswith("/api/")

    def _error_title(status_code: int) -> str:
        titles = {
            400: "Bad Request",
            403: "Forbidden",
            404: "Page Not Found",
            405: "Method Not Allowed",
            500: "Server Error",
        }
        return titles.get(status_code, "Something Went Wrong")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        """Render HTML error pages for web UI, JSON for API."""
        # Pass through redirects (3xx) — don't render error pages for them
        if 300 <= exc.status_code < 400:
            headers = getattr(exc, "headers", None) or {}
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=headers,
            )
        if _is_web_request(request):
            from lucent.web.routes._shared import templates

            ctx = {
                "request": request,
                "status_code": exc.status_code,
                "title": _error_title(exc.status_code),
                "detail": exc.detail or _error_title(exc.status_code),
                "user": None,
                "is_admin": False,
                "team_mode": is_team_mode,
            }
            return templates.TemplateResponse(
                request, "error.html", ctx, status_code=exc.status_code
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """Handle unhandled exceptions — log detail, return generic error."""
        logger.error(f"Unhandled exception on {request.method} {request.url.path}", exc_info=exc)
        if _is_web_request(request):
            from lucent.web.routes._shared import templates

            ctx = {
                "request": request,
                "status_code": 500,
                "title": "Server Error",
                "detail": "An unexpected error occurred. Please try again.",
                "user": None,
                "is_admin": False,
                "team_mode": is_team_mode,
            }
            return templates.TemplateResponse(request, "error.html", ctx, status_code=500)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"},
        )

    return app


# Note: app instance is created by server.py main() via create_app().
# Do NOT create a module-level app instance here to avoid double initialization.
