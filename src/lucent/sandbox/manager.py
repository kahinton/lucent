"""Sandbox Manager — high-level API for sandbox lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg
import bcrypt
import secrets

from lucent.sandbox.backend import SandboxBackend
from lucent.sandbox.models import (
    ExecResult,
    SandboxConfig,
    SandboxInfo,
    SandboxStatus,
)
from lucent.sandbox.output import OutputResult, SandboxOutputHandler

logger = logging.getLogger(__name__)

# How often the idle-sweep background task runs (seconds).
_IDLE_SWEEP_INTERVAL = 30


class SandboxManager:
    """Manages sandbox lifecycle across backends.

    Persists sandbox records to the database so the UI can show
    all sandboxes (not just currently running ones).
    Delegates container operations to a SandboxBackend (Docker, Kubernetes).
    """

    def __init__(self, backend: SandboxBackend | None = None):
        self._backend = backend or self._default_backend()
        self._cleanup_tasks: dict[str, asyncio.Task] = {}
        self._sandbox_bridge_api_keys: dict[str, UUID] = {}
        # Idle-timeout tracking: sandbox_id → monotonic timestamp of last activity
        self._last_activity: dict[str, float] = {}
        # Per-sandbox idle_timeout_seconds (0 = disabled)
        self._idle_timeout_config: dict[str, int] = {}
        # Credential expiry: sandbox_id → monotonic deadline (0 = no expiry)
        self._credential_expiry: dict[str, float] = {}
        self._idle_sweep_task: asyncio.Task | None = None

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
        key_id: UUID | None = None
        effective_config = SandboxConfig(**asdict(config))

        # For task-linked sandboxes, provision a short-lived scoped key for bridge proxying.
        if effective_config.task_id:
            key_id, raw_key = await self._create_task_scoped_api_key(effective_config)
            effective_config.env_vars = dict(effective_config.env_vars)
            effective_config.env_vars.setdefault(
                "LUCENT_API_URL",
                os.environ.get("LUCENT_SANDBOX_BRIDGE_API_URL", "http://host.docker.internal:8766/api"),
            )
            effective_config.env_vars["LUCENT_SANDBOX_MCP_API_KEY"] = raw_key
            effective_config.env_vars["LUCENT_SANDBOX_TASK_ID"] = effective_config.task_id
            effective_config.env_vars["LUCENT_SANDBOX_MCP_PORT"] = str(
                effective_config.mcp_bridge_port
            )

        # Create the container via backend
        info = await self._backend.create(effective_config)

        if key_id and info.status != SandboxStatus.FAILED:
            self._sandbox_bridge_api_keys[info.id] = key_id

        # Persist to database
        try:
            repo = await self._repo()
            config_dict = asdict(effective_config)
            await repo.create(
                id=info.id,
                name=info.name,
                status=info.status.value,
                image=effective_config.image,
                repo_url=effective_config.repo_url,
                branch=effective_config.branch,
                config=config_dict,
                container_id=info.container_id,
                task_id=effective_config.task_id,
                request_id=effective_config.request_id,
                organization_id=effective_config.organization_id,
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
        if effective_config.timeout_seconds > 0 and info.status != SandboxStatus.FAILED:
            task = asyncio.create_task(self._auto_destroy(info.id, effective_config.timeout_seconds))
            self._cleanup_tasks[info.id] = task
        if info.status == SandboxStatus.FAILED and key_id:
            await self._revoke_api_key(key_id)
            self._sandbox_bridge_api_keys.pop(info.id, None)

        # Record initial activity timestamp and idle-timeout config.
        if info.status != SandboxStatus.FAILED:
            now = time.monotonic()
            self._last_activity[info.id] = now
            idle_to = effective_config.idle_timeout_seconds
            if idle_to > 0:
                self._idle_timeout_config[info.id] = idle_to
            # Track credential expiry if git creds were injected.
            ttl = effective_config.git_credentials_ttl
            if effective_config.git_credentials and ttl > 0:
                self._credential_expiry[info.id] = now + ttl
            self._ensure_idle_sweep()

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
        self._touch(sandbox_id)
        return await self._backend.exec(sandbox_id, command, cwd=cwd, env=env, timeout=timeout)

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        self._touch(sandbox_id)
        return await self._backend.read_file(sandbox_id, path)

    async def write_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        self._touch(sandbox_id)
        return await self._backend.write_file(sandbox_id, path, content)

    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> list[dict]:
        self._touch(sandbox_id)
        return await self._backend.list_files(sandbox_id, path)

    async def get(self, sandbox_id: str) -> dict | None:
        """Get sandbox record from DB."""
        try:
            repo = await self._repo()
            return await repo.get(sandbox_id)
        except Exception:
            logger.debug("Failed to get sandbox %s from DB", sandbox_id, exc_info=True)
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

        # Clean up per-sandbox tracking.
        self._last_activity.pop(sandbox_id, None)
        self._idle_timeout_config.pop(sandbox_id, None)
        self._credential_expiry.pop(sandbox_id, None)

        try:
            await self._backend.destroy(sandbox_id)
        except Exception as e:
            logger.warning("Backend destroy failed for %s: %s", sandbox_id[:12], e)
        finally:
            key_id = self._sandbox_bridge_api_keys.pop(sandbox_id, None)
            if key_id:
                await self._revoke_api_key(key_id)

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

    async def process_output(
        self,
        *,
        sandbox_id: str,
        task_id: str,
        task_description: str,
        config: SandboxConfig,
        request_api,
        memory_api,
        log,
    ) -> OutputResult | None:
        """Process sandbox output_mode for a completed task."""
        handler = SandboxOutputHandler(
            manager=self,
            request_api=request_api,
            memory_api=memory_api,
            logger=log,
        )
        return await handler.process(
            sandbox_id=sandbox_id,
            task_id=task_id,
            task_description=task_description,
            config=config,
        )

    async def list_all(self, organization_id: str | None = None, limit: int = 25, offset: int = 0) -> dict:
        """List all sandboxes from DB."""
        try:
            repo = await self._repo()
            return await repo.list_all(organization_id=organization_id, limit=limit, offset=offset)
        except Exception as e:
            logger.error("Failed to list sandboxes: %s", e, exc_info=True)
            return {"items": [], "total_count": 0, "offset": offset, "limit": limit, "has_more": False}

    async def list_active(self, organization_id: str | None = None, limit: int = 25, offset: int = 0) -> dict:
        """List non-destroyed sandboxes from DB."""
        try:
            repo = await self._repo()
            return await repo.list_active(organization_id=organization_id, limit=limit, offset=offset)
        except Exception as e:
            logger.error("Failed to list active sandboxes: %s", e, exc_info=True)
            return {"items": [], "total_count": 0, "offset": offset, "limit": limit, "has_more": False}

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
            logger.debug("Auto-destroy cancelled for sandbox %s (manual destroy)", sandbox_id[:12])

    # ------------------------------------------------------------------
    # Idle timeout & credential expiry
    # ------------------------------------------------------------------

    def _touch(self, sandbox_id: str) -> None:
        """Update the last-activity timestamp for a sandbox."""
        if sandbox_id in self._last_activity:
            self._last_activity[sandbox_id] = time.monotonic()

    def _ensure_idle_sweep(self) -> None:
        """Start the background idle-sweep task if it is not already running."""
        if self._idle_sweep_task is None or self._idle_sweep_task.done():
            self._idle_sweep_task = asyncio.create_task(self._idle_sweep_loop())

    async def _idle_sweep_loop(self) -> None:
        """Periodically destroy idle sandboxes and rotate expired credentials."""
        try:
            while True:
                await asyncio.sleep(_IDLE_SWEEP_INTERVAL)
                await self._sweep_once()
        except asyncio.CancelledError:
            pass

    async def _sweep_once(self) -> None:
        """Single sweep: check idle timeouts and credential TTLs."""
        now = time.monotonic()

        # Copy keys to avoid mutation during iteration.
        for sandbox_id, last in list(self._last_activity.items()):
            idle_limit = self._idle_timeout_config.get(sandbox_id, 0)
            if idle_limit > 0 and (now - last) >= idle_limit:
                logger.info(
                    "Idle timeout reached for sandbox %s (idle %.0fs >= limit %ds); destroying",
                    sandbox_id[:12], now - last, idle_limit,
                )
                try:
                    await self.destroy(sandbox_id)
                except Exception as exc:
                    logger.warning("Failed to idle-destroy sandbox %s: %s", sandbox_id[:12], exc)

        for sandbox_id, expiry in list(self._credential_expiry.items()):
            if now >= expiry:
                logger.info(
                    "Git credentials expired for sandbox %s; invalidating",
                    sandbox_id[:12],
                )
                await self._invalidate_git_credentials(sandbox_id)

    async def _invalidate_git_credentials(self, sandbox_id: str) -> None:
        """Clear stored git credentials inside the sandbox container."""
        self._credential_expiry.pop(sandbox_id, None)
        try:
            # Clear git credential helper and any cached credentials.
            await self._backend.exec(
                sandbox_id,
                "git config --global --unset credential.helper 2>/dev/null || true; "
                "git credential-cache exit 2>/dev/null || true",
                timeout=10,
            )
            logger.info("Invalidated git credentials in sandbox %s", sandbox_id[:12])
        except Exception as exc:
            logger.warning(
                "Failed to invalidate git credentials in sandbox %s: %s", sandbox_id[:12], exc
            )

    async def _create_task_scoped_api_key(self, config: SandboxConfig) -> tuple[UUID, str]:
        """Create a short-lived API key scoped for sandbox bridge operations."""
        pool = await self._pool()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=config.timeout_seconds + 600)

        daemon_user = await self._ensure_daemon_service_user(config.organization_id)
        user_id = str(daemon_user["id"])
        org_id = str(daemon_user["organization_id"]) if daemon_user.get("organization_id") else None

        key_name = f"sandbox-task-{config.task_id}-{secrets.token_hex(4)}"
        raw_key = secrets.token_urlsafe(32)
        plain_key = f"hs_{raw_key}"
        key_prefix = plain_key[:11]
        key_hash = bcrypt.hashpw(plain_key.encode(), bcrypt.gensalt()).decode()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO api_keys
                    (user_id, organization_id, name, key_prefix, key_hash, scopes, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                user_id,
                org_id,
                key_name,
                key_prefix,
                key_hash,
                ["sandbox-memory", "sandbox-task-events"],
                expires_at,
            )
        return row["id"], plain_key

    async def _revoke_api_key(self, key_id: UUID) -> None:
        pool = await self._pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE api_keys SET is_active = false, revoked_at = NOW() "
                    "WHERE id = $1 AND revoked_at IS NULL",
                    key_id,
                )
        except Exception as e:
            logger.warning("Failed to revoke sandbox API key %s: %s", str(key_id)[:8], e)

    async def _pool(self) -> asyncpg.Pool:
        from lucent.db import get_pool

        return await get_pool()

    async def _ensure_daemon_service_user(self, organization_id: str | None) -> dict:
        pool = await self._pool()
        org_uuid = UUID(organization_id) if organization_id else None
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, organization_id
                FROM users
                WHERE external_id = 'daemon-service'
                  AND is_active = true
                  AND ($1::uuid IS NULL OR organization_id = $1)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                org_uuid,
            )
            if row:
                return dict(row)

            if org_uuid is None:
                org = await conn.fetchrow("SELECT id FROM organizations ORDER BY created_at ASC LIMIT 1")
                if not org:
                    raise RuntimeError("No organization available for sandbox API key provisioning")
                org_uuid = org["id"]

            created = await conn.fetchrow(
                """
                INSERT INTO users (
                    external_id,
                    provider,
                    organization_id,
                    email,
                    display_name,
                    role
                )
                VALUES ('daemon-service', 'local', $1, 'daemon@lucent.local', 'Lucent Daemon', 'member')
                RETURNING id, organization_id
                """,
                org_uuid,
            )
            return dict(created)


# Global singleton
_manager: SandboxManager | None = None


def get_sandbox_manager() -> SandboxManager:
    """Get or create the global SandboxManager instance."""
    global _manager
    if _manager is None:
        _manager = SandboxManager()
    return _manager
