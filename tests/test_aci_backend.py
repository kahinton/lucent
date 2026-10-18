"""Unit tests for the ACI sandbox backend (no Azure calls).

The SDK client and the exec WebSocket are faked; every assertion is about the
container-group specification, the env split, lifecycle mapping, or the exec
protocol wrapper.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from lucent.sandbox import aci_backend
from lucent.sandbox.aci_backend import (
    _MAX_FILE_BYTES,
    EXEC_SENTINEL_PREFIX,
    TAG_ID,
    TAG_MANAGED,
    TAG_NETWORK_ENFORCEMENT,
    TAG_NETWORK_MODE,
    TAG_WORKING_DIR,
    AzureContainerInstancesBackend,
    ExecOutputLimitError,
    _new_exec_sentinel,
    _strip_sentinel,
)
from lucent.sandbox.models import ExecResult, SandboxConfig, SandboxInfo, SandboxStatus

SUBNET_ID = (
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/"
    "virtualNetworks/vnet/subnets/sandboxes"
)
IDENTITY_ID = (
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ManagedIdentity/"
    "userAssignedIdentities/acr-pull"
)
# A distinct identity representing the app/controller/provisioner; the sandbox
# group must never reference it.
APP_IDENTITY_ID = (
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ManagedIdentity/"
    "userAssignedIdentities/lucent-app"
)


class NotFoundError(Exception):
    status_code = 404


class _Poller:
    def __init__(self, error: Exception | None = None):
        self._error = error

    def result(self, timeout: float | None = None) -> None:
        if self._error:
            raise self._error


def _container(
    *,
    name: str = "sandbox",
    image: str = "alpine:3.20",
    state: str = "Running",
    exit_code: int | None = None,
    detail: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        image=image,
        instance_view=SimpleNamespace(
            current_state=SimpleNamespace(
                state=state, exit_code=exit_code, detail_status=detail
            )
        ),
    )


def _group(
    *,
    name: str = "lucent-sandbox-12345678",
    sandbox_id: str = "sid-1234",
    managed: bool = True,
    state: str = "Running",
    exit_code: int | None = None,
    provisioning: str = "Succeeded",
    containers: list | None = None,
) -> SimpleNamespace:
    tags = {TAG_ID: sandbox_id} if managed else {}
    if managed:
        tags[TAG_MANAGED] = "true"
    return SimpleNamespace(
        name=name,
        tags=tags,
        containers=containers or [_container(state=state, exit_code=exit_code)],
        provisioning_state=provisioning,
    )


class _FakeContainers:
    def __init__(self) -> None:
        self.requests: list[tuple] = []

    def execute_command(self, resource_group, group_name, container_name, request):
        self.requests.append((resource_group, group_name, container_name, request))
        return SimpleNamespace(web_socket_uri="wss://example.invalid/exec", password="pw")


class _FakeContainerGroups:
    def __init__(self, groups: list | None = None, create_error: Exception | None = None):
        self.groups = list(groups or [])
        self.create_error = create_error
        self.created: list[tuple] = []
        self.deleted: list[str] = []

    def begin_create_or_update(self, resource_group, name, group):
        self.created.append((resource_group, name, group))
        if self.create_error:
            raise self.create_error
        sandbox_id = group.get("tags", {}).get(TAG_ID, "sid") if isinstance(group, dict) else "sid"
        self.groups.append(_group(name=name, sandbox_id=sandbox_id))
        return _Poller()

    def get(self, resource_group, name):
        for group in self.groups:
            if group.name == name:
                return group
        raise NotFoundError(name)

    def list_by_resource_group(self, resource_group):
        return list(self.groups)

    def begin_delete(self, resource_group, name):
        self.deleted.append(name)
        self.groups = [group for group in self.groups if group.name != name]
        return _Poller()


class _FakeClient:
    def __init__(self, groups: list | None = None, create_error: Exception | None = None):
        self.container_groups = _FakeContainerGroups(groups, create_error)
        self.containers = _FakeContainers()


def _backend(client=None, **overrides) -> AzureContainerInstancesBackend:
    kwargs = dict(
        subscription_id="sub",
        resource_group="rg",
        subnet_id=SUBNET_ID,
        identity_id=IDENTITY_ID,
        location="eastus",
        allow_subnet_egress=False,
        client=client if client is not None else _FakeClient(),
        ready_timeout=2,
        bridge_timeout=1,
        poll_interval=0.01,
    )
    kwargs.update(overrides)
    return AzureContainerInstancesBackend(**kwargs)


def _patch_sdk(monkeypatch, backend):
    """Keep create() tests independent of the Azure SDK models."""
    monkeypatch.setattr(backend, "_build_group", lambda spec: spec)


def _sentinel_output(stdout: str, exit_code: int, marker: str) -> str:
    """Raw terminal output as the wrapper produces it: output, newline, marker."""
    return f"{stdout}\n{marker}{exit_code}\n"


def _patch_ws(monkeypatch, backend, *, stdout: str = "", exit_code: int = 0):
    """Fake the exec stream for commands run during create()."""

    async def fake_websocket(uri, password, *, timeout=300, stop_marker=None):
        return _sentinel_output(stdout, exit_code, stop_marker)

    monkeypatch.setattr(backend, "_exec_via_websocket", fake_websocket)


def _wrapped_commands(fake) -> list[str]:
    return [request[3].command for request in fake.containers.requests]


def _user_and_bridge(spec):
    containers = {item["name"]: item for item in spec["containers"]}
    return containers["sandbox"], containers["mcp-bridge"]


# ----------------------------------------------------------------------
# Specification: env split, bridge sidecar, hardening, tags
# ----------------------------------------------------------------------


def test_group_spec_forces_bridge_env_split():
    backend = _backend()
    config = SandboxConfig(
        name="demo",
        image="python:3.12-slim",
        task_id="task-1",
        mcp_bridge_port=9123,
        env_vars={
            "CI": "true",
            "LUCENT_API_URL": "http://lucent.internal/api",
            "LUCENT_SANDBOX_MCP_API_KEY": "hs_task_key",
            "LUCENT_SANDBOX_MCP_ENABLED": "1",
            "LUCENT_SANDBOX_TASK_ID": "task-1",
            "LUCENT_SANDBOX_MCP_PORT": "9123",
        },
    )

    spec = backend._group_spec("12345678-aaaa-bbbb", "demo-12345678", config)
    user, bridge = _user_and_bridge(spec)
    user_env = {item["name"]: item["value"] for item in user["environment_variables"]}
    bridge_env = {
        item["name"]: item.get("value", item.get("secure_value"))
        for item in bridge["environment_variables"]
    }
    bridge_secure = {
        item["name"] for item in bridge["environment_variables"] if "secure_value" in item
    }

    # The task-scoped key must reach only the trusted sidecar.
    assert "LUCENT_SANDBOX_MCP_API_KEY" not in user_env
    assert "LUCENT_SANDBOX_TASK_ID" not in user_env
    assert "LUCENT_API_URL" not in user_env
    assert "LUCENT_MCP_BRIDGE_SOURCE_B64" not in user_env
    assert user_env["CI"] == "true"
    assert user_env["LUCENT_SANDBOX_MCP_ENABLED"] == "1"

    assert bridge_env["LUCENT_SANDBOX_MCP_API_KEY"] == "hs_task_key"
    # The key is write-only in the group definition.
    assert bridge_secure == {"LUCENT_SANDBOX_MCP_API_KEY"}
    assert bridge_env["LUCENT_SANDBOX_TASK_ID"] == "task-1"
    assert bridge_env["LUCENT_API_URL"] == "http://lucent.internal/api"
    assert bridge_env["LUCENT_SANDBOX_MCP_PORT"] == "9123"
    assert "LUCENT_MCP_BRIDGE_SOURCE_B64" in bridge_env
    assert "CI" not in bridge_env

    assert user["image"] == "python:3.12-slim"
    assert bridge["command"][0] == "python3"


def test_group_spec_hardening_private_networking_and_tags():
    backend = _backend()
    config = SandboxConfig(
        name="demo",
        image="alpine:3.20",
        organization_id="org-1",
        task_id="task-1",
        request_id="req-1",
        working_dir="/workspace/project",
        memory_limit="512m",
        cpu_limit=0.5,
    )

    spec = backend._group_spec("sid-1234", "demo-1234", config)
    user = spec["containers"][0]  # no bridge key in env_vars, so no sidecar
    requests = user["resources"]["requests"]

    # ACI group minimums are enforced even when the caller asks for less.
    assert requests["cpu"] == 1.0
    assert requests["memory_in_gb"] == 1.0

    # Public image: no managed identity anywhere in the group.
    assert "identity" not in spec

    # Private networking only: no public IP, no DNS name label.
    assert spec["ip_address"]["type"] == "Private"
    assert "dns_name_label" not in spec["ip_address"]
    assert spec["subnet_ids"] == [SUBNET_ID]

    # Workspace is an emptyDir; host bind mounts are unsupported best-effort.
    assert spec["volumes"] == [{"name": "workspace", "empty_dir": {}}]
    assert user["volume_mounts"] == [{"name": "workspace", "mount_path": "/workspace"}]

    assert spec["restart_policy"] == "Never"
    assert spec["os_type"] == "Linux"

    # Standard ACI only supports the privileged flag; everything else must be
    # baked into the image.  Never grant privilege.
    assert user["security_context"] == {"privileged": False}

    tags = spec["tags"]
    assert tags[TAG_MANAGED] == "true"
    assert tags[TAG_ID] == "sid-1234"
    assert tags["lucent-organization-id"] == "org-1"
    assert tags["lucent-task-id"] == "task-1"
    assert tags["lucent-request-id"] == "req-1"
    assert tags[TAG_WORKING_DIR] == "/workspace/project"
    # ACI has no per-group network policy; report the real boundary.
    assert tags[TAG_NETWORK_MODE] == "none"
    assert tags[TAG_NETWORK_ENFORCEMENT] == "subnet-nsg"


def test_group_spec_uses_acr_identity_without_a_password():
    backend = _backend()
    config = SandboxConfig(image="myacr.azurecr.io/lucent/sandbox:1", env_vars={})

    spec = backend._group_spec("sid-1234", "demo-1234", config)

    assert spec["image_registry_credentials"] == [
        {"server": "myacr.azurecr.io", "identity": IDENTITY_ID}
    ]
    # ACI requires the *same* identity on the group as well, or the private
    # pull fails with InvalidImageRegistryIdentity.
    assert spec["identity"] == {
        "type": "UserAssigned",
        "user_assigned_identities": {IDENTITY_ID: {}},
    }
    # Registry auth is identity-only: never a username/password/key.
    registry = str(spec["image_registry_credentials"])
    assert "password" not in registry
    assert "username" not in registry
    assert "key" not in registry


def test_acr_group_uses_only_the_configured_dedicated_pull_identity():
    """The group identity is the configured pull identity, not the app's."""
    backend = _backend(identity_id=IDENTITY_ID)
    config = SandboxConfig(image="myacr.azurecr.io/lucent/sandbox:1", env_vars={})

    spec = backend._group_spec("sid-1234", "demo-1234", config)

    assert set(spec["identity"]["user_assigned_identities"]) == {IDENTITY_ID}
    assert spec["image_registry_credentials"][0]["identity"] == IDENTITY_ID
    # The app/controller/provisioner identity must never leak into the group.
    assert APP_IDENTITY_ID not in str(spec)


def test_build_group_wires_the_identity_in_both_required_places():
    """The SDK model carries the identity on the group and the credential."""
    backend = _backend()
    config = SandboxConfig(image="myacr.azurecr.io/lucent/sandbox:1", env_vars={})

    group = backend._build_group(
        backend._group_spec("sid-1234", "demo-1234", config)
    )

    assert group.identity is not None
    assert group.identity.type == "UserAssigned"
    assert set(group.identity.user_assigned_identities) == {IDENTITY_ID}
    assert len(group.image_registry_credentials) == 1
    credential = group.image_registry_credentials[0]
    assert credential.server == "myacr.azurecr.io"
    assert credential.identity == IDENTITY_ID
    assert credential.username is None
    assert credential.password is None


def test_acr_image_without_a_configured_identity_fails_closed():
    """An ACR pull with no identity is refused, not deployed to fail later."""
    backend = _backend(identity_id="")
    config = SandboxConfig(image="myacr.azurecr.io/lucent/sandbox:1", env_vars={})

    with pytest.raises(RuntimeError, match="InvalidImageRegistryIdentity"):
        backend._group_spec("sid-1234", "demo-1234", config)


def test_docker_hub_image_gets_no_registry_credential_or_identity():
    backend = _backend()
    config = SandboxConfig(image="python:3.12-slim", env_vars={})

    spec = backend._group_spec("sid-1234", "demo-1234", config)

    assert "image_registry_credentials" not in spec
    # Public images never need a group identity.
    assert "identity" not in spec


def test_non_acr_registry_gets_no_identity_credential():
    backend = _backend()
    config = SandboxConfig(image="ghcr.io/lucent/sandbox:1", env_vars={})

    spec = backend._group_spec("sid-1234", "demo-1234", config)

    assert "image_registry_credentials" not in spec
    assert "identity" not in spec


def _bridge_config(**overrides) -> SandboxConfig:
    env_vars = {
        "LUCENT_API_URL": "https://lucent.internal/api",
        "LUCENT_SANDBOX_MCP_API_KEY": "hs_task_key",
        "LUCENT_SANDBOX_MCP_ENABLED": "1",
    }
    env_vars.update(overrides.pop("env_vars", {}))
    return SandboxConfig(
        image=overrides.pop("image", "python:3.12-slim"), env_vars=env_vars, **overrides
    )


def test_public_user_image_with_private_bridge_image_gets_identity(monkeypatch):
    """The Terraform default bridge sidecar is ACR-hosted even when the user
    image is public; the group must still carry the pull identity."""
    monkeypatch.setenv(
        "LUCENT_SANDBOX_BRIDGE_IMAGE", "myacr.azurecr.io/lucent/bridge:1"
    )
    backend = _backend()
    config = _bridge_config()

    spec = backend._group_spec("sid-1234", "demo-1234", config)

    assert spec["image_registry_credentials"] == [
        {"server": "myacr.azurecr.io", "identity": IDENTITY_ID}
    ]
    assert spec["identity"] == {
        "type": "UserAssigned",
        "user_assigned_identities": {IDENTITY_ID: {}},
    }
    assert {item["name"] for item in spec["containers"]} == {"sandbox", "mcp-bridge"}


def test_two_acr_registries_get_one_credential_each(monkeypatch):
    monkeypatch.setenv(
        "LUCENT_SANDBOX_BRIDGE_IMAGE", "bridge.azurecr.io/lucent/bridge:1"
    )
    backend = _backend()
    config = _bridge_config(image="user.azurecr.io/lucent/sandbox:1")

    spec = backend._group_spec("sid-1234", "demo-1234", config)

    assert spec["image_registry_credentials"] == [
        {"server": "user.azurecr.io", "identity": IDENTITY_ID},
        {"server": "bridge.azurecr.io", "identity": IDENTITY_ID},
    ]
    # Still a single group identity; ACI requires it in both places.
    assert spec["identity"]["user_assigned_identities"] == {IDENTITY_ID: {}}


def test_public_images_only_get_no_identity_or_credentials(monkeypatch):
    monkeypatch.delenv("LUCENT_SANDBOX_BRIDGE_IMAGE", raising=False)
    backend = _backend()
    config = _bridge_config(image="python:3.12-slim")

    spec = backend._group_spec("sid-1234", "demo-1234", config)

    assert "image_registry_credentials" not in spec
    assert "identity" not in spec


def test_private_bridge_without_identity_fails_closed(monkeypatch):
    monkeypatch.setenv(
        "LUCENT_SANDBOX_BRIDGE_IMAGE", "myacr.azurecr.io/lucent/bridge:1"
    )
    backend = _backend(identity_id="")
    config = _bridge_config(image="python:3.12-slim")

    with pytest.raises(RuntimeError, match="myacr.azurecr.io/lucent/bridge:1"):
        backend._group_spec("sid-1234", "demo-1234", config)


def test_bridge_url_rejects_docker_only_default():
    backend = _backend()
    config = SandboxConfig(
        image="alpine",
        task_id="task-1",
        env_vars={
            "LUCENT_API_URL": "http://host.docker.internal:8766/api",
            "LUCENT_SANDBOX_MCP_API_KEY": "key",
            "LUCENT_SANDBOX_MCP_ENABLED": "1",
        },
    )

    with pytest.raises(RuntimeError, match="Docker-only default"):
        backend._validate_config(config)


def test_bridge_url_requires_https():
    """http would expose the task key on the shared network namespace."""
    backend = _backend()
    config = SandboxConfig(
        image="alpine",
        task_id="task-1",
        env_vars={
            "LUCENT_API_URL": "http://lucent.internal/api",
            "LUCENT_SANDBOX_MCP_API_KEY": "key",
            "LUCENT_SANDBOX_MCP_ENABLED": "1",
        },
    )

    with pytest.raises(RuntimeError, match="https"):
        backend._validate_config(config)


def test_bridge_url_accepts_https():
    backend = _backend()
    config = SandboxConfig(
        image="alpine",
        task_id="task-1",
        env_vars={
            "LUCENT_API_URL": "https://lucent.internal/api",
            "LUCENT_SANDBOX_MCP_API_KEY": "key",
            "LUCENT_SANDBOX_MCP_ENABLED": "1",
        },
    )

    backend._validate_config(config)


def test_none_network_mode_is_reported_as_subnet_policy(caplog):
    backend = _backend()
    config = SandboxConfig(image="alpine", network_mode="none")

    with caplog.at_level("WARNING"):
        backend._validate_config(config)

    message = " ".join(record.getMessage() for record in caplog.records)
    assert "does NOT mean no network" in message
    assert "subnet NSG" in message


def test_bridge_network_mode_is_reported_as_subnet_policy(caplog):
    backend = _backend()
    config = SandboxConfig(image="alpine", network_mode="bridge")

    with caplog.at_level("WARNING"):
        backend._validate_config(config)

    message = " ".join(record.getMessage() for record in caplog.records)
    assert "subnet policy, not per-group isolation" in message


def test_allowlist_reports_allowed_hosts_are_not_enforced(caplog):
    backend = _backend(allow_subnet_egress=True)
    config = SandboxConfig(
        image="alpine", network_mode="allowlist", allowed_hosts=["example.com"]
    )

    with caplog.at_level("WARNING"):
        backend._validate_config(config)

    message = " ".join(record.getMessage() for record in caplog.records)
    assert "allowed_hosts=['example.com'] is NOT enforced" in message


# ----------------------------------------------------------------------
# create(): never raises, fail-closed policies
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_never_raises_and_returns_failed(monkeypatch):
    fake = _FakeClient(create_error=RuntimeError("azure exploded"))
    backend = _backend(client=fake)
    _patch_sdk(monkeypatch, backend)

    info = await backend.create(SandboxConfig(image="alpine:3.20"))

    assert info.status == SandboxStatus.FAILED
    assert "azure exploded" in (info.error or "")
    assert info.id not in backend._configs


@pytest.mark.asyncio
async def test_create_succeeds_and_reports_ready(monkeypatch):
    fake = _FakeClient()
    backend = _backend(client=fake)
    _patch_sdk(monkeypatch, backend)
    _patch_ws(monkeypatch, backend, stdout="1000")

    info = await backend.create(SandboxConfig(name="demo", image="alpine:3.20"))

    assert info.status == SandboxStatus.READY
    assert info.container_id == info.name
    assert len(fake.container_groups.created) == 1
    assert (await backend.get(info.id)).status == SandboxStatus.READY


@pytest.mark.asyncio
async def test_create_fails_closed_when_image_runs_as_root(monkeypatch):
    fake = _FakeClient()
    backend = _backend(client=fake)
    _patch_sdk(monkeypatch, backend)
    _patch_ws(monkeypatch, backend, stdout="0")

    info = await backend.create(SandboxConfig(name="demo", image="alpine:3.20"))

    assert info.status == SandboxStatus.FAILED
    assert "root" in (info.error or "").lower()
    # The group created for the failed sandbox is cleaned up.
    assert fake.container_groups.groups == []


@pytest.mark.asyncio
async def test_create_allows_root_only_with_explicit_opt_in(monkeypatch, caplog):
    fake = _FakeClient()
    backend = _backend(client=fake, allow_root=True)
    _patch_sdk(monkeypatch, backend)
    _patch_ws(monkeypatch, backend, stdout="0")

    with caplog.at_level("WARNING"):
        info = await backend.create(SandboxConfig(name="demo", image="alpine:3.20"))

    assert info.status == SandboxStatus.READY
    assert any("runs as root" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_create_fails_closed_when_uid_cannot_be_verified(monkeypatch):
    fake = _FakeClient()
    backend = _backend(client=fake)
    _patch_sdk(monkeypatch, backend)
    _patch_ws(monkeypatch, backend, stdout="not-a-uid")

    info = await backend.create(SandboxConfig(name="demo", image="alpine:3.20"))

    assert info.status == SandboxStatus.FAILED
    assert "Could not verify" in (info.error or "")


@pytest.mark.asyncio
async def test_create_creates_a_non_workspace_working_directory(monkeypatch):
    fake = _FakeClient()
    backend = _backend(client=fake)
    _patch_sdk(monkeypatch, backend)
    _patch_ws(monkeypatch, backend, stdout="1000")

    info = await backend.create(
        SandboxConfig(
            name="demo", image="alpine:3.20", working_dir="/workspace/app"
        )
    )

    assert info.status == SandboxStatus.READY
    commands = _wrapped_commands(fake)
    assert any("mkdir -p -- /workspace/app" in command for command in commands)


@pytest.mark.asyncio
async def test_allowlist_fails_closed_without_subnet_opt_in(monkeypatch):
    fake = _FakeClient()
    backend = _backend(client=fake, allow_subnet_egress=False)
    _patch_sdk(monkeypatch, backend)

    info = await backend.create(
        SandboxConfig(image="alpine:3.20", network_mode="allowlist")
    )

    assert info.status == SandboxStatus.FAILED
    assert "LUCENT_SANDBOX_ACI_ALLOW_SUBNET_EGRESS" in (info.error or "")
    assert fake.container_groups.created == []


@pytest.mark.asyncio
async def test_allowlist_creates_only_with_explicit_subnet_acknowledgement(monkeypatch):
    fake = _FakeClient()
    backend = _backend(client=fake, allow_subnet_egress=True)
    _patch_sdk(monkeypatch, backend)
    _patch_ws(monkeypatch, backend, stdout="1000")

    info = await backend.create(
        SandboxConfig(image="alpine:3.20", network_mode="allowlist")
    )

    assert info.status == SandboxStatus.READY
    assert len(fake.container_groups.created) == 1


@pytest.mark.asyncio
async def test_create_fails_closed_when_configuration_is_missing():
    backend = AzureContainerInstancesBackend(
        subscription_id="",
        resource_group="",
        subnet_id="",
        location="",
        client=_FakeClient(),
        ready_timeout=1,
        poll_interval=0.01,
    )

    info = await backend.create(SandboxConfig(image="alpine"))

    assert info.status == SandboxStatus.FAILED
    assert "not configured" in (info.error or "")


# ----------------------------------------------------------------------
# exec: sentinel, timeout, command wrapping
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_parses_sentinel_and_merges_stderr(monkeypatch):
    group = _group(name="lucent-demo-12345678", sandbox_id="sid-1234")
    fake = _FakeClient(groups=[group])
    backend = _backend(client=fake)
    seen = {}

    async def fake_websocket(uri, password, *, timeout=300, stop_marker=None):
        seen.update(uri=uri, password=password, timeout=timeout, marker=stop_marker)
        # User output, then the newline the wrapper injects before the sentinel.
        return _sentinel_output("out\nerr", 7, stop_marker)

    monkeypatch.setattr(backend, "_exec_via_websocket", fake_websocket)

    result = await backend.exec("sid-1234", "echo out; echo err >&2; exit 7")

    assert result.exit_code == 7
    assert result.stdout == "out\nerr"  # stderr merged into stdout
    assert result.stderr == ""
    assert result.timed_out is False
    assert seen["password"] == "pw"
    assert seen["marker"].startswith(EXEC_SENTINEL_PREFIX)
    assert seen["marker"] != EXEC_SENTINEL_PREFIX + "_"  # not the old fixed marker

    request = fake.containers.requests[0][3]
    assert request.command.startswith("/bin/sh -c ")
    assert seen["marker"] in request.command


@pytest.mark.asyncio
async def test_exec_output_containing_the_legacy_marker_is_not_truncated(monkeypatch):
    """`git diff`/`grep` over this repo prints the old fixed marker verbatim."""
    fake = _FakeClient(groups=[_group(sandbox_id="sid-1234")])
    backend = _backend(client=fake)
    diff = (
        "diff --git a/x b/x\n"
        "+_EXEC_SENTINEL = \"__LUCENT_ACI_EXIT__\"\n"
        "+    f\"printf '__LUCENT_ACI_EXIT__%s\\n'\"\n"
    )

    async def fake_websocket(uri, password, *, timeout=300, stop_marker=None):
        return _sentinel_output(diff, 0, stop_marker)

    monkeypatch.setattr(backend, "_exec_via_websocket", fake_websocket)

    result = await backend.exec("sid-1234", "git diff")

    assert result.exit_code == 0
    assert result.stdout == diff


@pytest.mark.asyncio
async def test_exec_output_with_a_marker_shaped_filename_is_not_an_exit(monkeypatch):
    fake = _FakeClient(groups=[_group(sandbox_id="sid-1234")])
    backend = _backend(client=fake)
    output = "listing:\n__LUCENT_ACI_EXIT__/passwd\n__LUCENT_ACI_EXIT__3/file.txt"

    async def fake_websocket(uri, password, *, timeout=300, stop_marker=None):
        return _sentinel_output(output, 4, stop_marker)

    monkeypatch.setattr(backend, "_exec_via_websocket", fake_websocket)

    result = await backend.exec("sid-1234", "find /workspace")

    assert result.exit_code == 4
    assert result.stdout == output


@pytest.mark.asyncio
async def test_exec_forged_legacy_marker_does_not_drop_later_output(monkeypatch):
    fake = _FakeClient(groups=[_group(sandbox_id="sid-1234")])
    backend = _backend(client=fake)
    output = "before\n__LUCENT_ACI_EXIT__0\nafter"

    async def fake_websocket(uri, password, *, timeout=300, stop_marker=None):
        return _sentinel_output(output, 9, stop_marker)

    monkeypatch.setattr(backend, "_exec_via_websocket", fake_websocket)

    result = await backend.exec("sid-1234", "printf 'forged'")

    assert result.exit_code == 9
    assert result.stdout == output


def test_each_execution_gets_a_fresh_nonce():
    first = _new_exec_sentinel()
    second = _new_exec_sentinel()

    assert first.startswith(EXEC_SENTINEL_PREFIX)
    assert first.endswith("__")
    assert first != second


def test_strip_sentinel_requires_the_marker_at_the_end():
    sentinel = "__LUCENT_ACI_EXIT_abc123__"

    # The injected newline is consumed; the user's own trailing newline stays.
    assert _strip_sentinel(f"out\nmore\n\n{sentinel}7\n", sentinel) == ("out\nmore\n", 7)
    # Present but not terminal: user output, not an exit code.
    assert _strip_sentinel(f"a\n{sentinel}0\nb\n", sentinel) == (
        f"a\n{sentinel}0\nb\n",
        None,
    )
    # The old fixed marker never matches a nonce sentinel.
    assert _strip_sentinel("a\n__LUCENT_ACI_EXIT__0\n", sentinel) == (
        "a\n__LUCENT_ACI_EXIT__0\n",
        None,
    )


@pytest.mark.asyncio
async def test_exec_reports_timeout(monkeypatch):
    fake = _FakeClient(groups=[_group(sandbox_id="sid-1234")])
    backend = _backend(client=fake)

    async def fake_websocket(uri, password, *, timeout=300, stop_marker=None):
        raise TimeoutError("slow")

    monkeypatch.setattr(backend, "_exec_via_websocket", fake_websocket)

    result = await backend.exec("sid-1234", "sleep 100", timeout=3)

    assert result.exit_code == 124
    assert result.timed_out is True
    assert "timed out" in result.stderr


@pytest.mark.asyncio
async def test_exec_without_sentinel_is_not_reported_as_success(monkeypatch):
    fake = _FakeClient(groups=[_group(sandbox_id="sid-1234")])
    backend = _backend(client=fake)

    async def fake_websocket(uri, password, *, timeout=300, stop_marker=None):
        return "partial output"

    monkeypatch.setattr(backend, "_exec_via_websocket", fake_websocket)

    result = await backend.exec("sid-1234", "echo hi")

    assert result.exit_code == 1
    assert "exit sentinel" in result.stderr
    assert result.stdout == "partial output"


def test_wrap_command_quotes_env_and_cwd():
    sentinel = "__LUCENT_ACI_EXIT_testnonce__"
    wrapped = AzureContainerInstancesBackend._wrap_command(
        ["echo", "hi there"],
        cwd="/workspace/a b",
        env={"FOO": "value with spaces"},
        sentinel=sentinel,
    )

    assert wrapped.startswith("/bin/sh -c ")
    assert "export FOO=" in wrapped
    assert "value with spaces" in wrapped
    assert "/workspace/a b" in wrapped
    assert sentinel in wrapped


def test_wrap_command_generates_a_random_nonce_by_default():
    wrapped = AzureContainerInstancesBackend._wrap_command(
        "true", cwd=None, env=None
    )

    assert EXEC_SENTINEL_PREFIX in wrapped
    # The old fixed marker (and its immediately-forgeable form) is gone.
    assert "__LUCENT_ACI_EXIT__" not in wrapped
    assert EXEC_SENTINEL_PREFIX + "_" not in wrapped


def test_wrap_command_rejects_invalid_env_names():
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        AzureContainerInstancesBackend._wrap_command(
            "true", cwd=None, env={"BAD=NAME": "x"}
        )


class ConnectionClosedOKError(Exception):
    pass


class _FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent: list[object] = []
        self.closed = False

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        raise ConnectionClosedOKError()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_websocket_sends_password_first_and_stops_on_sentinel():
    marker = "__LUCENT_ACI_EXIT_0123456789abcdef__"
    websocket = _FakeWebSocket(
        [b"hello ", "world", f"\n{marker}3\n", "must not be read"]
    )
    backend = _backend()

    async def fake_open(uri):
        assert uri == "wss://example.invalid/exec"
        return websocket

    backend._open_websocket = fake_open  # type: ignore[method-assign]

    output = await backend._exec_via_websocket(
        "wss://example.invalid/exec", "pw", timeout=5, stop_marker=marker
    )

    assert websocket.sent == ["pw"]
    assert output == "hello world\n" + marker + "3\n"
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_websocket_does_not_stop_on_a_marker_in_the_middle():
    marker = "__LUCENT_ACI_EXIT_0123456789abcdef__"
    websocket = _FakeWebSocket(
        [f"out\n{marker}0\nmore output", f"\n{marker}5\n", "must not be read"]
    )
    backend = _backend()

    async def fake_open(uri):
        return websocket

    backend._open_websocket = fake_open  # type: ignore[method-assign]

    output = await backend._exec_via_websocket(
        "wss://x", "pw", timeout=5, stop_marker=marker
    )

    assert output == f"out\n{marker}0\nmore output\n{marker}5\n"


@pytest.mark.asyncio
async def test_websocket_detects_a_sentinel_split_across_frames():
    marker = "__LUCENT_ACI_EXIT_0123456789abcdef__"
    split = len(marker) // 2
    websocket = _FakeWebSocket(["out\n" + marker[:split], marker[split:] + "6\n"])
    backend = _backend()

    async def fake_open(uri):
        return websocket

    backend._open_websocket = fake_open  # type: ignore[method-assign]

    output = await backend._exec_via_websocket(
        "wss://x", "pw", timeout=5, stop_marker=marker
    )

    assert output == "out\n" + marker + "6\n"


@pytest.mark.asyncio
async def test_websocket_enforces_the_output_cap(monkeypatch):
    monkeypatch.setattr(aci_backend, "_MAX_EXEC_OUTPUT_BYTES", 8)
    websocket = _FakeWebSocket(["12345", "6789", "never read"])
    backend = _backend()

    async def fake_open(uri):
        return websocket

    backend._open_websocket = fake_open  # type: ignore[method-assign]

    with pytest.raises(ExecOutputLimitError) as excinfo:
        await backend._exec_via_websocket("wss://x", "pw", timeout=5)

    assert excinfo.value.partial_output == "12345"
    assert "8-byte limit" in str(excinfo.value)
    # The socket is closed and the offending chunk was not buffered.
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_exec_returns_an_explicit_error_when_output_is_capped(monkeypatch):
    fake = _FakeClient(groups=[_group(sandbox_id="sid-1234")])
    backend = _backend(client=fake)

    async def fake_websocket(uri, password, *, timeout=300, stop_marker=None):
        raise ExecOutputLimitError("partial", 16)

    monkeypatch.setattr(backend, "_exec_via_websocket", fake_websocket)

    result = await backend.exec("sid-1234", "yes")

    assert result.exit_code == 1
    assert result.stdout == "partial"
    assert "16-byte limit" in result.stderr
    assert result.timed_out is False


class ConnectionClosedTooBigError(Exception):
    code = 1009


@pytest.mark.asyncio
async def test_websocket_reports_oversized_frame_as_output_limit():
    """The library's 1009 close is our explicit limit failure, not EOF."""
    backend = _backend()

    class _TooBigWebSocket:
        async def send(self, message):
            pass

        async def recv(self):
            raise ConnectionClosedTooBigError("message too big")

        async def close(self):
            pass

    async def fake_open(uri):
        return _TooBigWebSocket()

    backend._open_websocket = fake_open  # type: ignore[method-assign]

    with pytest.raises(ExecOutputLimitError):
        await backend._exec_via_websocket("wss://x", "pw", timeout=5)


@pytest.mark.asyncio
async def test_websocket_returns_partial_output_when_connection_closes():
    websocket = _FakeWebSocket(["partial"])
    backend = _backend()

    async def fake_open(uri):
        return websocket

    backend._open_websocket = fake_open  # type: ignore[method-assign]

    output = await backend._exec_via_websocket("wss://x", "pw", timeout=5)

    assert output == "partial"


# ----------------------------------------------------------------------
# Files, state, list, rebind
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_decodes_base64(monkeypatch):
    backend = _backend()
    payload = b"\x00binary\xff"

    async def fake_exec(sandbox_id, command, *, cwd=None, env=None, timeout=300):
        if command.startswith("realpath"):
            return ExecResult(0, "/workspace/file\n", "")
        if command.startswith("wc -c"):
            return ExecResult(0, f"{len(payload)}\n", "")
        return ExecResult(0, base64.b64encode(payload).decode(), "")

    monkeypatch.setattr(backend, "exec", fake_exec)

    assert await backend.read_file("sid", "/workspace/file") == payload


@pytest.mark.asyncio
async def test_read_file_size_guard(monkeypatch):
    backend = _backend()
    oversized = base64.b64encode(b"x" * (_MAX_FILE_BYTES + 1)).decode()

    async def fake_exec(sandbox_id, command, *, cwd=None, env=None, timeout=300):
        if command.startswith("realpath"):
            return ExecResult(0, "/workspace/big\n", "")
        if command.startswith("wc -c"):
            return ExecResult(0, "", "")
        return ExecResult(0, oversized, "")

    monkeypatch.setattr(backend, "exec", fake_exec)

    with pytest.raises(ValueError, match="transport limit"):
        await backend.read_file("sid", "/workspace/big")


@pytest.mark.asyncio
async def test_read_file_rejects_a_symlink_escape(monkeypatch):
    """`/workspace/link/passwd` passes the host-lexical check but resolves
    outside /workspace in the container; it must be refused."""
    backend = _backend()

    async def fake_exec(sandbox_id, command, *, cwd=None, env=None, timeout=300):
        if command.startswith("realpath"):
            return ExecResult(0, "/etc/passwd\n", "")
        if command.startswith("wc -c"):
            return ExecResult(0, "12\n", "")
        return ExecResult(0, "", "")

    monkeypatch.setattr(backend, "exec", fake_exec)

    with pytest.raises(ValueError, match="escapes workspace root"):
        await backend.read_file("sid", "/workspace/link/passwd")


@pytest.mark.asyncio
async def test_read_file_rejects_absolute_paths_outside_workspace():
    backend = _backend()

    with pytest.raises(ValueError, match="escapes workspace root"):
        await backend.read_file("sid", "/etc/passwd")


@pytest.mark.asyncio
async def test_write_file_round_trips_base64_inside_workspace(monkeypatch):
    backend = _backend()
    commands: list[str] = []

    async def fake_exec(sandbox_id, command, *, cwd=None, env=None, timeout=300):
        commands.append(command)
        if command.startswith("realpath"):
            return ExecResult(0, "/workspace/sub\n", "")
        return ExecResult(0, "", "")

    monkeypatch.setattr(backend, "exec", fake_exec)
    payload = b"hello\x00world"
    await backend.write_file("sid", "/workspace/sub/file.bin", payload)

    writes = [command for command in commands if "base64 -d" in command]
    assert len(writes) == 1
    assert "base64 -d > /workspace/sub/file.bin" in writes[0]
    encoded = base64.b64encode(payload).decode()
    assert encoded in writes[0]


@pytest.mark.asyncio
async def test_write_file_rejects_a_symlink_parent_escape(monkeypatch):
    backend = _backend()

    async def fake_exec(sandbox_id, command, *, cwd=None, env=None, timeout=300):
        if command.startswith("realpath"):
            return ExecResult(0, "/etc\n", "")
        return ExecResult(0, "", "")

    monkeypatch.setattr(backend, "exec", fake_exec)

    with pytest.raises(ValueError, match="escapes workspace root"):
        await backend.write_file("sid", "/workspace/link/passwd", b"nope")


@pytest.mark.asyncio
async def test_write_file_rejects_a_symlink_at_the_target(monkeypatch):
    """A shell redirect follows a symlink at the final component."""
    backend = _backend()

    async def fake_exec(sandbox_id, command, *, cwd=None, env=None, timeout=300):
        if command.startswith("realpath"):
            return ExecResult(0, "/workspace/link\n", "")
        if command.startswith("if [ -L"):
            return ExecResult(0, "yes\n", "")
        return ExecResult(0, "", "")

    monkeypatch.setattr(backend, "exec", fake_exec)

    with pytest.raises(ValueError, match="symlink"):
        await backend.write_file("sid", "/workspace/link/passwd", b"nope")


@pytest.mark.asyncio
async def test_write_file_chunks_under_the_shell_argument_limit(monkeypatch):
    backend = _backend()
    commands: list[str] = []

    async def fake_exec(sandbox_id, command, *, cwd=None, env=None, timeout=300):
        commands.append(command)
        if command.startswith("realpath"):
            return ExecResult(0, "/workspace\n", "")
        return ExecResult(0, "", "")

    monkeypatch.setattr(backend, "exec", fake_exec)
    payload = b"x" * (100 * 1024)  # spans three 48 KiB chunks
    await backend.write_file("sid", "/workspace/big.bin", payload)

    writes = [command for command in commands if "base64 -d" in command]
    assert len(writes) == 3
    assert "> /workspace/big.bin" in writes[0]
    assert all(">> /workspace/big.bin" in command for command in writes[1:])
    # Linux caps a single /bin/sh -c argument at 128 KiB.
    assert all(len(command.encode()) < 128 * 1024 for command in writes)


@pytest.mark.asyncio
async def test_write_file_refuses_paths_outside_workspace():
    backend = _backend()

    with pytest.raises(ValueError, match="escapes workspace root"):
        await backend.write_file("sid", "/etc/passwd", b"nope")


@pytest.mark.asyncio
async def test_write_file_size_guard(monkeypatch):
    backend = _backend()

    async def fake_exec(sandbox_id, command, *, cwd=None, env=None, timeout=300):
        return ExecResult(0, "", "")

    monkeypatch.setattr(backend, "exec", fake_exec)

    with pytest.raises(ValueError, match="transport limit"):
        await backend.write_file("sid", "/workspace/big", b"x" * (_MAX_FILE_BYTES + 1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "exit_code", "provisioning", "expected"),
    [
        ("Running", None, "Succeeded", SandboxStatus.READY),
        ("Waiting", None, "Creating", SandboxStatus.CREATING),
        ("Terminated", 0, "Succeeded", SandboxStatus.STOPPED),
        ("Terminated", 2, "Succeeded", SandboxStatus.FAILED),
        ("Running", None, "Failed", SandboxStatus.FAILED),
    ],
)
async def test_get_maps_platform_state(state, exit_code, provisioning, expected):
    group = _group(
        sandbox_id="sid-1234",
        state=state,
        exit_code=exit_code,
        provisioning=provisioning,
    )
    backend = _backend(client=_FakeClient(groups=[group]))

    info = await backend.get("sid-1234")

    assert info is not None
    assert info.status == expected


@pytest.mark.asyncio
async def test_get_returns_none_for_absent_group():
    backend = _backend(client=_FakeClient(groups=[]))

    assert await backend.get("missing") is None


@pytest.mark.asyncio
async def test_list_all_only_returns_managed_groups():
    managed = _group(name="managed", sandbox_id="sid-managed", managed=True)
    foreign = _group(name="foreign", sandbox_id="sid-foreign", managed=False)
    backend = _backend(client=_FakeClient(groups=[managed, foreign]))

    infos = await backend.list_all()

    assert [info.id for info in infos] == ["sid-managed"]


@pytest.mark.asyncio
async def test_rebind_bridge_is_refused_for_immutable_groups():
    backend = _backend()

    assert await backend.rebind_bridge("sid", SandboxConfig(task_id="task-b")) is False


@pytest.mark.asyncio
async def test_destroy_deletes_the_container_group():
    fake = _FakeClient(groups=[_group(name="lucent-demo-12345678", sandbox_id="sid-1234")])
    backend = _backend(client=fake)
    backend._group_names["sid-1234"] = "lucent-demo-12345678"

    await backend.destroy("sid-1234")

    assert fake.container_groups.deleted == ["lucent-demo-12345678"]
    assert "sid-1234" not in backend._group_names


@pytest.mark.asyncio
async def test_exec_reloads_working_dir_from_tags_after_a_restart(monkeypatch):
    """The in-process config cache is empty after a worker restart; exec must
    recover working_dir from the group's tags instead of using the shell
    default."""
    group = _group(name="lucent-demo-12345678", sandbox_id="sid-1234")
    group.tags[TAG_WORKING_DIR] = "/workspace/app"
    fake = _FakeClient(groups=[group])
    backend = _backend(client=fake)
    seen: dict[str, str] = {}

    async def fake_websocket(uri, password, *, timeout=300, stop_marker=None):
        seen["command"] = fake.containers.requests[-1][3].command
        return _sentinel_output("ok", 0, stop_marker)

    monkeypatch.setattr(backend, "_exec_via_websocket", fake_websocket)

    assert "sid-1234" not in backend._configs
    result = await backend.exec("sid-1234", "pwd")

    assert result.exit_code == 0
    assert "cd /workspace/app" in seen["command"]
    assert backend._configs["sid-1234"].working_dir == "/workspace/app"


def test_find_group_rechecks_cached_group_tags():
    """A cached name is not trusted without the managed tags."""
    wrong = _group(name="cached-name", sandbox_id="some-other-id")
    right = _group(name="right-name", sandbox_id="sid-1234")
    backend = _backend(client=_FakeClient(groups=[wrong, right]))
    backend._group_names["sid-1234"] = "cached-name"

    group = backend._find_group("sid-1234")

    assert group.name == "right-name"
    assert backend._group_names["sid-1234"] == "right-name"


def test_stop_does_not_preserve_state():
    assert AzureContainerInstancesBackend.preserves_state is False


@pytest.mark.asyncio
async def test_stop_maps_to_destroy(monkeypatch):
    backend = _backend()
    destroyed: list[str] = []

    async def fake_destroy(sandbox_id):
        destroyed.append(sandbox_id)

    monkeypatch.setattr(backend, "destroy", fake_destroy)

    await backend.stop("sid-1234")

    assert destroyed == ["sid-1234"]


def test_disk_limit_is_reported_as_unenforceable(caplog):
    backend = _backend()
    config = SandboxConfig(image="alpine", disk_limit="10g")

    with caplog.at_level("WARNING"):
        backend._group_spec("sid-1234", "demo-1234", config)

    assert any(
        "cannot enforce disk_limit" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_create_error_redacts_secrets(monkeypatch):
    key = "hs_supersecrettaskkey"
    token = "ghp_supersecrettoken"
    fake = _FakeClient(
        create_error=RuntimeError(
            f"boom key={key} token={token} "
            "https://user:password@example.com/x?token=querysecret"
        )
    )
    backend = _backend(client=fake)
    _patch_sdk(monkeypatch, backend)
    config = SandboxConfig(
        image="alpine",
        env_vars={"LUCENT_SANDBOX_MCP_API_KEY": key},
        git_credentials=f"user:{token}",
    )

    info = await backend.create(config)

    error = info.error or ""
    assert info.status == SandboxStatus.FAILED
    assert key not in error
    assert token not in error
    assert "user:password" not in error
    assert "querysecret" not in error
    assert "***" in error


@pytest.mark.asyncio
async def test_sandbox_info_and_cached_config_do_not_retain_secrets(monkeypatch):
    fake = _FakeClient()
    backend = _backend(client=fake)
    _patch_sdk(monkeypatch, backend)
    _patch_ws(monkeypatch, backend, stdout="1000")
    key = "hs_supersecrettaskkey"

    info = await backend.create(
        SandboxConfig(
            name="demo",
            image="alpine:3.20",
            env_vars={"LUCENT_SANDBOX_MCP_API_KEY": key, "CI": "true"},
            git_credentials="user:ghp_secrettoken",
        )
    )

    assert info.status == SandboxStatus.READY
    assert "LUCENT_SANDBOX_MCP_API_KEY" not in info.config.env_vars
    assert info.config.env_vars["CI"] == "true"
    assert info.config.git_credentials is None
    cached = backend._configs[info.id]
    assert "LUCENT_SANDBOX_MCP_API_KEY" not in cached.env_vars
    assert cached.git_credentials is None


def test_scrub_config_sanitizes_the_repo_url():
    backend = _backend()
    config = SandboxConfig(
        image="alpine", repo_url="https://user:token@example.com/org/repo.git"
    )

    scrubbed = backend._scrub_config(config)

    assert scrubbed.repo_url == "https://example.com/org/repo.git"


# ----------------------------------------------------------------------
# Manager integration: unknown backend, ACI reuse fallback
# ----------------------------------------------------------------------


def test_unknown_backend_fails_closed(monkeypatch):
    from lucent.sandbox.manager import SandboxManager

    monkeypatch.setenv("LUCENT_SANDBOX_BACKEND", "azure-container-apps")
    with pytest.raises(RuntimeError, match="Unknown LUCENT_SANDBOX_BACKEND"):
        SandboxManager._default_backend()


def test_aci_backend_is_selected(monkeypatch):
    from lucent.sandbox.manager import SandboxManager

    monkeypatch.setenv("LUCENT_SANDBOX_BACKEND", "aci")
    backend = SandboxManager._default_backend()

    assert isinstance(backend, AzureContainerInstancesBackend)


@pytest.mark.asyncio
async def test_reused_aci_sandbox_falls_back_to_a_fresh_create(monkeypatch):
    """ACI cannot rebind a sidecar, so reuse must mint a new sandbox."""
    from lucent.sandbox.manager import SandboxManager

    reused_id = "reused-sandbox"
    group = _group(name="lucent-old-12345678", sandbox_id=reused_id, state="Running")
    backend = _backend(client=_FakeClient(groups=[group]))

    manager = SandboxManager.__new__(SandboxManager)
    manager._backend = backend
    manager._sandbox_bridge_api_keys = {}

    class _Repo:
        async def find_reusable_for_request(self, **kwargs):
            return {"id": reused_id}

    async def repo():
        return _Repo()

    monkeypatch.setattr(manager, "_repo", repo)

    config = SandboxConfig(
        image="alpine",
        task_id="task-b",
        reuse_within_request=True,
        request_id="req-1",
        organization_id="org-1",
        reuse_key="key-1",
    )

    async def prepare(cfg):
        return cfg, "key-new", "org-1"

    monkeypatch.setattr(manager, "_prepare_bridge", prepare)
    revoked: list[tuple] = []

    async def revoke(key_id, org):
        revoked.append((key_id, org))

    monkeypatch.setattr(manager, "_revoke_api_key", revoke)
    created: list[SandboxConfig] = []
    fresh = SandboxInfo(
        id="fresh-sandbox", name="fresh", status=SandboxStatus.READY, config=config
    )

    async def create(cfg):
        created.append(cfg)
        return fresh

    monkeypatch.setattr(manager, "create", create)

    info, reused = await manager.get_or_create_for_request(config, sequence_order=1)

    assert reused is False
    assert info.id == "fresh-sandbox"
    assert created == [config]
    # The key minted for the refused rebind must not be left behind.
    assert revoked == [("key-new", "org-1")]
