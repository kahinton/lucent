"""Data models for sandbox management."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


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
    git_credentials_ttl: int = 3600  # Seconds before credential is considered expired (0 = no expiry)

    # Setup
    setup_commands: list[str] = field(default_factory=list)  # Run after container start
    env_vars: dict[str, str] = field(default_factory=dict)
    working_dir: str = "/workspace"

    # Resources
    memory_limit: str = "2g"
    cpu_limit: float = 2.0
    disk_limit: str = "10g"

    # Network
    network_mode: str = "none"  # none, allowlist, bridge
    allowed_hosts: list[str] = field(default_factory=list)  # For allowlist mode

    # Lifecycle
    timeout_seconds: int = 1800  # Max lifetime (30 min default)
    idle_timeout_seconds: int = 300  # Destroy after idle (5 min default)
    mcp_bridge_port: int = 8765
    output_mode: Literal["diff", "pr", "review", "commit"] | None = None
    commit_approved: bool = False

    # Linking
    task_id: str | None = None
    request_id: str | None = None
    organization_id: str | None = None


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
