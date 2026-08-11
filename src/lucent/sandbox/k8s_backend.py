"""Kubernetes backend for sandbox execution."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import shlex
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from lucent.sandbox.backend import SandboxBackend
from lucent.sandbox.models import ExecResult, SandboxConfig, SandboxInfo, SandboxStatus

LABEL_MANAGED = "io.lucent.sandbox.managed"
LABEL_ID = "io.lucent.sandbox.id"
_GIT_ASKPASS_PATH = "/tmp/lucent-git-askpass.sh"
logger = logging.getLogger(__name__)


def _k8s_memory(value: str) -> str:
    suffixes = {"m": "Mi", "g": "Gi", "t": "Ti"}
    value = value.strip()
    return f"{value[:-1]}{suffixes[value[-1].lower()]}" if value[-1:].lower() in suffixes else value


class KubernetesBackend(SandboxBackend):
    """Runs sandboxes as long-lived Kubernetes pods."""

    def __init__(self, namespace: str = "lucent-sandboxes", kubeconfig: str | None = None):
        self._namespace = namespace
        self._kubeconfig = kubeconfig
        self._core = None
        self._networking = None
        self._configs: dict[str, SandboxConfig] = {}

    def _clients(self):
        if self._core is not None:
            return self._core, self._networking
        try:
            from kubernetes import client, config
        except ImportError as exc:
            raise RuntimeError(
                "Kubernetes sandbox support requires the 'kubernetes' extra"
            ) from exc
        if self._kubeconfig:
            config.load_kube_config(config_file=self._kubeconfig)
        else:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
        self._core = client.CoreV1Api()
        self._networking = client.NetworkingV1Api()
        return self._core, self._networking

    @staticmethod
    def _pod_name(config: SandboxConfig, sandbox_id: str) -> str:
        base = (config.name or "lucent-sandbox").lower()
        safe = "".join(char if char.isalnum() or char == "-" else "-" for char in base)
        return f"{safe.strip('-')[:40]}-{sandbox_id[:8]}"

    def _pod_manifest(self, sandbox_id: str, name: str, config: SandboxConfig) -> dict:
        labels = {LABEL_MANAGED: "true", LABEL_ID: sandbox_id}
        if config.organization_id:
            labels["io.lucent.organization.id"] = config.organization_id
        resources = {
            "requests": {
                "cpu": str(config.cpu_limit),
                "memory": _k8s_memory(config.memory_limit),
            },
            "limits": {
                "cpu": str(config.cpu_limit),
                "memory": _k8s_memory(config.memory_limit),
            },
        }
        workspace = {
            "name": "workspace",
            "emptyDir": {"sizeLimit": _k8s_memory(config.disk_limit)},
        }
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [
                    {
                        "name": "sandbox",
                        "image": config.image,
                        "command": [
                            "sh",
                            "-c",
                            "trap 'exit 0' TERM INT; while :; do sleep 30; done",
                        ],
                        "workingDir": config.working_dir,
                        "env": [
                            {"name": key, "value": value}
                            for key, value in config.env_vars.items()
                        ],
                        "resources": resources,
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["NET_RAW"]},
                        },
                        "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
                    }
                ],
                "volumes": [workspace],
                "activeDeadlineSeconds": config.timeout_seconds or None,
            },
        }

    async def create(self, config: SandboxConfig) -> SandboxInfo:
        sandbox_id = str(uuid.uuid4())
        name = self._pod_name(config, sandbox_id)
        info = SandboxInfo(
            id=sandbox_id,
            name=name,
            status=SandboxStatus.CREATING,
            config=config,
            created_at=datetime.now(timezone.utc),
        )
        try:
            core, _ = self._clients()
            manifest = self._pod_manifest(sandbox_id, name, config)
            await asyncio.to_thread(core.create_namespaced_pod, self._namespace, manifest)
            self._configs[sandbox_id] = config
            await self._wait_until_ready(name, timeout=120)
            if config.repo_url:
                clone_env = None
                if config.git_credentials and config.repo_url.startswith("https://"):
                    await self._write_git_askpass(sandbox_id)
                    username, token = self._parse_git_credentials(config.git_credentials)
                    clone_env = {
                        "GIT_ASKPASS": _GIT_ASKPASS_PATH,
                        "GIT_TERMINAL_PROMPT": "0",
                        "LUCENT_GIT_USERNAME": username,
                        "LUCENT_GIT_TOKEN": token,
                    }
                result = await self.exec(
                    sandbox_id,
                    self._clone_command(config),
                    env=clone_env,
                    timeout=180,
                )
                if result.exit_code != 0:
                    raise RuntimeError(f"Git clone failed: {result.stderr.strip()}")
            for command in config.setup_commands:
                await self.exec(sandbox_id, command, timeout=300)
            if config.env_vars.get("LUCENT_SANDBOX_MCP_API_KEY"):
                if not await self._start_mcp_bridge(sandbox_id, config):
                    raise RuntimeError("Failed to start sandbox MCP bridge")
            await self._apply_network_policy(sandbox_id, name, config)
            info.status = SandboxStatus.READY
            info.ready_at = datetime.now(timezone.utc)
            info.container_id = name
            return info
        except Exception as exc:
            info.status = SandboxStatus.FAILED
            info.error = str(exc)
            try:
                await self.destroy(sandbox_id)
            except Exception:
                logger.warning(
                    "Failed to clean up Kubernetes sandbox %s", sandbox_id, exc_info=True
                )
            return info

    async def _wait_until_ready(self, name: str, timeout: int) -> None:
        core, _ = self._clients()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pod = await asyncio.to_thread(core.read_namespaced_pod, name, self._namespace)
            phase = pod.status.phase
            sandbox_statuses = [
                status
                for status in (pod.status.container_statuses or [])
                if status.name == "sandbox"
            ]
            if phase == "Running" and sandbox_statuses and sandbox_statuses[0].ready:
                return
            if phase in ("Failed", "Succeeded"):
                raise RuntimeError(f"Sandbox pod entered {phase} phase")
            await asyncio.sleep(1)
        raise TimeoutError("Timed out waiting for sandbox pod")

    @staticmethod
    def _clone_command(config: SandboxConfig) -> str:
        branch = f" --branch {shlex.quote(config.branch)}" if config.branch else ""
        repo_url = shlex.quote(config.repo_url or "")
        return (
            "command -v git >/dev/null 2>&1 || "
            "((command -v apt-get >/dev/null && apt-get update -qq && "
            "apt-get install -y -qq git) || "
            "(command -v apk >/dev/null && apk add --no-cache git)); "
            f"git clone{branch} -- {repo_url} /workspace"
        )

    async def _write_git_askpass(self, sandbox_id: str) -> None:
        script = (
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *Username*) printf "%s\\n" "${LUCENT_GIT_USERNAME:-x-access-token}" ;;\n'
            '  *Password*) printf "%s\\n" "${LUCENT_GIT_TOKEN:-}" ;;\n'
            '  *) printf "\\n" ;;\n'
            "esac\n"
        )
        await self.write_file(sandbox_id, _GIT_ASKPASS_PATH, script.encode())
        await self.exec(sandbox_id, ["chmod", "700", _GIT_ASKPASS_PATH])

    @staticmethod
    def _parse_git_credentials(credentials: str) -> tuple[str, str]:
        if ":" in credentials:
            username, token = credentials.split(":", 1)
            if username and token:
                return username, token
        return "x-access-token", credentials

    async def _start_mcp_bridge(self, sandbox_id: str, config: SandboxConfig) -> bool:
        source = Path(__file__).with_name("mcp_bridge.py").read_bytes()
        bridge_path = "/tmp/lucent_mcp_bridge.py"
        await self.write_file(sandbox_id, bridge_path, source)
        port = int(config.mcp_bridge_port or 8765)
        result = await self.exec(
            sandbox_id,
            f"python {shlex.quote(bridge_path)} --host 127.0.0.1 --port {port} "
            ">/tmp/lucent-mcp-bridge.log 2>&1 &",
            timeout=10,
        )
        if result.exit_code != 0:
            return False
        probe_command = (
            "python -c \"import urllib.request,sys;"
            f"r=urllib.request.urlopen('http://127.0.0.1:{port}/health',timeout=5);"
            "sys.exit(0 if r.status==200 else 1)\""
        )
        for _ in range(5):
            await asyncio.sleep(1)
            if (await self.exec(sandbox_id, probe_command, timeout=10)).exit_code == 0:
                return True
        return False

    async def _apply_network_policy(
        self, sandbox_id: str, name: str, config: SandboxConfig
    ) -> None:
        if config.network_mode == "bridge":
            return
        _, networking = self._clients()
        egress: list[dict] = []
        allowed_hosts = list(config.allowed_hosts) if config.network_mode == "allowlist" else []
        api_url = config.env_vars.get("LUCENT_API_URL")
        api_host = urlsplit(api_url).hostname if api_url else None
        if api_host:
            allowed_hosts.append(api_host)
        if allowed_hosts:
            egress.append(
                {
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ]
                }
            )
            ip_blocks: list[dict] = []
            for host in allowed_hosts:
                try:
                    addresses = [str(ipaddress.ip_address(host))]
                except ValueError:
                    addresses = list({item[4][0] for item in socket.getaddrinfo(host, None)})
                ip_blocks.extend(
                    {"ipBlock": {"cidr": f"{address}/{'32' if ':' not in address else '128'}"}}
                    for address in addresses
                )
            if ip_blocks:
                egress.append({"to": ip_blocks})
        policy = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": name},
            "spec": {
                "podSelector": {"matchLabels": {LABEL_ID: sandbox_id}},
                "policyTypes": ["Egress"],
                "egress": egress,
            },
        }
        await asyncio.to_thread(
            networking.create_namespaced_network_policy, self._namespace, policy
        )

    def _pod_for_id(self, sandbox_id: str):
        core, _ = self._clients()
        pods = core.list_namespaced_pod(
            self._namespace, label_selector=f"{LABEL_ID}={sandbox_id}"
        ).items
        return pods[0] if pods else None

    async def exec(
        self,
        sandbox_id: str,
        command: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> ExecResult:
        started = time.monotonic()
        pod = await asyncio.to_thread(self._pod_for_id, sandbox_id)
        if pod is None:
            return ExecResult(1, "", "Sandbox pod not found")
        shell_command = command if isinstance(command, str) else shlex.join(command)
        if cwd:
            shell_command = f"cd {shlex.quote(cwd)} && {shell_command}"
        if env:
            assignments = " ".join(
                f"{key}={shlex.quote(value)}" for key, value in env.items()
            )
            shell_command = f"export {assignments}; {shell_command}"

        def run_exec() -> tuple[str, str, int]:
            from kubernetes.stream import stream

            core, _ = self._clients()
            response = stream(
                core.connect_get_namespaced_pod_exec,
                pod.metadata.name,
                self._namespace,
                command=["sh", "-c", shell_command],
                container="sandbox",
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
            response.run_forever(timeout=timeout)
            stdout = response.read_stdout()
            stderr = response.read_stderr()
            exit_code = int(response.returncode or 0)
            response.close()
            return stdout, stderr, exit_code

        try:
            stdout, stderr, exit_code = await asyncio.wait_for(
                asyncio.to_thread(run_exec), timeout=timeout + 5
            )
            return ExecResult(
                exit_code, stdout, stderr, int((time.monotonic() - started) * 1000)
            )
        except TimeoutError:
            return ExecResult(
                124, "", f"Command timed out after {timeout}s", timeout * 1000, timed_out=True
            )

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        result = await self.exec(sandbox_id, ["base64", path])
        if result.exit_code != 0:
            raise FileNotFoundError(result.stderr or path)
        return base64.b64decode(result.stdout)

    async def write_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        encoded = base64.b64encode(content).decode()
        command = f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}"
        result = await self.exec(sandbox_id, command)
        if result.exit_code != 0:
            raise OSError(result.stderr)

    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> list[dict]:
        command = (
            f"find {shlex.quote(path)} -mindepth 1 -maxdepth 1 "
            "-printf '%f\\t%y\\t%s\\n'"
        )
        result = await self.exec(sandbox_id, command)
        if result.exit_code != 0:
            raise OSError(result.stderr)
        files = []
        for line in result.stdout.splitlines():
            name, file_type, size = line.split("\t", 2)
            files.append(
                {
                    "name": name,
                    "type": "directory" if file_type == "d" else "file",
                    "size": int(size),
                }
            )
        return files

    async def get(self, sandbox_id: str) -> SandboxInfo | None:
        pod = await asyncio.to_thread(self._pod_for_id, sandbox_id)
        if pod is None:
            return None
        status_map = {
            "Pending": SandboxStatus.CREATING,
            "Running": SandboxStatus.READY,
            "Succeeded": SandboxStatus.STOPPED,
            "Failed": SandboxStatus.FAILED,
        }
        return SandboxInfo(
            id=sandbox_id,
            name=pod.metadata.name,
            status=status_map.get(pod.status.phase, SandboxStatus.FAILED),
            config=self._configs.get(sandbox_id, SandboxConfig()),
            container_id=pod.metadata.name,
            created_at=pod.metadata.creation_timestamp,
        )

    async def stop(self, sandbox_id: str) -> None:
        await self.destroy(sandbox_id)

    async def destroy(self, sandbox_id: str) -> None:
        pod = await asyncio.to_thread(self._pod_for_id, sandbox_id)
        if pod is None:
            return
        core, networking = self._clients()
        await asyncio.to_thread(
            core.delete_namespaced_pod, pod.metadata.name, self._namespace
        )
        try:
            await asyncio.to_thread(
                networking.delete_namespaced_network_policy,
                pod.metadata.name,
                self._namespace,
            )
        except Exception:
            pass
        self._configs.pop(sandbox_id, None)

    async def list_all(self) -> list[SandboxInfo]:
        core, _ = self._clients()
        pods = await asyncio.to_thread(
            core.list_namespaced_pod,
            self._namespace,
            label_selector=f"{LABEL_MANAGED}=true",
        )
        results = []
        for pod in pods.items:
            sandbox_id = pod.metadata.labels.get(LABEL_ID)
            if sandbox_id:
                info = await self.get(sandbox_id)
                if info:
                    results.append(info)
        return results
