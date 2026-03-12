"""FastAPI application for the Lucent Admin API and Web Interface.

This module provides:
- REST API for memory management
- Web-based admin dashboard using Jinja2 + HTMX

The API and web interface run alongside the MCP server.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from lucent.api.routers import daemon_messages as daemon_messages_router
from lucent.api.routers import daemon_tasks as daemon_tasks_router
from lucent.api.routers import export, memories, search
from lucent.db import close_db, init_db
from lucent.logging import get_logger, set_correlation_id
from lucent.mode import is_team_mode
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - startup and shutdown."""
    # Startup: Initialize database pool
    import os

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        await init_db(database_url)

    # Start MCP session manager if configured
    if _mcp_session_manager:
        async with _mcp_session_manager.run():
            yield
    else:
        yield

    # Shutdown: Close database pool
    await close_db()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Lucent Admin API",
        description="REST API for managing the Lucent memory system",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # Configure CORS
    allowed_origins = os.environ.get("LUCENT_CORS_ORIGINS", "*").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=allowed_origins != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def correlation_id_middleware(request: Request, call_next):
        """Generate or propagate a correlation ID for every request."""
        cid = request.headers.get("X-Request-ID")
        if cid:
            set_correlation_id(cid)
        else:
            cid = set_correlation_id()
        response = await call_next(request)
        response.headers["X-Request-ID"] = cid
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
        from lucent.api.routers import access, audit, organizations, users

        app.include_router(audit.router, prefix="/api/audit", tags=["Audit"])
        app.include_router(access.router, prefix="/api/access", tags=["Access"])
        app.include_router(users.router, prefix="/api/users", tags=["Users"])
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

    # Include web interface routes (excluded from API docs)
    app.include_router(web_router, include_in_schema=False)

    # Mount static files (logo, images, etc.)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/api/health", include_in_schema=False)
    async def health_check() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "healthy"}

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """Handle unhandled exceptions — log detail, return generic error."""
        logger.error(f"Unhandled exception on {request.method} {request.url.path}", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"},
        )

    return app


# Note: app instance is created by server.py main() via create_app().
# Do NOT create a module-level app instance here to avoid double initialization.
