"""Reusing a sandbox must not carry the previous task's bridge identity.

Reuse hands a live container to a later task in the same request. The bridge
sidecar inside it was built for the task that created the container, so without
rebinding the later task writes events and memory links onto its predecessor.
"""

import pytest

from lucent.sandbox.models import SandboxConfig


class FakeBackend:
    def __init__(self, rebind_result: bool = True):
        self.rebind_result = rebind_result
        self.calls: list[tuple[str, SandboxConfig]] = []

    async def rebind_bridge(self, sandbox_id: str, config: SandboxConfig) -> bool:
        self.calls.append((sandbox_id, config))
        return self.rebind_result


def _manager(backend):
    """A SandboxManager with only the state the rebind path touches."""
    from lucent.sandbox.manager import SandboxManager

    manager = SandboxManager.__new__(SandboxManager)
    manager._backend = backend
    manager._sandbox_bridge_api_keys = {}
    return manager


def _stub_prepare(manager, monkeypatch, *, effective, key_id="key-new", org="org-1"):
    async def prepare(config):
        return effective, key_id, org

    monkeypatch.setattr(manager, "_prepare_bridge", prepare)


@pytest.mark.asyncio
async def test_rebind_hands_the_sandbox_to_the_new_task(monkeypatch):
    backend = FakeBackend(rebind_result=True)
    manager = _manager(backend)
    effective = SandboxConfig(task_id="task-b")
    _stub_prepare(manager, monkeypatch, effective=effective)
    revoked = []

    async def revoke(key_id, org):
        revoked.append((key_id, org))

    monkeypatch.setattr(manager, "_revoke_api_key", revoke)

    assert await manager._rebind_bridge_for_task("sbx-1", SandboxConfig(task_id="task-b"))
    assert backend.calls == [("sbx-1", effective)]
    assert manager._sandbox_bridge_api_keys["sbx-1"] == ("key-new", "org-1")
    assert revoked == []  # nothing previous to revoke


@pytest.mark.asyncio
async def test_rebind_revokes_the_previous_tasks_key(monkeypatch):
    backend = FakeBackend(rebind_result=True)
    manager = _manager(backend)
    manager._sandbox_bridge_api_keys["sbx-1"] = ("key-old", "org-1")
    _stub_prepare(manager, monkeypatch, effective=SandboxConfig(task_id="task-b"))
    revoked = []

    async def revoke(key_id, org):
        revoked.append((key_id, org))

    monkeypatch.setattr(manager, "_revoke_api_key", revoke)

    assert await manager._rebind_bridge_for_task("sbx-1", SandboxConfig(task_id="task-b"))

    assert revoked == [("key-old", "org-1")]
    assert manager._sandbox_bridge_api_keys["sbx-1"] == ("key-new", "org-1")


@pytest.mark.asyncio
async def test_rebind_refuses_when_the_backend_cannot(monkeypatch):
    """A backend that cannot rebind must not leave a minted key behind."""
    backend = FakeBackend(rebind_result=False)
    manager = _manager(backend)
    _stub_prepare(manager, monkeypatch, effective=SandboxConfig(task_id="task-b"))
    revoked = []

    async def revoke(key_id, org):
        revoked.append((key_id, org))

    monkeypatch.setattr(manager, "_revoke_api_key", revoke)

    assert not await manager._rebind_bridge_for_task("sbx-1", SandboxConfig(task_id="task-b"))

    assert revoked == [("key-new", "org-1")]
    assert "sbx-1" not in manager._sandbox_bridge_api_keys


@pytest.mark.asyncio
async def test_docker_rebind_replaces_the_sidecar(monkeypatch):
    from lucent.sandbox.docker_backend import DockerBackend

    backend = DockerBackend()
    removed, started = [], []

    async def remove(sandbox_id):
        removed.append(sandbox_id)

    async def start(sandbox_id, config):
        started.append(sandbox_id)
        return True

    monkeypatch.setattr(backend, "_remove_bridge", remove)
    monkeypatch.setattr(backend, "_start_mcp_bridge", start)

    # A config with a task gets a fresh bridge on the same sandbox.
    bridged = SandboxConfig(task_id="task-b")
    bridged.env_vars = {"LUCENT_SANDBOX_MCP_API_KEY": "k", "LUCENT_SANDBOX_MCP_ENABLED": "1"}
    assert await backend.rebind_bridge("sbx-1", bridged) is True
    assert removed == ["sbx-1"] and started == ["sbx-1"]

    # A config with no task ends up with no bridge, like a fresh create().
    removed.clear()
    assert await backend.rebind_bridge("sbx-2", SandboxConfig()) is True
    assert removed == ["sbx-2"] and started == ["sbx-1"]


@pytest.mark.asyncio
async def test_kubernetes_refuses_to_rebind_in_place():
    """A pod's sidecar cannot be swapped, so reuse must not be attempted."""
    from lucent.sandbox.k8s_backend import KubernetesBackend

    backend = KubernetesBackend()
    assert await backend.rebind_bridge("sbx-1", SandboxConfig(task_id="task-b")) is False
