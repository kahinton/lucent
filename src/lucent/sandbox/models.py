"""Data models for sandbox management."""

from __future__ import annotations

import enum
import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


def validate_extra_hosts(extra_hosts: dict[str, str]) -> dict[str, str]:
    """Validate Docker ``--add-host`` entries and return a normalized copy."""
    if not isinstance(extra_hosts, dict):
        raise ValueError("Extra hosts must be a hostname-to-address mapping")
    if len(extra_hosts) > 20:
        raise ValueError("At most 20 extra host mappings are allowed")

    validated: dict[str, str] = {}
    for hostname, destination in extra_hosts.items():
        if not isinstance(hostname, str) or not _HOSTNAME_RE.fullmatch(hostname):
            raise ValueError(f"Invalid extra host name: {hostname!r}")
        if not isinstance(destination, str):
            raise ValueError(f"Invalid destination for extra host {hostname!r}")
        destination = destination.strip()
        if destination != "host-gateway":
            try:
                ipaddress.ip_address(destination)
            except ValueError as exc:
                raise ValueError(
                    f"Extra host {hostname!r} must map to an IP address or host-gateway"
                ) from exc
        validated[hostname] = destination
    return validated


class SandboxStatus(str, enum.Enum):
    CREATING = "creating"
    READY = "ready"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    DESTROYED = "destroyed"


@dataclass
class SandboxConfig:
    """Configuration for creating a sandbox environment."""

    # Identity
    name: str | None = None  # Auto-generated if not set

    # Image / environment
    image: str = "lucent-sandbox:base"  # Docker image
    dockerfile: str | None = None  # Build from Dockerfile instead

    # Repository (optional — not all sandboxes need a repo)
    repo_url: str | None = None
    branch: str | None = None
    git_credentials: str | None = None  # Token for private repos
    # Seconds before credential is considered expired (0 = no expiry)
    git_credentials_ttl: int = 3600

    # Setup
    setup_commands: list[str] = field(default_factory=list)  # Run after container start
    env_vars: dict[str, str] = field(default_factory=dict)
    working_dir: str = "/workspace"
    docker_bind_mounts: list[dict[str, str | bool]] = field(default_factory=list)

    # Resources
    memory_limit: str = "2g"
    cpu_limit: float = 2.0
    disk_limit: str = "10g"

    # Network
    network_mode: str = "none"  # none, allowlist, bridge
    allowed_hosts: list[str] = field(default_factory=list)  # For allowlist mode
    extra_hosts: dict[str, str] = field(default_factory=dict)  # Docker --add-host entries

    # Lifecycle
    timeout_seconds: int = 1800  # Max lifetime (30 min default)
    idle_timeout_seconds: int = 300  # Destroy after idle (5 min default)
    # Reuse an existing compatible sandbox for later, sequential tasks in the
    # same request. Disabled by default for callers that need isolated state.
    reuse_within_request: bool = False
    reuse_key: str | None = None
    reuse_sequence_order: int = 0
    mcp_bridge_port: int = 8765
    output_mode: Literal["diff", "pr", "review", "commit"] | None = None
    commit_approved: bool = False

    # Linking
    task_id: str | None = None
    request_id: str | None = None
    organization_id: str | None = None
    requesting_user_id: str | None = None

    def __post_init__(self) -> None:
        self.extra_hosts = validate_extra_hosts(self.extra_hosts)


@dataclass
class ExecResult:
    """Result of executing a command in a sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = 0
    timed_out: bool = False


@dataclass
class FileInfo:
    """File metadata from sandbox filesystem."""

    path: str
    size: int
    is_dir: bool
    modified_at: datetime | None = None


@dataclass
class SandboxInfo:
    """Full state of a sandbox instance."""

    id: str
    name: str
    status: SandboxStatus
    config: SandboxConfig
    container_id: str | None = None
    created_at: datetime | None = None
    ready_at: datetime | None = None
    stopped_at: datetime | None = None
    host: str | None = None  # For network access (MCP bridge, etc.)
    port: int | None = None
    error: str | None = None
    devcontainer: dict | None = None
