"""FastAPI application for the Hindsight Admin API and Web Interface.

This module provides:
- REST API for memory management
- Web-based admin dashboard using Jinja2 + HTMX

The API and web interface run alongside the MCP server.
"""

import traceback
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from hindsight.api.routers import memories, search, audit, access, users, organizations
from hindsight.web.routes import router as web_router
from hindsight.db.client import init_db, close_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - startup and shutdown."""
    # Startup: Initialize database pool
    import os
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        await init_db(database_url)
    
    yield
    
    # Shutdown: Close database pool
    await close_db()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Hindsight Admin API",
        description="REST API for managing the Hindsight memory system",
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
    
    # Include API routers
    app.include_router(memories.router, prefix="/api/memories", tags=["Memories"])
    app.include_router(search.router, prefix="/api/search", tags=["Search"])
    app.include_router(audit.router, prefix="/api/audit", tags=["Audit"])
    app.include_router(access.router, prefix="/api/access", tags=["Access"])
    app.include_router(users.router, prefix="/api/users", tags=["Users"])
    app.include_router(organizations.router, prefix="/api/organizations", tags=["Organizations"])
    
    # Include web interface routes
    app.include_router(web_router, tags=["Web Interface"])
    
    @app.get("/api/health", tags=["Health"])
    async def health_check() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "healthy"}
    
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """Handle unhandled exceptions with detailed logging."""
        error_detail = traceback.format_exc()
        print(f"Unhandled exception: {error_detail}")
        return JSONResponse(
            status_code=500,
            content={"error": str(exc), "detail": error_detail},
        )
    
    return app


# Create the app instance
app = create_app()
