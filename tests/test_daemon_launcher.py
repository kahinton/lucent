"""DockerLauncher: worker container construction, binding, and tracking."""

import pytest

from daemon.launcher.base import WorkerSpec, build_worker_env
from daemon.launcher.docker_launcher import DockerLauncher


class FakeContainer:
    def __init__(self, cid, name, status="running", created="2026-01-01T00:00:00Z"):
        self.id = cid
        self.name = name
        self.status = status
        self.attrs = {"Created": created}
        self.stopped = False
        self.removed = False

    def stop(self, timeout=None):
        self.stopped = True

    def kill(self):
        self.stopped = True

    def remove(self, force=False):
        self.removed = True


class FakeContainersApi:
    def __init__(self, owner):
        self._owner = owner

    def get(self, name):
        return self._owner.self_container

    def run(self, **kwargs):
        self._owner.runs.append(kwargs)
        return FakeContainer("new-id", kwargs["name"])

    def list(self, all=False, filters=None):
        return list(self._owner.listed)


class FakeClient:
    def __init__(self, self_container, listed=None):
        self.self_container = self_container
        self.listed = listed or []
        self.runs: list[dict] = []
        self.containers = FakeContainersApi(self)


class FakeSelfContainer:
    attrs = {
        "Image": "sha256:controller-image",
        "NetworkSettings": {"Networks": {"lucent_default": {}}},
        "Mounts": [
            {"Type": "bind", "Source": "/host/daemon", "Destination": "/app/daemon", "RW": True},
            {"Type": "bind", "Source": "/host/shared", "Destination": "/shared", "RW": False},
            {"Type": "volume", "Source": "vol", "Destination": "/data", "RW": True},
        ],
    }

    def __init__(self):
        self.image = "sha256:controller-image"


def make_launcher(listed=None) -> tuple[DockerLauncher, FakeClient]:
    launcher = DockerLauncher(controller_id="ctrl-1")
    client = FakeClient(FakeSelfContainer(), listed)
    launcher._client = client
    return launcher, client


# ── build_worker_env ──────────────────────────────────────────────────────


def test_worker_env_forces_the_binding(monkeypatch):
    monkeypatch.setenv("LUCENT_MCP_URL", "http://lucent:8766/mcp")
    env = build_worker_env("org-123")

    assert env["LUCENT_DAEMON_ORG"] == "org-123"
    assert env["LUCENT_DAEMON_ROLES"] == "dispatcher"
    assert env["LUCENT_DAEMON_WORKER"] == "1"
    # A worker must never source-reload (which would os.execv).
    assert env["LUCENT_DAEMON_SOURCE_RELOAD"] == "false"
    assert env["LUCENT_MCP_URL"] == "http://lucent:8766/mcp"


def test_worker_env_drops_non_allowlisted_variables(monkeypatch):
    monkeypatch.setenv("LUCENT_SIGNING_SECRET", "super-secret")
    env = build_worker_env("org-123")

    assert "LUCENT_SIGNING_SECRET" not in env


def test_worker_env_ignores_a_hostile_binding_override(monkeypatch):
    """The whole point: caller-supplied env can never change the org binding."""
    env = build_worker_env("org-123", {"LUCENT_DAEMON_ORG": "org-evil"})

    assert env["LUCENT_DAEMON_ORG"] == "org-123"


def test_worker_env_prefers_the_restricted_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://full-privilege@db/lucent")
    monkeypatch.setenv("DAEMON_DATABASE_URL", "postgresql://lucent_daemon@db/lucent")
    env = build_worker_env("org-123")

    assert env["DAEMON_DATABASE_URL"].startswith("postgresql://lucent_daemon")
    assert "DATABASE_URL" not in env


# ── ensure / container construction ────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_spawns_a_correctly_configured_worker(monkeypatch):
    monkeypatch.setenv("LUCENT_MCP_URL", "http://lucent:8766/mcp")
    launcher, client = make_launcher()

    started = await launcher.ensure("org-abc12345", 1, WorkerSpec())

    assert started == 1
    kwargs = client.runs[0]
    env = kwargs["environment"]
    assert env["LUCENT_DAEMON_ORG"] == "org-abc12345"
    assert env["LUCENT_DAEMON_ROLES"] == "dispatcher"
    assert kwargs["image"] == "sha256:controller-image"
    assert kwargs["network"] == "lucent_default"
    assert kwargs["command"] == ["python", "-m", "daemon.daemon"]
    assert kwargs["working_dir"] == "/app"
    assert kwargs["detach"] is True
    assert kwargs["auto_remove"] is False
    assert kwargs["restart_policy"] == {"Name": "no"}
    assert kwargs["stop_signal"] == "SIGTERM"
    assert kwargs["labels"]["io.lucent.worker.managed"] == "true"
    assert kwargs["labels"]["io.lucent.worker.org"] == "org-abc12345"
    assert kwargs["labels"]["io.lucent.worker.controller"] == "ctrl-1"
    # Bind mounts are cloned and keyed by host path; the volume mount is not.
    assert kwargs["volumes"]["/host/daemon"] == {"bind": "/app/daemon", "mode": "rw"}
    assert kwargs["volumes"]["/host/shared"] == {"bind": "/shared", "mode": "ro"}
    assert "/data" not in kwargs["volumes"]


@pytest.mark.asyncio
async def test_ensure_spawns_only_the_shortfall():
    launcher, client = make_launcher(
        listed=[FakeContainer("a", "lucent-worker-1"), FakeContainer("b", "lucent-worker-2")]
    )

    started = await launcher.ensure("org-abc12345", 2, WorkerSpec())

    assert started == 0
    assert client.runs == []


@pytest.mark.asyncio
async def test_self_location_failure_fails_closed(monkeypatch):
    monkeypatch.delenv("LUCENT_WORKER_IMAGE", raising=False)
    monkeypatch.delenv("LUCENT_WORKER_NETWORK", raising=False)
    launcher = DockerLauncher(controller_id="ctrl-1")

    class EmptyClient:
        class containers:  # noqa: N801
            @staticmethod
            def get(name):
                raise RuntimeError("no such container")

            @staticmethod
            def list(all=False, filters=None):
                return []

    launcher._client = EmptyClient()

    with pytest.raises(RuntimeError, match="cannot locate its own container"):
        await launcher.ensure("org-abc12345", 1, WorkerSpec())


@pytest.mark.asyncio
async def test_explicit_overrides_allow_spawning_without_self_location(monkeypatch):
    monkeypatch.setenv("LUCENT_WORKER_IMAGE", "lucent-worker:dev")
    monkeypatch.setenv("LUCENT_WORKER_NETWORK", "lucent_default")
    launcher = DockerLauncher(controller_id="ctrl-1")

    class EmptyClient:
        class containers:  # noqa: N801
            @staticmethod
            def get(name):
                raise RuntimeError("no such container")

            @staticmethod
            def list(all=False, filters=None):
                return []

            @staticmethod
            def run(**kwargs):
                EmptyClient.runs.append(kwargs)
                return FakeContainer("x", kwargs["name"])

    EmptyClient.runs = []
    launcher._client = EmptyClient()

    await launcher.ensure("org-abc12345", 1, WorkerSpec())

    assert EmptyClient.runs[0]["image"] == "lucent-worker:dev"
    assert EmptyClient.runs[0]["network"] == "lucent_default"


# ── running / stop / prune ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_running_counts_only_running_workers():
    launcher, _ = make_launcher(
        listed=[FakeContainer("a", "w1"), FakeContainer("b", "w2")]
    )
    assert await launcher.running("org-abc12345") == 2


@pytest.mark.asyncio
async def test_stop_graceful_sends_sigterm_then_removes():
    container = FakeContainer("a", "w1")
    launcher, client = make_launcher()
    client.self_container = container  # containers.get returns it

    await launcher.stop("a", graceful=True)

    assert container.stopped is True
    assert container.removed is True


@pytest.mark.asyncio
async def test_prune_removes_exited_but_not_running():
    live = FakeContainer("a", "w1", status="running")
    dead = FakeContainer("b", "w2", status="exited")
    launcher, _ = make_launcher(listed=[live, dead])

    removed = await launcher.prune("org-abc12345")

    assert removed == 1
    assert dead.removed is True
    assert live.removed is False


def test_get_launcher_rejects_unknown_backend(monkeypatch):
    from daemon.launcher import get_launcher

    monkeypatch.setenv("LUCENT_WORKER_LAUNCHER", "nomad")
    with pytest.raises(ValueError, match="Unknown worker launcher"):
        get_launcher()


def test_get_launcher_defaults_to_docker(monkeypatch):
    from daemon.launcher import get_launcher

    monkeypatch.delenv("LUCENT_WORKER_LAUNCHER", raising=False)
    assert isinstance(get_launcher(controller_id="c"), DockerLauncher)
