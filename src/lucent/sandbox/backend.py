"""Abstract backend interface for sandbox providers."""

from __future__ import annotations

import abc

from lucent.sandbox.models import ExecResult, SandboxConfig, SandboxInfo


class SandboxBackend(abc.ABC):
    """Interface that all sandbox backends must implement."""

    @abc.abstractmethod
    async def create(self, config: SandboxConfig) -> SandboxInfo:
        """Create and start a new sandbox. Returns when ready."""

    @abc.abstractmethod
    async def exec(
        self,
        sandbox_id: str,
        command: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> ExecResult:
        """Execute a command inside a running sandbox."""

    @abc.abstractmethod
    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        """Read a file from the sandbox filesystem."""

    @abc.abstractmethod
    async def write_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        """Write a file to the sandbox filesystem."""

    @abc.abstractmethod
    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> list[dict]:
        """List files at a path in the sandbox."""

    @abc.abstractmethod
    async def get(self, sandbox_id: str) -> SandboxInfo | None:
        """Get current state of a sandbox."""

    @abc.abstractmethod
    async def rebind_bridge(self, sandbox_id: str, config: SandboxConfig) -> bool:
        """Point an existing sandbox's MCP bridge at ``config``'s task.

        A reused container keeps the bridge sidecar built for the task that
        created it — that task's id and its task-scoped key. Without rebinding,
        the reusing task would write events and memory links onto its
        predecessor. This makes the bridge match what a fresh
        ``create(config)`` would have produced, including having no bridge when
        ``config`` carries no task.

        Returns False when the backend cannot rebind in place; the caller must
        then not reuse the sandbox for a different task.
        """

    @abc.abstractmethod
    async def stop(self, sandbox_id: str) -> None:
        """Stop a running sandbox (can be resumed)."""

    @abc.abstractmethod
    async def destroy(self, sandbox_id: str) -> None:
        """Permanently destroy a sandbox and all its data."""

    @abc.abstractmethod
    async def list_all(self) -> list[SandboxInfo]:
        """List all sandboxes managed by this backend."""
