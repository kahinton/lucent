"""FastAPI application for the Lucent Admin API and Web Interface.

This module provides:
- REST API for memory management
- Web-based admin dashboard using Jinja2 + HTMX

The API and web interface run alongside the MCP server.
"""

import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from lucent.api.routers import memories, search
from lucent.logging import get_logger
from lucent.mode import is_team_mode

# Get logger for this module
logger = get_logger("api.app")
from lucent.web.routes import router as web_router
from lucent.db import init_db, close_db

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
    
    # Configure CORS for web dashboard
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Configure appropriately for production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Include core API routers
    app.include_router(memories.router, prefix="/api/memories", tags=["Memories"])
    app.include_router(search.router, prefix="/api/search", tags=["Search"])
    
    # Include team-only API routers
    if is_team_mode():
        from lucent.api.routers import audit, access, users, organizations
        app.include_router(audit.router, prefix="/api/audit", tags=["Audit"])
        app.include_router(access.router, prefix="/api/access", tags=["Access"])
        app.include_router(users.router, prefix="/api/users", tags=["Users"])
        app.include_router(organizations.router, prefix="/api/organizations", tags=["Organizations"])
    
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
        """Handle unhandled exceptions with detailed logging."""
        logger.error(f"Unhandled exception on {request.method} {request.url.path}", exc_info=exc)
        error_detail = traceback.format_exc()
        return JSONResponse(
            status_code=500,
            content={"error": str(exc), "detail": error_detail},
        )
    
    return app


# Create the app instance
app = create_app()
