"""Devcontainer.json detection and parsing for sandbox environments.

Supports the devcontainer spec fields most relevant to sandbox execution:
lifecycle commands, environment variables, image/Dockerfile overrides,
forward ports, and remote user.

Reference: https://containers.dev/implementors/json_reference/
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Type alias for the exec callback used by detect_devcontainer.
# Signature: (command: str, cwd: str | None) -> (exit_code, stdout, stderr)
ExecFn = Callable[[str, str | None], Awaitable[tuple[int, str, str]]]

# Paths checked in priority order (per the devcontainer spec).
DEVCONTAINER_PATHS = [
    ".devcontainer/devcontainer.json",
    ".devcontainer.json",
]


def _normalize_command(value: Any) -> list[str]:
    """Normalize a devcontainer command field to a list of shell strings.

    The spec allows three formats:
      - string: "npm install"
      - list:   ["npm", "install"]  (executed as single command)
      - dict:   {"setup": "npm install", "db": "initdb"}  (values run in order)
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        # Join list items into a single shell command
        cmd = " ".join(str(v) for v in value if v)
        return [cmd] if cmd.strip() else []
    if isinstance(value, dict):
        # Dict values are individual commands, run in insertion order
        return [str(v) for v in value.values() if v and str(v).strip()]
    return []


@dataclass
class DevcontainerConfig:
    """Parsed representation of a devcontainer.json file."""

    # Image / build
    image: str | None = None
    build_dockerfile: str | None = None
    build_context: str | None = None
    build_args: dict[str, str] = field(default_factory=dict)

    # Features (stored for reference; individual feature install is not yet supported)
    features: dict[str, Any] = field(default_factory=dict)

    # Lifecycle commands
    on_create_command: list[str] = field(default_factory=list)
    post_create_command: list[str] = field(default_factory=list)
    update_content_command: list[str] = field(default_factory=list)
    post_start_command: list[str] = field(default_factory=list)
    post_attach_command: list[str] = field(default_factory=list)

    # Environment
    container_env: dict[str, str] = field(default_factory=dict)
    remote_env: dict[str, str] = field(default_factory=dict)

    # Networking
    forward_ports: list[int] = field(default_factory=list)

    # User
    remote_user: str | None = None

    # Raw JSON for reference
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_rebuild(self) -> bool:
        """True if the devcontainer specifies a custom image or Dockerfile."""
        return bool(self.image or self.build_dockerfile)

    @property
    def all_setup_commands(self) -> list[str]:
        """Return lifecycle commands in devcontainer-specified order.

        Order: onCreateCommand → updateContentCommand → postCreateCommand
        (postStartCommand runs separately after sandbox is READY.)
        """
        return self.on_create_command + self.update_content_command + self.post_create_command

    @property
    def merged_env(self) -> dict[str, str]:
        """Return containerEnv overlaid with remoteEnv."""
        merged = dict(self.container_env)
        merged.update(self.remote_env)
        return merged


def parse_devcontainer_json(raw_json: str) -> DevcontainerConfig:
    """Parse a devcontainer.json string into a DevcontainerConfig.

    Raises ValueError if the JSON is invalid or not an object.
    Tolerates missing/unknown fields gracefully.
    """
    data = json.loads(raw_json)
    if not isinstance(data, dict):
        raise ValueError("devcontainer.json root must be a JSON object")

    build = data.get("build", {}) or {}

    return DevcontainerConfig(
        image=data.get("image"),
        build_dockerfile=build.get("dockerfile") or data.get("dockerFile"),
        build_context=build.get("context"),
        build_args=build.get("args") or {},
        features=data.get("features") or {},
        on_create_command=_normalize_command(data.get("onCreateCommand")),
        post_create_command=_normalize_command(data.get("postCreateCommand")),
        update_content_command=_normalize_command(data.get("updateContentCommand")),
        post_start_command=_normalize_command(data.get("postStartCommand")),
        post_attach_command=_normalize_command(data.get("postAttachCommand")),
        container_env=data.get("containerEnv") or {},
        remote_env=data.get("remoteEnv") or {},
        forward_ports=[int(p) for p in (data.get("forwardPorts") or []) if _is_port(p)],
        remote_user=data.get("remoteUser"),
        raw=data,
    )


def _is_port(value: Any) -> bool:
    """Check if a value is a valid port number."""
    try:
        return 1 <= int(value) <= 65535
    except (TypeError, ValueError):
        return False


async def detect_devcontainer(
    exec_fn: ExecFn,
    working_dir: str = "/workspace",
) -> DevcontainerConfig | None:
    """Detect and parse a devcontainer.json inside a running sandbox.

    Uses exec_fn to run commands inside the container (avoids coupling
    to a specific backend). Returns None if no devcontainer.json is found
    or if parsing fails.

    Args:
        exec_fn: Async callback (command, cwd) → (exit_code, stdout, stderr)
        working_dir: Directory where the repo was cloned.
    """
    for path in DEVCONTAINER_PATHS:
        full_path = f"{working_dir}/{path}"
        exit_code, stdout, stderr = await exec_fn(f"cat {full_path}", working_dir)
        if exit_code == 0 and stdout.strip():
            try:
                config = parse_devcontainer_json(stdout)
                logger.info("Detected devcontainer config at %s", full_path)
                return config
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to parse %s: %s", full_path, e)
                return None

    logger.debug("No devcontainer.json found in %s", working_dir)
    return None


@dataclass
class DevcontainerApplyResult:
    """Result of applying a devcontainer config to a sandbox."""

    commands_run: int = 0
    commands_failed: int = 0
    env_vars_applied: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.commands_failed == 0 and not self.errors


async def apply_devcontainer_config(
    config: DevcontainerConfig,
    exec_fn: ExecFn,
    working_dir: str = "/workspace",
    *,
    run_post_start: bool = False,
) -> DevcontainerApplyResult:
    """Apply a devcontainer config by running lifecycle commands.

    Runs commands in the devcontainer-specified order:
      1. onCreateCommand
      2. updateContentCommand
      3. postCreateCommand
      4. postStartCommand (only if run_post_start=True)

    Environment variables from the config are passed to each command.

    Args:
        config: Parsed devcontainer configuration.
        exec_fn: Async callback (command, cwd) → (exit_code, stdout, stderr)
        working_dir: Working directory for commands.
        run_post_start: If True, also run postStartCommand.
    """
    result = DevcontainerApplyResult()
    env_str = _build_env_prefix(config.merged_env)
    result.env_vars_applied = len(config.merged_env)

    commands = list(config.all_setup_commands)
    if run_post_start:
        commands.extend(config.post_start_command)

    for cmd in commands:
        full_cmd = f"{env_str}{cmd}" if env_str else cmd
        try:
            exit_code, stdout, stderr = await exec_fn(full_cmd, working_dir)
            result.commands_run += 1
            if exit_code != 0:
                result.commands_failed += 1
                error_msg = f"Command failed (exit {exit_code}): {cmd}"
                if stderr.strip():
                    error_msg += f" — {stderr.strip()[:200]}"
                result.errors.append(error_msg)
                logger.warning(
                    "Devcontainer command failed in %s: %s (exit %d)",
                    working_dir, cmd, exit_code,
                )
            else:
                logger.debug("Devcontainer command succeeded: %s", cmd)
        except Exception as e:
            result.commands_run += 1
            result.commands_failed += 1
            result.errors.append(f"Command exception: {cmd} — {e}")
            logger.error("Devcontainer command raised exception: %s — %s", cmd, e)

    return result


def _build_env_prefix(env: dict[str, str]) -> str:
    """Build a shell env prefix like 'FOO=bar BAZ=qux ' for passing env vars."""
    if not env:
        return ""
    parts = []
    for key, value in env.items():
        # Shell-escape the value
        escaped = value.replace("'", "'\\''")
        parts.append(f"{key}='{escaped}'")
    return "env " + " ".join(parts) + " "


def config_summary(config: DevcontainerConfig) -> dict:
    """Return a JSON-serializable summary of a devcontainer config for storage."""
    return {
        "image": config.image,
        "build_dockerfile": config.build_dockerfile,
        "remote_user": config.remote_user,
        "forward_ports": config.forward_ports,
        "features": list(config.features.keys()) if config.features else [],
        "lifecycle_commands": {
            "onCreateCommand": config.on_create_command,
            "postCreateCommand": config.post_create_command,
            "updateContentCommand": config.update_content_command,
            "postStartCommand": config.post_start_command,
        },
        "env_var_count": len(config.merged_env),
    }
