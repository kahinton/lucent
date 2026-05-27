"""Tests for CSP nonce-based policy (Finding 14) and secret provider registry (Finding 15)."""

from __future__ import annotations

import os
import re
from unittest.mock import patch

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Finding 14: CSP nonce tests
# ---------------------------------------------------------------------------


class TestCSPNonce:
    """Verify CSP headers use nonce-based policy instead of unsafe-inline."""

    @pytest_asyncio.fixture
    async def client(self, db_pool):
        """Create a test HTTP client."""
        import httpx
        from httpx import ASGITransport

        from lucent.api.app import create_app

        app = create_app()
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_csp_header_contains_nonce(self, client):
        """CSP header should contain a nonce directive, not unsafe-inline for scripts."""
        resp = await client.get("/api/health")
        csp = resp.headers.get("Content-Security-Policy", "")

        # Must contain a nonce
        assert "'nonce-" in csp, f"CSP missing nonce: {csp}"

        # Extract nonce value
        nonce_match = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp)
        assert nonce_match, f"Could not extract nonce from CSP: {csp}"
        nonce = nonce_match.group(1)
        assert len(nonce) >= 16, "Nonce too short — must be cryptographically random"

    @pytest.mark.asyncio
    async def test_csp_no_unsafe_inline_in_script_src(self, client):
        """script-src must NOT contain 'unsafe-inline' — nonce replaces it."""
        resp = await client.get("/api/health")
        csp = resp.headers.get("Content-Security-Policy", "")

        # Parse script-src directive
        script_src_match = re.search(r"script-src\s+([^;]+)", csp)
        assert script_src_match, f"No script-src in CSP: {csp}"
        script_src = script_src_match.group(1)

        assert "'unsafe-inline'" not in script_src, (
            f"script-src still contains 'unsafe-inline': {script_src}"
        )

    @pytest.mark.asyncio
    async def test_csp_nonce_is_unique_per_request(self, client):
        """Each request must get a different nonce."""
        resp1 = await client.get("/api/health")
        resp2 = await client.get("/api/health")

        csp1 = resp1.headers.get("Content-Security-Policy", "")
        csp2 = resp2.headers.get("Content-Security-Policy", "")

        nonce1 = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp1).group(1)
        nonce2 = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp2).group(1)

        assert nonce1 != nonce2, "Nonces must be unique per request"

    @pytest.mark.asyncio
    async def test_csp_still_has_unsafe_eval_for_tailwind(self, client):
        """unsafe-eval is still needed for Tailwind JIT — verify it's documented."""
        resp = await client.get("/api/health")
        csp = resp.headers.get("Content-Security-Policy", "")

        script_src_match = re.search(r"script-src\s+([^;]+)", csp)
        script_src = script_src_match.group(1)

        # unsafe-eval is still required for Tailwind CSS JIT
        assert "'unsafe-eval'" in script_src

    @pytest.mark.asyncio
    async def test_csp_other_directives_intact(self, client):
        """Other security directives should still be present."""
        resp = await client.get("/api/health")
        csp = resp.headers.get("Content-Security-Policy", "")

        assert "frame-ancestors 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "form-action 'self'" in csp
        assert "default-src 'self'" in csp

    @pytest.mark.asyncio
    async def test_other_security_headers_present(self, client):
        """Non-CSP security headers must still be set."""
        resp = await client.get("/api/health")

        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert "strict-origin" in resp.headers.get("Referrer-Policy", "")


# ---------------------------------------------------------------------------
# Finding 15: Secret provider registry tests
# ---------------------------------------------------------------------------


class TestSecretProviderRegistry:
    """Verify AWS/Azure providers are properly excluded from the registry."""

    def setup_method(self):
        """Reset registry state before each test."""
        from lucent.secrets.registry import SecretRegistry
        SecretRegistry.reset()

    def test_aws_provider_rejected_at_startup(self):
        """Selecting LUCENT_SECRET_PROVIDER=aws must fail with clear message."""
        from lucent.secrets.registry import get_selected_provider_name

        with patch.dict(os.environ, {"LUCENT_SECRET_PROVIDER": "aws"}):
            with pytest.raises(ValueError, match="not yet implemented"):
                get_selected_provider_name()

    def test_azure_provider_rejected_at_startup(self):
        """Selecting LUCENT_SECRET_PROVIDER=azure must fail with clear message."""
        from lucent.secrets.registry import get_selected_provider_name

        with patch.dict(os.environ, {"LUCENT_SECRET_PROVIDER": "azure"}):
            with pytest.raises(ValueError, match="not yet implemented"):
                get_selected_provider_name()

    def test_error_message_lists_available_providers(self):
        """The error for planned providers should list what IS available."""
        from lucent.secrets.registry import get_selected_provider_name

        with patch.dict(os.environ, {"LUCENT_SECRET_PROVIDER": "aws"}):
            with pytest.raises(ValueError, match="builtin") as exc_info:
                get_selected_provider_name()
            msg = str(exc_info.value)
            assert "vault" in msg
            assert "transit" in msg
            assert "builtin" in msg

    def test_supported_providers_are_builtin_vault_transit(self):
        """Only builtin, vault, and transit should be supported."""
        from lucent.secrets.registry import _SUPPORTED_PROVIDERS

        assert _SUPPORTED_PROVIDERS == {"builtin", "vault", "transit"}

    def test_planned_providers_listed(self):
        """AWS and Azure should be in the planned set."""
        from lucent.secrets.registry import _PLANNED_PROVIDERS

        assert "aws" in _PLANNED_PROVIDERS
        assert "azure" in _PLANNED_PROVIDERS

    def test_unknown_provider_rejected(self):
        """Completely unknown providers still get a clear error."""
        from lucent.secrets.registry import get_selected_provider_name

        with patch.dict(os.environ, {"LUCENT_SECRET_PROVIDER": "nonexistent"}):
            with pytest.raises(ValueError, match="Invalid"):
                get_selected_provider_name()

    def test_builtin_provider_still_works(self):
        """Builtin provider selection should still work."""
        from lucent.secrets.registry import get_selected_provider_name

        with patch.dict(os.environ, {"LUCENT_SECRET_PROVIDER": "builtin"}):
            assert get_selected_provider_name() == "builtin"

    def test_vault_provider_still_works(self):
        from lucent.secrets.registry import get_selected_provider_name

        with patch.dict(os.environ, {"LUCENT_SECRET_PROVIDER": "vault"}):
            assert get_selected_provider_name() == "vault"

    def test_transit_provider_still_works(self):
        from lucent.secrets.registry import get_selected_provider_name

        with patch.dict(os.environ, {"LUCENT_SECRET_PROVIDER": "transit"}):
            assert get_selected_provider_name() == "transit"

    def test_auto_still_works(self):
        from lucent.secrets.registry import get_selected_provider_name

        with patch.dict(os.environ, {"LUCENT_SECRET_PROVIDER": "auto"}):
            assert get_selected_provider_name() == "auto"

    def test_empty_defaults_to_auto(self):
        from lucent.secrets.registry import get_selected_provider_name

        with patch.dict(os.environ, {}, clear=True):
            # Remove LUCENT_SECRET_PROVIDER if set
            os.environ.pop("LUCENT_SECRET_PROVIDER", None)
            assert get_selected_provider_name() == "auto"

    def test_aws_stub_methods_raise_not_implemented(self):
        """AWS provider stub methods should raise NotImplementedError."""
        from lucent.secrets.aws import AWSSecretProvider
        from lucent.secrets.base import SecretScope

        provider = AWSSecretProvider()
        scope = SecretScope(organization_id="test-org")

        with pytest.raises(NotImplementedError, match="not yet implemented"):
            import asyncio
            asyncio.get_event_loop().run_until_complete(provider.get("key", scope))

    def test_azure_stub_methods_raise_not_implemented(self):
        """Azure provider stub methods should raise NotImplementedError."""
        from lucent.secrets.azure import AzureSecretProvider
        from lucent.secrets.base import SecretScope

        provider = AzureSecretProvider()
        scope = SecretScope(organization_id="test-org")

        with pytest.raises(NotImplementedError, match="not yet implemented"):
            import asyncio
            asyncio.get_event_loop().run_until_complete(provider.get("key", scope))
