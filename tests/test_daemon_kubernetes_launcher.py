"""KubernetesLauncher: Job manifest construction, scaling, and cleanup.

Plain unit tests: a fake ``BatchV1Api`` is injected so no cluster and no
``kubeconfig`` are needed. The launcher's client is built lazily, so nothing here
imports the real Kubernetes client.
"""

from types import SimpleNamespace

import pytest

from daemon.launcher.base import LABEL_PREFIX, WorkerSpec
from daemon.launcher.kubernetes_launcher import KubernetesLauncher

ORG = "org-abc12345"


# ── Fake Kubernetes client ──────────────────────────────────────────────────


class FakeJobConflictError(Exception):
    def __init__(self, status: int):
        super().__init__(f"({status}) Conflict: job already exists")
        self.status = status


class FakeJob:
    def __init__(
        self,
        name,
        *,
        org=ORG,
        active=1,
        succeeded=0,
        failed=0,
        conditions=None,
        created="2026-01-01T00:00:00Z",
        managed=True,
    ):
        labels = {f"{LABEL_PREFIX}.managed": "true"} if managed else {}
        if org is not None:
            labels[f"{LABEL_PREFIX}.org"] = org
        self.metadata = SimpleNamespace(name=name, creation_timestamp=created, labels=labels)
        self.status = SimpleNamespace(
            active=active,
            succeeded=succeeded,
            failed=failed,
            conditions=conditions or [],
        )


class FakeJobList:
    def __init__(self, items):
        self.items = items


class FakeBatchApi:
    def __init__(self, jobs=None, fail_create=None):
        self.jobs = list(jobs or [])
        self.fail_create = fail_create
        self.created: list[dict] = []
        self.deleted: list[tuple[str, dict]] = []

    def list_namespaced_job(self, namespace, label_selector=None):
        wanted: dict[str, str] = {}
        for part in (label_selector or "").split(","):
            if not part:
                continue
            key, _, value = part.partition("=")
            wanted[key] = value
        items = [
            job
            for job in self.jobs
            if all(job.metadata.labels.get(k) == v for k, v in wanted.items())
        ]
        return FakeJobList(items)

    def create_namespaced_job(self, namespace, body):
        if self.fail_create is not None:
            raise self.fail_create
        self.created.append(body)
        return body

    def delete_namespaced_job(self, name, namespace, **kwargs):
        self.deleted.append((name, kwargs))


def make_launcher(api: FakeBatchApi) -> KubernetesLauncher:
    launcher = KubernetesLauncher(controller_id="ctrl-1")
    launcher._api = api  # pre-seed the lazy client; no cluster needed
    return launcher


# ── Manifest: binding env, labels, hardening ────────────────────────────────


def test_job_manifest_carries_forced_binding_and_labels(monkeypatch):
    monkeypatch.setenv("LUCENT_MCP_URL", "http://lucent:8766/mcp")
    monkeypatch.setenv("LUCENT_WORKER_IMAGE", "env-image:latest")
    launcher = make_launcher(FakeBatchApi())

    # A hostile caller-supplied env must not be able to change the binding, and
    # WorkerSpec.image must win over the controller env var.
    spec = WorkerSpec(
        image="spec-image:latest",
        env={"LUCENT_DAEMON_ORG": "org-evil", "EXTRA": "1"},
    )
    manifest = launcher._job_manifest(ORG, "lucent-worker-org-abc1-abcdef", spec)

    # Top-level Job identity and labels.
    labels = manifest["metadata"]["labels"]
    assert labels[f"{LABEL_PREFIX}.managed"] == "true"
    assert labels[f"{LABEL_PREFIX}.org"] == ORG
    assert labels[f"{LABEL_PREFIX}.controller"] == "ctrl-1"
    # Pod template carries the same labels so selectors/cleanup can find it.
    assert manifest["spec"]["template"]["metadata"]["labels"] == labels

    # Job completion policy: no retries, TTL sweep, deadline above the session.
    assert manifest["spec"]["backoffLimit"] == 0
    assert manifest["spec"]["ttlSecondsAfterFinished"] > 0
    assert manifest["spec"]["activeDeadlineSeconds"] > 3600

    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "lucent-worker-org-abc1-abcdef"
    assert container["image"] == "spec-image:latest"
    assert container["command"] == ["python", "-m", "daemon.daemon"]
    assert container["workingDir"] == "/app"

    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["LUCENT_DAEMON_ORG"] == ORG
    assert env["LUCENT_DAEMON_ROLES"] == "dispatcher"
    assert env["LUCENT_DAEMON_WORKER"] == "1"
    assert env["LUCENT_DAEMON_SOURCE_RELOAD"] == "false"
    assert env["LUCENT_MCP_URL"] == "http://lucent:8766/mcp"
    assert env["EXTRA"] == "1"


def test_job_manifest_hardens_the_pod(monkeypatch):
    monkeypatch.delenv("LUCENT_WORKER_IMAGE", raising=False)
    launcher = make_launcher(FakeBatchApi())

    manifest = launcher._job_manifest(ORG, "lucent-worker-org-abc1-abcdef", WorkerSpec(image="w:1"))
    pod = manifest["spec"]["template"]["spec"]

    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert pod["terminationGracePeriodSeconds"] > 3600
    security = pod["containers"][0]["securityContext"]
    assert security["allowPrivilegeEscalation"] is False
    assert security["capabilities"]["drop"] == ["ALL"]


def test_active_deadline_tracks_the_session_timeout(monkeypatch):
    monkeypatch.setenv("LUCENT_SESSION_TIMEOUT", "100")
    launcher = make_launcher(FakeBatchApi())
    manifest = launcher._job_manifest(ORG, "n", WorkerSpec(image="w:1"))
    assert manifest["spec"]["activeDeadlineSeconds"] > 100


def test_missing_image_fails_closed(monkeypatch):
    monkeypatch.delenv("LUCENT_WORKER_IMAGE", raising=False)
    launcher = make_launcher(FakeBatchApi())

    with pytest.raises(RuntimeError, match="no image"):
        launcher._resolve_image(WorkerSpec())


# ── ensure: spawn only the shortfall, never kill ─────────────────────────────


@pytest.mark.asyncio
async def test_ensure_spawns_only_the_shortfall(monkeypatch):
    monkeypatch.setenv("LUCENT_WORKER_IMAGE", "w:1")
    live = FakeJob("lucent-worker-a", active=1)
    api = FakeBatchApi(jobs=[live])
    launcher = make_launcher(api)

    started = await launcher.ensure(ORG, 2, WorkerSpec())

    assert started == 1
    assert len(api.created) == 1
    assert api.deleted == []


@pytest.mark.asyncio
async def test_ensure_is_a_noop_when_satisfied(monkeypatch):
    monkeypatch.setenv("LUCENT_WORKER_IMAGE", "w:1")
    api = FakeBatchApi(jobs=[FakeJob("a"), FakeJob("b")])
    launcher = make_launcher(api)

    assert await launcher.ensure(ORG, 2, WorkerSpec()) == 0
    assert api.created == []


@pytest.mark.asyncio
async def test_ensure_never_kills_a_surplus(monkeypatch):
    monkeypatch.setenv("LUCENT_WORKER_IMAGE", "w:1")
    api = FakeBatchApi(jobs=[FakeJob("a"), FakeJob("b"), FakeJob("c")])
    launcher = make_launcher(api)

    assert await launcher.ensure(ORG, 1, WorkerSpec()) == 0
    assert api.created == []
    assert api.deleted == []


@pytest.mark.asyncio
async def test_ensure_tolerates_an_existing_job_conflict(monkeypatch):
    monkeypatch.setenv("LUCENT_WORKER_IMAGE", "w:1")
    api = FakeBatchApi(fail_create=FakeJobConflictError(409))
    launcher = make_launcher(api)

    # A duplicate Job is the expected race, not a failure.
    assert await launcher.ensure(ORG, 1, WorkerSpec()) == 0


# ── running / list_workers ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_running_counts_only_active_jobs():
    api = FakeBatchApi(
        jobs=[
            FakeJob("a", active=1),
            FakeJob(
                "b",
                active=0,
                succeeded=1,
                conditions=[SimpleNamespace(type="Complete", status="True")],
            ),
            FakeJob("c", active=1, org="org-other"),
        ]
    )
    launcher = make_launcher(api)

    assert await launcher.running(ORG) == 1


@pytest.mark.asyncio
async def test_list_workers_is_oldest_first():
    api = FakeBatchApi(
        jobs=[
            FakeJob("newer", created="2026-01-02T00:00:00Z"),
            FakeJob("older", created="2026-01-01T00:00:00Z"),
        ]
    )
    launcher = make_launcher(api)

    workers = await launcher.list_workers(ORG)

    assert [w.name for w in workers] == ["older", "newer"]
    assert all(w.org_id == ORG for w in workers)


# ── stop ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_graceful_uses_foreground_deletion():
    api = FakeBatchApi()
    launcher = make_launcher(api)

    await launcher.stop("lucent-worker-a", graceful=True)

    name, kwargs = api.deleted[0]
    assert name == "lucent-worker-a"
    assert kwargs["propagation_policy"] == "Foreground"


@pytest.mark.asyncio
async def test_stop_force_deletes_with_zero_grace():
    api = FakeBatchApi()
    launcher = make_launcher(api)

    await launcher.stop("lucent-worker-a", graceful=False)

    _, kwargs = api.deleted[0]
    assert kwargs["propagation_policy"] == "Background"
    assert kwargs["grace_period_seconds"] == 0


# ── prune ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_sweeps_across_all_orgs():
    finished_a = FakeJob("a", org="org-a", active=0, succeeded=1)
    finished_b = FakeJob("b", org="org-b", active=0, failed=1)
    running_c = FakeJob("c", org="org-c", active=1)
    api = FakeBatchApi(jobs=[finished_a, finished_b, running_c])
    launcher = make_launcher(api)

    removed = await launcher.prune(None)

    assert removed == 2
    deleted = {name for name, _ in api.deleted}
    assert deleted == {"a", "b"}  # the running job across orgs is spared


@pytest.mark.asyncio
async def test_prune_scopes_to_a_single_org():
    finished_a = FakeJob("a", org="org-a", active=0, succeeded=1)
    finished_b = FakeJob("b", org="org-b", active=0, succeeded=1)
    api = FakeBatchApi(jobs=[finished_a, finished_b])
    launcher = make_launcher(api)

    removed = await launcher.prune("org-a")

    assert removed == 1
    assert [name for name, _ in api.deleted] == ["a"]


@pytest.mark.asyncio
async def test_prune_spares_running_jobs():
    api = FakeBatchApi(jobs=[FakeJob("a", active=2), FakeJob("b", active=1)])
    launcher = make_launcher(api)

    assert await launcher.prune(None) == 0
    assert api.deleted == []


# ── configuration / wiring ───────────────────────────────────────────────────


def test_namespace_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("LUCENT_WORKER_NAMESPACE", raising=False)
    assert KubernetesLauncher()._namespace == "lucent-workers"

    monkeypatch.setenv("LUCENT_WORKER_NAMESPACE", "lucent")
    assert KubernetesLauncher()._namespace == "lucent"


def test_get_launcher_selects_kubernetes(monkeypatch):
    from daemon.launcher import get_launcher

    monkeypatch.setenv("LUCENT_WORKER_LAUNCHER", "kubernetes")
    assert isinstance(get_launcher(controller_id="c"), KubernetesLauncher)
