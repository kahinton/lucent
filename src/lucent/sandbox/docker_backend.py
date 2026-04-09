"""Docker backend for sandbox execution."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import shlex
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import docker.errors

import docker
from lucent.sandbox.backend import SandboxBackend
from lucent.sandbox.devcontainer import DevcontainerConfig, detect_devcontainer
from lucent.sandbox.models import (
    ExecResult,
    SandboxConfig,
    SandboxInfo,
    SandboxStatus,
)

logger = logging.getLogger(__name__)

# Label prefix for identifying Lucent-managed containers
LABEL_PREFIX = "io.lucent.sandbox"
_GIT_ASKPASS_PATH = "/tmp/lucent-git-askpass.sh"


def _devcontainer_to_dict(dc: DevcontainerConfig) -> dict:
    """Serialize a DevcontainerConfig for storage in SandboxInfo."""
    return {
        "image": dc.image,
        "build": {
            "dockerfile": dc.build_dockerfile,
            "context": dc.build_context,
            "args": dc.build_args,
        } if dc.build_dockerfile else None,
        "features": dc.features or None,
        "lifecycle_commands": {
            "onCreateCommand": dc.on_create_command,
            "postCreateCommand": dc.post_create_command,
            "updateContentCommand": dc.update_content_command,
            "postStartCommand": dc.post_start_command,
            "postAttachCommand": dc.post_attach_command,
        },
        "environment": dc.merged_env or None,
        "forward_ports": dc.forward_ports or None,
        "remote_user": dc.remote_user,
    }


class DockerBackend(SandboxBackend):
    """Runs sandboxes as Docker containers on the local Docker daemon."""

    def __init__(self, network_name: str = "lucent-sandbox-net"):
        self._client: docker.DockerClient | None = None
        self._network_name = network_name

    def _workspace_volume_name(self, sandbox_id: str) -> str:
        """Return the named volume used to persist /workspace across rebuilds."""
        return f"lucent-sandbox-{sandbox_id}-workspace"

    def _docker(self) -> docker.DockerClient:
        if self._client is None:
            import os
            import subprocess

            base_url = os.environ.get("DOCKER_HOST")
            if not base_url:
                # Auto-detect: try docker context for Colima/Rancher/etc.
                try:
                    result = subprocess.run(
                        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        base_url = result.stdout.strip()
                except Exception:
                    logger.debug("Failed to inspect docker context", exc_info=True)

            if base_url:
                self._client = docker.DockerClient(base_url=base_url)
            else:
                self._client = docker.from_env()
        return self._client

    def _ensure_network(self, *, internal: bool = False) -> None:
        """Create the sandbox network if it doesn't exist.

        Args:
            internal: When True the network is isolated from the host (no
                external routing).  For ``allowlist`` mode this should be
                False — egress is controlled by iptables rules applied
                post-create, not by Docker's internal flag.
        """
        client = self._docker()
        try:
            client.networks.get(self._network_name)
        except docker.errors.NotFound:
            client.networks.create(self._network_name, driver="bridge", internal=internal)
            logger.info("Created sandbox network: %s (internal=%s)", self._network_name, internal)

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

        dc_config: DevcontainerConfig | None = None

        try:
            container = await asyncio.to_thread(self._create_container, sandbox_id, name, config)
            info.container_id = container.id

            # Clone repo if configured
            if config.repo_url:
                clone_env = None
                if config.git_credentials and config.repo_url.startswith("https://"):
                    await self._ensure_git_askpass_script(sandbox_id)
                    clone_env = self._build_git_auth_env(config.git_credentials)
                clone_result = await self.exec(
                    sandbox_id,
                    self._build_clone_command(config),
                    env=clone_env,
                    timeout=120,
                )
                if clone_result.exit_code != 0:
                    info.status = SandboxStatus.FAILED
                    info.error = f"Git clone failed: {self._sanitize_git_output(clone_result.stderr, config)}"
                    return info

            # Detect devcontainer.json in the workspace
            dc_config = await self._detect_devcontainer(sandbox_id)
            if dc_config:
                info.devcontainer = _devcontainer_to_dict(dc_config)

                # Handle image override: rebuild with devcontainer image
                if dc_config.image and dc_config.image != config.image:
                    info = await self._rebuild_with_image(
                        sandbox_id, name, config, dc_config.image, info
                    )

                # Handle Dockerfile build
                if dc_config.build_dockerfile:
                    await self._build_devcontainer_image(sandbox_id, config, dc_config)

                # Run devcontainer lifecycle commands (before user setup)
                for cmd in dc_config.all_setup_commands:
                    result = await self.exec(sandbox_id, cmd, timeout=300)
                    if result.exit_code != 0:
                        logger.warning(
                            "Devcontainer command failed in %s: %s (exit %d)",
                            name, cmd, result.exit_code,
                        )

            # Run user setup commands (after devcontainer commands)
            for cmd in config.setup_commands:
                result = await self.exec(sandbox_id, cmd, timeout=300)
                if result.exit_code != 0:
                    logger.warning(
                        "Setup command failed in %s: %s (exit %d)",
                        name,
                        cmd,
                        result.exit_code,
                    )
                    # Don't fail the sandbox — setup commands are best-effort

            # Start MCP bridge when task-scoped key is present
            if config.env_vars.get("LUCENT_SANDBOX_MCP_API_KEY"):
                bridge_started = await self._start_mcp_bridge(sandbox_id, config)
                if not bridge_started:
                    info.status = SandboxStatus.FAILED
                    info.error = "Failed to start sandbox MCP bridge"
                    return info

            # Apply network allowlist after all setup is complete so clone and
            # package installation can proceed freely beforehand.
            if config.network_mode == "allowlist":
                await self._apply_network_allowlist(sandbox_id, config)

            info.status = SandboxStatus.READY
            info.ready_at = datetime.now(timezone.utc)
            logger.info("Sandbox ready: %s (%s)", name, sandbox_id[:12])

            # Run postStartCommand after sandbox is READY
            if dc_config and dc_config.post_start_command:
                for cmd in dc_config.post_start_command:
                    result = await self.exec(sandbox_id, cmd, timeout=300)
                    if result.exit_code != 0:
                        logger.warning(
                            "Devcontainer postStart failed in %s: %s (exit %d)",
                            name, cmd, result.exit_code,
                        )

        except Exception as e:
            info.status = SandboxStatus.FAILED
            info.error = str(e)
            logger.error("Failed to create sandbox %s: %s", name, e)

        return info

    async def _detect_devcontainer(
        self, sandbox_id: str, working_dir: str = "/workspace",
    ) -> DevcontainerConfig | None:
        """Detect devcontainer.json inside the sandbox."""
        async def exec_fn(command: str, cwd: str | None) -> tuple[int, str, str]:
            result = await self.exec(sandbox_id, command, cwd=cwd or working_dir, timeout=30)
            return result.exit_code, result.stdout, result.stderr
        return await detect_devcontainer(exec_fn, working_dir)

    async def _rebuild_with_image(
        self,
        sandbox_id: str,
        name: str,
        config: SandboxConfig,
        image: str,
        info: SandboxInfo,
    ) -> SandboxInfo:
        """Rebuild the sandbox with a different container image."""
        logger.info("Rebuilding sandbox %s with devcontainer image: %s", name, image)
        try:
            # Stop and remove current container
            container = self._find_container(sandbox_id)
            if container:
                await asyncio.to_thread(container.stop, timeout=10)
                await asyncio.to_thread(container.remove, force=True)

            # Create new container with devcontainer image
            new_config = SandboxConfig(
                image=image,
                name=config.name,
                repo_url=config.repo_url,
                branch=config.branch,
                setup_commands=config.setup_commands,
                env_vars=config.env_vars,
                working_dir=config.working_dir,
                memory_limit=config.memory_limit,
                cpu_limit=config.cpu_limit,
                disk_limit=config.disk_limit,
                network_mode=config.network_mode,
                allowed_hosts=config.allowed_hosts,
                timeout_seconds=config.timeout_seconds,
                idle_timeout_seconds=config.idle_timeout_seconds,
                mcp_bridge_port=config.mcp_bridge_port,
                git_credentials=config.git_credentials,
                git_credentials_ttl=config.git_credentials_ttl,
                task_id=config.task_id,
                request_id=config.request_id,
                organization_id=config.organization_id,
            )
            new_container = await asyncio.to_thread(
                self._create_container, sandbox_id, name, new_config,
            )
            info.container_id = new_container.id
            info.status = SandboxStatus.CREATING
        except Exception as e:
            logger.error("Failed to rebuild sandbox %s with image %s: %s", name, image, e)
        return info

    async def _build_devcontainer_image(
        self,
        sandbox_id: str,
        config: SandboxConfig,
        dc_config: DevcontainerConfig,
    ) -> str | None:
        """Build a Docker image from a devcontainer Dockerfile. Returns image tag or None."""
        logger.info(
            "Building devcontainer image from %s for sandbox %s",
            dc_config.build_dockerfile, sandbox_id[:12],
        )
        # Delegate to docker build inside the container
        context = dc_config.build_context or "."
        dockerfile = dc_config.build_dockerfile
        image_tag = f"lucent-dc-{sandbox_id[:12]}"
        build_cmd = (
            f"docker build -f {shlex.quote(dockerfile)} "
            f"-t {shlex.quote(image_tag)} {shlex.quote(context)}"
        )
        result = await self.exec(sandbox_id, build_cmd, cwd="/workspace", timeout=600)
        if result.exit_code != 0:
            logger.warning("Devcontainer Dockerfile build failed: %s", result.stderr[:200])
            return None
        return f"lucent-dc-{sandbox_id[:12]}"

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
        cap_add = []
        dns: list[str] = []
        if config.network_mode == "none":
            network_mode = "none"
        elif config.network_mode in ("bridge", "allowlist"):
            # Use internal=False for both modes: for allowlist, iptables rules
            # (applied post-create) control egress rather than Docker's internal
            # flag, which would block DNS resolution during the setup phase.
            self._ensure_network(internal=False)
            networking_config = client.api.create_networking_config(
                {self._network_name: client.api.create_endpoint_config()}
            )
            # Explicit DNS servers so the custom bridge network resolves external
            # hostnames correctly on Docker Desktop and Colima.
            dns = ["8.8.8.8", "1.1.1.1"]
            if config.network_mode == "allowlist":
                # NET_ADMIN needed to apply iptables rules post-create
                cap_add = ["NET_ADMIN"]
        # For "allowlist" mode, we use bridge + iptables (handled post-create)

        # Disk quota via storage driver (overlay2 with quota support, btrfs, zfs).
        # Falls back gracefully when the storage driver does not support it.
        storage_opt: dict[str, str] | None = None
        if config.disk_limit:
            storage_opt = {"size": config.disk_limit}

        workspace_volume = self._workspace_volume_name(sandbox_id)
        container_kwargs: dict = dict(
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
            # Docker SDK 7.x requires network= alongside networking_config=
            network=self._network_name if networking_config is not None else None,
            networking_config=networking_config,
            dns=dns or None,
            cap_add=cap_add or None,
            security_opt=["no-new-privileges"],
            read_only=False,  # Repos need write access
            volumes={workspace_volume: {"bind": "/workspace", "mode": "rw"}},
            tmpfs={"/tmp": "size=512m"},
            stop_signal="SIGTERM",
            auto_remove=False,
        )
        if storage_opt:
            try:
                container_kwargs["storage_opt"] = storage_opt
                container = client.containers.run(**container_kwargs)
                return container
            except docker.errors.APIError as exc:
                if "storage opt" in str(exc).lower() or "quota" in str(exc).lower():
                    logger.warning(
                        "Storage quota not supported by Docker storage driver (%s); "
                        "creating container without disk limit",
                        exc,
                    )
                    del container_kwargs["storage_opt"]
                else:
                    raise

        container = client.containers.run(**container_kwargs)
        return container

    async def _start_mcp_bridge(self, sandbox_id: str, config: SandboxConfig) -> bool:
        """Inject and start the MCP bridge process inside the container."""
        bridge_source_path = Path(__file__).with_name("mcp_bridge.py")
        if not bridge_source_path.exists():
            logger.error("Sandbox MCP bridge source missing: %s", bridge_source_path)
            return False

        source = bridge_source_path.read_bytes()
        bridge_path = "/tmp/lucent_mcp_bridge.py"
        await self.write_file(sandbox_id, bridge_path, source)

        port = int(config.mcp_bridge_port or 8765)
        start_cmd = (
            f"python {shlex.quote(bridge_path)} --host 127.0.0.1 --port {port} "
            f">/tmp/lucent-mcp-bridge.log 2>&1 &"
        )
        result = await self.exec(sandbox_id, start_cmd, timeout=10)
        if result.exit_code != 0:
            logger.error("Failed to launch MCP bridge in sandbox %s: %s", sandbox_id[:12], result.stderr)
            return False

        # Give the process a chance to bind and verify health endpoint.
        health_cmd = (
            "python -c \"import urllib.request,sys;"
            f"resp=urllib.request.urlopen('http://127.0.0.1:{port}/health',timeout=5);"
            "sys.exit(0 if resp.status==200 else 1)\""
        )
        for _ in range(5):
            await asyncio.sleep(1)
            probe = await self.exec(sandbox_id, health_cmd, timeout=10)
            if probe.exit_code == 0:
                logger.info("Sandbox MCP bridge started on 127.0.0.1:%d (%s)", port, sandbox_id[:12])
                return True

        logger.error("Sandbox MCP bridge health check failed for %s", sandbox_id[:12])
        return False

    async def _apply_network_allowlist(
        self, sandbox_id: str, config: SandboxConfig
    ) -> None:
        """Apply iptables egress rules inside the container for allowlist mode.

        Resolves each entry in ``config.allowed_hosts`` to an IP address, then
        installs OUTPUT chain rules that allow only those destinations (plus
        loopback and already-established flows) and drop everything else.

        Requires the container to have the NET_ADMIN capability and iptables
        available in the image.  Failures are logged as warnings rather than
        surfaced as hard errors so a missing iptables binary doesn't break
        non-security-sensitive deployments.
        """
        # Resolve allowed hosts to IP addresses inside the container.
        allowed_ips: list[str] = []
        for host in config.allowed_hosts:
            literal_ip = self._validate_iptables_destination(host)
            if literal_ip is not None:
                allowed_ips.append(literal_ip)
            else:
                res = await self.exec(
                    sandbox_id,
                    f"getent hosts {shlex.quote(host)} | awk '{{print $1}}' | head -n1",
                    timeout=10,
                )
                ip = self._validate_iptables_destination(res.stdout.strip())
                if ip:
                    allowed_ips.append(ip)
                else:
                    logger.warning(
                        "Allowlist: invalid or unresolvable host %r in sandbox %s; skipping",
                        host, sandbox_id[:12],
                    )

        rules: list[str] = [
            "iptables -F OUTPUT",
            "iptables -A OUTPUT -o lo -j ACCEPT",
            "iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT",
        ]
        for ip in allowed_ips:
            rules.append(f"iptables -A OUTPUT -d {shlex.quote(ip)} -j ACCEPT")
        rules.append("iptables -P OUTPUT DROP")

        for rule in rules:
            res = await self.exec(sandbox_id, rule, timeout=10)
            if res.exit_code != 0:
                logger.warning(
                    "Allowlist iptables rule failed in sandbox %s: %r — %s",
                    sandbox_id[:12], rule, res.stderr[:200],
                )

        logger.info(
            "Applied network allowlist in sandbox %s (%d allowed IPs)",
            sandbox_id[:12], len(allowed_ips),
        )

    def _build_clone_command(self, config: SandboxConfig) -> str:
        url = self._sanitize_repo_url(config.repo_url)
        parts: list[str] = ["git", "clone", "--depth=1"]
        if config.branch:
            parts.extend(["-b", config.branch])
        if url:
            parts.append(url)
        # Clone directly into working_dir (which starts empty)
        parts.append(".")
        return " ".join(shlex.quote(part) for part in parts)

    async def _ensure_git_askpass_script(self, sandbox_id: str) -> None:
        script = (
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *Username*) printf "%s\\n" "${LUCENT_GIT_USERNAME:-x-access-token}" ;;\n'
            '  *Password*) printf "%s\\n" "${LUCENT_GIT_TOKEN:-}" ;;\n'
            '  *) printf "\\n" ;;\n'
            "esac\n"
        )
        await self.write_file(sandbox_id, _GIT_ASKPASS_PATH, script.encode("utf-8"))
        await self.exec(sandbox_id, f"chmod 700 {shlex.quote(_GIT_ASKPASS_PATH)}", timeout=10)

    @staticmethod
    def _parse_git_credentials(credentials: str) -> tuple[str, str]:
        if ":" in credentials:
            username, password = credentials.split(":", 1)
            if username and password:
                return username, password
        return "x-access-token", credentials

    def _build_git_auth_env(self, credentials: str) -> dict[str, str]:
        username, token = self._parse_git_credentials(credentials)
        return {
            "GIT_ASKPASS": _GIT_ASKPASS_PATH,
            "GIT_TERMINAL_PROMPT": "0",
            "LUCENT_GIT_USERNAME": username,
            "LUCENT_GIT_TOKEN": token,
        }

    @staticmethod
    def _sanitize_repo_url(repo_url: str | None) -> str | None:
        if not repo_url:
            return repo_url
        try:
            parsed = urlsplit(repo_url)
            if not parsed.scheme or not parsed.netloc or "@" not in parsed.netloc:
                return repo_url
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
        except Exception:
            return repo_url

    def _sanitize_git_output(self, text: str, config: SandboxConfig) -> str:
        sanitized = text
        if config.git_credentials:
            sanitized = sanitized.replace(config.git_credentials, "***")
            _, token = self._parse_git_credentials(config.git_credentials)
            sanitized = sanitized.replace(token, "***")
        if config.repo_url:
            clean_url = self._sanitize_repo_url(config.repo_url)
            if clean_url and clean_url != config.repo_url:
                sanitized = sanitized.replace(config.repo_url, clean_url)
        return sanitized

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
            return ExecResult(exit_code=-1, stdout="", stderr="Sandbox not found", timed_out=False)

        if isinstance(command, list):
            cmd = command
        else:
            cmd = ["sh", "-c", command]

        start = time.monotonic()
        try:
            result = await asyncio.to_thread(self._exec_sync, container, cmd, cwd, env, timeout)
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

        safe_path = self._validate_workspace_path(path)

        # Create a tar archive with the file
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            file_info = tarfile.TarInfo(name=safe_path.lstrip("/"))
            file_info.size = len(content)
            tar.addfile(file_info, io.BytesIO(content))
        tar_stream.seek(0)

        await asyncio.to_thread(container.put_archive, "/", tar_stream.read())

    @staticmethod
    def _validate_iptables_destination(value: str) -> str | None:
        """Validate IPv4 destination used in iptables commands."""
        value = value.strip()
        if not value:
            return None
        try:
            addr = ipaddress.ip_address(value)
            if isinstance(addr, ipaddress.IPv4Address):
                return str(addr)
            logger.warning("Allowlist: IPv6 destination %r is not supported by iptables", value)
            return None
        except ValueError:
            pass
        try:
            net = ipaddress.ip_network(value, strict=False)
            if isinstance(net, ipaddress.IPv4Network):
                return str(net)
            logger.warning("Allowlist: IPv6 network %r is not supported by iptables", value)
            return None
        except ValueError:
            return None

    @staticmethod
    def _validate_workspace_path(path: str, workspace_root: str = "/workspace") -> str:
        """Resolve and validate path is contained inside workspace_root."""
        if not path:
            raise ValueError("Path must not be empty")

        normalized = os.path.normpath(path)
        if os.path.isabs(normalized):
            candidate = normalized
        else:
            candidate = os.path.join(workspace_root, normalized)
        resolved_candidate = os.path.realpath(candidate)
        resolved_root = os.path.realpath(workspace_root)
        root_prefix = resolved_root.rstrip("/") + "/"
        if resolved_candidate != resolved_root and not resolved_candidate.startswith(root_prefix):
            raise ValueError(f"Path escapes workspace root: {path}")
        return resolved_candidate

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
                files.append(
                    {
                        "path": parts[3],
                        "size": int(parts[1]) if parts[1].isdigit() else 0,
                        "is_dir": parts[2] == "d",
                    }
                )
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
                logger.debug(
                    "Failed to stop container %s before removal",
                    sandbox_id[:12],
                    exc_info=True,
                )
            await asyncio.to_thread(container.remove, force=True)
            logger.info("Destroyed sandbox: %s", sandbox_id[:12])

        # Remove the named workspace volume so data doesn't persist indefinitely
        volume_name = self._workspace_volume_name(sandbox_id)
        try:
            client = self._docker()
            volume = await asyncio.to_thread(client.volumes.get, volume_name)
            await asyncio.to_thread(volume.remove)
            logger.info("Removed workspace volume: %s", volume_name)
        except docker.errors.NotFound:
            pass
        except Exception:
            logger.debug("Failed to remove workspace volume %s", volume_name, exc_info=True)

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
            results.append(
                SandboxInfo(
                    id=sid,
                    name=c.name,
                    status=status_map.get(c.status, SandboxStatus.FAILED),
                    config=SandboxConfig(),
                    container_id=c.id,
                )
            )
        return results

    def _find_container(self, sandbox_id: str) -> docker.models.containers.Container | None:
        client = self._docker()
        containers = client.containers.list(
            all=True,
            filters={"label": f"{LABEL_PREFIX}.id={sandbox_id}"},
        )
        return containers[0] if containers else None
