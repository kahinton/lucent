"""Docker backend for sandbox execution."""

from __future__ import annotations

import asyncio
import shlex
import logging
import time
import uuid
from datetime import datetime, timezone

import docker
import docker.errors

from lucent.sandbox.backend import SandboxBackend
from lucent.sandbox.models import (
    ExecResult,
    SandboxConfig,
    SandboxInfo,
    SandboxStatus,
)

logger = logging.getLogger(__name__)

# Label prefix for identifying Lucent-managed containers
LABEL_PREFIX = "io.lucent.sandbox"


class DockerBackend(SandboxBackend):
    """Runs sandboxes as Docker containers on the local Docker daemon."""

    def __init__(self, network_name: str = "lucent-sandbox-net"):
        self._client: docker.DockerClient | None = None
        self._network_name = network_name

    def _docker(self) -> docker.DockerClient:
        if self._client is None:
            import os
            import subprocess

            base_url = os.environ.get("DOCKER_HOST")
            if not base_url:
                # Auto-detect: try docker context for Colima/Rancher/etc.
                try:
                    result = subprocess.run(
                        ["docker", "context", "inspect", "--format",
                         "{{.Endpoints.docker.Host}}"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        base_url = result.stdout.strip()
                except Exception:
                    pass

            if base_url:
                self._client = docker.DockerClient(base_url=base_url)
            else:
                self._client = docker.from_env()
        return self._client

    def _ensure_network(self) -> None:
        """Create the sandbox network if it doesn't exist."""
        client = self._docker()
        try:
            client.networks.get(self._network_name)
        except docker.errors.NotFound:
            client.networks.create(
                self._network_name, driver="bridge", internal=True
            )
            logger.info("Created sandbox network: %s", self._network_name)

    async def create(self, config: SandboxConfig) -> SandboxInfo:
        sandbox_id = str(uuid.uuid4())
        name = config.name or f"lucent-sandbox-{sandbox_id[:12]}"
        info = SandboxInfo(
            id=sandbox_id,
            name=name,
            status=SandboxStatus.CREATING,
            config=config,
            created_at=datetime.now(timezone.utc),
        )

        try:
            container = await asyncio.to_thread(
                self._create_container, sandbox_id, name, config
            )
            info.container_id = container.id

            # Clone repo if configured
            if config.repo_url:
                clone_result = await self.exec(
                    sandbox_id,
                    self._build_clone_command(config),
                    timeout=120,
                )
                if clone_result.exit_code != 0:
                    info.status = SandboxStatus.FAILED
                    info.error = f"Git clone failed: {clone_result.stderr}"
                    return info

            # Run setup commands
            for cmd in config.setup_commands:
                result = await self.exec(sandbox_id, cmd, timeout=300)
                if result.exit_code != 0:
                    logger.warning(
                        "Setup command failed in %s: %s (exit %d)",
                        name, cmd, result.exit_code,
                    )
                    # Don't fail the sandbox — setup commands are best-effort

            info.status = SandboxStatus.READY
            info.ready_at = datetime.now(timezone.utc)
            logger.info("Sandbox ready: %s (%s)", name, sandbox_id[:12])

        except Exception as e:
            info.status = SandboxStatus.FAILED
            info.error = str(e)
            logger.error("Failed to create sandbox %s: %s", name, e)

        return info

    def _create_container(
        self, sandbox_id: str, name: str, config: SandboxConfig
    ) -> docker.models.containers.Container:
        client = self._docker()

        # Pull image if needed
        try:
            client.images.get(config.image)
        except docker.errors.ImageNotFound:
            logger.info("Pulling image: %s", config.image)
            client.images.pull(config.image)

        labels = {
            f"{LABEL_PREFIX}.id": sandbox_id,
            f"{LABEL_PREFIX}.managed": "true",
        }
        if config.task_id:
            labels[f"{LABEL_PREFIX}.task-id"] = config.task_id
        if config.request_id:
            labels[f"{LABEL_PREFIX}.request-id"] = config.request_id

        # Network configuration
        network_mode = None
        networking_config = None
        if config.network_mode == "none":
            network_mode = "none"
        elif config.network_mode == "bridge":
            self._ensure_network()
            networking_config = client.api.create_networking_config({
                self._network_name: client.api.create_endpoint_config()
            })
        # For "allowlist" mode, we use bridge + iptables (handled post-create)

        container = client.containers.run(
            image=config.image,
            name=name,
            labels=labels,
            detach=True,
            stdin_open=True,
            tty=False,
            working_dir=config.working_dir,
            environment=config.env_vars,
            mem_limit=config.memory_limit,
            nano_cpus=int(config.cpu_limit * 1e9),
            network_mode=network_mode,
            networking_config=networking_config,
            security_opt=["no-new-privileges"],
            read_only=False,  # Repos need write access
            tmpfs={"/tmp": "size=512m"},
            stop_signal="SIGTERM",
            # Auto-remove after stop timeout
            auto_remove=False,
        )
        return container

    def _build_clone_command(self, config: SandboxConfig) -> str:
        url = config.repo_url
        # Inject credentials for HTTPS URLs
        if config.git_credentials and url and url.startswith("https://"):
            # Insert token into URL: https://token@github.com/...
            url = url.replace("https://", f"https://{config.git_credentials}@")

        parts = ["git", "clone", "--depth=1"]
        if config.branch:
            parts.extend(["-b", config.branch])
        parts.append(url or "")
        # Clone directly into working_dir (which starts empty)
        parts.append(".")
        return " ".join(parts)

    async def exec(
        self,
        sandbox_id: str,
        command: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> ExecResult:
        container = self._find_container(sandbox_id)
        if container is None:
            return ExecResult(
                exit_code=-1, stdout="", stderr="Sandbox not found", timed_out=False
            )

        if isinstance(command, list):
            cmd = command
        else:
            cmd = ["sh", "-c", command]

        start = time.monotonic()
        try:
            result = await asyncio.to_thread(
                self._exec_sync, container, cmd, cwd, env, timeout
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            return ExecResult(
                exit_code=result[0],
                stdout=result[1],
                stderr=result[2],
                duration_ms=duration_ms,
                timed_out=False,
            )
        except TimeoutError:
            duration_ms = int((time.monotonic() - start) * 1000)
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                duration_ms=duration_ms,
                timed_out=True,
            )

    def _exec_sync(
        self,
        container: docker.models.containers.Container,
        cmd: list[str],
        cwd: str | None,
        env: dict[str, str] | None,
        timeout: int,
    ) -> tuple[int, str, str]:
        exec_id = container.client.api.exec_create(
            container.id,
            cmd,
            workdir=cwd,
            environment=env,
            stdout=True,
            stderr=True,
        )
        output = container.client.api.exec_start(exec_id, demux=True)
        # output is (stdout_bytes, stderr_bytes) when demux=True
        stdout = (output[0] or b"").decode("utf-8", errors="replace")
        stderr = (output[1] or b"").decode("utf-8", errors="replace")
        inspect = container.client.api.exec_inspect(exec_id)
        exit_code = inspect.get("ExitCode", -1)
        return (exit_code, stdout, stderr)

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        container = self._find_container(sandbox_id)
        if container is None:
            raise FileNotFoundError(f"Sandbox {sandbox_id} not found")

        result = await self.exec(sandbox_id, f"cat {shlex.quote(path)}", timeout=10)
        if result.exit_code != 0:
            raise FileNotFoundError(f"File not found: {path}")
        return result.stdout.encode("utf-8")

    async def write_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        container = self._find_container(sandbox_id)
        if container is None:
            raise FileNotFoundError(f"Sandbox {sandbox_id} not found")

        import io
        import tarfile

        # Create a tar archive with the file
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            file_info = tarfile.TarInfo(name=path.lstrip("/"))
            file_info.size = len(content)
            tar.addfile(file_info, io.BytesIO(content))
        tar_stream.seek(0)

        await asyncio.to_thread(
            container.put_archive, "/", tar_stream.read()
        )

    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> list[dict]:
        result = await self.exec(
            sandbox_id,
            f"find {shlex.quote(path)} -maxdepth 1 -printf '%T@ %s %y %p\\n' 2>/dev/null || "
            f"ls -la {shlex.quote(path)} 2>/dev/null",
            timeout=10,
        )
        files = []
        for line in result.stdout.strip().splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 4:
                files.append({
                    "path": parts[3],
                    "size": int(parts[1]) if parts[1].isdigit() else 0,
                    "is_dir": parts[2] == "d",
                })
        return files

    async def get(self, sandbox_id: str) -> SandboxInfo | None:
        container = self._find_container(sandbox_id)
        if container is None:
            return None

        container.reload()
        status_map = {
            "created": SandboxStatus.CREATING,
            "running": SandboxStatus.READY,
            "paused": SandboxStatus.STOPPED,
            "exited": SandboxStatus.STOPPED,
            "dead": SandboxStatus.FAILED,
        }
        status = status_map.get(container.status, SandboxStatus.FAILED)

        return SandboxInfo(
            id=sandbox_id,
            name=container.name,
            status=status,
            config=SandboxConfig(),  # Config not stored on container; caller tracks it
            container_id=container.id,
        )

    async def stop(self, sandbox_id: str) -> None:
        container = self._find_container(sandbox_id)
        if container:
            await asyncio.to_thread(container.stop, timeout=10)
            logger.info("Stopped sandbox: %s", sandbox_id[:12])

    async def destroy(self, sandbox_id: str) -> None:
        container = self._find_container(sandbox_id)
        if container:
            try:
                await asyncio.to_thread(container.stop, timeout=5)
            except Exception:
                pass
            await asyncio.to_thread(container.remove, force=True)
            logger.info("Destroyed sandbox: %s", sandbox_id[:12])

    async def list_all(self) -> list[SandboxInfo]:
        client = self._docker()
        containers = await asyncio.to_thread(
            client.containers.list,
            all=True,
            filters={"label": f"{LABEL_PREFIX}.managed=true"},
        )
        results = []
        for c in containers:
            sid = c.labels.get(f"{LABEL_PREFIX}.id", c.id)
            status_map = {
                "created": SandboxStatus.CREATING,
                "running": SandboxStatus.READY,
                "paused": SandboxStatus.STOPPED,
                "exited": SandboxStatus.STOPPED,
                "dead": SandboxStatus.FAILED,
            }
            results.append(SandboxInfo(
                id=sid,
                name=c.name,
                status=status_map.get(c.status, SandboxStatus.FAILED),
                config=SandboxConfig(),
                container_id=c.id,
            ))
        return results

    def _find_container(
        self, sandbox_id: str
    ) -> docker.models.containers.Container | None:
        client = self._docker()
        containers = client.containers.list(
            all=True,
            filters={"label": f"{LABEL_PREFIX}.id={sandbox_id}"},
        )
        return containers[0] if containers else None
