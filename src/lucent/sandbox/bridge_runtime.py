"""Shared configuration for the sandbox MCP bridge sidecar."""

from __future__ import annotations

import base64
import os
from pathlib import Path

from lucent.sandbox.models import SandboxConfig

BRIDGE_IMAGE_ENV = "LUCENT_SANDBOX_BRIDGE_IMAGE"
DEFAULT_BRIDGE_IMAGE = "lucent-sandbox:base"

_BRIDGE_SOURCE = Path(__file__).with_name("mcp_bridge.py").read_bytes()
_BRIDGE_SOURCE_B64 = base64.b64encode(_BRIDGE_SOURCE).decode("ascii")
_BRIDGE_ENV_KEYS = frozenset({
    "LUCENT_API_URL",
    "LUCENT_SANDBOX_MCP_API_KEY",
    "LUCENT_SANDBOX_TASK_ID",
    "LUCENT_SANDBOX_MCP_LOG_LEVEL",
})
_PRIMARY_BRIDGE_ENV_KEYS = _BRIDGE_ENV_KEYS | {
    "LUCENT_SANDBOX_MCP_ENABLED",
    "LUCENT_SANDBOX_MCP_PORT",
}

_BRIDGE_COMMAND = (
    "import base64, os; "
    "source = base64.b64decode(os.environ['LUCENT_MCP_BRIDGE_SOURCE_B64']).decode('utf-8'); "
    "exec(compile(source, 'lucent_mcp_bridge.py', 'exec'), {'__name__': '__main__'})"
)


def mcp_enabled(config: SandboxConfig) -> bool:
    """Return whether Lucent has enabled the trusted bridge sidecar."""
    return (
        config.env_vars.get("LUCENT_SANDBOX_MCP_ENABLED") == "1"
        and bool(config.env_vars.get("LUCENT_SANDBOX_MCP_API_KEY"))
    )


def bridge_image() -> str:
    """Return the operator-configured, Python-capable bridge image."""
    image = os.environ.get(BRIDGE_IMAGE_ENV, DEFAULT_BRIDGE_IMAGE).strip()
    return image or DEFAULT_BRIDGE_IMAGE


def bridge_port(config: SandboxConfig) -> int:
    return int(config.mcp_bridge_port or 8765)


def bridge_env(config: SandboxConfig) -> dict[str, str]:
    """Return only bridge credentials and settings for the trusted sidecar."""
    return {
        **{key: config.env_vars[key] for key in _BRIDGE_ENV_KEYS if key in config.env_vars},
        "LUCENT_SANDBOX_MCP_PORT": str(bridge_port(config)),
        "LUCENT_MCP_BRIDGE_SOURCE_B64": _BRIDGE_SOURCE_B64,
    }


def primary_env(config: SandboxConfig) -> dict[str, str]:
    """Return user-container environment with bridge credentials removed."""
    environment = {
        key: value
        for key, value in config.env_vars.items()
        if key not in _PRIMARY_BRIDGE_ENV_KEYS
    }
    if mcp_enabled(config):
        environment["LUCENT_SANDBOX_MCP_ENABLED"] = "1"
    return environment


def bridge_command() -> list[str]:
    """Return an argv command that loads the trusted in-memory bridge source."""
    return ["python3", "-c", _BRIDGE_COMMAND]
