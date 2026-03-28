"""Tests for SSRF protection in URL validation (Finding 13).

Verifies that server-side requests to MCP bridge URLs are blocked
when they target private IPs, loopback, link-local, or cloud
metadata endpoints.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Unit tests for url_validation.validate_url
# ---------------------------------------------------------------------------

class TestValidateUrl:
    """Core SSRF validation tests."""

    def _validate(self, url, **kwargs):
        from lucent.url_validation import validate_url
        return validate_url(url, **kwargs)

    def _ssrf_error(self):
        from lucent.url_validation import SSRFError
        return SSRFError

    # ── Scheme checks ────────────────────────────────────────────

    def test_rejects_empty_url(self):
        with pytest.raises(self._ssrf_error()):
            self._validate("")

    def test_rejects_none_url(self):
        with pytest.raises(self._ssrf_error()):
            self._validate(None)

    def test_rejects_ftp_scheme(self):
        with pytest.raises(self._ssrf_error(), match="scheme"):
            self._validate("ftp://example.com/file")

    def test_rejects_file_scheme(self):
        with pytest.raises(self._ssrf_error(), match="scheme"):
            self._validate("file:///etc/passwd")

    def test_rejects_javascript_scheme(self):
        with pytest.raises(self._ssrf_error(), match="scheme"):
            self._validate("javascript:alert(1)")

    def test_rejects_no_scheme(self):
        with pytest.raises(self._ssrf_error(), match="scheme"):
            self._validate("example.com:8080/mcp")

    def test_accepts_http(self):
        """HTTP URLs to public hosts should be accepted."""
        # Use a well-known public IP (Google DNS)
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (2, 1, 6, "", ("8.8.8.8", 443)),
            ]
            result = self._validate("http://public.example.com/mcp")
            assert result == "http://public.example.com/mcp"

    def test_accepts_https(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (2, 1, 6, "", ("8.8.8.8", 443)),
            ]
            result = self._validate("https://public.example.com/mcp")
            assert result == "https://public.example.com/mcp"

    # ── Missing hostname ─────────────────────────────────────────

    def test_rejects_missing_hostname(self):
        with pytest.raises(self._ssrf_error(), match="hostname"):
            self._validate("http://")

    # ── Private IP ranges (RFC 1918) ─────────────────────────────

    def test_rejects_10_0_0_0_range(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("10.0.0.1", 8080))]
            with pytest.raises(self._ssrf_error(), match="blocked"):
                self._validate("http://internal-server.example.com:8080/mcp")

    def test_rejects_172_16_range(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("172.16.5.10", 443))]
            with pytest.raises(self._ssrf_error(), match="blocked"):
                self._validate("http://internal.example.com/mcp")

    def test_rejects_192_168_range(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("192.168.1.100", 443))]
            with pytest.raises(self._ssrf_error(), match="blocked"):
                self._validate("http://home-server.example.com/mcp")

    # ── Loopback ─────────────────────────────────────────────────

    def test_rejects_localhost_127_0_0_1(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("127.0.0.1", 8080))]
            with pytest.raises(self._ssrf_error(), match="blocked"):
                self._validate("http://localhost:8080/mcp")

    def test_rejects_127_x_loopback(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("127.0.0.2", 443))]
            with pytest.raises(self._ssrf_error(), match="blocked"):
                self._validate("http://trick.example.com/mcp")

    def test_rejects_ipv6_loopback(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(10, 1, 6, "", ("::1", 443, 0, 0))]
            with pytest.raises(self._ssrf_error(), match="blocked"):
                self._validate("http://ipv6-trick.example.com/mcp")

    # ── Link-local / metadata ────────────────────────────────────

    def test_rejects_link_local_169_254(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("169.254.1.1", 443))]
            with pytest.raises(self._ssrf_error(), match="blocked"):
                self._validate("http://link-local.example.com/mcp")

    def test_rejects_metadata_endpoint(self):
        """Cloud metadata endpoint 169.254.169.254 must be blocked."""
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("169.254.169.254", 80))]
            with pytest.raises(self._ssrf_error(), match="blocked"):
                self._validate("http://metadata.example.com/latest/meta-data/")

    def test_rejects_metadata_direct_ip(self):
        """Direct IP access to metadata endpoint must be blocked."""
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("169.254.169.254", 80))]
            with pytest.raises(self._ssrf_error(), match="blocked"):
                self._validate("http://169.254.169.254/latest/meta-data/")

    # ── IPv6 link-local ──────────────────────────────────────────

    def test_rejects_ipv6_link_local(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(10, 1, 6, "", ("fe80::1", 443, 0, 0))]
            with pytest.raises(self._ssrf_error(), match="blocked"):
                self._validate("http://ipv6-link-local.example.com/mcp")

    # ── DNS resolution failure ───────────────────────────────────

    def test_rejects_unresolvable_hostname(self):
        import socket

        with patch("socket.getaddrinfo", side_effect=socket.gaierror("Name not found")):
            with pytest.raises(self._ssrf_error(), match="resolve"):
                self._validate("http://nonexistent.invalid/mcp")

    # ── Multiple DNS results ─────────────────────────────────────

    def test_rejects_if_any_address_is_private(self):
        """If DNS returns both public and private IPs, reject."""
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (2, 1, 6, "", ("8.8.8.8", 443)),
                (2, 1, 6, "", ("10.0.0.1", 443)),  # Private!
            ]
            with pytest.raises(self._ssrf_error(), match="blocked"):
                self._validate("http://dual-homed.example.com/mcp")

    # ── Allowlist ────────────────────────────────────────────────

    def test_allowlist_permits_private_ip(self):
        """Hosts in LUCENT_MCP_URL_ALLOWLIST bypass SSRF checks."""
        with patch.dict(os.environ, {"LUCENT_MCP_URL_ALLOWLIST": "internal-mcp.corp.local"}):
            # Don't even need DNS mock — allowlist skips resolution check
            result = self._validate("http://internal-mcp.corp.local:8080/mcp")
            assert result == "http://internal-mcp.corp.local:8080/mcp"

    def test_allowlist_is_case_insensitive(self):
        with patch.dict(os.environ, {"LUCENT_MCP_URL_ALLOWLIST": "Internal-MCP.Corp.Local"}):
            result = self._validate("http://internal-mcp.corp.local:8080/mcp")
            assert result == "http://internal-mcp.corp.local:8080/mcp"

    def test_allowlist_supports_multiple_hosts(self):
        with patch.dict(os.environ, {"LUCENT_MCP_URL_ALLOWLIST": "host1.local, host2.local, 10.0.1.5"}):
            result = self._validate("http://host2.local:9000/mcp")
            assert result == "http://host2.local:9000/mcp"

    def test_non_allowlisted_host_still_blocked(self):
        """Allowlisting one host doesn't open everything."""
        with patch.dict(os.environ, {"LUCENT_MCP_URL_ALLOWLIST": "allowed.local"}):
            with patch("socket.getaddrinfo") as mock_dns:
                mock_dns.return_value = [(2, 1, 6, "", ("10.0.0.1", 443))]
                with pytest.raises(self._ssrf_error(), match="blocked"):
                    self._validate("http://not-allowed.local/mcp")

    # ── Public IPs pass ──────────────────────────────────────────

    def test_accepts_public_ipv4(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("203.0.113.10", 443))]
            result = self._validate("https://public-mcp.example.com/mcp")
            assert result == "https://public-mcp.example.com/mcp"

    def test_accepts_public_ipv6(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(10, 1, 6, "", ("2001:db8::1", 443, 0, 0))]
            result = self._validate("https://ipv6-mcp.example.com/mcp")
            assert result == "https://ipv6-mcp.example.com/mcp"


# ---------------------------------------------------------------------------
# Integration: MCPToolBridge constructor validates URL
# ---------------------------------------------------------------------------

class TestMCPBridgeSSRF:
    """Ensure MCPToolBridge validates URL on construction."""

    def test_bridge_rejects_private_url(self):
        from lucent.llm.mcp_bridge import MCPToolBridge
        from lucent.url_validation import SSRFError

        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("192.168.1.1", 443))]
            with pytest.raises(SSRFError, match="blocked"):
                MCPToolBridge(mcp_url="http://evil.example.com/mcp")

    def test_bridge_skip_validation_flag(self):
        """Internal callers can skip SSRF validation."""
        from lucent.llm.mcp_bridge import MCPToolBridge

        # Should not raise even for localhost — skip_url_validation=True
        bridge = MCPToolBridge(
            mcp_url="http://localhost:8766/mcp",
            skip_url_validation=True,
        )
        assert bridge._mcp_url == "http://localhost:8766/mcp"


# ---------------------------------------------------------------------------
# Integration: MCP discovery validates URL
# ---------------------------------------------------------------------------

class TestMCPDiscoverySSRF:
    """Ensure MCP discovery refuses to contact private addresses."""

    @pytest.mark.asyncio
    async def test_discover_http_rejects_private_ip(self):
        from lucent.services.mcp_discovery import MCPDiscoveryError, _discover_http

        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("10.0.0.5", 443))]
            with pytest.raises(MCPDiscoveryError, match="blocked"):
                await _discover_http({"url": "http://internal.example.com/mcp"})

    @pytest.mark.asyncio
    async def test_discover_http_rejects_metadata(self):
        from lucent.services.mcp_discovery import MCPDiscoveryError, _discover_http

        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("169.254.169.254", 80))]
            with pytest.raises(MCPDiscoveryError, match="blocked"):
                await _discover_http({"url": "http://169.254.169.254/latest/"})
