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

    def list(self, all=False, filters=None, **kwargs):
        self._owner.list_kwargs.append(kwargs)
        # docker-py re-inspects each container while enumerating, so a vanished
        # one raises from list() itself unless ignore_removed=True is passed.
        owner = self._owner
        if any(getattr(c, "vanished", False) for c in owner.listed) and not kwargs.get(
            "ignore_removed"
        ):
            from docker.errors import NotFound

            raise NotFound("No such container: vanished")
        return list(owner.listed)


class FakeClient:
    def __init__(self, self_container, listed=None):
        self.self_container = self_container
        self.listed = listed or []
        self.runs: list[dict] = []
        self.list_kwargs: list[dict] = []
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


def test_worker_env_carries_the_aci_sandbox_configuration(monkeypatch):
    """Task sandboxes are created by the dispatcher, so workers need the ACI
    settings — the web process having them is not enough."""
    expected = {
        "LUCENT_SANDBOX_BACKEND": "aci",
        "LUCENT_SANDBOX_BRIDGE_API_URL": "https://lucent.example.com/api",
        "LUCENT_SANDBOX_ACI_RESOURCE_GROUP": "lucent-sandbox-rg",
        "LUCENT_SANDBOX_ACI_SUBNET_ID": "/subscriptions/s/resourceGroups/rg/providers/x",
        "LUCENT_SANDBOX_ACI_IDENTITY_ID": "/subscriptions/s/identities/pull",
        "LUCENT_SANDBOX_ACI_LOCATION": "eastus",
        "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
        "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
    }
    for key, value in expected.items():
        monkeypatch.setenv(key, value)

    env = build_worker_env("org-123")

    for key, value in expected.items():
        assert env[key] == value


def test_worker_identity_is_mapped_for_the_worker_only(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "controller-identity")
    monkeypatch.setenv("LUCENT_WORKER_AZURE_CLIENT_ID", "app-identity")

    env = build_worker_env("org-123")

    assert env["AZURE_CLIENT_ID"] == "app-identity"


def test_worker_never_inherits_the_controller_identity(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "controller-identity")
    monkeypatch.delenv("LUCENT_WORKER_AZURE_CLIENT_ID", raising=False)

    env = build_worker_env("org-123")

    assert "AZURE_CLIENT_ID" not in env


def test_worker_identity_ignores_a_caller_supplied_client_id(monkeypatch):
    monkeypatch.delenv("LUCENT_WORKER_AZURE_CLIENT_ID", raising=False)

    env = build_worker_env("org-123", {"AZURE_CLIENT_ID": "org-controlled"})

    assert "AZURE_CLIENT_ID" not in env


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
            def list(all=False, filters=None, **kwargs):
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
            def list(all=False, filters=None, **kwargs):
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


@pytest.mark.asyncio
async def test_prune_tolerates_a_container_that_vanishes_mid_enumeration():
    """A worker exiting between list() and inspect() must not break prune.

    docker-py's ``containers.list()`` calls ``self.get()`` (which inspects each
    container) for every entry, so a container removed during enumeration raises
    ``NotFound`` *inside list()* — no per-container guard downstream can catch
    it. That aborted the entire pass, and because the listing order is stable the
    same dead entry failed first every time: prune never made progress and
    exited workers accumulated without bound (100 containers and 29 live daemons
    in a 4 GB VM, which OOM-killed the web server).

    The fix is docker-py's own ``ignore_removed=True`` for that exact race.
    """
    vanished = FakeContainer("gone", "w-gone", status="exited")
    vanished.vanished = True

    def already_gone(force=False):
        from docker.errors import NotFound

        raise NotFound("No such container: gone")

    vanished.remove = already_gone
    dead = FakeContainer("a", "w1", status="exited")
    launcher, client = make_launcher(listed=[vanished, dead])

    removed = await launcher.prune("org-abc12345")

    # The pass completed instead of raising, and still cleaned up what it could.
    assert dead.removed is True
    # The already-gone container counts as settled: that is the goal state.
    assert removed == 2
    # And it asked docker-py to tolerate the race rather than papering over it.
    assert client.list_kwargs[-1].get("ignore_removed") is True


@pytest.mark.asyncio
async def test_worker_count_survives_a_worker_vanishing_mid_enumeration():
    """The scale-up decision must not break when a worker exits while counting.

    A failed count is a lost data point, and the controller responds by spawning
    replacements — one of the ways worker counts ran away.
    """
    gone = FakeContainer("gone", "w-gone")
    gone.vanished = True
    fine = FakeContainer("b", "w2")
    launcher, client = make_launcher(listed=[gone, fine])

    # The enumeration tolerated the race; the fake still returns both entries,
    # which is the count docker-py itself would yield with ignore_removed.
    assert await launcher.running("org-abc12345") == 2
    assert client.list_kwargs[-1].get("ignore_removed") is True


@pytest.mark.asyncio
async def test_prune_does_not_let_a_removal_error_stop_the_pass():
    """A container that refuses removal must not block the ones after it."""
    stubborn = FakeContainer("s", "w-stuck", status="exited")

    def boom(force=False):
        raise RuntimeError("device or resource busy")

    stubborn.remove = boom
    good = FakeContainer("g", "w-ok", status="exited")
    launcher, _ = make_launcher(listed=[stubborn, good])

    removed = await launcher.prune("org-abc12345")

    assert good.removed is True
    assert removed == 1


def test_get_launcher_rejects_unknown_backend(monkeypatch):
    from daemon.launcher import get_launcher

    monkeypatch.setenv("LUCENT_WORKER_LAUNCHER", "nomad")
    with pytest.raises(ValueError, match="Unknown worker launcher"):
        get_launcher()


def test_get_launcher_defaults_to_docker(monkeypatch):
    from daemon.launcher import get_launcher

    monkeypatch.delenv("LUCENT_WORKER_LAUNCHER", raising=False)
    assert isinstance(get_launcher(controller_id="c"), DockerLauncher)
