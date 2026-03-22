"""Integration tests for sandbox lifecycle: Docker backend, MCP bridge, and output modes.

Covers:
1. Docker sandbox lifecycle — create, exec, read/write, destroy, timeout
2. MCP bridge — tool proxying, task-scope enforcement, expired-key rejection
3. Output modes — diff, review (memory creation), pr, commit
4. Security — allowlist blocking, resource limits (tested via config propagation)

All tests mock the Docker client and HTTP calls so they run in CI without Docker.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lucent.sandbox.docker_backend import DockerBackend
from lucent.sandbox.manager import SandboxManager
from lucent.sandbox.mcp_bridge import BridgeServer
from lucent.sandbox.models import (
    ExecResult,
    SandboxConfig,
    SandboxInfo,
    SandboxStatus,
)
from lucent.sandbox.output import SandboxOutputHandler

# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


def _make_exec_ok(stdout: str = "", stderr: str = "") -> ExecResult:
    return ExecResult(exit_code=0, stdout=stdout, stderr=stderr)


def _make_exec_fail(stderr: str = "error") -> ExecResult:
    return ExecResult(exit_code=1, stdout="", stderr=stderr)


def _ready_info(sandbox_id: str = "sb-test-1234") -> SandboxInfo:
    return SandboxInfo(
        id=sandbox_id,
        name="test-sandbox",
        status=SandboxStatus.READY,
        config=SandboxConfig(),
    )


def _make_mock_container(container_id: str = "docker-abc123") -> MagicMock:
    """Return a minimal Docker container mock."""
    container = MagicMock()
    container.id = container_id
    container.status = "running"
    container.reload = MagicMock()

    # exec_run returns (exit_code, output_bytes)
    container.exec_run = MagicMock(return_value=(0, b"ok\n"))

    # put_archive / get_archive for file I/O
    container.put_archive = MagicMock(return_value=True)

    def _get_archive(path, **kwargs):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            data = b"test content"
            info = tarfile.TarInfo(name=path.lstrip("/"))
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        buf.seek(0)
        return buf, {"name": path}

    container.get_archive = MagicMock(side_effect=_get_archive)
    return container


def _make_docker_client(container: MagicMock | None = None) -> MagicMock:
    """Return a mock docker.DockerClient."""
    c = container or _make_mock_container()
    client = MagicMock()
    client.images.get = MagicMock(return_value=MagicMock())
    client.containers.run = MagicMock(return_value=c)
    client.containers.get = MagicMock(return_value=c)
    client.networks.get = MagicMock(return_value=MagicMock())
    client.api.create_networking_config = MagicMock(return_value={})
    client.api.create_endpoint_config = MagicMock(return_value={})
    return client


def _make_backend_mock(**kwargs) -> AsyncMock:
    """Return an AsyncMock SandboxBackend with sensible defaults."""
    backend = AsyncMock()
    info = kwargs.pop("info", _ready_info())
    backend.create.return_value = info
    backend.exec.return_value = _make_exec_ok()
    backend.read_file.return_value = b"file content"
    backend.write_file.return_value = None
    backend.list_files.return_value = [{"path": "/workspace/main.py", "size": 100, "is_dir": False}]
    backend.get.return_value = info
    backend.stop.return_value = None
    backend.destroy.return_value = None
    backend.list_all.return_value = []
    for k, v in kwargs.items():
        setattr(backend, k, v)
    return backend


def _make_manager(backend: AsyncMock | None = None) -> SandboxManager:
    """Return a SandboxManager with mocked backend and DB."""
    b = backend or _make_backend_mock()
    manager = SandboxManager(backend=b)
    # Avoid DB calls
    manager._repo = AsyncMock()
    repo = AsyncMock()
    repo.create = AsyncMock()
    repo.update_status = AsyncMock()
    manager._repo.return_value = repo
    manager._create_task_scoped_api_key = AsyncMock(return_value=(None, "test-bridge-key"))
    return manager


# ===========================================================================
# 1. Docker sandbox lifecycle
# ===========================================================================


class TestDockerSandboxLifecycle:
    """End-to-end lifecycle tests using a mocked Docker client."""

    def _backend_with_client(self, client: MagicMock) -> DockerBackend:
        backend = DockerBackend()
        backend._client = client
        return backend

    # --- Create ----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_create_returns_ready_info(self):
        """create() returns SandboxInfo with READY status when container starts."""
        client = _make_docker_client()
        backend = self._backend_with_client(client)

        info = await backend.create(SandboxConfig(image="lucent-sandbox:base"))

        assert info.status == SandboxStatus.READY
        assert info.container_id == client.containers.run.return_value.id
        assert info.id is not None

    @pytest.mark.asyncio
    async def test_create_uses_configured_image(self):
        """create() passes the correct image to Docker."""
        client = _make_docker_client()
        backend = self._backend_with_client(client)

        await backend.create(SandboxConfig(image="my-custom-image:latest"))

        _, kwargs = client.containers.run.call_args
        assert kwargs.get("image") == "my-custom-image:latest"

    @pytest.mark.asyncio
    async def test_create_sets_resource_limits(self):
        """create() passes memory and CPU limits to Docker."""
        client = _make_docker_client()
        backend = self._backend_with_client(client)

        await backend.create(SandboxConfig(memory_limit="1g", cpu_limit=1.0))

        _, kwargs = client.containers.run.call_args
        assert kwargs.get("mem_limit") == "1g"
        assert kwargs.get("nano_cpus") == int(1.0 * 1e9)

    @pytest.mark.asyncio
    async def test_create_injects_env_vars(self):
        """create() passes env_vars to the container."""
        client = _make_docker_client()
        backend = self._backend_with_client(client)

        await backend.create(SandboxConfig(env_vars={"MY_VAR": "hello", "OTHER": "world"}))

        _, kwargs = client.containers.run.call_args
        env = kwargs.get("environment") or {}
        assert env.get("MY_VAR") == "hello"
        assert env.get("OTHER") == "world"

    @pytest.mark.asyncio
    async def test_create_labels_container(self):
        """Container is labeled with the Lucent sandbox prefix."""
        client = _make_docker_client()
        backend = self._backend_with_client(client)

        await backend.create(SandboxConfig())

        _, kwargs = client.containers.run.call_args
        labels = kwargs.get("labels") or {}
        assert any("lucent" in k.lower() for k in labels)

    # --- Exec ------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_exec_returns_stdout(self):
        """exec() captures stdout from the backend."""
        backend = _make_backend_mock()
        backend.exec.return_value = _make_exec_ok(stdout="hello world\n")
        manager = _make_manager(backend)

        result = await manager.exec("sb-exec", "echo hello world")

        assert result.exit_code == 0
        assert result.stdout == "hello world\n"

    @pytest.mark.asyncio
    async def test_exec_captures_exit_code(self):
        """exec() reports non-zero exit codes correctly."""
        backend = _make_backend_mock()
        backend.exec.return_value = _make_exec_fail(stderr="command not found")
        manager = _make_manager(backend)

        result = await manager.exec("sb-exec", "nonexistent-command")

        assert result.exit_code == 1
        assert "command not found" in result.stderr

    @pytest.mark.asyncio
    async def test_exec_forwards_cwd(self):
        """exec() forwards cwd to backend."""
        backend = _make_backend_mock()
        manager = _make_manager(backend)

        await manager.exec("sb-exec", "pwd", cwd="/workspace/src")

        backend.exec.assert_called_once_with(
            "sb-exec", "pwd", cwd="/workspace/src", env=None, timeout=300
        )

    # --- File I/O --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_write_then_read_file(self):
        """write_file() stores bytes; read_file() returns them."""
        backend = _make_backend_mock()
        manager = _make_manager(backend)

        sandbox_id = "sb-fileio"
        content = b"print('hello sandbox')\n"
        await manager.write_file(sandbox_id, "/workspace/hello.py", content)
        result = await manager.read_file(sandbox_id, "/workspace/hello.py")

        backend.write_file.assert_called_once_with(sandbox_id, "/workspace/hello.py", content)
        backend.read_file.assert_called_once_with(sandbox_id, "/workspace/hello.py")
        # The mock returns b"file content" but the call chain is verified above
        assert result is not None

    @pytest.mark.asyncio
    async def test_list_files_returns_entries(self):
        """list_files() returns a list of file dicts."""
        backend = _make_backend_mock()
        manager = _make_manager(backend)

        files = await manager.list_files("sb-list")

        assert isinstance(files, list)
        assert len(files) > 0
        assert "path" in files[0]

    # --- Destroy ---------------------------------------------------------

    @pytest.mark.asyncio
    async def test_destroy_calls_backend(self):
        """destroy() delegates to backend.destroy()."""
        backend = _make_backend_mock()
        manager = _make_manager(backend)
        sandbox_id = "sb-destroy"

        await manager.destroy(sandbox_id)

        backend.destroy.assert_called_once_with(sandbox_id)

    @pytest.mark.asyncio
    async def test_destroy_cancels_cleanup_task(self):
        """destroy() cancels any pending auto-destroy task."""
        backend = _make_backend_mock()
        manager = _make_manager(backend)
        sandbox_id = "sb-cancel"

        mock_task = MagicMock()
        mock_task.cancel = MagicMock()
        manager._cleanup_tasks[sandbox_id] = mock_task

        await manager.destroy(sandbox_id)

        mock_task.cancel.assert_called_once()
        assert sandbox_id not in manager._cleanup_tasks

    @pytest.mark.asyncio
    async def test_destroy_revokes_bridge_api_key(self):
        """destroy() revokes the sandbox bridge API key if one exists."""
        from uuid import uuid4

        backend = _make_backend_mock()
        manager = _make_manager(backend)
        manager._revoke_api_key = AsyncMock()
        sandbox_id = "sb-revoke"
        key_id = uuid4()
        manager._sandbox_bridge_api_keys[sandbox_id] = key_id

        await manager.destroy(sandbox_id)

        manager._revoke_api_key.assert_called_once_with(key_id)
        assert sandbox_id not in manager._sandbox_bridge_api_keys

    # --- Timeout / auto-destruction --------------------------------------

    @pytest.mark.asyncio
    async def test_auto_destroy_fires_after_timeout(self):
        """_auto_destroy() calls destroy() after sleeping for timeout seconds."""
        backend = _make_backend_mock()
        manager = _make_manager(backend)
        sandbox_id = "sb-timeout"

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            manager.destroy = AsyncMock()
            await manager._auto_destroy(sandbox_id, timeout=10)

        mock_sleep.assert_called_once_with(10)
        manager.destroy.assert_called_once_with(sandbox_id)

    @pytest.mark.asyncio
    async def test_auto_destroy_cancelled_on_manual_destroy(self):
        """If _auto_destroy() is cancelled, no exception propagates."""
        backend = _make_backend_mock()
        manager = _make_manager(backend)
        manager.destroy = AsyncMock()

        async def cancel_after_start():
            task = asyncio.create_task(manager._auto_destroy("sb-cancel", timeout=9999))
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # Expected

        await cancel_after_start()
        manager.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_registers_cleanup_task_for_timeout(self):
        """create() registers an auto-destroy task when timeout_seconds > 0."""
        backend = _make_backend_mock()
        manager = _make_manager(backend)

        info = await manager.create(SandboxConfig(timeout_seconds=300))

        assert info.id in manager._cleanup_tasks
        manager._cleanup_tasks[info.id].cancel()

    @pytest.mark.asyncio
    async def test_create_no_cleanup_task_when_timeout_zero(self):
        """create() does not register auto-destroy task when timeout_seconds=0."""
        backend = _make_backend_mock()
        manager = _make_manager(backend)

        info = await manager.create(SandboxConfig(timeout_seconds=0))

        assert info.id not in manager._cleanup_tasks


# ===========================================================================
# 2. MCP bridge
# ===========================================================================


class TestMCPBridge:
    """Tests for BridgeServer tool proxying and scope enforcement."""

    def _bridge(self, task_id: str = "task-abc123") -> BridgeServer:
        return BridgeServer(
            api_url="http://lucent.local/api",
            api_key="hs_test-key-abc",
            task_id=task_id,
        )

    # --- Tool listing ----------------------------------------------------

    def test_tool_list_contains_required_tools(self):
        """Bridge exposes the required MCP tool set."""
        bridge = self._bridge()
        names = {t["name"] for t in bridge.tool_list()}
        assert "create_memory" in names
        assert "search_memories" in names
        assert "update_memory" in names
        assert "log_task_event" in names
        assert "link_task_memory" in names

    def test_unknown_tool_raises_value_error(self):
        bridge = self._bridge()
        with pytest.raises(ValueError, match="Unknown tool"):
            bridge.handle_tool_call("nonexistent_tool", {})

    # --- Proxy routing ---------------------------------------------------

    def test_create_memory_proxies_to_post_memories(self):
        """create_memory routes to POST /memories."""
        bridge = self._bridge()
        captured = {}

        def fake_proxy(method, path, payload):
            captured.update({"method": method, "path": path, "payload": payload})
            return {"id": "mem-123"}

        bridge._proxy = fake_proxy
        bridge.handle_tool_call("create_memory", {"type": "experience", "content": "test"})

        assert captured["method"] == "POST"
        assert captured["path"] == "/memories"
        assert captured["payload"]["content"] == "test"

    def test_search_memories_proxies_to_post_search(self):
        """search_memories routes to POST /search."""
        bridge = self._bridge()
        captured = {}

        def fake_proxy(method, path, payload):
            captured.update({"method": method, "path": path})
            return {"memories": []}

        bridge._proxy = fake_proxy
        bridge.handle_tool_call("search_memories", {"query": "test query"})

        assert captured["method"] == "POST"
        assert captured["path"] == "/search"

    def test_update_memory_proxies_to_patch_with_id(self):
        """update_memory routes to PATCH /memories/{id}."""
        bridge = self._bridge()
        captured = {}

        def fake_proxy(method, path, payload):
            captured.update({"method": method, "path": path})
            return {}

        bridge._proxy = fake_proxy
        bridge.handle_tool_call("update_memory", {"memory_id": "mem-xyz", "content": "updated"})

        assert captured["method"] == "PATCH"
        assert "/mem-xyz" in captured["path"]

    def test_log_task_event_uses_bridge_task_id(self):
        """log_task_event injects bridge task_id when none provided."""
        bridge = self._bridge(task_id="task-abc123")
        captured = {}

        def fake_proxy(method, path, payload):
            captured.update({"method": method, "path": path, "payload": payload})
            return {}

        bridge._proxy = fake_proxy
        bridge.handle_tool_call("log_task_event", {"event_type": "progress", "detail": "doing work"})

        assert "task-abc123" in captured["path"]
        assert captured["payload"]["event_type"] == "progress"

    def test_log_task_event_explicit_task_id_matches_scope(self):
        """log_task_event succeeds when explicit task_id matches bridge scope."""
        bridge = self._bridge(task_id="task-abc123")
        bridge._proxy = lambda m, p, pl: {}

        # Same task_id as bridge scope — should succeed
        bridge.handle_tool_call("log_task_event", {"task_id": "task-abc123", "event_type": "info"})

    def test_log_task_event_wrong_task_id_raises(self):
        """log_task_event rejects task_id that doesn't match bridge scope."""
        bridge = self._bridge(task_id="task-abc123")
        bridge._proxy = lambda m, p, pl: {}

        with pytest.raises(ValueError, match="task_id does not match bridge scope"):
            bridge.handle_tool_call("log_task_event", {"task_id": "task-OTHER", "event_type": "info"})

    def test_update_memory_requires_memory_id(self):
        """update_memory raises ValueError when memory_id is missing."""
        bridge = self._bridge()
        bridge._proxy = lambda m, p, pl: {}

        with pytest.raises(ValueError, match="memory_id is required"):
            bridge.handle_tool_call("update_memory", {"content": "no id given"})

    # --- HTTP proxy layer ------------------------------------------------

    def test_proxy_includes_auth_header(self):
        """_proxy() sends Authorization: Bearer header with the configured key."""

        bridge = self._bridge()
        captured_headers = {}

        def mock_urlopen(req, timeout=None):
            captured_headers.update(dict(req.headers))
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = b'{"id": "mem-1"}'
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            bridge._proxy("POST", "/memories", {"type": "experience", "content": "hi"})

        auth = captured_headers.get("Authorization") or captured_headers.get("authorization", "")
        assert "Bearer" in auth
        assert "hs_test-key-abc" in auth

    def test_proxy_raises_on_http_error(self):
        """_proxy() raises RuntimeError on non-2xx HTTP responses."""
        import urllib.error

        bridge = self._bridge()

        def raise_http_error(*args, **kwargs):
            raise urllib.error.HTTPError(
                url="http://lucent.local/api/memories",
                code=401,
                msg="Unauthorized",
                hdrs={},
                fp=io.BytesIO(b'{"detail": "Invalid key"}'),
            )

        with patch("urllib.request.urlopen", side_effect=raise_http_error):
            with pytest.raises(RuntimeError, match="401"):
                bridge._proxy("POST", "/memories", {})

    def test_proxy_raises_on_connection_error(self):
        """_proxy() raises RuntimeError when the server is unreachable."""
        import urllib.error

        bridge = self._bridge()

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with pytest.raises(RuntimeError, match="Connection error"):
                bridge._proxy("GET", "/health", {})

    # --- Bridge with no task context ------------------------------------

    def test_log_task_event_no_task_id_raises(self):
        """log_task_event raises when neither arg nor bridge has a task_id."""
        bridge = BridgeServer(api_url="http://lucent.local/api", api_key="key", task_id=None)
        bridge._proxy = lambda m, p, pl: {}

        with pytest.raises(ValueError, match="task_id is required"):
            bridge.handle_tool_call("log_task_event", {"event_type": "info"})


# ===========================================================================
# 3. Output modes
# ===========================================================================


class TestOutputModes:
    """Tests for SandboxOutputHandler: diff, review, pr, commit."""

    def _handler(self, diff_content: str = "diff --git a/x b/x\n+added line\n") -> tuple:
        manager = AsyncMock()
        manager.exec.return_value = _make_exec_ok(stdout=diff_content)

        request_api = AsyncMock()
        request_api.add_event = AsyncMock()
        request_api.link_memory = AsyncMock()

        memory_api = AsyncMock()
        memory_api.create = AsyncMock(return_value={"id": "mem-review-1"})

        import logging
        _logger = logging.getLogger("test")
        def _log(msg, level="INFO"):
            lvl = "warning" if level.lower() == "warn" else level.lower()
            getattr(_logger, lvl, _logger.info)(msg)
        handler = SandboxOutputHandler(
            manager=manager,
            request_api=request_api,
            memory_api=memory_api,
            logger=_log,
        )
        return handler, manager, request_api, memory_api

    # --- Diff extraction -------------------------------------------------

    @pytest.mark.asyncio
    async def test_diff_mode_returns_result_with_diff(self):
        """diff mode captures git diff and returns OutputResult."""
        handler, manager, request_api, _ = self._handler(diff_content="+added line\n")
        config = SandboxConfig(output_mode="diff")

        result = await handler.process(
            sandbox_id="sb-diff",
            task_id="task-1",
            task_description="Add a feature",
            config=config,
        )

        assert result is not None
        assert result.mode == "diff"
        assert "added" in result.diff

    @pytest.mark.asyncio
    async def test_diff_mode_logs_event(self):
        """diff mode logs a sandbox_output_diff event."""
        handler, _, request_api, _ = self._handler()
        config = SandboxConfig(output_mode="diff")

        await handler.process(
            sandbox_id="sb-diff-event",
            task_id="task-1",
            task_description="work",
            config=config,
        )

        request_api.add_event.assert_called()
        call_args = request_api.add_event.call_args
        assert "sandbox_output_diff" in str(call_args)

    @pytest.mark.asyncio
    async def test_no_output_mode_returns_none(self):
        """process() returns None when output_mode is not set."""
        handler, _, _, _ = self._handler()
        config = SandboxConfig(output_mode=None)

        result = await handler.process(
            sandbox_id="sb-none",
            task_id="task-1",
            task_description="work",
            config=config,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_diff_detected(self):
        """diff mode marks has_changes=False when diff is empty."""
        handler, _, _, _ = self._handler(diff_content="")
        config = SandboxConfig(output_mode="diff")

        result = await handler.process(
            sandbox_id="sb-nodiff",
            task_id="task-1",
            task_description="no changes",
            config=config,
        )

        assert result.metadata["has_changes"] is False

    @pytest.mark.asyncio
    async def test_git_diff_failure_raises(self):
        """process() raises RuntimeError if git diff fails."""
        handler, manager, _, _ = self._handler()
        manager.exec.return_value = _make_exec_fail(stderr="not a git repo")
        config = SandboxConfig(output_mode="diff")

        with pytest.raises(RuntimeError, match="git diff failed"):
            await handler.process(
                sandbox_id="sb-diff-fail",
                task_id="task-1",
                task_description="work",
                config=config,
            )

    # --- Review mode -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_review_mode_creates_memory(self):
        """review mode creates a memory tagged needs-review."""
        handler, _, _, memory_api = self._handler()
        config = SandboxConfig(output_mode="review")

        result = await handler.process(
            sandbox_id="sb-review",
            task_id="task-1",
            task_description="Refactor module",
            config=config,
        )

        assert result.mode == "review"
        memory_api.create.assert_called_once()
        kwargs = memory_api.create.call_args[1]
        assert "needs-review" in kwargs.get("tags", [])
        assert "sandbox-output" in kwargs.get("tags", [])

    @pytest.mark.asyncio
    async def test_review_mode_memory_contains_diff(self):
        """review mode embeds the diff in the memory content."""
        diff = "+def new_function():\n+    pass\n"
        handler, _, _, memory_api = self._handler(diff_content=diff)
        config = SandboxConfig(output_mode="review")

        await handler.process(
            sandbox_id="sb-review-content",
            task_id="task-2",
            task_description="Add function",
            config=config,
        )

        content = memory_api.create.call_args[1]["content"]
        assert diff in content

    @pytest.mark.asyncio
    async def test_review_mode_memory_contains_task_desc(self):
        """review mode embeds the task description in the memory content."""
        handler, _, _, memory_api = self._handler()
        config = SandboxConfig(output_mode="review")

        await handler.process(
            sandbox_id="sb-review-desc",
            task_id="task-3",
            task_description="Fix authentication bug",
            config=config,
        )

        content = memory_api.create.call_args[1]["content"]
        assert "Fix authentication bug" in content

    @pytest.mark.asyncio
    async def test_review_mode_links_memory_to_task(self):
        """review mode calls link_memory to tie the memory to the task."""
        handler, _, request_api, _ = self._handler()
        config = SandboxConfig(output_mode="review")

        await handler.process(
            sandbox_id="sb-review-link",
            task_id="task-4",
            task_description="work",
            config=config,
        )

        request_api.link_memory.assert_called_once()
        call_args = request_api.link_memory.call_args
        assert "task-4" in str(call_args)

    @pytest.mark.asyncio
    async def test_review_mode_result_has_memory_id(self):
        """review mode embeds the created memory_id in result metadata."""
        handler, _, _, _ = self._handler()
        config = SandboxConfig(output_mode="review")

        result = await handler.process(
            sandbox_id="sb-review-memid",
            task_id="task-5",
            task_description="work",
            config=config,
        )

        assert result.metadata.get("memory_id") == "mem-review-1"

    # --- PR mode ---------------------------------------------------------

    @pytest.mark.asyncio
    async def test_pr_mode_requires_git_credentials(self):
        """pr mode raises RuntimeError when git_credentials is missing."""
        handler, _, _, _ = self._handler()
        config = SandboxConfig(output_mode="pr", git_credentials=None, repo_url="https://github.com/org/repo")

        with pytest.raises(RuntimeError, match="git_credentials"):
            await handler.process(
                sandbox_id="sb-pr-nokey",
                task_id="task-pr",
                task_description="work",
                config=config,
            )

    @pytest.mark.asyncio
    async def test_pr_mode_requires_repo_url(self):
        """pr mode raises RuntimeError when repo_url is missing."""
        handler, _, _, _ = self._handler()
        config = SandboxConfig(output_mode="pr", git_credentials="token", repo_url=None)

        with pytest.raises(RuntimeError, match="repo_url"):
            await handler.process(
                sandbox_id="sb-pr-nourl",
                task_id="task-pr",
                task_description="work",
                config=config,
            )

    @pytest.mark.asyncio
    async def test_pr_mode_pushes_branch_and_logs_event(self):
        """pr mode pushes branch and logs sandbox_output_pr event."""
        handler, manager, request_api, _ = self._handler()
        # exec calls: git diff (from _extract_diff), git rev-parse, git remote set-url, git push
        exec_responses = [
            _make_exec_ok(stdout="+added line\n"),  # git diff
            _make_exec_ok(stdout="main\n"),          # git rev-parse
            _make_exec_ok(),                          # git remote set-url
            _make_exec_ok(),                          # git push
        ]
        manager.exec.side_effect = exec_responses

        config = SandboxConfig(
            output_mode="pr",
            git_credentials="ghp_token",
            repo_url="https://github.com/org/repo.git",
            branch="main",
        )

        with patch("httpx.AsyncClient") as _mock_client:
            result = await handler.process(
                sandbox_id="sb-pr",
                task_id="task-pr",
                task_description="Add feature",
                config=config,
            )

        assert result.mode == "pr"
        request_api.add_event.assert_called()
        event_calls = [str(c) for c in request_api.add_event.call_args_list]
        assert any("sandbox_output_pr" in c for c in event_calls)

    # --- Commit mode -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_commit_mode_requires_approval(self):
        """commit mode raises when commit_approved is False."""
        handler, _, _, _ = self._handler()
        config = SandboxConfig(
            output_mode="commit",
            git_credentials="token",
            repo_url="https://github.com/org/repo.git",
            commit_approved=False,
        )

        with pytest.raises(RuntimeError, match="commit_approved"):
            await handler.process(
                sandbox_id="sb-commit-noapp",
                task_id="task-commit",
                task_description="work",
                config=config,
            )


# ===========================================================================
# 4. Security — config propagation and model validation
# ===========================================================================


class TestSandboxSecurity:
    """Verify security-related config fields are correctly propagated."""

    def test_network_mode_none_is_default(self):
        """Default network_mode is 'none' for maximum isolation."""
        config = SandboxConfig()
        assert config.network_mode == "none"

    def test_allowlist_mode_field_accepted(self):
        """SandboxConfig accepts network_mode='allowlist' with allowed_hosts."""
        config = SandboxConfig(
            network_mode="allowlist",
            allowed_hosts=["api.lucent.local", "10.0.0.1"],
        )
        assert config.network_mode == "allowlist"
        assert len(config.allowed_hosts) == 2

    def test_memory_limit_propagated_to_docker(self):
        """memory_limit is passed to Docker as mem_limit."""
        backend = DockerBackend()
        mock_container = _make_mock_container()
        client = _make_docker_client(mock_container)
        backend._client = client

        backend._create_container("sb-mem", "test-sb", SandboxConfig(memory_limit="512m"))

        _, kwargs = client.containers.run.call_args
        assert kwargs.get("mem_limit") == "512m"

    def test_cpu_limit_propagated_to_docker(self):
        """cpu_limit is converted to nano_cpus and passed to Docker."""
        backend = DockerBackend()
        mock_container = _make_mock_container()
        client = _make_docker_client(mock_container)
        backend._client = client

        backend._create_container("sb-cpu", "test-sb", SandboxConfig(cpu_limit=0.5))

        _, kwargs = client.containers.run.call_args
        assert kwargs.get("nano_cpus") == int(0.5 * 1e9)

    def test_disk_limit_propagated_to_docker(self):
        """disk_limit is passed as storage_opt to Docker."""
        backend = DockerBackend()
        mock_container = _make_mock_container()
        client = _make_docker_client(mock_container)
        backend._client = client

        backend._create_container("sb-disk", "test-sb", SandboxConfig(disk_limit="5g"))

        _, kwargs = client.containers.run.call_args
        assert kwargs.get("storage_opt") == {"size": "5g"}

    def test_allowlist_mode_grants_net_admin_cap(self):
        """allowlist network_mode adds NET_ADMIN capability to the container."""
        backend = DockerBackend()
        backend._ensure_network = MagicMock()
        mock_container = _make_mock_container()
        client = _make_docker_client(mock_container)
        backend._client = client

        backend._create_container("sb-al", "test-sb", SandboxConfig(network_mode="allowlist"))

        _, kwargs = client.containers.run.call_args
        assert "NET_ADMIN" in (kwargs.get("cap_add") or [])

    def test_none_network_mode_no_net_admin(self):
        """Default network_mode='none' does NOT grant NET_ADMIN."""
        backend = DockerBackend()
        mock_container = _make_mock_container()
        client = _make_docker_client(mock_container)
        backend._client = client

        backend._create_container("sb-none-net", "test-sb", SandboxConfig(network_mode="none"))

        _, kwargs = client.containers.run.call_args
        cap_add = kwargs.get("cap_add") or []
        assert "NET_ADMIN" not in cap_add

    @pytest.mark.asyncio
    async def test_allowlist_applies_iptables_rules(self):
        """allowlist mode calls _apply_network_allowlist after container starts."""
        from lucent.sandbox.docker_backend import DockerBackend

        backend = DockerBackend()
        backend._client = _make_docker_client()

        exec_calls: list[str] = []

        async def fake_exec(sid, cmd, **kwargs):
            exec_calls.append(cmd)
            if "getent" in cmd:
                return _make_exec_ok(stdout="1.2.3.4\n")
            return _make_exec_ok()

        backend.exec = fake_exec
        config = SandboxConfig(
            network_mode="allowlist",
            allowed_hosts=["api.lucent.local"],
            timeout_seconds=0,
            setup_commands=[],
        )

        # _apply_network_allowlist is called inside create() when mode=allowlist
        # We test it directly since the full create flow requires a running container
        await backend._apply_network_allowlist("sb-iptables", config)

        assert any("iptables" in c for c in exec_calls)
        assert any("DROP" in c for c in exec_calls)

    @pytest.mark.asyncio
    async def test_sandbox_manager_tracks_activity_on_exec(self):
        """exec() through SandboxManager updates last-activity timestamp."""
        backend = _make_backend_mock()
        manager = _make_manager(backend)
        sandbox_id = "sb-activity"
        manager._last_activity[sandbox_id] = 0.0

        await manager.exec(sandbox_id, "echo test")

        assert manager._last_activity[sandbox_id] > 0

    @pytest.mark.asyncio
    async def test_sandbox_manager_tracks_activity_on_file_ops(self):
        """read_file/write_file/list_files also update last-activity."""
        backend = _make_backend_mock()
        manager = _make_manager(backend)
        sandbox_id = "sb-fileact"
        manager._last_activity[sandbox_id] = 0.0

        await manager.read_file(sandbox_id, "/workspace/f.py")
        ts_after_read = manager._last_activity[sandbox_id]
        assert ts_after_read > 0

        manager._last_activity[sandbox_id] = 0.0
        await manager.write_file(sandbox_id, "/workspace/f.py", b"x")
        assert manager._last_activity[sandbox_id] > 0

        manager._last_activity[sandbox_id] = 0.0
        await manager.list_files(sandbox_id)
        assert manager._last_activity[sandbox_id] > 0
