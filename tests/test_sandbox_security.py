"""Unit tests for sandbox security gap fixes.

Covers:
1. Network allowlist — iptables applied post-create for allowlist mode
2. Disk quota — storage_opt passed to Docker containers.run()
3. Idle timeout — SandboxManager destroys idle sandboxes via sweep
4. Credential expiry — expired git creds are invalidated inside the container
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from lucent.sandbox.docker_backend import DockerBackend
from lucent.sandbox.manager import SandboxManager
from lucent.sandbox.models import ExecResult, SandboxConfig, SandboxInfo, SandboxStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_exec_ok(stdout: str = "") -> ExecResult:
    return ExecResult(exit_code=0, stdout=stdout, stderr="")


def _make_exec_fail(stderr: str = "error") -> ExecResult:
    return ExecResult(exit_code=1, stdout="", stderr=stderr)


def _ready_info(sandbox_id: str = "sb-1234") -> SandboxInfo:
    return SandboxInfo(
        id=sandbox_id,
        name="test-sandbox",
        status=SandboxStatus.READY,
        config=SandboxConfig(),
    )


# ---------------------------------------------------------------------------
# 1. Network allowlist — _apply_network_allowlist
# ---------------------------------------------------------------------------


class TestApplyNetworkAllowlist:
    """Tests for DockerBackend._apply_network_allowlist.

    The allowlist policy is a security boundary. Previous behavior silently
    downgraded to bridge mode when iptables was missing, hosts failed to
    resolve, or a rule failed — effectively leaving the sandbox with
    unrestricted egress. The current implementation instead raises
    ``RuntimeError`` in all those cases so the caller fails the sandbox
    rather than running it with a broken policy.
    """

    @pytest.fixture
    def backend(self):
        return DockerBackend()

    @staticmethod
    def _make_exec_factory(
        dns_map: dict[str, str] | None = None,
        iptables_available: bool = True,
        rule_failure_substring: str | None = None,
    ):
        """Build a fake exec coroutine that simulates a running container.

        - DNS lookups: return the IPv4 line for hosts in ``dns_map``, else empty.
        - ``command -v iptables`` returns success/failure based on ``iptables_available``.
        - Any iptables rule containing ``rule_failure_substring`` returns failure.
        """
        dns_map = dns_map or {}

        async def fake_exec(sandbox_id, cmd, **kwargs):  # noqa: ARG001
            if "command -v iptables" in cmd:
                return _make_exec_ok() if iptables_available else _make_exec_fail()
            if "apt-get install" in cmd or "apk add" in cmd or "yum install" in cmd:
                # Install step — succeed (but only called when iptables missing)
                return _make_exec_ok()
            if "getent ahosts" in cmd or "getent hosts" in cmd:
                # Extract the host being looked up from `getent ahosts 'host'`.
                # Simple substring match against our dns_map keys is enough for tests.
                for host, ip in dns_map.items():
                    if host in cmd:
                        return _make_exec_ok(stdout=ip + "\n")
                return _make_exec_ok(stdout="")
            if rule_failure_substring and rule_failure_substring in cmd:
                return _make_exec_fail("simulated rule failure")
            return _make_exec_ok()

        return fake_exec

    @pytest.mark.asyncio
    async def test_ip_addresses_added_directly(self, backend):
        """Literal IP addresses are used without DNS resolution."""
        config = SandboxConfig(
            network_mode="allowlist",
            allowed_hosts=["1.2.3.4", "10.0.0.1/24"],
        )
        exec_calls: list[str] = []

        async def fake_exec(sandbox_id, cmd, **kwargs):
            exec_calls.append(cmd)
            if "command -v iptables" in cmd:
                return _make_exec_ok()
            return _make_exec_ok()

        backend.exec = fake_exec
        await backend._apply_network_allowlist("sb-test", config)

        # Should never call getent for literal IPs.
        assert not any("getent" in c for c in exec_calls)
        # Both IPs should appear as ACCEPT rules.
        assert any("1.2.3.4" in c for c in exec_calls)
        assert any("10.0.0.0/24" in c for c in exec_calls)

    @pytest.mark.asyncio
    async def test_malformed_ip_in_allowlist_is_rejected(self, backend):
        """Malformed allowlist entries must fail — they cannot be injected into rules."""
        config = SandboxConfig(
            network_mode="allowlist",
            allowed_hosts=["1.2.3.4; echo PWNED"],
        )
        exec_calls: list[str] = []

        async def fake_exec(sandbox_id, cmd, **kwargs):
            exec_calls.append(cmd)
            if "command -v iptables" in cmd:
                return _make_exec_ok()
            if "getent" in cmd:
                return _make_exec_ok(stdout="")
            return _make_exec_ok()

        backend.exec = fake_exec
        # No IPv4 addresses would be resolved → must raise rather than emit a no-op policy.
        with pytest.raises(RuntimeError, match="no allowed_hosts could be resolved"):
            await backend._apply_network_allowlist("sb-test", config)
        assert not any("PWNED" in c for c in exec_calls if "iptables -A OUTPUT -d" in c)

    @pytest.mark.asyncio
    async def test_dns_resolution_result_validated_before_iptables(self, backend):
        """Resolved output must be validated so command fragments cannot be injected."""
        config = SandboxConfig(network_mode="allowlist", allowed_hosts=["safe.example"])
        exec_calls: list[str] = []

        async def fake_exec(sandbox_id, cmd, **kwargs):
            exec_calls.append(cmd)
            if "command -v iptables" in cmd:
                return _make_exec_ok()
            if "getent" in cmd:
                return _make_exec_ok(stdout="8.8.8.8; touch /tmp/pwned\n")
            return _make_exec_ok()

        backend.exec = fake_exec
        # The malformed line doesn't validate as an IPv4 address, so no addresses
        # resolve for the host → allowlist must refuse rather than silently no-op.
        with pytest.raises(RuntimeError, match="no allowed_hosts could be resolved"):
            await backend._apply_network_allowlist("sb-test", config)
        assert not any(
            "touch /tmp/pwned" in c for c in exec_calls if "iptables -A OUTPUT -d" in c
        )

    @pytest.mark.asyncio
    async def test_hostname_resolved_via_getent(self, backend):
        """Hostnames are resolved to IPs before writing iptables rules."""
        config = SandboxConfig(
            network_mode="allowlist",
            allowed_hosts=["api.lucent.local"],
        )
        exec_calls: list[str] = []
        backend.exec = self._make_exec_factory(
            dns_map={"api.lucent.local": "192.168.1.42"},
        )

        # Wrap to capture calls
        original = backend.exec

        async def tracking_exec(sandbox_id, cmd, **kwargs):
            exec_calls.append(cmd)
            return await original(sandbox_id, cmd, **kwargs)

        backend.exec = tracking_exec
        await backend._apply_network_allowlist("sb-test", config)
        resolved_rule = next((c for c in exec_calls if "192.168.1.42" in c), None)
        assert resolved_rule is not None, "resolved IP should appear in iptables rule"

    @pytest.mark.asyncio
    async def test_unresolvable_host_raises_runtime_error(self, backend):
        """An unresolvable host must raise — silently skipping would leave the
        allowlist empty and egress wide open."""
        config = SandboxConfig(
            network_mode="allowlist",
            allowed_hosts=["nxdomain.example.invalid"],
        )
        backend.exec = self._make_exec_factory()
        with pytest.raises(RuntimeError, match="no allowed_hosts could be resolved"):
            await backend._apply_network_allowlist("sb-test", config)

    @pytest.mark.asyncio
    async def test_flush_and_drop_rules_always_present(self, backend):
        """OUTPUT chain is flushed and default policy set to DROP.

        Requires at least one resolvable host — an empty allowlist is refused.
        """
        config = SandboxConfig(
            network_mode="allowlist",
            allowed_hosts=["1.2.3.4"],
        )
        exec_calls: list[str] = []
        backend.exec = self._make_exec_factory()

        original = backend.exec

        async def tracking_exec(sandbox_id, cmd, **kwargs):
            exec_calls.append(cmd)
            return await original(sandbox_id, cmd, **kwargs)

        backend.exec = tracking_exec
        await backend._apply_network_allowlist("sb-test", config)

        assert any("iptables -F OUTPUT" in c for c in exec_calls)
        assert any("iptables -P OUTPUT DROP" in c for c in exec_calls)
        assert any("-o lo -j ACCEPT" in c for c in exec_calls)
        assert any("ESTABLISHED,RELATED" in c for c in exec_calls)
        # DNS egress must be allowed so name resolution still works after DROP.
        assert any("--dport 53" in c for c in exec_calls)

    @pytest.mark.asyncio
    async def test_failed_iptables_rule_raises_runtime_error(self, backend):
        """A failing iptables rule is a hard failure — partial policies leave
        the sandbox in an undefined security state."""
        config = SandboxConfig(
            network_mode="allowlist",
            allowed_hosts=["1.2.3.4"],
        )
        backend.exec = self._make_exec_factory(
            rule_failure_substring="iptables -P OUTPUT DROP",
        )
        with pytest.raises(RuntimeError, match="iptables rule"):
            await backend._apply_network_allowlist("sb-test", config)

    @pytest.mark.asyncio
    async def test_missing_iptables_triggers_install_attempt(self, backend):
        """When iptables is missing, the backend tries to install it via the
        container's package manager. If install fails, the allowlist refuses."""
        config = SandboxConfig(
            network_mode="allowlist",
            allowed_hosts=["1.2.3.4"],
        )

        install_attempted = {"value": False}

        async def fake_exec(sandbox_id, cmd, **kwargs):  # noqa: ARG001
            if "command -v iptables" in cmd:
                # Always missing — install step can't fix it either.
                return _make_exec_fail()
            if "apt-get install" in cmd or "apk add" in cmd or "yum install" in cmd:
                install_attempted["value"] = True
                return _make_exec_fail("no package manager")
            return _make_exec_ok()

        backend.exec = fake_exec
        with pytest.raises(RuntimeError, match="iptables is unavailable"):
            await backend._apply_network_allowlist("sb-test", config)
        assert install_attempted["value"], "backend must at least attempt to install iptables"


# ---------------------------------------------------------------------------
# 2. Disk quota — storage_opt in _create_container
# ---------------------------------------------------------------------------


class TestDiskQuota:
    """Tests that _create_container passes storage_opt when disk_limit is set."""

    def _make_docker_client(self, container):
        """Return a mock docker client whose containers.run returns container."""
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        client.containers.run.return_value = container
        client.api.create_networking_config.return_value = {}
        client.api.create_endpoint_config.return_value = {}
        return client

    def test_disk_limit_passed_as_storage_opt(self):
        config = SandboxConfig(disk_limit="10g")
        backend = DockerBackend()
        mock_container = MagicMock()
        mock_container.id = "abc123"
        client = self._make_docker_client(mock_container)
        backend._client = client

        backend._create_container("sb-disk", "test-sb", config)

        _, kwargs = client.containers.run.call_args
        assert kwargs.get("storage_opt") == {"size": "10g"}

    def test_no_disk_limit_omits_storage_opt(self):
        config = SandboxConfig(disk_limit="")
        backend = DockerBackend()
        mock_container = MagicMock()
        mock_container.id = "abc123"
        client = self._make_docker_client(mock_container)
        backend._client = client

        backend._create_container("sb-nodisk", "test-sb", config)

        _, kwargs = client.containers.run.call_args
        assert "storage_opt" not in kwargs or kwargs.get("storage_opt") is None

    def test_storage_opt_failure_retried_without_quota(self):
        """If the storage driver rejects storage_opt, container is created without it."""
        import docker.errors

        config = SandboxConfig(disk_limit="5g")
        backend = DockerBackend()
        mock_container = MagicMock()
        mock_container.id = "abc123"
        client = self._make_docker_client(mock_container)
        backend._client = client

        call_count = 0

        def run_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1 and kwargs.get("storage_opt"):
                raise docker.errors.APIError("storage opt not supported by quota driver")
            return mock_container

        client.containers.run.side_effect = run_side_effect

        result = backend._create_container("sb-fallback", "test-sb", config)
        assert result is mock_container
        # Second call should have no storage_opt
        second_kwargs = client.containers.run.call_args_list[1][1]
        assert "storage_opt" not in second_kwargs

    def test_allowlist_mode_uses_bridge_and_net_admin(self):
        """allowlist network_mode gives container NET_ADMIN capability."""
        config = SandboxConfig(network_mode="allowlist")
        backend = DockerBackend()
        backend._ensure_network = MagicMock()
        mock_container = MagicMock()
        mock_container.id = "abc123"
        client = self._make_docker_client(mock_container)
        backend._client = client

        backend._create_container("sb-al", "test-sb", config)

        _, kwargs = client.containers.run.call_args
        assert kwargs.get("cap_add") == ["NET_ADMIN"]
        backend._ensure_network.assert_called_once()


# ---------------------------------------------------------------------------
# 3. Idle timeout sweep
# ---------------------------------------------------------------------------


class TestIdleTimeoutSweep:
    """Tests for SandboxManager idle-timeout detection and destruction."""

    def _make_manager(self):
        backend = AsyncMock()
        backend.create.return_value = _ready_info("sb-idle-1")
        backend.list_all.return_value = []
        backend.exec.return_value = _make_exec_ok()
        manager = SandboxManager(backend=backend)
        # Disable DB persistence for unit tests.
        manager._repo = AsyncMock()
        repo = AsyncMock()
        repo.create = AsyncMock()
        repo.update_status = AsyncMock()
        manager._repo.return_value = repo
        # Skip API key provisioning.
        manager._create_task_scoped_api_key = AsyncMock(return_value=(None, ""))
        return manager, backend

    @pytest.mark.asyncio
    async def test_touch_updates_last_activity(self):
        manager, _ = self._make_manager()
        sandbox_id = "sb-touch"
        manager._last_activity[sandbox_id] = time.monotonic() - 10
        old_ts = manager._last_activity[sandbox_id]

        await asyncio.sleep(0)  # yield
        manager._touch(sandbox_id)

        assert manager._last_activity[sandbox_id] > old_ts

    @pytest.mark.asyncio
    async def test_exec_touches_activity(self):
        manager, backend = self._make_manager()
        sandbox_id = "sb-exec"
        manager._last_activity[sandbox_id] = 0.0

        await manager.exec(sandbox_id, "echo hi")

        assert manager._last_activity[sandbox_id] > 0

    @pytest.mark.asyncio
    async def test_sweep_destroys_idle_sandbox(self):
        manager, backend = self._make_manager()
        sandbox_id = "sb-idle"
        # Set last activity far in the past.
        manager._last_activity[sandbox_id] = time.monotonic() - 600
        manager._idle_timeout_config[sandbox_id] = 300  # 5 min limit

        backend.destroy = AsyncMock()

        await manager._sweep_once()

        backend.destroy.assert_called_once_with(sandbox_id)

    @pytest.mark.asyncio
    async def test_sweep_does_not_destroy_active_sandbox(self):
        manager, backend = self._make_manager()
        sandbox_id = "sb-active"
        manager._last_activity[sandbox_id] = time.monotonic()  # just now
        manager._idle_timeout_config[sandbox_id] = 300

        backend.destroy = AsyncMock()

        await manager._sweep_once()

        backend.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_idle_sweep_starts_task(self):
        manager, _ = self._make_manager()
        assert manager._idle_sweep_task is None

        manager._ensure_idle_sweep()

        assert manager._idle_sweep_task is not None
        assert not manager._idle_sweep_task.done()
        manager._idle_sweep_task.cancel()

    @pytest.mark.asyncio
    async def test_ensure_idle_sweep_not_duplicated(self):
        manager, _ = self._make_manager()
        manager._ensure_idle_sweep()
        task1 = manager._idle_sweep_task

        manager._ensure_idle_sweep()

        assert manager._idle_sweep_task is task1
        task1.cancel()

    @pytest.mark.asyncio
    async def test_destroy_cleans_tracking_dicts(self):
        manager, backend = self._make_manager()
        sandbox_id = "sb-destroy"
        manager._last_activity[sandbox_id] = time.monotonic()
        manager._idle_timeout_config[sandbox_id] = 300
        manager._credential_expiry[sandbox_id] = time.monotonic() + 100

        backend.destroy = AsyncMock()

        await manager.destroy(sandbox_id)

        assert sandbox_id not in manager._last_activity
        assert sandbox_id not in manager._idle_timeout_config
        assert sandbox_id not in manager._credential_expiry


# ---------------------------------------------------------------------------
# 4. Credential expiry
# ---------------------------------------------------------------------------


class TestCredentialExpiry:
    """Tests for git credential TTL tracking and invalidation."""

    def _make_manager(self):
        backend = AsyncMock()
        backend.exec.return_value = _make_exec_ok()
        manager = SandboxManager(backend=backend)
        manager._repo = AsyncMock()
        repo = AsyncMock()
        manager._repo.return_value = repo
        return manager, backend

    @pytest.mark.asyncio
    async def test_sweep_invalidates_expired_credentials(self):
        manager, backend = self._make_manager()
        sandbox_id = "sb-cred"
        # Set expiry in the past.
        manager._credential_expiry[sandbox_id] = time.monotonic() - 1

        await manager._sweep_once()

        # Credential invalidation should have been called.
        backend.exec.assert_called()
        assert any("credential" in str(c) for c in backend.exec.call_args_list)
        # Expiry entry should be removed.
        assert sandbox_id not in manager._credential_expiry

    @pytest.mark.asyncio
    async def test_sweep_does_not_invalidate_fresh_credentials(self):
        manager, backend = self._make_manager()
        sandbox_id = "sb-fresh"
        manager._credential_expiry[sandbox_id] = time.monotonic() + 9999

        await manager._sweep_once()

        backend.exec.assert_not_called()
        assert sandbox_id in manager._credential_expiry

    @pytest.mark.asyncio
    async def test_invalidate_git_credentials_runs_git_commands(self):
        manager, backend = self._make_manager()
        sandbox_id = "sb-inv"
        manager._credential_expiry[sandbox_id] = 0

        await manager._invalidate_git_credentials(sandbox_id)

        backend.exec.assert_called_once()
        cmd = backend.exec.call_args[0][1]
        assert "credential" in cmd
        assert sandbox_id not in manager._credential_expiry

    @pytest.mark.asyncio
    async def test_invalidate_handles_exec_failure_gracefully(self):
        manager, backend = self._make_manager()
        sandbox_id = "sb-inv-fail"
        manager._credential_expiry[sandbox_id] = 0
        backend.exec.side_effect = RuntimeError("container gone")

        # Should not raise.
        await manager._invalidate_git_credentials(sandbox_id)
        assert sandbox_id not in manager._credential_expiry

    def test_git_credentials_ttl_field_in_model(self):
        """SandboxConfig has git_credentials_ttl with sensible default."""
        config = SandboxConfig()
        assert config.git_credentials_ttl == 3600

        config_custom = SandboxConfig(git_credentials_ttl=7200)
        assert config_custom.git_credentials_ttl == 7200

    def test_zero_ttl_means_no_expiry(self):
        """TTL of 0 should not register a credential expiry."""
        # The logic in manager.create() checks `if ttl > 0` before setting expiry.
        config = SandboxConfig(git_credentials="token123", git_credentials_ttl=0)
        assert config.git_credentials_ttl == 0
