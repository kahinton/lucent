"""Kubernetes backend for sandbox execution (stub for future implementation)."""

from __future__ import annotations

from lucent.sandbox.backend import SandboxBackend
from lucent.sandbox.models import ExecResult, SandboxConfig, SandboxInfo


class KubernetesBackend(SandboxBackend):
    """Runs sandboxes as Kubernetes Jobs/Pods.

    This is a stub — the interface is defined but not yet implemented.
    The Docker backend covers local development. This backend will be
    implemented for production/multi-node deployments.
    """

    def __init__(
        self,
        namespace: str = "lucent-sandboxes",
        kubeconfig: str | None = None,
    ):
        self._namespace = namespace
        self._kubeconfig = kubeconfig

    async def create(self, config: SandboxConfig) -> SandboxInfo:
        raise NotImplementedError("Kubernetes backend not yet implemented")

    async def exec(
        self,
        sandbox_id: str,
        command: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> ExecResult:
        raise NotImplementedError("Kubernetes backend not yet implemented")

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        raise NotImplementedError("Kubernetes backend not yet implemented")

    async def write_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        raise NotImplementedError("Kubernetes backend not yet implemented")

    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> list[dict]:
        raise NotImplementedError("Kubernetes backend not yet implemented")

    async def get(self, sandbox_id: str) -> SandboxInfo | None:
        raise NotImplementedError("Kubernetes backend not yet implemented")

    async def stop(self, sandbox_id: str) -> None:
        raise NotImplementedError("Kubernetes backend not yet implemented")

    async def destroy(self, sandbox_id: str) -> None:
        raise NotImplementedError("Kubernetes backend not yet implemented")

    async def list_all(self) -> list[SandboxInfo]:
        raise NotImplementedError("Kubernetes backend not yet implemented")
