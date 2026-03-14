"""Sandbox Manager — high-level API for sandbox lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone

from lucent.sandbox.backend import SandboxBackend
from lucent.sandbox.models import (
    ExecResult,
    SandboxConfig,
    SandboxInfo,
    SandboxStatus,
)

logger = logging.getLogger(__name__)


class SandboxManager:
    """Manages sandbox lifecycle across backends.

    Persists sandbox records to the database so the UI can show
    all sandboxes (not just currently running ones).
    Delegates container operations to a SandboxBackend (Docker, Kubernetes).
    """

    def __init__(self, backend: SandboxBackend | None = None):
        self._backend = backend or self._default_backend()
        self._cleanup_tasks: dict[str, asyncio.Task] = {}

    @staticmethod
    def _default_backend() -> SandboxBackend:
        backend_type = os.environ.get("LUCENT_SANDBOX_BACKEND", "docker")
        if backend_type == "kubernetes":
            from lucent.sandbox.k8s_backend import KubernetesBackend

            return KubernetesBackend()
        else:
            from lucent.sandbox.docker_backend import DockerBackend

            return DockerBackend()

    async def _repo(self):
        """Lazy-load the DB repository."""
        from lucent.db import get_pool
        from lucent.db.sandbox import SandboxRepository

        pool = await get_pool()
        return SandboxRepository(pool)

    async def create(self, config: SandboxConfig) -> SandboxInfo:
        """Create a new sandbox, persist to DB, and start the container."""
        # Create the container via backend
        info = await self._backend.create(config)

        # Persist to database
        try:
            repo = await self._repo()
            config_dict = asdict(config)
            await repo.create(
                id=info.id,
                name=info.name,
                status=info.status.value,
                image=config.image,
                repo_url=config.repo_url,
                branch=config.branch,
                config=config_dict,
                container_id=info.container_id,
                task_id=config.task_id,
                request_id=config.request_id,
                organization_id=config.organization_id,
            )
            # Update status if ready or failed
            if info.status == SandboxStatus.READY:
                await repo.update_status(
                    info.id,
                    "ready",
                    container_id=info.container_id,
                    ready_at=info.ready_at,
                )
            elif info.status == SandboxStatus.FAILED:
                await repo.update_status(info.id, "failed", error=info.error)
        except Exception as e:
            logger.warning("Failed to persist sandbox %s to DB: %s", info.id[:12], e)

        # Schedule auto-destruction based on timeout
        if config.timeout_seconds > 0 and info.status != SandboxStatus.FAILED:
            task = asyncio.create_task(self._auto_destroy(info.id, config.timeout_seconds))
            self._cleanup_tasks[info.id] = task

        return info

    async def exec(
        self,
        sandbox_id: str,
        command: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> ExecResult:
        """Execute a command in a sandbox."""
        return await self._backend.exec(sandbox_id, command, cwd=cwd, env=env, timeout=timeout)

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        return await self._backend.read_file(sandbox_id, path)

    async def write_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        return await self._backend.write_file(sandbox_id, path, content)

    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> list[dict]:
        return await self._backend.list_files(sandbox_id, path)

    async def get(self, sandbox_id: str) -> dict | None:
        """Get sandbox record from DB."""
        try:
            repo = await self._repo()
            return await repo.get(sandbox_id)
        except Exception:
            return None

    async def get_live(self, sandbox_id: str) -> SandboxInfo | None:
        """Get live container state from backend."""
        return await self._backend.get(sandbox_id)

    async def stop(self, sandbox_id: str) -> None:
        """Stop a sandbox (preserves state)."""
        await self._backend.stop(sandbox_id)
        try:
            repo = await self._repo()
            await repo.update_status(
                sandbox_id,
                "stopped",
                stopped_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.warning("Failed to update sandbox %s status: %s", sandbox_id[:12], e)

    async def destroy(self, sandbox_id: str) -> None:
        """Permanently destroy a sandbox."""
        if sandbox_id in self._cleanup_tasks:
            self._cleanup_tasks[sandbox_id].cancel()
            del self._cleanup_tasks[sandbox_id]

        try:
            await self._backend.destroy(sandbox_id)
        except Exception as e:
            logger.warning("Backend destroy failed for %s: %s", sandbox_id[:12], e)

        try:
            repo = await self._repo()
            await repo.update_status(
                sandbox_id,
                "destroyed",
                destroyed_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.warning("Failed to update sandbox %s status: %s", sandbox_id[:12], e)

        logger.info("Destroyed sandbox: %s", sandbox_id[:12])

    async def list_all(self, organization_id: str | None = None) -> list[dict]:
        """List all sandboxes from DB."""
        try:
            repo = await self._repo()
            return await repo.list_all(organization_id=organization_id)
        except Exception as e:
            logger.error("Failed to list sandboxes: %s", e, exc_info=True)
            return []

    async def list_active(self, organization_id: str | None = None) -> list[dict]:
        """List non-destroyed sandboxes from DB."""
        try:
            repo = await self._repo()
            return await repo.list_active(organization_id=organization_id)
        except Exception as e:
            logger.error("Failed to list active sandboxes: %s", e, exc_info=True)
            return []

    async def cleanup_all(self) -> int:
        """Destroy all managed sandboxes. Returns count destroyed."""
        live = await self._backend.list_all()
        count = 0
        for sb in live:
            try:
                await self.destroy(sb.id)
                count += 1
            except Exception as e:
                logger.warning("Failed to destroy sandbox %s: %s", sb.id, e)
        return count

    async def _auto_destroy(self, sandbox_id: str, timeout: int) -> None:
        """Auto-destroy a sandbox after its timeout expires."""
        try:
            await asyncio.sleep(timeout)
            logger.info(
                "Auto-destroying sandbox %s after %ds timeout",
                sandbox_id[:12],
                timeout,
            )
            await self.destroy(sandbox_id)
        except asyncio.CancelledError:
            pass  # Normal — sandbox was manually destroyed


# Global singleton
_manager: SandboxManager | None = None


def get_sandbox_manager() -> SandboxManager:
    """Get or create the global SandboxManager instance."""
    global _manager
    if _manager is None:
        _manager = SandboxManager()
    return _manager
