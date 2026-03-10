"""Tests for the MCP server entry point (src/lucent/server.py).

Tests server configuration, tool registration, prompt registration,
ASGI app creation, and auth middleware behavior.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

# ============================================================================
# Server Configuration
# ============================================================================


class TestServerConfig:
    """Tests for server configuration defaults and env overrides."""

    def test_default_host(self):
        """Test that default host is 0.0.0.0."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LUCENT_HOST", None)
            # Re-import to pick up env
            import importlib

            import lucent.server as srv
            importlib.reload(srv)
            assert srv.HOST == "0.0.0.0"

    def test_default_port(self):
        """Test that default port is 8766."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LUCENT_PORT", None)
            import importlib

            import lucent.server as srv
            importlib.reload(srv)
            assert srv.PORT == 8766

    def test_custom_host(self):
        """Test that LUCENT_HOST env var overrides default."""
        with patch.dict(os.environ, {"LUCENT_HOST": "127.0.0.1"}):
            import importlib

            import lucent.server as srv
            importlib.reload(srv)
            assert srv.HOST == "127.0.0.1"

    def test_custom_port(self):
        """Test that LUCENT_PORT env var overrides default."""
        with patch.dict(os.environ, {"LUCENT_PORT": "9999"}):
            import importlib

            import lucent.server as srv
            importlib.reload(srv)
            assert srv.PORT == 9999


# ============================================================================
# Tool Registration
# ============================================================================


class TestToolRegistration:
    """Tests that register_tools registers all expected MCP tools."""

    def test_register_tools_adds_expected_tools(self):
        """Test that register_tools registers the core tool set."""
        from lucent.tools.memories import register_tools

        mcp = FastMCP("test-registration")
        register_tools(mcp)

        tool_names = set(mcp._tool_manager._tools.keys())

        expected_tools = {
            "create_memory",
            "get_memory",
            "get_memories",
            "get_current_user_context",
            "search_memories",
            "search_memories_full",
            "update_memory",
            "delete_memory",
            "get_existing_tags",
            "get_tag_suggestions",
            "get_memory_versions",
            "restore_memory_version",
            "create_daemon_task",
            "claim_task",
            "release_claim",
            "export_memories",
            "import_memories",
        }

        for tool in expected_tools:
            assert tool in tool_names, f"Missing expected tool: {tool}"

    def test_register_tools_idempotent(self):
        """Test that calling register_tools twice doesn't break things."""
        from lucent.tools.memories import register_tools

        mcp = FastMCP("test-idempotent")
        register_tools(mcp)
        count_first = len(mcp._tool_manager._tools)
        register_tools(mcp)
        count_second = len(mcp._tool_manager._tools)

        # FastMCP overwrites tools with the same name, so count should stay same
        assert count_second >= count_first


# ============================================================================
# Prompt Registration
# ============================================================================


class TestPromptRegistration:
    """Tests for server-level prompt registration."""

    def test_memory_usage_guide_returns_string(self):
        """Test that the memory_usage_guide prompt returns content."""
        from lucent.server import memory_usage_guide

        result = memory_usage_guide()
        assert isinstance(result, str)
        assert len(result) > 100  # Should be substantial
        assert "memory" in result.lower()

    def test_memory_usage_guide_short_returns_string(self):
        """Test that the short guide returns shorter content."""
        from lucent.server import memory_usage_guide, memory_usage_guide_short

        short = memory_usage_guide_short()
        full = memory_usage_guide()
        assert isinstance(short, str)
        assert len(short) > 50
        assert len(short) <= len(full)

    def test_user_introduction_returns_string(self):
        """Test that the user_introduction prompt returns content."""
        from lucent.server import user_introduction

        result = user_introduction()
        assert isinstance(result, str)
        assert len(result) > 50


# ============================================================================
# get_mcp_app
# ============================================================================


class TestGetMcpApp:
    """Tests for the get_mcp_app function."""

    def test_returns_asgi_app(self):
        """Test that get_mcp_app returns a valid ASGI-like app."""
        from lucent.server import get_mcp_app

        app = get_mcp_app()
        assert app is not None
        # Starlette app should have routes
        assert hasattr(app, "routes")


# ============================================================================
# MCPAuthMiddleware
# ============================================================================


class TestMCPAuthMiddleware:
    """Tests for the MCPAuthMiddleware ASGI middleware."""

    def _make_scope(self, path="/mcp", headers=None):
        """Build a minimal ASGI HTTP scope."""
        raw_headers = []
        if headers:
            for k, v in headers.items():
                raw_headers.append((k.encode(), v.encode()))
        return {
            "type": "http",
            "path": path,
            "headers": raw_headers,
        }

    @pytest.fixture
    def inner_app(self):
        """A simple inner ASGI app that records whether it was called."""
        calls = []

        async def app(scope, receive, send):
            calls.append(scope)

        app._calls = calls
        return app

    async def test_non_http_passes_through(self, inner_app):
        """Test that non-HTTP scopes (e.g., websocket) pass through."""
        from lucent.server import MCPAuthMiddleware

        middleware = MCPAuthMiddleware(inner_app)
        scope = {"type": "websocket", "path": "/mcp"}
        await middleware(scope, AsyncMock(), AsyncMock())

        assert len(inner_app._calls) == 1

    async def test_non_mcp_path_passes_through(self, inner_app):
        """Test that non-/mcp paths pass through without auth."""
        from lucent.server import MCPAuthMiddleware

        middleware = MCPAuthMiddleware(inner_app)
        scope = self._make_scope(path="/api/health")
        await middleware(scope, AsyncMock(), AsyncMock())

        assert len(inner_app._calls) == 1

    async def test_mcp_no_auth_header_returns_401(self):
        """Test that /mcp requests without auth header get 401."""
        from lucent.server import MCPAuthMiddleware

        inner = AsyncMock()
        middleware = MCPAuthMiddleware(inner)
        scope = self._make_scope(path="/mcp")

        responses = []

        async def mock_send(message):
            responses.append(message)

        await middleware(scope, AsyncMock(), mock_send)

        # Inner app should NOT have been called
        inner.assert_not_awaited()
        # Should have sent a response
        assert len(responses) >= 1
        # First message should be http.response.start with 401
        start_msg = responses[0]
        assert start_msg["type"] == "http.response.start"
        assert start_msg["status"] == 401

    async def test_mcp_invalid_api_key_returns_401(self):
        """Test that /mcp with a non-hs_ prefixed key gets 401."""
        from lucent.server import MCPAuthMiddleware

        inner = AsyncMock()
        middleware = MCPAuthMiddleware(inner)
        scope = self._make_scope(
            path="/mcp",
            headers={"authorization": "Bearer invalid_key_here"},
        )

        responses = []

        async def mock_send(message):
            responses.append(message)

        await middleware(scope, AsyncMock(), mock_send)

        inner.assert_not_awaited()
        start_msg = responses[0]
        assert start_msg["status"] == 401

    async def test_mcp_path_prefix_matches(self):
        """Test that paths like /mcp/messages also trigger auth."""
        from lucent.server import MCPAuthMiddleware

        inner = AsyncMock()
        middleware = MCPAuthMiddleware(inner)
        scope = self._make_scope(path="/mcp/messages")

        responses = []

        async def mock_send(message):
            responses.append(message)

        await middleware(scope, AsyncMock(), mock_send)

        # Should still get 401 (no auth header)
        inner.assert_not_awaited()
        assert responses[0]["status"] == 401
