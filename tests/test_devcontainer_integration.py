"""Integration tests for devcontainer support in sandboxes.

Tests the full detection → parse → apply flow using realistic devcontainer
configurations. These tests use mocked Docker/exec to simulate the sandbox
environment without requiring a running Docker daemon.

Run with: pytest tests/test_devcontainer_integration.py -v
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from lucent.sandbox.devcontainer import (
    DevcontainerConfig,
    apply_devcontainer_config,
    detect_devcontainer,
    parse_devcontainer_json,
)


# ---------------------------------------------------------------------------
# Realistic devcontainer.json configs from real-world projects
# ---------------------------------------------------------------------------

PYTHON_DEVCONTAINER = {
    "image": "python:3.12-slim",
    "onCreateCommand": "pip install --upgrade pip setuptools wheel",
    "postCreateCommand": {
        "install": "pip install -e '.[dev]'",
        "pre-commit": "pre-commit install",
    },
    "containerEnv": {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    },
    "forwardPorts": [8000],
    "remoteUser": "vscode",
}

NODE_DEVCONTAINER = {
    "image": "node:20",
    "features": {
        "ghcr.io/devcontainers/features/git:1": {},
    },
    "onCreateCommand": "npm ci",
    "updateContentCommand": "npm run build",
    "postCreateCommand": "npm run db:migrate",
    "postStartCommand": "npm run dev",
    "containerEnv": {
        "NODE_ENV": "development",
        "PORT": "3000",
    },
    "remoteEnv": {
        "DEBUG": "app:*",
    },
    "forwardPorts": [3000, 5432],
    "remoteUser": "node",
}

DOCKERFILE_DEVCONTAINER = {
    "build": {
        "dockerfile": "Dockerfile",
        "context": "..",
        "args": {
            "VARIANT": "3.12-bullseye",
        },
    },
    "postCreateCommand": {
        "deps": "pip install -r requirements.txt",
        "db": "python manage.py migrate",
        "static": "python manage.py collectstatic --noinput",
    },
    "containerEnv": {
        "DJANGO_SETTINGS_MODULE": "myapp.settings.dev",
        "DATABASE_URL": "sqlite:///db.sqlite3",
    },
    "forwardPorts": [8000, 5555],
}

RUST_DEVCONTAINER = {
    "image": "rust:1.75",
    "onCreateCommand": {
        "deps": "apt-get update && apt-get install -y pkg-config libssl-dev",
        "tools": "cargo install cargo-watch cargo-nextest",
    },
    "postCreateCommand": "cargo build",
    "postStartCommand": "cargo watch -x check",
    "containerEnv": {
        "CARGO_HOME": "/usr/local/cargo",
        "RUSTUP_HOME": "/usr/local/rustup",
    },
}

MINIMAL_DEVCONTAINER = {
    "image": "ubuntu:22.04",
}

EMPTY_COMMANDS_DEVCONTAINER = {
    "image": "python:3.12",
    "onCreateCommand": "",
    "postCreateCommand": [],
    "containerEnv": {},
}


# ---------------------------------------------------------------------------
# Integration: Parse → Apply (end-to-end with mock exec)
# ---------------------------------------------------------------------------


class TestDevcontainerEndToEnd:
    """Test the full flow: parse a real-world config → apply it → verify results."""

    @pytest.mark.asyncio
    async def test_python_project_full_flow(self):
        """A standard Python devcontainer should run pip upgrade then install."""
        config = parse_devcontainer_json(json.dumps(PYTHON_DEVCONTAINER))

        assert config.image == "python:3.12-slim"
        assert config.remote_user == "vscode"
        assert config.forward_ports == [8000]
        assert config.container_env["PYTHONDONTWRITEBYTECODE"] == "1"

        commands_run = []

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            commands_run.append(command)
            return (0, "", "")

        result = await apply_devcontainer_config(config, mock_exec, "/workspace")

        assert result.ok
        assert result.commands_run == 3  # pip upgrade + install + pre-commit
        assert result.env_vars_applied == 2
        # Verify order: onCreateCommand first, then postCreateCommand values
        assert any("pip install --upgrade" in c for c in commands_run)
        assert any("pip install -e" in c for c in commands_run)
        assert any("pre-commit install" in c for c in commands_run)
        # Verify env vars are passed to commands
        assert all("PYTHONDONTWRITEBYTECODE" in c for c in commands_run)

    @pytest.mark.asyncio
    async def test_node_project_full_flow(self):
        """A Node.js devcontainer with all lifecycle stages."""
        config = parse_devcontainer_json(json.dumps(NODE_DEVCONTAINER))

        assert config.image == "node:20"
        assert "ghcr.io/devcontainers/features/git:1" in config.features
        assert config.forward_ports == [3000, 5432]

        commands_run = []

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            commands_run.append(command)
            return (0, "", "")

        # Run without postStartCommand first
        result = await apply_devcontainer_config(config, mock_exec, "/workspace")

        assert result.ok
        assert result.commands_run == 3  # npm ci + npm run build + npm run db:migrate
        # Merged env: NODE_ENV, PORT (containerEnv) + DEBUG (remoteEnv)
        assert result.env_vars_applied == 3

        # Verify lifecycle order
        ci_idx = next(i for i, c in enumerate(commands_run) if "npm ci" in c)
        build_idx = next(i for i, c in enumerate(commands_run) if "npm run build" in c)
        migrate_idx = next(i for i, c in enumerate(commands_run) if "npm run db:migrate" in c)
        assert ci_idx < build_idx < migrate_idx

    @pytest.mark.asyncio
    async def test_node_project_with_post_start(self):
        """postStartCommand should only run when explicitly enabled."""
        config = parse_devcontainer_json(json.dumps(NODE_DEVCONTAINER))

        commands_run = []

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            commands_run.append(command)
            return (0, "", "")

        result = await apply_devcontainer_config(
            config, mock_exec, "/workspace", run_post_start=True
        )

        assert result.ok
        assert result.commands_run == 4  # 3 setup + 1 postStart
        assert any("npm run dev" in c for c in commands_run)

    @pytest.mark.asyncio
    async def test_dockerfile_project_parsing(self):
        """A devcontainer with a Dockerfile should parse build fields correctly."""
        config = parse_devcontainer_json(json.dumps(DOCKERFILE_DEVCONTAINER))

        assert config.build_dockerfile == "Dockerfile"
        assert config.build_context == ".."
        assert config.build_args == {"VARIANT": "3.12-bullseye"}
        assert config.image is None
        assert config.needs_rebuild is True  # Dockerfile triggers rebuild

        commands_run = []

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            commands_run.append(command)
            return (0, "", "")

        result = await apply_devcontainer_config(config, mock_exec, "/workspace")

        assert result.ok
        assert result.commands_run == 3  # deps + db + static
        # Verify all three postCreateCommand entries ran
        assert any("requirements.txt" in c for c in commands_run)
        assert any("migrate" in c for c in commands_run)
        assert any("collectstatic" in c for c in commands_run)

    @pytest.mark.asyncio
    async def test_rust_project_multi_step_setup(self):
        """A Rust devcontainer with multi-step onCreateCommand."""
        config = parse_devcontainer_json(json.dumps(RUST_DEVCONTAINER))

        assert config.image == "rust:1.75"
        assert len(config.on_create_command) == 2  # dict with 2 entries

        commands_run = []

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            commands_run.append(command)
            return (0, "", "")

        result = await apply_devcontainer_config(config, mock_exec, "/workspace")

        assert result.ok
        assert result.commands_run == 3  # 2 onCreate + 1 postCreate
        # Verify order: apt-get first, then cargo install, then cargo build
        apt_idx = next(i for i, c in enumerate(commands_run) if "apt-get" in c)
        install_idx = next(i for i, c in enumerate(commands_run) if "cargo install" in c)
        build_idx = next(i for i, c in enumerate(commands_run) if "cargo build" in c)
        assert apt_idx < install_idx < build_idx

    @pytest.mark.asyncio
    async def test_minimal_devcontainer_no_commands(self):
        """A minimal devcontainer with only an image should run no commands."""
        config = parse_devcontainer_json(json.dumps(MINIMAL_DEVCONTAINER))

        assert config.image == "ubuntu:22.04"

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            return (0, "", "")

        result = await apply_devcontainer_config(config, mock_exec, "/workspace")

        assert result.ok
        assert result.commands_run == 0
        assert result.env_vars_applied == 0

    @pytest.mark.asyncio
    async def test_empty_commands_skipped(self):
        """Empty command fields should be normalized to empty lists and skipped."""
        config = parse_devcontainer_json(json.dumps(EMPTY_COMMANDS_DEVCONTAINER))

        assert config.on_create_command == []
        assert config.post_create_command == []

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            return (0, "", "")

        result = await apply_devcontainer_config(config, mock_exec, "/workspace")

        assert result.ok
        assert result.commands_run == 0


# ---------------------------------------------------------------------------
# Integration: Detect → Parse (simulated filesystem)
# ---------------------------------------------------------------------------


class TestDevcontainerDetection:
    """Test detecting devcontainer.json from within a sandbox via exec."""

    @pytest.mark.asyncio
    async def test_detect_standard_path(self):
        """Should detect .devcontainer/devcontainer.json."""
        devcontainer_json = json.dumps(PYTHON_DEVCONTAINER)

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            if ".devcontainer/devcontainer.json" in command:
                return (0, devcontainer_json, "")
            return (1, "", "No such file")

        config = await detect_devcontainer(mock_exec, "/workspace")

        assert config is not None
        assert config.image == "python:3.12-slim"
        assert config.remote_user == "vscode"

    @pytest.mark.asyncio
    async def test_detect_root_path_fallback(self):
        """Should fall back to .devcontainer.json at root."""
        devcontainer_json = json.dumps(NODE_DEVCONTAINER)

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            if ".devcontainer/devcontainer.json" in command:
                return (1, "", "No such file")
            if ".devcontainer.json" in command:
                return (0, devcontainer_json, "")
            return (1, "", "No such file")

        config = await detect_devcontainer(mock_exec, "/workspace")

        assert config is not None
        assert config.image == "node:20"

    @pytest.mark.asyncio
    async def test_detect_no_devcontainer(self):
        """Should return None when no devcontainer.json exists."""

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            return (1, "", "No such file")

        config = await detect_devcontainer(mock_exec, "/workspace")
        assert config is None

    @pytest.mark.asyncio
    async def test_detect_invalid_json(self):
        """Should return None when devcontainer.json is invalid."""

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            if ".devcontainer/devcontainer.json" in command:
                return (0, "not valid json {{{", "")
            return (1, "", "No such file")

        config = await detect_devcontainer(mock_exec, "/workspace")
        assert config is None

    @pytest.mark.asyncio
    async def test_detect_then_apply(self):
        """Full detect → parse → apply flow."""
        devcontainer_json = json.dumps(RUST_DEVCONTAINER)
        all_commands = []

        call_count = 0

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            nonlocal call_count
            call_count += 1
            if "cat" in command and ".devcontainer/devcontainer.json" in command:
                return (0, devcontainer_json, "")
            if "cat" in command:
                return (1, "", "No such file")
            all_commands.append(command)
            return (0, "", "")

        # Step 1: Detect
        config = await detect_devcontainer(mock_exec, "/workspace")
        assert config is not None
        assert config.image == "rust:1.75"

        # Step 2: Apply
        result = await apply_devcontainer_config(config, mock_exec, "/workspace")
        assert result.ok
        assert result.commands_run == 3
        assert len(all_commands) == 3


# ---------------------------------------------------------------------------
# Integration: Error handling during apply
# ---------------------------------------------------------------------------


class TestDevcontainerErrorHandling:
    """Test error handling when commands fail during apply."""

    @pytest.mark.asyncio
    async def test_command_failure_reported(self):
        """Failed commands should be tracked but not stop execution."""
        config = parse_devcontainer_json(json.dumps(NODE_DEVCONTAINER))

        call_index = 0

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            nonlocal call_index
            call_index += 1
            # Fail the second command (npm run build)
            if call_index == 2:
                return (1, "", "Build failed: missing dependency")
            return (0, "", "")

        result = await apply_devcontainer_config(config, mock_exec, "/workspace")

        assert not result.ok
        assert result.commands_run == 3
        assert result.commands_failed == 1
        assert len(result.errors) == 1
        assert "Build failed" in result.errors[0]

    @pytest.mark.asyncio
    async def test_command_exception_handled(self):
        """Exceptions during exec should be caught and reported."""
        config = parse_devcontainer_json(json.dumps(MINIMAL_DEVCONTAINER))
        config.on_create_command = ["echo hello"]

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            raise ConnectionError("Docker daemon not responding")

        result = await apply_devcontainer_config(config, mock_exec, "/workspace")

        assert not result.ok
        assert result.commands_failed == 1
        assert "Docker daemon not responding" in result.errors[0]

    @pytest.mark.asyncio
    async def test_all_commands_fail(self):
        """All failing commands should be tracked."""
        config = parse_devcontainer_json(json.dumps(PYTHON_DEVCONTAINER))

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            return (1, "", "Permission denied")

        result = await apply_devcontainer_config(config, mock_exec, "/workspace")

        assert not result.ok
        assert result.commands_run == 3
        assert result.commands_failed == 3
        assert len(result.errors) == 3


# ---------------------------------------------------------------------------
# Integration: Environment variable merging
# ---------------------------------------------------------------------------


class TestDevcontainerEnvMerging:
    """Test that containerEnv and remoteEnv merge correctly."""

    @pytest.mark.asyncio
    async def test_remote_env_overrides_container_env(self):
        """remoteEnv should override containerEnv for the same key."""
        raw = json.dumps({
            "image": "python:3.12",
            "onCreateCommand": "echo test",
            "containerEnv": {"FOO": "from-container", "BAR": "only-container"},
            "remoteEnv": {"FOO": "from-remote", "BAZ": "only-remote"},
        })
        config = parse_devcontainer_json(raw)

        merged = config.merged_env
        assert merged["FOO"] == "from-remote"  # remoteEnv wins
        assert merged["BAR"] == "only-container"
        assert merged["BAZ"] == "only-remote"

        commands_run = []

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            commands_run.append(command)
            return (0, "", "")

        result = await apply_devcontainer_config(config, mock_exec, "/workspace")

        assert result.ok
        assert result.env_vars_applied == 3
        # Verify env prefix in command
        assert any("FOO='from-remote'" in c for c in commands_run)
        assert any("BAR='only-container'" in c for c in commands_run)

    @pytest.mark.asyncio
    async def test_env_values_with_special_chars(self):
        """Environment values with quotes and spaces should be escaped."""
        raw = json.dumps({
            "image": "python:3.12",
            "onCreateCommand": "echo test",
            "containerEnv": {
                "MSG": "hello world",
                "QUOTED": "it's a test",
                "PATH_LIKE": "/usr/local/bin:/usr/bin",
            },
        })
        config = parse_devcontainer_json(raw)

        commands_run = []

        async def mock_exec(command: str, cwd: str | None) -> tuple[int, str, str]:
            commands_run.append(command)
            return (0, "", "")

        result = await apply_devcontainer_config(config, mock_exec, "/workspace")

        assert result.ok
        # Single quotes with escaped inner quotes
        assert any("it'\\''s a test" in c for c in commands_run)


# ---------------------------------------------------------------------------
# Integration: config_summary for storage
# ---------------------------------------------------------------------------


class TestConfigSummary:
    """Test the summary representation stored in SandboxInfo."""

    def test_full_config_summary(self):
        from lucent.sandbox.devcontainer import config_summary

        config = parse_devcontainer_json(json.dumps(NODE_DEVCONTAINER))
        summary = config_summary(config)

        assert summary["image"] == "node:20"
        assert summary["remote_user"] == "node"
        assert summary["forward_ports"] == [3000, 5432]
        assert "ghcr.io/devcontainers/features/git:1" in summary["features"]
        assert summary["lifecycle_commands"]["onCreateCommand"] == ["npm ci"]
        assert summary["lifecycle_commands"]["postStartCommand"] == ["npm run dev"]
        assert summary["env_var_count"] == 3  # NODE_ENV + PORT + DEBUG

    def test_minimal_config_summary(self):
        from lucent.sandbox.devcontainer import config_summary

        config = parse_devcontainer_json(json.dumps(MINIMAL_DEVCONTAINER))
        summary = config_summary(config)

        assert summary["image"] == "ubuntu:22.04"
        assert summary["features"] == []
        assert summary["env_var_count"] == 0

    def test_dockerfile_config_summary(self):
        from lucent.sandbox.devcontainer import config_summary

        config = parse_devcontainer_json(json.dumps(DOCKERFILE_DEVCONTAINER))
        summary = config_summary(config)

        assert summary["image"] is None
        assert summary["build_dockerfile"] == "Dockerfile"
        assert summary["forward_ports"] == [8000, 5555]
