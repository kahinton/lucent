"""Lucent Server - Unified MCP + API + Web Interface.

This module provides a single unified server that handles:
- MCP protocol at /mcp
- REST API at /api/*
- Web dashboard at /
"""

import os
import sys
from urllib.parse import urlsplit

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from lucent.auth import set_current_api_key_id, set_current_user
from lucent.llm.context import clear_llm_context, set_llm_context
from lucent.logging import configure_logging, get_logger
from lucent.prompts.memory_usage import (
    get_memory_system_prompt,
    get_memory_system_prompt_short,
    get_user_introduction_prompt,
)
from lucent.rate_limit import get_rate_limiter
from lucent.tools.memories import register_tools

# Load environment variables
load_dotenv()

# Server configuration
HOST = os.environ.get("LUCENT_HOST", "0.0.0.0")
PORT = int(os.environ.get("LUCENT_PORT", "8766"))


def _build_mcp_transport_security() -> TransportSecuritySettings:
    """Build the MCP DNS-rebinding-protection allowlist for this deployment.

    Recent MCP SDK versions auto-enable DNS-rebinding protection on FastMCP and,
    by default, only permit loopback ``Host``/``Origin`` headers
    (``localhost``/``127.0.0.1``/``[::1]``). Lucent's ``/mcp`` endpoint, however,
    is reached over the container/service network — e.g. the daemon connects to
    ``http://lucent:8766/mcp`` — so the default allowlist rejects those requests
    with HTTP 421 Misdirected Request. (That 421 then surfaces as an
    "Attempted to exit cancel scope in a different task" error when the MCP
    client's anyio transport unwinds.)

    Protection stays enabled; we extend the allowlist with the hostnames this
    deployment is actually served as, derived from configuration:
    - loopback (host-mode daemon, local browser, health checks),
    - the host of ``LUCENT_MCP_URL`` / ``LUCENT_PUBLIC_URL`` when set,
    - any extra hosts listed in ``LUCENT_MCP_ALLOWED_HOSTS`` (comma-separated
      ``host`` or ``host:port`` values, e.g. ``lucent,lucent.internal:8766``).
    """
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    allowed_origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        "https://127.0.0.1:*",
        "https://localhost:*",
        "https://[::1]:*",
    ]

    def _host_from_url(url: str | None) -> str | None:
        if not url:
            return None
        netloc = urlsplit(url if "://" in url else f"//{url}").netloc
        return netloc or None

    extra_hosts: list[str] = []
    for url in (os.environ.get("LUCENT_MCP_URL"), os.environ.get("LUCENT_PUBLIC_URL")):
        host = _host_from_url(url)
        if host:
            extra_hosts.append(host)
    extra_hosts += [
        h.strip()
        for h in os.environ.get("LUCENT_MCP_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    ]

    for host in extra_hosts:
        bare = host.split(":", 1)[0]
        for pattern in (host, bare, f"{bare}:*"):
            if pattern not in allowed_hosts:
                allowed_hosts.append(pattern)
        for scheme in ("http", "https"):
            for origin in (f"{scheme}://{host}", f"{scheme}://{bare}", f"{scheme}://{bare}:*"):
                if origin not in allowed_origins:
                    allowed_origins.append(origin)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


# Create the MCP server
mcp = FastMCP("Lucent", transport_security=_build_mcp_transport_security())

# Register all memory tools
register_tools(mcp)

# Register request tracking tools
from lucent.tools.requests import register_request_tools  # noqa: E402

register_request_tools(mcp)

# Register schedule management tools
from lucent.tools.schedules import register_schedule_tools  # noqa: E402

register_schedule_tools(mcp)

# Register definition management tools
from lucent.tools.definitions import register_definition_tools  # noqa: E402

register_definition_tools(mcp)

# Register tool-call audit analysis and definition-improvement proposal tools
from lucent.tools.tool_audit import register_tool_audit_tools  # noqa: E402

register_tool_audit_tools(mcp)

# Get logger for this module
logger = get_logger("server")


class MCPAuthMiddleware:
    """Pure ASGI middleware to handle authentication for MCP requests.

    Uses pure ASGI instead of BaseHTTPMiddleware to preserve ContextVar across
    the request lifecycle. BaseHTTPMiddleware runs call_next in a thread pool
    which breaks ContextVar propagation.

    API key authentication is ALWAYS required for MCP access, even in dev mode.
    Dev mode only bypasses auth for the web UI, not for programmatic access.

    Only applies to /mcp routes - other routes pass through unmodified.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    @staticmethod
    def _extract_llm_context(headers: dict[bytes, bytes]) -> dict[str, str | None]:
        """Extract model-session lineage headers from an MCP request."""
        def _get(name: bytes) -> str | None:
            value = headers.get(name, b"").decode("utf-8", errors="ignore").strip()
            return value or None

        return {
            "session_id": _get(b"x-lucent-llm-session-id"),
            "turn_id": _get(b"x-lucent-llm-turn-id"),
            "message_id": _get(b"x-lucent-llm-message-id"),
            "request_id": _get(b"x-lucent-request-id"),
            "task_id": _get(b"x-lucent-task-id"),
            "schedule_run_id": _get(b"x-lucent-schedule-run-id"),
            "agent_definition_id": _get(b"x-lucent-agent-definition-id"),
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Only apply auth to /mcp routes
        path = scope.get("path", "")
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        from starlette.responses import JSONResponse

        from lucent.db import ApiKeyRepository, UserRepository, get_pool, init_db

        # Get authorization header from scope
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")

        # Try API key authentication
        if auth_header:
            api_key = auth_header
            if api_key.startswith("Bearer "):
                api_key = api_key[7:]

            if api_key.startswith("hs_"):
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
                            # Check rate limit before proceeding
                            rate_limiter = get_rate_limiter()
                            rate_result = rate_limiter.check_rate_limit(key_info["id"])

                            if not rate_result.allowed:
                                # Rate limited - return 429
                                logger.warning(
                                    "Rate limit exceeded: api_key_id=%s, retry_after=%s",
                                    key_info["id"],
                                    rate_result.headers.get("Retry-After"),
                                )
                                response = JSONResponse(
                                    status_code=429,
                                    content={
                                        "jsonrpc": "2.0",
                                        "error": {
                                            "code": -32000,
                                            "message": (
                                                "Rate limit exceeded."
                                                " Please slow down your requests."
                                            ),
                                        },
                                        "id": None,
                                    },
                                    headers=rate_result.headers,
                                )
                                await response(scope, receive, send)
                                return

                            # Get full user record and set context
                            user_repo = UserRepository(pool)
                            user = await user_repo.get_by_id(key_info["user_id"])
                            if user:
                                # Thread memory scope from API key into context dict
                                user["memory_scope_user_id"] = key_info.get("memory_scope_user_id")
                                user["memory_scope"] = key_info.get("memory_scope")
                                set_current_user(user)
                                set_current_api_key_id(key_info["id"])
                                set_llm_context(**self._extract_llm_context(headers))

                                # Wrap send to inject rate limit headers
                                rate_headers = rate_result.headers

                                async def send_with_headers(message):
                                    if message["type"] == "http.response.start":
                                        headers = list(message.get("headers", []))
                                        for name, value in rate_headers.items():
                                            headers.append((name.lower().encode(), value.encode()))
                                        message = {**message, "headers": headers}
                                    await send(message)

                                try:
                                    await self.app(scope, receive, send_with_headers)
                                finally:
                                    set_current_user(None)
                                    set_current_api_key_id(None)
                                    clear_llm_context()
                                return
                except Exception as e:
                    logger.error("API key auth error: %s", type(e).__name__)

            # Not an hs_ key — try session token auth
            # This allows the chat agent to pass the user's web session
            # token so MCP operations run as the actual user.
            if not api_key.startswith("hs_"):
                try:
                    from lucent.auth_providers import validate_session

                    try:
                        pool = await get_pool()
                    except RuntimeError:
                        database_url = os.environ.get("DATABASE_URL")
                        pool = await init_db(database_url) if database_url else None

                    if pool:
                        logger.debug("Trying session token auth on MCP: session token present")
                        user = await validate_session(pool, api_key)
                        if user:
                            logger.info(
                                "MCP session auth succeeded for user %s", user.get("display_name")
                            )

                            # Check rate limit for session token auth
                            rate_limiter = get_rate_limiter()
                            rate_result = rate_limiter.check_rate_limit(f"session:{user['id']}")

                            if not rate_result.allowed:
                                logger.warning(
                                    "Rate limit exceeded: session user_id=%s, retry_after=%s",
                                    user["id"],
                                    rate_result.headers.get("Retry-After"),
                                )
                                response = JSONResponse(
                                    status_code=429,
                                    content={
                                        "jsonrpc": "2.0",
                                        "error": {
                                            "code": -32000,
                                            "message": (
                                                "Rate limit exceeded."
                                                " Please slow down your requests."
                                            ),
                                        },
                                        "id": None,
                                    },
                                    headers=rate_result.headers,
                                )
                                await response(scope, receive, send)
                                return

                            set_current_user(user)
                            set_llm_context(**self._extract_llm_context(headers))

                            # Wrap send to inject rate limit headers
                            rate_headers = rate_result.headers

                            async def send_with_session_headers(message):
                                if message["type"] == "http.response.start":
                                    headers = list(message.get("headers", []))
                                    for name, value in rate_headers.items():
                                        headers.append((name.lower().encode(), value.encode()))
                                    message = {**message, "headers": headers}
                                await send(message)

                            try:
                                await self.app(scope, receive, send_with_session_headers)
                            finally:
                                set_current_user(None)
                                clear_llm_context()
                            return
                        else:
                            logger.warning("MCP session token auth: validate_session returned None")
                    else:
                        logger.warning("MCP session token auth: no pool available")
                except Exception as e:
                    logger.error("Session token auth error on MCP: %s", type(e).__name__)

            # Auth failed
            logger.warning("MCP auth failed: invalid credentials on %s", path)
            response = JSONResponse(
                status_code=401,
                content={
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32001,
                        "message": "Unauthorized: Invalid or expired credentials",
                    },
                    "id": None,
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        # No authorization header - reject the request
        response = JSONResponse(
            status_code=401,
            content={
                "jsonrpc": "2.0",
                "error": {
                    "code": -32001,
                    "message": (
                        "Unauthorized: API key or session token required."
                        " Use Authorization: Bearer <token>"
                    ),
                },
                "id": None,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


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
    """Get the raw MCP Starlette app.

    Returns the MCP app (auth is handled by wrapping the entire FastAPI app).
    """
    return mcp.streamable_http_app()


def main() -> None:
    """Main entry point for the unified Lucent server."""
    import uvicorn

    from lucent.api.app import create_app, set_mcp_session_manager

    # Configure logging first
    configure_logging()

    # Validate DATABASE_URL is set
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable is required")
        logger.error("Set DATABASE_URL, e.g.: postgresql://user:password@host:5432/dbname")
        sys.exit(1)

    # Show deployment mode
    from lucent.mode import get_mode

    mode = get_mode()
    logger.info(f"Deployment mode: {mode.value}")

    # Get MCP app
    mcp_app = get_mcp_app()

    # Set the session manager for lifecycle integration
    set_mcp_session_manager(mcp.session_manager)

    # Create the unified FastAPI app (after setting session manager)
    app = create_app()

    # Add MCP routes directly (MCP SDK creates route at /mcp)
    for route in mcp_app.routes:
        app.routes.append(route)

    # Wrap with webhook signature verification (only /integrations/webhook/*)
    from lucent.integrations.middleware import SignatureVerificationMiddleware

    wrapped_app = SignatureVerificationMiddleware(app)

    # Wrap the entire app with our auth middleware (only applies to /mcp paths)
    wrapped_app = MCPAuthMiddleware(wrapped_app)

    logger.info(f"Starting Lucent server on http://{HOST}:{PORT}")
    logger.info(f"  MCP endpoint: http://{HOST}:{PORT}/mcp")
    logger.info(f"  REST API: http://{HOST}:{PORT}/api")
    logger.info(f"  Web UI: http://{HOST}:{PORT}/")
    logger.info(f"  API docs: http://{HOST}:{PORT}/api/docs")

    # Run the unified server with the wrapped app
    uvicorn.run(wrapped_app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
