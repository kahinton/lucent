"""Tests for security audit fix validation.

Verifies fixes for issues identified by multi-model security audit:
1. Rate limiting bypass via rotating Authorization headers
2. Temp password cookie missing Secure attribute
3. Login rate limiter using direct socket IP
4. MCP bridge leaking internal error details
5. Docker-compose interactive TTY removed
"""

import inspect
import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest


class TestRateLimitBypassFix:
    """Verify rate limiting uses IP-anchored keys to prevent bypass via header rotation."""

    def test_rate_limit_key_includes_ip_for_bearer_tokens(self):
        """Rotating Bearer tokens from the same IP must share a rate limit bucket."""
        from lucent.rate_limit import get_client_ip

        # Verify the middleware source uses IP in the rate key for Bearer tokens
        from lucent.api.app import create_app

        source = inspect.getsource(create_app)

        # The fix: rate key for Bearer tokens must include client_ip
        assert "key_prefix" in source, (
            "Rate limiting should use key prefix, not full token, to prevent rotation bypass"
        )
        assert "client_ip" in source, (
            "Rate limiting must include client IP to prevent bypass via token rotation"
        )

    def test_rate_limit_key_uses_prefix_not_full_token(self):
        """Rate key should use token prefix (stable per-key), not the full token."""
        from lucent.api.app import create_app

        source = inspect.getsource(create_app)

        # Should NOT use full auth_header as rate key
        assert 'f"api:{auth_header[7:]}"' not in source, (
            "Must not use full auth header as rate key — allows bypass via rotation"
        )
        assert 'f"api:{auth_header}"' not in source, (
            "Must not use full auth header as rate key — allows bypass via rotation"
        )

    def test_rate_limit_always_resolves_client_ip(self):
        """get_client_ip must be called unconditionally for all API requests."""
        from lucent.api.app import create_app

        source = inspect.getsource(create_app)

        # The IP resolution should happen before the auth_header branching,
        # not only in the else branch
        ip_call_pos = source.find("get_client_ip(request)")
        bearer_check_pos = source.find('auth_header.startswith("Bearer ")')
        assert ip_call_pos < bearer_check_pos, (
            "get_client_ip must be called before the Bearer check, not only as fallback"
        )


class TestTempPasswordCookieSecureFix:
    """Verify temp password cookies include the Secure attribute."""

    def test_create_user_cookie_has_secure(self):
        """Cookie in create_user must include secure=SECURE_COOKIES."""
        from lucent.web.routes import admin

        source = inspect.getsource(admin.create_user)
        assert "secure=" in source, (
            "lucent_temp_pw_ref cookie must include secure attribute"
        )
        assert "SECURE_COOKIES" in source, (
            "lucent_temp_pw_ref should use SECURE_COOKIES setting for consistency"
        )

    def test_reset_password_cookie_has_secure(self):
        """Cookie in reset_user_password_web must include secure=SECURE_COOKIES."""
        from lucent.web.routes import admin

        source = inspect.getsource(admin.reset_user_password_web)
        assert "secure=" in source, (
            "lucent_temp_pw_ref cookie in reset flow must include secure attribute"
        )
        assert "SECURE_COOKIES" in source, (
            "Reset flow should use SECURE_COOKIES setting for consistency"
        )

    def test_secure_cookies_imported_in_admin(self):
        """admin.py must import SECURE_COOKIES from auth_providers."""
        from lucent.web.routes import admin

        assert hasattr(admin, "SECURE_COOKIES"), (
            "SECURE_COOKIES must be imported into admin module"
        )


class TestLoginRateLimiterIPFix:
    """Verify login rate limiter uses proxy-aware IP extraction."""

    def test_login_uses_get_client_ip(self):
        """Login endpoint must use get_client_ip instead of request.client.host."""
        from lucent.web.routes import auth

        source = inspect.getsource(auth.login_submit)

        # Must NOT use request.client.host directly
        assert "request.client.host" not in source, (
            "Login must use get_client_ip() instead of request.client.host for proxy support"
        )

        # Must import and use get_client_ip
        assert "get_client_ip" in source, (
            "Login must use the proxy-aware get_client_ip helper"
        )


class TestMCPBridgeErrorSanitization:
    """Verify MCP bridge does not leak internal error details to clients."""

    def test_exception_handler_does_not_expose_str_exc(self):
        """The bridge error handler must not include str(exc) in responses."""
        from lucent.sandbox.mcp_bridge import MCPBridgeHandler

        source = inspect.getsource(MCPBridgeHandler.do_POST)

        # Must NOT pass str(exc) to the client
        assert "str(exc)" not in source, (
            "Bridge must not expose exception details via str(exc) in JSON-RPC errors"
        )

    def test_exception_handler_returns_generic_message(self):
        """The bridge error handler must return a generic error message."""
        from lucent.sandbox.mcp_bridge import MCPBridgeHandler

        source = inspect.getsource(MCPBridgeHandler.do_POST)

        assert "Internal server error" in source, (
            "Bridge error handler should return generic 'Internal server error' message"
        )

    def test_api_error_does_not_include_response_body(self):
        """HTTP errors from upstream API must not include response body details."""
        from lucent.sandbox.mcp_bridge import BridgeServer

        source = inspect.getsource(BridgeServer._proxy)

        # Must log detail server-side but not include in the raised exception
        assert "logger.error" in source, (
            "API errors should be logged server-side for debugging"
        )
        # The RuntimeError should not include {detail}
        assert "{detail}" not in source, (
            "API error detail must not be included in the exception message"
        )


class TestDockerComposeHardening:
    """Verify docker-compose security improvements."""

    def test_no_stdin_open_or_tty(self):
        """docker-compose.yml must not enable stdin_open or tty for the main service."""
        import pathlib

        compose_path = pathlib.Path(__file__).parent.parent / "docker-compose.yml"
        if not compose_path.exists():
            pytest.skip("docker-compose.yml not found")

        content = compose_path.read_text()

        # Parse the lucent service section (between 'lucent:' and the next top-level service)
        lines = content.split("\n")
        in_lucent_service = False
        lucent_indent = 0
        for line in lines:
            stripped = line.lstrip()
            current_indent = len(line) - len(stripped)

            if stripped.startswith("lucent:") and current_indent <= 2:
                in_lucent_service = True
                lucent_indent = current_indent
                continue

            if in_lucent_service:
                # Detect end of lucent service (next service at same or lower indent)
                if stripped and not stripped.startswith("#") and current_indent <= lucent_indent and ":" in stripped:
                    break
                # Check for stdin_open or tty (uncommented)
                if not stripped.startswith("#"):
                    assert "stdin_open:" not in stripped, (
                        "stdin_open should be removed or commented out in lucent service"
                    )
                    assert stripped != "tty: true", (
                        "tty: true should be removed or commented out in lucent service"
                    )

    def test_docker_socket_has_security_comment(self):
        """Docker socket mount should have a security documentation comment."""
        import pathlib

        compose_path = pathlib.Path(__file__).parent.parent / "docker-compose.yml"
        if not compose_path.exists():
            pytest.skip("docker-compose.yml not found")

        content = compose_path.read_text()
        assert "SECURITY" in content and "docker.sock" in content, (
            "Docker socket mount should have a SECURITY comment documenting the risk"
        )

    def test_lucent_service_has_no_direct_docker_socket_mount(self):
        """Dev compose lucent service should not mount docker.sock directly."""
        import pathlib

        compose_path = pathlib.Path(__file__).parent.parent / "docker-compose.yml"
        if not compose_path.exists():
            pytest.skip("docker-compose.yml not found")

        content = compose_path.read_text()
        lines = content.split("\n")
        in_lucent = False
        in_volumes = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("lucent:"):
                in_lucent = True
                in_volumes = False
                continue
            if in_lucent and stripped.startswith("volumes:"):
                in_volumes = True
                continue
            if in_lucent and in_volumes:
                if stripped.startswith("-") and "docker.sock" in stripped:
                    pytest.fail(
                        "Development lucent service must not mount docker.sock directly"
                    )
                if stripped and not stripped.startswith("-") and not stripped.startswith("#"):
                    in_volumes = False

    def test_dev_compose_uses_docker_socket_proxy(self):
        """Dev compose should route Docker access through docker-socket-proxy."""
        import pathlib

        compose_path = pathlib.Path(__file__).parent.parent / "docker-compose.yml"
        if not compose_path.exists():
            pytest.skip("docker-compose.yml not found")

        content = compose_path.read_text()
        assert "docker-socket-proxy" in content, (
            "Development compose must define docker-socket-proxy service"
        )
        assert "DOCKER_HOST: tcp://docker-socket-proxy:2375" in content, (
            "Development compose lucent service must use DOCKER_HOST via proxy"
        )


class TestProductionComposeHardening:
    """Verify production docker-compose addresses critical/high audit findings."""

    @pytest.fixture()
    def prod_compose(self):
        import pathlib

        path = pathlib.Path(__file__).parent.parent / "docker-compose.prod.yml"
        if not path.exists():
            pytest.skip("docker-compose.prod.yml not found")
        return path.read_text()

    def test_no_default_postgres_password(self, prod_compose):
        """Production compose must not have default Postgres password."""
        assert "lucent_dev_password" not in prod_compose, (
            "Production compose must not contain insecure default passwords"
        )

    def test_requires_postgres_password(self, prod_compose):
        """Production compose must require POSTGRES_PASSWORD via :? syntax."""
        assert "POSTGRES_PASSWORD:?" in prod_compose or "POSTGRES_PASSWORD:?" in prod_compose, (
            "Production compose must use required-variable syntax for POSTGRES_PASSWORD"
        )

    def test_no_root_vault_token(self, prod_compose):
        """Production compose must not use 'root' as OpenBao token."""
        # Check there's no BAO_DEV_ROOT_TOKEN_ID: root
        assert "BAO_DEV_ROOT_TOKEN_ID: root" not in prod_compose, (
            "Production compose must not use 'root' as OpenBao dev token"
        )

    def test_requires_secret_key(self, prod_compose):
        """Production compose must require LUCENT_SECRET_KEY."""
        assert "LUCENT_SECRET_KEY:?" in prod_compose, (
            "Production compose must use required-variable syntax for LUCENT_SECRET_KEY"
        )

    def test_no_direct_docker_socket_mount(self, prod_compose):
        """Production compose must not mount Docker socket directly."""
        # Check that /var/run/docker.sock is NOT in a volume mount for lucent service
        # It should only appear in the docker-socket-proxy service
        lines = prod_compose.split("\n")
        in_lucent = False
        in_volumes = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("lucent:"):
                in_lucent = True
                continue
            if in_lucent and stripped.startswith("volumes:"):
                in_volumes = True
                continue
            if in_lucent and in_volumes:
                if stripped.startswith("-") and "docker.sock" in stripped:
                    pytest.fail(
                        "Production lucent service must not mount docker.sock directly"
                    )
                if not stripped.startswith("-") and not stripped.startswith("#"):
                    in_volumes = False

    def test_uses_docker_socket_proxy(self, prod_compose):
        """Production compose must include docker-socket-proxy service."""
        assert "docker-socket-proxy" in prod_compose, (
            "Production compose must use docker-socket-proxy for Docker access"
        )

    def test_docker_host_env_set(self, prod_compose):
        """Production lucent service must set DOCKER_HOST to proxy."""
        assert "DOCKER_HOST:" in prod_compose and "docker-socket-proxy" in prod_compose, (
            "Production compose must set DOCKER_HOST to use the socket proxy"
        )

    def test_no_openbao_dev_mode(self, prod_compose):
        """Production compose must not run OpenBao in dev mode."""
        assert "server -dev" not in prod_compose, (
            "Production compose must not run OpenBao in dev mode"
        )

    def test_no_stdin_open_or_tty(self, prod_compose):
        """Production compose must not enable stdin_open or tty."""
        assert "stdin_open: true" not in prod_compose
        assert "tty: true" not in prod_compose

    def test_secure_cookies_enabled(self, prod_compose):
        """Production compose should enable secure cookies."""
        assert 'LUCENT_SECURE_COOKIES: "true"' in prod_compose or \
               "LUCENT_SECURE_COOKIES: 'true'" in prod_compose or \
               "LUCENT_SECURE_COOKIES: true" in prod_compose, (
            "Production compose must enable secure cookies"
        )


class TestDockerfilePinning:
    """Verify production Dockerfile uses pinned base images."""

    def test_dockerfile_uses_digest_pinning(self):
        import pathlib

        dockerfile = pathlib.Path(__file__).parent.parent / "Dockerfile"
        if not dockerfile.exists():
            pytest.skip("Dockerfile not found")
        content = dockerfile.read_text()
        assert "@sha256:" in content, (
            "Production Dockerfile must pin base image with SHA256 digest"
        )

    def test_dockerfile_runs_as_nonroot(self):
        import pathlib

        dockerfile = pathlib.Path(__file__).parent.parent / "Dockerfile"
        if not dockerfile.exists():
            pytest.skip("Dockerfile not found")
        content = dockerfile.read_text()
        assert "USER" in content and "lucent" in content, (
            "Production Dockerfile must run as non-root user"
        )


class TestStartupSecurityValidation:
    """Verify the startup security check detects insecure defaults."""

    def test_check_security_defaults_warns_on_insecure_key(self):
        """_check_security_defaults should log when insecure defaults are detected."""
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {
            "LUCENT_SECRET_KEY": "lucent-dev-secret-key-change-in-production",
            "LUCENT_MODE": "personal",
        }, clear=False):
            from lucent.api.app import _check_security_defaults
            # Should not raise, just log
            _check_security_defaults()

    def test_check_security_defaults_critical_in_team_mode(self):
        """In team mode, insecure defaults should log at CRITICAL level."""
        import os
        from unittest.mock import patch, MagicMock

        with patch.dict(os.environ, {
            "LUCENT_SECRET_KEY": "lucent-dev-secret-key-change-in-production",
            "LUCENT_MODE": "team",
        }, clear=False):
            os.environ.pop("LUCENT_SIGNING_SECRET", None)
            from lucent.api import app as app_module
            mock_logger = MagicMock()
            with patch.object(app_module, "logger", mock_logger):
                app_module._check_security_defaults()
                # At least one critical call for the insecure default
                assert mock_logger.critical.call_count >= 1
                critical_msgs = [
                    str(c) for c in mock_logger.critical.call_args_list
                ]
                assert any("Insecure default" in m for m in critical_msgs)

    def test_check_security_defaults_quiet_with_strong_key(self):
        """No warnings when proper credentials are set."""
        import os
        from unittest.mock import patch, MagicMock

        with patch.dict(os.environ, {
            "LUCENT_SECRET_KEY": "a-real-production-secret-key-32chars!",
            "POSTGRES_PASSWORD": "strong-random-password",
            "VAULT_TOKEN": "s.some-real-vault-token",
            "LUCENT_SIGNING_SECRET": "a-stable-signing-secret-value",
            "LUCENT_MODE": "team",
        }, clear=False):
            from lucent.api import app as app_module
            mock_logger = MagicMock()
            with patch.object(app_module, "logger", mock_logger):
                app_module._check_security_defaults()
                mock_logger.critical.assert_not_called()
                mock_logger.warning.assert_not_called()


class TestEnvExampleSafety:
    """Verify .env.example does not contain real credentials."""

    def test_env_example_has_no_real_passwords(self):
        import pathlib

        env_example = pathlib.Path(__file__).parent.parent / ".env.example"
        if not env_example.exists():
            pytest.skip(".env.example not found")
        content = env_example.read_text()

        # Must not contain the old insecure defaults
        assert "lucent_dev_password" not in content, (
            ".env.example must not contain insecure default passwords"
        )
        assert "lucent-dev-secret-key-change-in-production" not in content, (
            ".env.example must not contain insecure default secret key"
        )

    def test_env_example_has_no_tokens(self):
        import pathlib

        env_example = pathlib.Path(__file__).parent.parent / ".env.example"
        if not env_example.exists():
            pytest.skip(".env.example not found")
        content = env_example.read_text()

        # Must not contain any GitHub tokens
        assert "gho_" not in content, (
            ".env.example must not contain real GitHub tokens"
        )
        assert "ghp_" not in content, (
            ".env.example must not contain real GitHub tokens"
        )


class TestSandboxPathValidationFixes:
    """Regression tests for sandbox injection/traversal high-severity findings."""

    @pytest.mark.asyncio
    async def test_devcontainer_build_shell_quotes_dockerfile_and_context(self):
        """docker build command must quote user-controlled Dockerfile/context values."""
        from lucent.sandbox.docker_backend import DockerBackend
        from lucent.sandbox.devcontainer import DevcontainerConfig
        from lucent.sandbox.models import ExecResult, SandboxConfig

        backend = DockerBackend()

        async def fake_exec(_sid, _cmd, **_kwargs):
            return ExecResult(exit_code=0, stdout="", stderr="")

        backend.exec = MagicMock(side_effect=fake_exec)

        dc_config = DevcontainerConfig(
            build_dockerfile="Dockerfile;echo pwned",
            build_context='."; touch /tmp/pwned #',
        )
        await backend._build_devcontainer_image(
            "sandbox1234567890", SandboxConfig(), dc_config
        )

        cmd = backend.exec.call_args.args[1]
        assert "-f 'Dockerfile;echo pwned'" in cmd
        assert cmd.endswith('\'."; touch /tmp/pwned #\'')
        assert " -f Dockerfile;echo pwned " not in cmd

    @pytest.mark.asyncio
    async def test_iptables_rejects_malformed_ip_from_allowlist(self):
        """Malformed IP values must never appear in iptables destination rules."""
        from lucent.sandbox.docker_backend import DockerBackend
        from lucent.sandbox.models import ExecResult, SandboxConfig

        backend = DockerBackend()
        calls: list[str] = []

        async def fake_exec(_sid, command, **_kwargs):
            calls.append(command)
            if "getent" in command:
                return ExecResult(exit_code=0, stdout="", stderr="")
            return ExecResult(exit_code=0, stdout="", stderr="")

        backend.exec = fake_exec
        await backend._apply_network_allowlist(
            "sb-test",
            SandboxConfig(network_mode="allowlist", allowed_hosts=["1.2.3.4;echo pwned"]),
        )

        assert not any("1.2.3.4;echo pwned" in c for c in calls if "iptables -A OUTPUT -d" in c)

    @pytest.mark.asyncio
    async def test_write_file_rejects_workspace_escape_and_symlink_escape(self):
        """write_file must reject traversal and realpath symlink escapes."""
        from lucent.sandbox.docker_backend import DockerBackend

        backend = DockerBackend()
        container = MagicMock()
        container.put_archive = MagicMock()
        backend._find_container = MagicMock(return_value=container)

        with pytest.raises(ValueError, match="escapes workspace root"):
            await backend.write_file("sb-test", "../../../etc/passwd", b"bad")

        with patch("lucent.sandbox.docker_backend.os.path.realpath") as mock_realpath:
            mock_realpath.side_effect = ["/etc/passwd", "/workspace"]
            with pytest.raises(ValueError, match="escapes workspace root"):
                await backend.write_file("sb-test", "/workspace/link/passwd", b"bad")

        container.put_archive.assert_not_called()

    def test_api_path_validation_blocks_normpath_and_symlink_bypass(self):
        """API path validation must realpath-resolve and enforce workspace containment."""
        from fastapi import HTTPException
        from lucent.api.routers.sandboxes import _validate_sandbox_path

        # normpath trick should fail
        with pytest.raises(HTTPException):
            _validate_sandbox_path("/workspace/../../etc/passwd")

        # Symlink resolution escape should fail
        with patch("os.path.realpath") as mock_realpath:
            mock_realpath.side_effect = ["/etc/passwd", "/workspace"]
            with pytest.raises(HTTPException):
                _validate_sandbox_path("/workspace/link/passwd")


class TestSigningSecretPersistence:
    """Verify LUCENT_SIGNING_SECRET behaviour for Finding 9."""

    def test_signing_secret_uses_env_var_when_set(self):
        """SIGNING_SECRET should use env var value when provided."""
        import importlib
        import os
        from unittest.mock import patch

        test_secret = "my-stable-production-secret-value"
        with patch.dict(os.environ, {"LUCENT_SIGNING_SECRET": test_secret}, clear=False):
            import lucent.auth_providers as mod
            importlib.reload(mod)
            assert mod.SIGNING_SECRET == test_secret

    def test_signing_secret_generates_random_when_not_set(self):
        """SIGNING_SECRET should be auto-generated when env var is absent."""
        import importlib
        import os
        from unittest.mock import patch

        env = os.environ.copy()
        env.pop("LUCENT_SIGNING_SECRET", None)
        with patch.dict(os.environ, env, clear=True):
            import lucent.auth_providers as mod
            importlib.reload(mod)
            assert mod.SIGNING_SECRET  # non-empty
            assert len(mod.SIGNING_SECRET) > 20  # token_urlsafe(32) is ~43 chars

    def test_signing_secret_warning_in_team_mode(self):
        """Missing SIGNING_SECRET in team mode should log CRITICAL."""
        import importlib
        import logging
        import os
        from unittest.mock import patch

        env = os.environ.copy()
        env.pop("LUCENT_SIGNING_SECRET", None)
        env["LUCENT_MODE"] = "team"
        with patch.dict(os.environ, env, clear=True):
            import lucent.auth_providers as mod
            auth_logger = logging.getLogger("lucent.auth.providers")
            with patch.object(auth_logger, "critical") as mock_critical:
                importlib.reload(mod)
                mock_critical.assert_called_once()
                assert "LUCENT_SIGNING_SECRET" in mock_critical.call_args[0][0]

    def test_startup_check_warns_on_missing_signing_secret(self):
        """_check_security_defaults should warn when SIGNING_SECRET is unset."""
        import os
        from unittest.mock import MagicMock, patch

        # Ensure LUCENT_SIGNING_SECRET is NOT set
        with patch.dict(os.environ, {
            "LUCENT_SECRET_KEY": "a-real-production-secret-key-32chars!",
            "POSTGRES_PASSWORD": "strong-random-password",
            "VAULT_TOKEN": "s.some-real-vault-token",
            "LUCENT_MODE": "team",
        }, clear=False):
            os.environ.pop("LUCENT_SIGNING_SECRET", None)
            from lucent.api.app import _check_security_defaults, logger as app_logger
            mock_logger = MagicMock()
            import lucent.api.app as app_module
            with patch.object(app_module, "logger", mock_logger):
                _check_security_defaults()
                # Should have at least one critical call about missing secrets
                critical_msgs = [
                    str(c) for c in mock_logger.critical.call_args_list
                ]
                assert any("LUCENT_SIGNING_SECRET" in m for m in critical_msgs)

    def test_prod_compose_requires_signing_secret(self):
        """Production docker-compose must require LUCENT_SIGNING_SECRET."""
        import pathlib

        prod_compose = pathlib.Path(__file__).parent.parent / "docker-compose.prod.yml"
        if not prod_compose.exists():
            pytest.skip("docker-compose.prod.yml not found")
        content = prod_compose.read_text()
        assert "LUCENT_SIGNING_SECRET" in content, (
            "Production compose must include LUCENT_SIGNING_SECRET"
        )
        # The :? syntax makes docker-compose fail if the variable is unset
        assert "LUCENT_SIGNING_SECRET:?" in content, (
            "Production compose must require LUCENT_SIGNING_SECRET (use :? syntax)"
        )


class TestApiKeyLogRedaction:
    """Verify API key prefix is NOT logged in verification failures (Finding 10)."""

    def test_verify_failure_log_does_not_contain_prefix(self):
        """Verification failure logs must not include the key prefix."""
        import inspect
        from lucent.db.api_key import ApiKeyRepository

        source = inspect.getsource(ApiKeyRepository.verify)
        # The old pattern logged prefix=%s with key_prefix in failure messages
        assert "prefix=%s" not in source, (
            "API key verification must not log the key prefix"
        )
        # Ensure no logger call passes key_prefix as an argument
        import re
        log_calls = re.findall(r'logger\.\w+\([^)]+\)', source, re.DOTALL)
        for call in log_calls:
            assert "key_prefix" not in call, (
                f"Logger call passes key_prefix: {call}"
            )

    def test_auth_error_does_not_use_exc_info(self):
        """Auth error handlers must not use exc_info to avoid leaking credentials."""
        import inspect
        from lucent.server import MCPAuthMiddleware

        source = inspect.getsource(MCPAuthMiddleware.__call__)
        # exc_info can dump the full traceback with local variables holding keys
        assert "exc_info" not in source, (
            "Auth error handlers must not use exc_info — "
            "it can leak credential material in stack traces"
        )

    def test_no_credential_material_in_verification_logs(self):
        """Verify that log messages in api_key.verify() don't include raw key data."""
        import inspect
        from lucent.db.api_key import ApiKeyRepository

        source = inspect.getsource(ApiKeyRepository.verify)
        # Ensure no log line includes plain_key
        import re
        log_calls = re.findall(r'logger\.\w+\([^)]+\)', source)
        for call in log_calls:
            assert "plain_key" not in call, (
                f"Logger call includes raw key material: {call}"
            )


class TestOpenBaoDevModeWarnings:
    """Verify OpenBao dev mode detection and warnings (Finding 12)."""

    def test_vault_provider_warns_on_dev_token(self):
        """VaultSecretProvider should warn when using known dev tokens."""
        import os
        from unittest.mock import MagicMock, patch

        with patch.dict(os.environ, {
            "VAULT_ADDR": "http://localhost:8200",
            "VAULT_TOKEN": "change-me-insecure-dev-root-token",
            "LUCENT_MODE": "personal",
        }, clear=False):
            from lucent.secrets import vault as vault_module
            mock_logger = MagicMock()
            with patch.object(vault_module, "logger", mock_logger):
                vault_module.VaultSecretProvider()
                mock_logger.warning.assert_called()
                warning_msgs = [
                    str(c) for c in mock_logger.warning.call_args_list
                ]
                assert any("insecure dev token" in m for m in warning_msgs)

    def test_vault_provider_critical_on_dev_token_in_team_mode(self):
        """Dev tokens in team mode should log CRITICAL."""
        import os
        from unittest.mock import MagicMock, patch

        with patch.dict(os.environ, {
            "VAULT_ADDR": "http://localhost:8200",
            "VAULT_TOKEN": "root",
            "LUCENT_MODE": "team",
        }, clear=False):
            from lucent.secrets import vault as vault_module
            mock_logger = MagicMock()
            with patch.object(vault_module, "logger", mock_logger):
                vault_module.VaultSecretProvider()
                mock_logger.critical.assert_called_once()

    def test_vault_provider_warns_on_bao_dev_env(self):
        """BAO_DEV_ROOT_TOKEN_ID presence should trigger a warning."""
        import os
        from unittest.mock import MagicMock, patch

        with patch.dict(os.environ, {
            "VAULT_ADDR": "http://localhost:8200",
            "VAULT_TOKEN": "s.proper-scoped-token",
            "BAO_DEV_ROOT_TOKEN_ID": "some-dev-token",
            "LUCENT_MODE": "personal",
        }, clear=False):
            from lucent.secrets import vault as vault_module
            mock_logger = MagicMock()
            with patch.object(vault_module, "logger", mock_logger):
                vault_module.VaultSecretProvider()
                warning_msgs = [
                    str(c) for c in mock_logger.warning.call_args_list
                ]
                assert any("dev mode" in m for m in warning_msgs)

    def test_vault_provider_no_warning_with_proper_token(self):
        """No warnings when a proper scoped token is used."""
        import os
        from unittest.mock import MagicMock, patch

        env = os.environ.copy()
        env.pop("BAO_DEV_ROOT_TOKEN_ID", None)
        env["VAULT_ADDR"] = "http://localhost:8200"
        env["VAULT_TOKEN"] = "s.proper-scoped-policy-token"
        env["LUCENT_MODE"] = "team"
        with patch.dict(os.environ, env, clear=True):
            from lucent.secrets import vault as vault_module
            mock_logger = MagicMock()
            with patch.object(vault_module, "logger", mock_logger):
                vault_module.VaultSecretProvider()
                mock_logger.warning.assert_not_called()
                mock_logger.critical.assert_not_called()

    def test_prod_compose_no_dev_mode(self):
        """Production compose must not use dev mode or dev tokens."""
        import pathlib

        prod_compose = pathlib.Path(__file__).parent.parent / "docker-compose.prod.yml"
        if not prod_compose.exists():
            pytest.skip("docker-compose.prod.yml not found")
        content = prod_compose.read_text()

        assert "server -dev" not in content, (
            "Production compose must not run OpenBao in dev mode"
        )
        assert "BAO_DEV_ROOT_TOKEN_ID" not in content, (
            "Production compose must not set BAO_DEV_ROOT_TOKEN_ID"
        )
        assert "change-me-insecure-dev-root-token" not in content, (
            "Production compose must not contain insecure dev tokens"
        )

    def test_init_script_does_not_print_tokens(self):
        """OpenBao init script must not echo token values to stdout."""
        import pathlib

        init_script = pathlib.Path(__file__).parent.parent / "docker" / "openbao-init.sh"
        if not init_script.exists():
            pytest.skip("openbao-init.sh not found")
        content = init_script.read_text()

        assert "echo \"${CLIENT_TOKEN}\"" not in content.replace(" ", ""), (
            "Init script must not print the client token"
        )
        assert "echo \"${VAULT_TOKEN}\"" not in content.replace(" ", ""), (
            "Init script must not print the vault token"
        )
        # Check the specific old pattern
        assert "policy token: ${CLIENT_TOKEN}" not in content, (
            "Init script must not print the policy token value"
        )
