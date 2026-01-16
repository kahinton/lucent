"""mnemeMCP Server - Unified MCP + API + Web Interface.

This module provides a single unified server that handles:
- MCP protocol at /mcp
- REST API at /api/*
- Web dashboard at /
"""

import os
import sys

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from mnememcp.auth import is_dev_mode, set_current_user, set_current_api_key_id
from mnememcp.prompts.memory_usage import get_memory_system_prompt, get_memory_system_prompt_short, get_user_introduction_prompt
from mnememcp.tools.memories import register_tools


# Load environment variables
load_dotenv()

# Server configuration
HOST = os.environ.get("MNEMEMCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MNEMEMCP_PORT", "8766"))

# Create the MCP server
mcp = FastMCP("mnemeMCP")

# Register all memory tools
register_tools(mcp)


class MCPAuthMiddleware(BaseHTTPMiddleware):
    """Middleware to handle authentication for MCP requests.
    
    API key authentication is ALWAYS required for MCP access, even in dev mode.
    Dev mode only bypasses auth for the web UI, not for programmatic access.
    """
    
    async def dispatch(self, request: Request, call_next):
        from starlette.responses import JSONResponse
        from mnememcp.db.client import ApiKeyRepository, UserRepository, get_pool, init_db
        
        # Get authorization header
        auth_header = request.headers.get("authorization", "")
        
        # Try API key authentication
        if auth_header:
            api_key = auth_header
            if api_key.startswith("Bearer "):
                api_key = api_key[7:]
            
            if api_key.startswith("mcp_"):
                try:
                    # Ensure pool is initialized
                    try:
                        pool = await get_pool()
                    except RuntimeError:
                        database_url = os.environ.get("DATABASE_URL")
                        if database_url:
                            pool = await init_db(database_url)
                        else:
                            pool = None
                    
                    if pool:
                        api_key_repo = ApiKeyRepository(pool)
                        key_info = await api_key_repo.verify(api_key)
                        
                        if key_info:
                            # Get full user record and set context
                            user_repo = UserRepository(pool)
                            user = await user_repo.get_by_id(key_info["user_id"])
                            if user:
                                set_current_user(user)
                                set_current_api_key_id(key_info["id"])  # Store API key ID for auditing
                                response = await call_next(request)
                                set_current_user(None)  # Clear after request
                                set_current_api_key_id(None)
                                return response
                except Exception as e:
                    print(f"API key auth error: {e}", file=sys.stderr)
            
            # Invalid API key provided
            return JSONResponse(
                status_code=401,
                content={
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32001,
                        "message": "Unauthorized: Invalid or expired API key",
                    },
                    "id": None,
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        # No authorization header - reject the request
        return JSONResponse(
            status_code=401,
            content={
                "jsonrpc": "2.0",
                "error": {
                    "code": -32001,
                    "message": "Unauthorized: API key required. Use Authorization: Bearer mcp_your_key_here",
                },
                "id": None,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )


# Register prompts
@mcp.prompt()
def memory_usage_guide() -> str:
    """Get comprehensive guidance on how to effectively use the memory system.
    
    This prompt provides detailed instructions on memory types, importance ratings,
    best practices for creating and searching memories, and example usage patterns.
    """
    return get_memory_system_prompt()


@mcp.prompt()
def memory_usage_guide_short() -> str:
    """Get a condensed guide on memory system usage.
    
    A shorter version of the memory usage guide for contexts with limited prompt space.
    """
    return get_memory_system_prompt_short()


@mcp.prompt()
def user_introduction() -> str:
    """Get guidance for greeting users and personalizing interactions.
    
    This prompt walks you through:
    1. Checking if the user is new or returning (via individual memories)
    2. Greeting them appropriately - warmly if returning, introductory if new
    3. Learning about their preferences, working style, and communication style
    4. Storing what you learn for personalized future interactions
    
    Use this at the start of conversations to make interactions feel like
    working with an actual teammate who remembers and knows the user.
    """
    return get_user_introduction_prompt()


def get_mcp_app():
    """Get the MCP Starlette app with auth middleware.
    
    Returns the MCP app which has routes at /mcp.
    """
    mcp_app = mcp.streamable_http_app()
    mcp_app.add_middleware(MCPAuthMiddleware)
    return mcp_app


def main() -> None:
    """Main entry point for the unified mnemeMCP server."""
    import uvicorn
    from mnememcp.api.app import create_app, set_mcp_session_manager
    
    # Validate DATABASE_URL is set
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is required.", file=sys.stderr)
        print("Example: postgresql://user:password@localhost:5432/mnememcp", file=sys.stderr)
        sys.exit(1)
    
    # Show dev mode status
    if is_dev_mode():
        print("Running in DEVELOPMENT MODE - authentication bypassed for web UI", file=sys.stderr)
    else:
        print("Running in PRODUCTION MODE - API key required for MCP/API access", file=sys.stderr)
    
    # Get MCP app and session manager
    mcp_app = get_mcp_app()
    
    # Set the session manager for lifecycle integration
    set_mcp_session_manager(mcp.session_manager)
    
    # Create the unified FastAPI app (after setting session manager)
    app = create_app()
    
    # Add MCP routes directly (MCP SDK creates route at /mcp)
    for route in mcp_app.routes:
        app.routes.append(route)
    
    print(f"Starting mnemeMCP server on http://{HOST}:{PORT}", file=sys.stderr)
    print(f"  MCP endpoint: http://{HOST}:{PORT}/mcp", file=sys.stderr)
    print(f"  REST API: http://{HOST}:{PORT}/api", file=sys.stderr)
    print(f"  Web UI: http://{HOST}:{PORT}/", file=sys.stderr)
    print(f"  API docs: http://{HOST}:{PORT}/api/docs", file=sys.stderr)
    
    # Run the unified server
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
