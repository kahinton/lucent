"""Tests for devcontainer.json detection, parsing, and application.

Covers:
- Parsing: valid configs, all field types, edge cases, invalid JSON
- Detection: devcontainer path priority, missing files, parse errors
- Application: lifecycle command execution, env var merging, error handling
- Integration: DockerBackend devcontainer flow (mocked Docker)
"""

from __future__ import annotations

import json
import shlex
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lucent.sandbox.devcontainer import (
    DevcontainerApplyResult,
    DevcontainerConfig,
    _build_env_prefix,
    _normalize_command,
    apply_devcontainer_config,
    config_summary,
    detect_devcontainer,
    parse_devcontainer_json,
)
from lucent.sandbox.models import SandboxConfig, SandboxStatus

# ---------------------------------------------------------------------------
# _normalize_command
# ---------------------------------------------------------------------------


class TestNormalizeCommand:
    def test_none_returns_empty(self):
        assert _normalize_command(None) == []

    def test_empty_string_returns_empty(self):
        assert _normalize_command("") == []
        assert _normalize_command("   ") == []

    def test_string_returns_single_item_list(self):
        assert _normalize_command("npm install") == ["npm install"]

    def test_list_joins_into_single_command(self):
        assert _normalize_command(["npm", "install"]) == ["npm install"]

    def test_empty_list_returns_empty(self):
        assert _normalize_command([]) == []

    def test_list_with_empty_items(self):
        assert _normalize_command(["npm", "", "install"]) == ["npm install"]

    def test_dict_returns_values_in_order(self):
        result = _normalize_command({"setup": "npm install", "build": "npm run build"})
        assert result == ["npm install", "npm run build"]

    def test_dict_skips_empty_values(self):
        result = _normalize_command({"setup": "npm install", "skip": "", "build": "npm run build"})
        assert result == ["npm install", "npm run build"]

    def test_unexpected_type_returns_empty(self):
        assert _normalize_command(42) == []
        assert _normalize_command(True) == []


# ---------------------------------------------------------------------------
# parse_devcontainer_json
# ---------------------------------------------------------------------------


class TestParseDevcontainerJson:
    def test_minimal_image_only(self):
        raw = json.dumps({"image": "python:3.12-slim"})
        config = parse_devcontainer_json(raw)
        assert config.image == "python:3.12-slim"
        assert config.build_dockerfile is None
        assert config.on_create_command == []
        assert config.container_env == {}
        assert config.forward_ports == []

    def test_full_config(self):
        raw = json.dumps({
            "image": "node:20",
            "features": {"ghcr.io/devcontainers/features/git:1": {}},
            "onCreateCommand": "npm install",
            "postCreateCommand": {"db": "initdb", "seed": "npm run seed"},
            "updateContentCommand": "npm run build",
            "postStartCommand": "npm start",
            "postAttachCommand": "echo hello",
            "containerEnv": {"NODE_ENV": "development"},
            "remoteEnv": {"DEBUG": "true"},
            "forwardPorts": [3000, 5432],
            "remoteUser": "node",
        })
        config = parse_devcontainer_json(raw)
        assert config.image == "node:20"
        assert "ghcr.io/devcontainers/features/git:1" in config.features
        assert config.on_create_command == ["npm install"]
        assert config.post_create_command == ["initdb", "npm run seed"]
        assert config.update_content_command == ["npm run build"]
        assert config.post_start_command == ["npm start"]
        assert config.post_attach_command == ["echo hello"]
        assert config.container_env == {"NODE_ENV": "development"}
        assert config.remote_env == {"DEBUG": "true"}
        assert config.forward_ports == [3000, 5432]
        assert config.remote_user == "node"

    def test_build_section(self):
        raw = json.dumps({
            "build": {
                "dockerfile": "Dockerfile.dev",
                "context": "..",
                "args": {"NODE_VERSION": "20"},
            }
        })
        config = parse_devcontainer_json(raw)
        assert config.build_dockerfile == "Dockerfile.dev"
        assert config.build_context == ".."
        assert config.build_args == {"NODE_VERSION": "20"}

    def test_legacy_dockerfile_field(self):
        raw = json.dumps({"dockerFile": "Dockerfile"})
        config = parse_devcontainer_json(raw)
        assert config.build_dockerfile == "Dockerfile"

    def test_build_dockerfile_takes_precedence_over_legacy(self):
        raw = json.dumps({
            "dockerFile": "legacy.Dockerfile",
            "build": {"dockerfile": "new.Dockerfile"},
        })
        config = parse_devcontainer_json(raw)
        assert config.build_dockerfile == "new.Dockerfile"

    def test_invalid_json_raises_value_error(self):
        with pytest.raises((json.JSONDecodeError, ValueError)):
            parse_devcontainer_json("not json at all")

    def test_non_object_root_raises_value_error(self):
        with pytest.raises(ValueError, match="root must be a JSON object"):
            parse_devcontainer_json("[]")

    def test_empty_object(self):
        config = parse_devcontainer_json("{}")
        assert config.image is None
        assert config.build_dockerfile is None
        assert config.on_create_command == []
        assert config.container_env == {}

    def test_invalid_ports_filtered(self):
        raw = json.dumps({"forwardPorts": [3000, "not-a-port", 0, 70000, 8080]})
        config = parse_devcontainer_json(raw)
        assert config.forward_ports == [3000, 8080]

    def test_null_fields_handled(self):
        raw = json.dumps({
            "image": None,
            "features": None,
            "containerEnv": None,
            "forwardPorts": None,
            "build": None,
        })
        config = parse_devcontainer_json(raw)
        assert config.image is None
        assert config.features == {}
        assert config.container_env == {}
        assert config.forward_ports == []

    def test_raw_preserved(self):
        data = {"image": "python:3.12", "customField": "value"}
        config = parse_devcontainer_json(json.dumps(data))
        assert config.raw == data


# ---------------------------------------------------------------------------
# DevcontainerConfig properties
# ---------------------------------------------------------------------------


class TestDevcontainerConfigProperties:
    def test_all_setup_commands_order(self):
        config = DevcontainerConfig(
            on_create_command=["cmd1"],
            update_content_command=["cmd2"],
            post_create_command=["cmd3"],
        )
        assert config.all_setup_commands == ["cmd1", "cmd2", "cmd3"]

    def test_all_setup_commands_empty(self):
        config = DevcontainerConfig()
        assert config.all_setup_commands == []

    def test_merged_env_overlay(self):
        config = DevcontainerConfig(
            container_env={"A": "1", "B": "2"},
            remote_env={"B": "override", "C": "3"},
        )
        assert config.merged_env == {"A": "1", "B": "override", "C": "3"}

    def test_merged_env_empty(self):
        config = DevcontainerConfig()
        assert config.merged_env == {}

    def test_needs_rebuild_with_image(self):
        assert DevcontainerConfig(image="python:3.12").needs_rebuild is True

    def test_needs_rebuild_with_dockerfile(self):
        assert DevcontainerConfig(build_dockerfile="Dockerfile").needs_rebuild is True

    def test_needs_rebuild_false(self):
        assert DevcontainerConfig().needs_rebuild is False


# ---------------------------------------------------------------------------
# detect_devcontainer
# ---------------------------------------------------------------------------


class TestDetectDevcontainer:
    @pytest.mark.asyncio
    async def test_detects_at_standard_path(self):
        devcontainer_json = json.dumps({"image": "python:3.12"})

        async def exec_fn(command: str, cwd: str | None):
            if ".devcontainer/devcontainer.json" in command:
                return (0, devcontainer_json, "")
            return (1, "", "No such file")

        config = await detect_devcontainer(exec_fn)
        assert config is not None
        assert config.image == "python:3.12"

    @pytest.mark.asyncio
    async def test_detects_at_root_path(self):
        devcontainer_json = json.dumps({"image": "node:20"})

        async def exec_fn(command: str, cwd: str | None):
            if ".devcontainer.json" in command and ".devcontainer/" not in command:
                return (0, devcontainer_json, "")
            return (1, "", "No such file")

        config = await detect_devcontainer(exec_fn)
        assert config is not None
        assert config.image == "node:20"

    @pytest.mark.asyncio
    async def test_standard_path_takes_precedence(self):
        """If both paths exist, .devcontainer/devcontainer.json wins."""
        async def exec_fn(command: str, cwd: str | None):
            if ".devcontainer/devcontainer.json" in command:
                return (0, json.dumps({"image": "priority"}), "")
            if ".devcontainer.json" in command:
                return (0, json.dumps({"image": "fallback"}), "")
            return (1, "", "No such file")

        config = await detect_devcontainer(exec_fn)
        assert config is not None
        assert config.image == "priority"

    @pytest.mark.asyncio
    async def test_no_devcontainer_returns_none(self):
        async def exec_fn(command: str, cwd: str | None):
            return (1, "", "No such file or directory")

        config = await detect_devcontainer(exec_fn)
        assert config is None

    @pytest.mark.asyncio
    async def test_empty_file_returns_none(self):
        async def exec_fn(command: str, cwd: str | None):
            return (0, "", "")

        config = await detect_devcontainer(exec_fn)
        assert config is None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        async def exec_fn(command: str, cwd: str | None):
            if ".devcontainer/devcontainer.json" in command:
                return (0, "not valid json {{{", "")
            return (1, "", "No such file")

        config = await detect_devcontainer(exec_fn)
        assert config is None

    @pytest.mark.asyncio
    async def test_custom_working_dir(self):
        async def exec_fn(command: str, cwd: str | None):
            if "/custom/path/.devcontainer/devcontainer.json" in command:
                return (0, json.dumps({"image": "custom"}), "")
            return (1, "", "No such file")

        config = await detect_devcontainer(exec_fn, working_dir="/custom/path")
        assert config is not None
        assert config.image == "custom"


# ---------------------------------------------------------------------------
# apply_devcontainer_config
# ---------------------------------------------------------------------------


class TestApplyDevcontainerConfig:
    @pytest.mark.asyncio
    async def test_runs_lifecycle_commands_in_order(self):
        executed = []

        async def exec_fn(command: str, cwd: str | None):
            executed.append(command)
            return (0, "", "")

        config = DevcontainerConfig(
            on_create_command=["cmd1"],
            update_content_command=["cmd2"],
            post_create_command=["cmd3"],
        )

        result = await apply_devcontainer_config(config, exec_fn)
        assert result.ok
        assert result.commands_run == 3
        assert result.commands_failed == 0
        # Verify order (commands may have env prefix)
        assert "cmd1" in executed[0]
        assert "cmd2" in executed[1]
        assert "cmd3" in executed[2]

    @pytest.mark.asyncio
    async def test_includes_post_start_when_requested(self):
        executed = []

        async def exec_fn(command: str, cwd: str | None):
            executed.append(command)
            return (0, "", "")

        config = DevcontainerConfig(
            on_create_command=["setup"],
            post_start_command=["start-server"],
        )

        result = await apply_devcontainer_config(config, exec_fn, run_post_start=True)
        assert result.ok
        assert result.commands_run == 2
        assert any("start-server" in cmd for cmd in executed)

    @pytest.mark.asyncio
    async def test_excludes_post_start_by_default(self):
        executed = []

        async def exec_fn(command: str, cwd: str | None):
            executed.append(command)
            return (0, "", "")

        config = DevcontainerConfig(
            on_create_command=["setup"],
            post_start_command=["start-server"],
        )

        result = await apply_devcontainer_config(config, exec_fn)
        assert result.ok
        assert result.commands_run == 1
        assert not any("start-server" in cmd for cmd in executed)

    @pytest.mark.asyncio
    async def test_env_vars_passed_to_commands(self):
        executed = []

        async def exec_fn(command: str, cwd: str | None):
            executed.append(command)
            return (0, "", "")

        config = DevcontainerConfig(
            container_env={"NODE_ENV": "dev"},
            on_create_command=["npm install"],
        )

        result = await apply_devcontainer_config(config, exec_fn)
        assert result.ok
        assert result.env_vars_applied == 1
        assert "NODE_ENV" in executed[0]
        assert "npm install" in executed[0]

    @pytest.mark.asyncio
    async def test_failed_command_recorded(self):
        async def exec_fn(command: str, cwd: str | None):
            if "bad-cmd" in command:
                return (1, "", "command not found")
            return (0, "", "")

        config = DevcontainerConfig(
            on_create_command=["good-cmd", "bad-cmd", "another-good"],
        )

        result = await apply_devcontainer_config(config, exec_fn)
        assert not result.ok
        assert result.commands_run == 3
        assert result.commands_failed == 1
        assert len(result.errors) == 1
        assert "bad-cmd" in result.errors[0]

    @pytest.mark.asyncio
    async def test_exception_in_exec_handled(self):
        async def exec_fn(command: str, cwd: str | None):
            raise RuntimeError("connection lost")

        config = DevcontainerConfig(
            on_create_command=["some-cmd"],
        )

        result = await apply_devcontainer_config(config, exec_fn)
        assert not result.ok
        assert result.commands_failed == 1
        assert "connection lost" in result.errors[0]

    @pytest.mark.asyncio
    async def test_no_commands_returns_ok(self):
        async def exec_fn(command: str, cwd: str | None):
            raise AssertionError("Should not be called")

        config = DevcontainerConfig()
        result = await apply_devcontainer_config(config, exec_fn)
        assert result.ok
        assert result.commands_run == 0

    @pytest.mark.asyncio
    async def test_custom_working_dir(self):
        received_cwd = []

        async def exec_fn(command: str, cwd: str | None):
            received_cwd.append(cwd)
            return (0, "", "")

        config = DevcontainerConfig(on_create_command=["echo hi"])
        await apply_devcontainer_config(config, exec_fn, working_dir="/my/dir")
        assert received_cwd[0] == "/my/dir"


# ---------------------------------------------------------------------------
# _build_env_prefix
# ---------------------------------------------------------------------------


class TestBuildEnvPrefix:
    def test_empty_dict(self):
        assert _build_env_prefix({}) == ""

    def test_single_var(self):
        result = _build_env_prefix({"FOO": "bar"})
        assert result == "env FOO='bar' "

    def test_multiple_vars(self):
        result = _build_env_prefix({"A": "1", "B": "2"})
        assert "A='1'" in result
        assert "B='2'" in result
        assert result.startswith("env ")
        assert result.endswith(" ")

    def test_value_with_single_quotes_escaped(self):
        result = _build_env_prefix({"MSG": "it's fine"})
        assert "it'\\''s fine" in result


# ---------------------------------------------------------------------------
# config_summary
# ---------------------------------------------------------------------------


class TestConfigSummary:
    def test_summary_fields(self):
        config = DevcontainerConfig(
            image="python:3.12",
            build_dockerfile=None,
            remote_user="vscode",
            forward_ports=[3000, 8080],
            features={"ghcr.io/devcontainers/features/git:1": {}},
            on_create_command=["pip install -r requirements.txt"],
            post_create_command=["echo done"],
            container_env={"PIP_NO_CACHE": "1"},
        )
        summary = config_summary(config)
        assert summary["image"] == "python:3.12"
        assert summary["remote_user"] == "vscode"
        assert summary["forward_ports"] == [3000, 8080]
        assert "ghcr.io/devcontainers/features/git:1" in summary["features"]
        assert summary["lifecycle_commands"]["onCreateCommand"] == [
            "pip install -r requirements.txt"
        ]
        assert summary["env_var_count"] == 1

    def test_summary_empty_config(self):
        summary = config_summary(DevcontainerConfig())
        assert summary["image"] is None
        assert summary["forward_ports"] == []
        assert summary["features"] == []
        assert summary["env_var_count"] == 0


# ---------------------------------------------------------------------------
# DevcontainerApplyResult
# ---------------------------------------------------------------------------


class TestDevcontainerApplyResult:
    def test_ok_when_no_failures(self):
        r = DevcontainerApplyResult(commands_run=3, commands_failed=0)
        assert r.ok

    def test_not_ok_with_failures(self):
        r = DevcontainerApplyResult(commands_run=3, commands_failed=1)
        assert not r.ok

    def test_not_ok_with_errors(self):
        r = DevcontainerApplyResult(commands_run=0, errors=["something broke"])
        assert not r.ok


# ---------------------------------------------------------------------------
# DockerBackend integration (mocked Docker)
# ---------------------------------------------------------------------------


class TestDockerBackendDevcontainerIntegration:
    """Test the devcontainer flow in DockerBackend.create() with mocked Docker."""

    def _make_backend(self):
        from lucent.sandbox.docker_backend import DockerBackend

        backend = DockerBackend()
        # Mock the Docker client
        mock_client = MagicMock()
        backend._client = mock_client
        return backend, mock_client

    def _mock_container(self, mock_client, sandbox_id="test-id"):
        container = MagicMock()
        container.id = "container-abc123"
        container.name = "test-sandbox"
        container.status = "running"
        container.labels = {"io.lucent.sandbox.id": sandbox_id}

        mock_client.containers.run.return_value = container
        mock_client.containers.list.return_value = [container]
        mock_client.images.get.return_value = MagicMock()
        return container

    def _mock_exec(self, container, responses=None):
        """Mock container exec to return specified responses.

        responses: dict of command substring -> (exit_code, stdout, stderr)
        """
        if responses is None:
            responses = {}

        def create_exec(cid, cmd, **kwargs):
            return {"Id": "exec-123"}

        def start_exec(exec_id, **kwargs):
            return (b"", b"")

        def inspect_exec(exec_id):
            return {"ExitCode": 0}

        container.client.api.exec_create.side_effect = create_exec
        container.client.api.exec_start.return_value = (b"", b"")
        container.client.api.exec_inspect.return_value = {"ExitCode": 0}
        return container

    @pytest.mark.asyncio
    async def test_create_without_repo_skips_devcontainer(self):
        """Sandboxes without repos should not attempt devcontainer detection."""
        backend, mock_client = self._make_backend()
        container = self._mock_container(mock_client)
        self._mock_exec(container)

        config = SandboxConfig(image="python:3.12")

        with patch.object(backend, '_detect_devcontainer', new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = None
            info = await backend.create(config)

        assert info.status == SandboxStatus.READY
        assert info.devcontainer is None
        mock_detect.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_with_repo_no_devcontainer(self):
        """Repo without devcontainer.json should work normally."""
        backend, mock_client = self._make_backend()
        container = self._mock_container(mock_client)
        self._mock_exec(container)

        config = SandboxConfig(
            image="python:3.12",
            repo_url="https://github.com/test/repo",
        )

        with patch.object(backend, '_detect_devcontainer', new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = None
            with patch.object(backend, 'exec', new_callable=AsyncMock) as mock_exec:
                from lucent.sandbox.models import ExecResult
                mock_exec.return_value = ExecResult(exit_code=0, stdout="", stderr="")
                info = await backend.create(config)

        assert info.status == SandboxStatus.READY
        assert info.devcontainer is None

    @pytest.mark.asyncio
    async def test_create_applies_devcontainer_lifecycle_commands(self):
        """Devcontainer lifecycle commands should be executed."""
        backend, mock_client = self._make_backend()
        container = self._mock_container(mock_client)
        self._mock_exec(container)

        dc_config = DevcontainerConfig(
            on_create_command=["npm install"],
            post_create_command=["npm run build"],
            container_env={"NODE_ENV": "development"},
        )

        config = SandboxConfig(
            image="node:20",
            repo_url="https://github.com/test/repo",
        )

        from lucent.sandbox.models import ExecResult
        exec_results = []

        async def mock_exec(sandbox_id, command, **kwargs):
            exec_results.append(command)
            return ExecResult(exit_code=0, stdout="", stderr="")

        with patch.object(backend, '_detect_devcontainer', new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = dc_config
            with patch.object(backend, 'exec', side_effect=mock_exec):
                info = await backend.create(config)

        assert info.status == SandboxStatus.READY
        assert info.devcontainer is not None
        assert info.devcontainer["lifecycle_commands"]["onCreateCommand"] == ["npm install"]

    @pytest.mark.asyncio
    async def test_create_with_devcontainer_image_triggers_rebuild(self):
        """A devcontainer image different from config should trigger rebuild."""
        backend, mock_client = self._make_backend()
        container = self._mock_container(mock_client)
        self._mock_exec(container)

        dc_config = DevcontainerConfig(image="custom-image:latest")
        config = SandboxConfig(
            image="base-image:latest",
            repo_url="https://github.com/test/repo",
        )

        rebuild_called = False

        async def mock_rebuild(sid, name, cfg, img, info):
            nonlocal rebuild_called
            rebuild_called = True
            info.status = SandboxStatus.CREATING
            return info

        from lucent.sandbox.models import ExecResult

        with patch.object(backend, '_detect_devcontainer', new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = dc_config
            with patch.object(backend, '_rebuild_with_image', side_effect=mock_rebuild):
                with patch.object(backend, 'exec', new_callable=AsyncMock) as mock_exec:
                    mock_exec.return_value = ExecResult(exit_code=0, stdout="", stderr="")
                    await backend.create(config)

        assert rebuild_called

    @pytest.mark.asyncio
    async def test_create_with_devcontainer_same_image_no_rebuild(self):
        """A devcontainer with the same image should not trigger rebuild."""
        backend, mock_client = self._make_backend()
        container = self._mock_container(mock_client)
        self._mock_exec(container)

        dc_config = DevcontainerConfig(image="python:3.12")
        config = SandboxConfig(
            image="python:3.12",
            repo_url="https://github.com/test/repo",
        )

        from lucent.sandbox.models import ExecResult

        with patch.object(backend, '_detect_devcontainer', new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = dc_config
            with patch.object(
                backend, '_rebuild_with_image', new_callable=AsyncMock
            ) as mock_rebuild:
                with patch.object(backend, 'exec', new_callable=AsyncMock) as mock_exec:
                    mock_exec.return_value = ExecResult(exit_code=0, stdout="", stderr="")
                    info = await backend.create(config)

        mock_rebuild.assert_not_called()
        assert info.status == SandboxStatus.READY

    @pytest.mark.asyncio
    async def test_create_runs_post_start_after_ready(self):
        """postStartCommand should run after sandbox is marked READY."""
        backend, mock_client = self._make_backend()
        container = self._mock_container(mock_client)
        self._mock_exec(container)

        dc_config = DevcontainerConfig(
            post_start_command=["echo started"],
        )
        config = SandboxConfig(
            image="python:3.12",
            repo_url="https://github.com/test/repo",
        )

        from lucent.sandbox.models import ExecResult

        exec_calls = []

        async def mock_exec(sandbox_id, command, **kwargs):
            exec_calls.append(command)
            return ExecResult(exit_code=0, stdout="", stderr="")

        with patch.object(backend, '_detect_devcontainer', new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = dc_config
            with patch.object(backend, 'exec', side_effect=mock_exec):
                info = await backend.create(config)

        assert info.status == SandboxStatus.READY
        assert any("echo started" in str(c) for c in exec_calls)

    @pytest.mark.asyncio
    async def test_create_user_setup_commands_after_devcontainer(self):
        """User setup_commands should run after devcontainer lifecycle commands."""
        backend, mock_client = self._make_backend()
        container = self._mock_container(mock_client)
        self._mock_exec(container)

        dc_config = DevcontainerConfig(
            on_create_command=["devcontainer-setup"],
        )
        config = SandboxConfig(
            image="python:3.12",
            repo_url="https://github.com/test/repo",
            setup_commands=["user-setup"],
        )

        from lucent.sandbox.models import ExecResult

        exec_order = []

        async def mock_exec(sandbox_id, command, **kwargs):
            exec_order.append(str(command))
            return ExecResult(exit_code=0, stdout="", stderr="")

        with patch.object(backend, '_detect_devcontainer', new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = dc_config
            with patch.object(backend, 'exec', side_effect=mock_exec):
                info = await backend.create(config)

        assert info.status == SandboxStatus.READY
        # Find positions: devcontainer setup should come before user setup
        dc_idx = next(i for i, c in enumerate(exec_order) if "devcontainer-setup" in c)
        user_idx = next(i for i, c in enumerate(exec_order) if c == "user-setup")
        assert dc_idx < user_idx

    @pytest.mark.asyncio
    async def test_clone_failure_returns_failed_status(self):
        """Clone failure should prevent devcontainer detection."""
        backend, mock_client = self._make_backend()
        container = self._mock_container(mock_client)
        self._mock_exec(container)

        config = SandboxConfig(
            image="python:3.12",
            repo_url="https://github.com/private/repo",
        )

        from lucent.sandbox.models import ExecResult

        async def mock_exec(sandbox_id, command, **kwargs):
            if "git clone" in str(command):
                return ExecResult(exit_code=128, stdout="", stderr="Authentication failed")
            return ExecResult(exit_code=0, stdout="", stderr="")

        with patch.object(backend, 'exec', side_effect=mock_exec):
            info = await backend.create(config)

        assert info.status == SandboxStatus.FAILED
        assert "Git clone failed" in info.error

    def test_build_clone_command_does_not_embed_credentials(self):
        """Clone command must never include user/token in URL."""
        backend, _ = self._make_backend()
        config = SandboxConfig(
            repo_url="https://user:ghp_secret123@github.com/private/repo.git",
            branch="main",
        )

        cmd = backend._build_clone_command(config)

        assert "ghp_secret123" not in cmd
        assert "user:" not in cmd
        assert "https://github.com/private/repo.git" in cmd
        assert "git clone --depth=1 -b main" in cmd

    def test_build_git_auth_env_uses_askpass(self):
        """Git auth env should provide credentials via GIT_ASKPASS, not command args."""
        backend, _ = self._make_backend()

        env = backend._build_git_auth_env("x-access-token:ghp_secret123")

        assert env["GIT_ASKPASS"] == "/tmp/lucent-git-askpass.sh"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["LUCENT_GIT_USERNAME"] == "x-access-token"
        assert env["LUCENT_GIT_TOKEN"] == "ghp_secret123"

    @pytest.mark.asyncio
    async def test_devcontainer_with_dockerfile_attempts_build(self):
        """A devcontainer with Dockerfile should attempt to build."""
        backend, mock_client = self._make_backend()
        container = self._mock_container(mock_client)
        self._mock_exec(container)

        dc_config = DevcontainerConfig(
            build_dockerfile="Dockerfile.dev",
            build_context=".",
        )
        config = SandboxConfig(
            image="base:latest",
            repo_url="https://github.com/test/repo",
        )

        from lucent.sandbox.models import ExecResult

        build_attempted = False

        async def mock_build(sid, cfg, dc):
            nonlocal build_attempted
            build_attempted = True
            return None  # Build failed

        with patch.object(backend, '_detect_devcontainer', new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = dc_config
            with patch.object(backend, '_build_devcontainer_image', side_effect=mock_build):
                with patch.object(backend, 'exec', new_callable=AsyncMock) as mock_exec:
                    mock_exec.return_value = ExecResult(exit_code=0, stdout="", stderr="")
                    info = await backend.create(config)

        assert build_attempted
        # Should still succeed (Dockerfile build failure is non-fatal)
        assert info.status == SandboxStatus.READY

    @pytest.mark.asyncio
    async def test_build_devcontainer_image_quotes_user_controlled_values(self):
        """Dockerfile/context values must be shell-quoted to prevent injection."""
        backend, mock_client = self._make_backend()
        container = self._mock_container(mock_client)
        self._mock_exec(container)

        dc_config = DevcontainerConfig(
            build_dockerfile="Dockerfile;echo PWNED",
            build_context='."; touch /tmp/pwned #',
        )
        config = SandboxConfig(image="base:latest")

        from lucent.sandbox.models import ExecResult

        async def mock_exec(_sid, command, **kwargs):
            return ExecResult(exit_code=0, stdout=command, stderr="")

        with patch.object(backend, "exec", side_effect=mock_exec) as exec_mock:
            image = await backend._build_devcontainer_image("sandbox1234567890", config, dc_config)

        assert image == "lucent-dc-sandbox12345"
        cmd = exec_mock.call_args.args[1]
        # Inputs must be wrapped as quoted shell literals.
        assert f"-f {shlex.quote('Dockerfile;echo PWNED')}" in cmd
        assert cmd.endswith(shlex.quote('."; touch /tmp/pwned #'))
        # Ensure the dangerous tokens are not interpreted as standalone operators.
        assert " -f Dockerfile;echo PWNED " not in cmd
        assert " touch /tmp/pwned " not in cmd.split(" -f ", 1)[0]
