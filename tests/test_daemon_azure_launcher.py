"""AzureLauncher: execution-template construction, scaling, and cleanup.

Plain unit tests: a fake ``ContainerAppsAPIClient`` is injected so no Azure
account, subscription or SDK install is needed. The launcher builds the ARM
request bodies as wire-shaped dicts and imports the Azure SDK only when it has
to construct a real client, so nothing here imports ``azure.*``.
"""

import json
from types import SimpleNamespace

import pytest

from daemon.launcher.azure_launcher import AzureLauncher
from daemon.launcher.base import WorkerSpec

ORG = "org-abc12345"


# ── Fake Container Apps client ──────────────────────────────────────────────


class FakePoller:
    def __init__(self, value=None):
        self._value = value

    def result(self):
        return self._value


class FakeExecutions:
    def __init__(self, owner):
        self._owner = owner

    def list(self, resource_group_name, job_name, filter=None):
        return list(self._owner.executions)


class FakeJobs:
    def __init__(self, owner):
        self._owner = owner

    def get(self, resource_group_name, job_name):
        return self._owner.job

    def begin_start(self, resource_group_name, job_name, template=None, **kwargs):
        if self._owner.fail_start is not None:
            raise self._owner.fail_start
        self._owner.started.append(template)
        return FakePoller(SimpleNamespace(name=f"{job_name}-{len(self._owner.started)}"))

    def begin_stop_execution(self, resource_group_name, job_name, job_execution_name, **kwargs):
        if self._owner.fail_stop is not None:
            raise self._owner.fail_stop
        self._owner.stopped.append(job_execution_name)
        return FakePoller(None)

    def begin_update(self, resource_group_name, job_name, body, **kwargs):
        self._owner.updates.append(body)
        return FakePoller(None)


class FakeContainerAppsClient:
    def __init__(self, job=None, executions=None, fail_start=None, fail_stop=None):
        self.job = job if job is not None else make_job()
        self.executions = list(executions or [])
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.started: list[dict] = []
        self.stopped: list[str] = []
        self.updates: list[dict] = []
        self.jobs = FakeJobs(self)
        self.jobs_executions = FakeExecutions(self)


def make_base_container(name="worker", image="base:1", env=None, resources=None):
    return SimpleNamespace(
        name=name,
        image=image,
        command=["python", "-m", "daemon.daemon"],
        args=None,
        env=env if env is not None else [SimpleNamespace(name="BASE_ONLY", value="base")],
        resources=resources
        if resources is not None
        else SimpleNamespace(cpu=0.5, memory="1Gi", ephemeral_storage=None),
    )


def make_job(containers=None, init_containers=None, tags=None, environment_id=None):
    template = SimpleNamespace(
        containers=containers if containers is not None else [make_base_container()],
        init_containers=init_containers,
    )
    properties = SimpleNamespace(template=template, environment_id=environment_id)
    return SimpleNamespace(name="lucent-worker", tags=dict(tags or {}), properties=properties)


def make_execution(name, org=ORG, status="Running", start="2026-01-01T00:00:00Z", env=None):
    env_vars = list(env or [])
    if org is not None:
        env_vars.append(SimpleNamespace(name="LUCENT_DAEMON_ORG", value=org))
    container = SimpleNamespace(name="worker", image="w:1", env=env_vars)
    template = SimpleNamespace(containers=[container], init_containers=None)
    return SimpleNamespace(
        name=name, status=status, start_time=start, end_time=None, template=template
    )


def make_launcher(client=None):
    client = client if client is not None else FakeContainerAppsClient()
    launcher = AzureLauncher(
        controller_id="ctrl-1",
        subscription_id="sub-1",
        resource_group="rg-lucent",
        job_name="lucent-worker",
    )
    launcher._api = client  # pre-seed the lazy client; no Azure SDK needed
    return launcher, client


def env_of(template, index=0):
    return {item["name"]: item.get("value") for item in template["containers"][index]["env"]}


# ── Execution template: forced binding, identifiers ─────────────────────────


@pytest.mark.asyncio
async def test_execution_template_carries_the_forced_binding(monkeypatch):
    monkeypatch.setenv("LUCENT_MCP_URL", "http://lucent:8766/mcp")
    launcher, client = make_launcher()

    # A hostile caller-supplied env must not be able to change the binding.
    spec = WorkerSpec(env={"LUCENT_DAEMON_ORG": "org-evil", "EXTRA": "1"})
    started = await launcher.ensure(ORG, 1, spec)

    assert started == 1
    env = env_of(client.started[0])
    assert env["LUCENT_DAEMON_ORG"] == ORG
    assert env["LUCENT_DAEMON_ROLES"] == "dispatcher"
    assert env["LUCENT_DAEMON_WORKER"] == "1"
    assert env["LUCENT_DAEMON_SOURCE_RELOAD"] == "false"
    assert env["LUCENT_MCP_URL"] == "http://lucent:8766/mcp"
    assert env["EXTRA"] == "1"


@pytest.mark.asyncio
async def test_launcher_never_writes_job_tags():
    """The static managed tag is Terraform's to set, not the launcher's.

    The controller's custom role grants no ``Microsoft.App/jobs/write``, so an
    ARM PATCH (``jobs.begin_update``) of the Job's tags is always refused. The
    launcher must not attempt it at all -- the fake would record the body in
    ``updates`` if it did.
    """
    launcher, client = make_launcher()

    await launcher.ensure(ORG, 1, WorkerSpec())

    assert client.updates == []


@pytest.mark.asyncio
async def test_missing_managed_tag_warns_once(monkeypatch):
    """A Job without Terraform's tag is worth one WARN, not a doomed write."""
    messages: list[tuple] = []
    fake_runtime = SimpleNamespace(log=lambda *args, **kwargs: messages.append(args))
    monkeypatch.setattr("daemon.runtime.module_proxy.runtime", fake_runtime)
    job = make_job(tags={})  # no io.lucent.worker.managed
    launcher, client = make_launcher(FakeContainerAppsClient(job=job))

    # Two spawns -> two job fetches; the check is one-shot.
    await launcher.ensure(ORG, 2, WorkerSpec())

    warnings = [m for m in messages if "io.lucent.worker.managed" in m[0]]
    assert len(warnings) == 1
    assert warnings[0][1] == "WARN"
    assert client.updates == []


@pytest.mark.asyncio
async def test_present_managed_tag_does_not_warn(monkeypatch):
    messages: list[tuple] = []
    fake_runtime = SimpleNamespace(log=lambda *args, **kwargs: messages.append(args))
    monkeypatch.setattr("daemon.runtime.module_proxy.runtime", fake_runtime)
    job = make_job(tags={"io.lucent.worker.managed": "true", "managed-by": "terraform"})
    launcher, _ = make_launcher(FakeContainerAppsClient(job=job))

    await launcher.ensure(ORG, 1, WorkerSpec())

    assert [m for m in messages if "io.lucent.worker.managed" in m[0]] == []


@pytest.mark.asyncio
async def test_start_preserves_unrelated_template_fields(monkeypatch):
    """The start body replaces the whole template; nothing unrelated may drop."""
    monkeypatch.delenv("LUCENT_WORKER_IMAGE", raising=False)
    job = make_job(
        containers=[
            make_base_container(env=[SimpleNamespace(name="BASE_ONLY", value="base")]),
            make_base_container(name="sidecar", image="side:1", env=[]),
        ],
        init_containers=[make_base_container(name="init", image="init:1")],
    )
    launcher, client = make_launcher(FakeContainerAppsClient(job=job))

    await launcher.ensure(ORG, 1, WorkerSpec())

    template = client.started[0]
    assert [c["name"] for c in template["containers"]] == ["worker", "sidecar"]
    assert template["initContainers"][0]["name"] == "init"
    # Unrelated fields on the primary container survive.
    assert template["containers"][0]["resources"] == {"cpu": 0.5, "memory": "1Gi"}
    env = env_of(template)
    assert env["BASE_ONLY"] == "base"
    assert env["LUCENT_DAEMON_ORG"] == ORG
    # The untouched sidecar keeps its (empty) env and image.
    assert template["containers"][1]["env"] == []
    assert template["containers"][1]["image"] == "side:1"


@pytest.mark.asyncio
async def test_read_only_ephemeral_storage_is_not_echoed(monkeypatch):
    """``ephemeralStorage`` is read-only on the SDK's ``ContainerResources``.

    A base Job that has it set keeps it at the Job level; echoing the value into
    the execution-template ``start`` body would ask the service to set a field
    the API does not accept from a caller, so it is dropped.
    """
    monkeypatch.delenv("LUCENT_WORKER_IMAGE", raising=False)
    resources = SimpleNamespace(cpu=0.5, memory="1Gi", ephemeral_storage="4Gi")
    job = make_job(containers=[make_base_container(resources=resources)])
    launcher, client = make_launcher(FakeContainerAppsClient(job=job))

    await launcher.ensure(ORG, 1, WorkerSpec())

    assert client.started[0]["containers"][0]["resources"] == {"cpu": 0.5, "memory": "1Gi"}
    # The read-only field is nowhere in the retained start body.
    assert "ephemeralStorage" not in json.dumps(client.started[0])
    assert "4Gi" not in json.dumps(client.started[0])


# ── Execution template: secret material stays by reference ──────────────────


@pytest.mark.asyncio
async def test_base_secret_ref_is_not_replaced_by_a_literal(monkeypatch):
    """A secret the base Job supplies by reference must stay a reference.

    ``build_worker_env`` copies the resolved plaintext out of ``os.environ``; if
    it were overlaid verbatim it would be written into the ARM ``start`` body,
    which Azure stores with the execution and returns from
    ``jobs_executions.list`` -- a persisted credential leak.
    """
    secret = "postgres://lucent:sup3r-s3cr3t@db/lucent"
    monkeypatch.setenv("DATABASE_URL", secret)
    monkeypatch.delenv("DAEMON_DATABASE_URL", raising=False)
    job = make_job(
        containers=[
            make_base_container(
                env=[
                    SimpleNamespace(name="DATABASE_URL", secret_ref="database-url"),
                    SimpleNamespace(name="BASE_ONLY", value="base"),
                ]
            )
        ]
    )
    launcher, client = make_launcher(FakeContainerAppsClient(job=job))

    await launcher.ensure(ORG, 1, WorkerSpec())

    template = client.started[0]
    entry = next(e for e in template["containers"][0]["env"] if e["name"] == "DATABASE_URL")
    # The secret reference survives; it is not downgraded to a literal value.
    assert entry == {"name": "DATABASE_URL", "secretRef": "database-url"}
    assert "value" not in entry
    # The plaintext appears nowhere in the request body Azure will retain.
    body = json.dumps(template)
    assert secret not in body
    assert "s3cr3t" not in body


@pytest.mark.asyncio
async def test_non_secret_env_still_reaches_the_worker(monkeypatch):
    """Preserving secretRefs must not starve the worker of its plain env."""
    monkeypatch.setenv("LUCENT_MCP_URL", "http://lucent:8766/mcp")
    job = make_job(
        containers=[
            make_base_container(
                env=[SimpleNamespace(name="DATABASE_URL", secret_ref="database-url")]
            )
        ]
    )
    launcher, client = make_launcher(FakeContainerAppsClient(job=job))

    await launcher.ensure(ORG, 1, WorkerSpec(env={"EXTRA": "1"}))

    env = env_of(client.started[0])
    assert env["LUCENT_MCP_URL"] == "http://lucent:8766/mcp"
    assert env["EXTRA"] == "1"
    # The secret reference was kept at the same time.
    assert env_of(client.started[0])["DATABASE_URL"] is None


@pytest.mark.asyncio
async def test_forced_binding_wins_over_a_hostile_spec_even_with_a_secret_ref():
    """The binding keys are exempt from the secretRef rule: a base secretRef on
    one of them must never be able to substitute a different org."""
    job = make_job(
        containers=[
            make_base_container(
                env=[SimpleNamespace(name="LUCENT_DAEMON_ORG", secret_ref="stale-org")]
            )
        ]
    )
    launcher, client = make_launcher(FakeContainerAppsClient(job=job))

    spec = WorkerSpec(env={"LUCENT_DAEMON_ORG": "org-evil", "LUCENT_DAEMON_ROLES": "web"})
    await launcher.ensure(ORG, 1, spec)

    template = client.started[0]
    env = env_of(template)
    assert env["LUCENT_DAEMON_ORG"] == ORG
    assert env["LUCENT_DAEMON_ROLES"] == "dispatcher"
    # The binding is a literal the worker is bound to, not the base's reference.
    entry = next(e for e in template["containers"][0]["env"] if e["name"] == "LUCENT_DAEMON_ORG")
    assert entry == {"name": "LUCENT_DAEMON_ORG", "value": ORG}


@pytest.mark.asyncio
async def test_init_container_env_survives(monkeypatch):
    """Init containers must keep their env (including a secretRef); dropping it
    silently changes how the base Job initialises."""
    monkeypatch.delenv("LUCENT_WORKER_IMAGE", raising=False)
    init = make_base_container(
        name="init",
        image="init:1",
        env=[
            SimpleNamespace(name="INIT_SECRET", secret_ref="init-secret"),
            SimpleNamespace(name="INIT_PLAIN", value="p"),
        ],
    )
    job = make_job(init_containers=[init])
    launcher, client = make_launcher(FakeContainerAppsClient(job=job))

    await launcher.ensure(ORG, 1, WorkerSpec())

    init_wire = client.started[0]["initContainers"][0]
    assert init_wire["env"] == [
        {"name": "INIT_SECRET", "secretRef": "init-secret"},
        {"name": "INIT_PLAIN", "value": "p"},
    ]


@pytest.mark.asyncio
async def test_sidecar_env_survives_untouched(monkeypatch):
    monkeypatch.delenv("LUCENT_WORKER_IMAGE", raising=False)
    job = make_job(
        containers=[
            make_base_container(),
            make_base_container(
                name="sidecar",
                image="side:1",
                env=[SimpleNamespace(name="SIDE", secret_ref="side-secret")],
            ),
        ]
    )
    launcher, client = make_launcher(FakeContainerAppsClient(job=job))

    await launcher.ensure(ORG, 1, WorkerSpec())

    side = client.started[0]["containers"][1]
    assert side["env"] == [{"name": "SIDE", "secretRef": "side-secret"}]
    assert side["image"] == "side:1"


def test_worker_spec_image_wins_over_the_controller_env(monkeypatch):
    monkeypatch.setenv("LUCENT_WORKER_IMAGE", "env-image:latest")
    launcher = AzureLauncher(
        subscription_id="s", resource_group="rg", job_name="j", image="env-image:latest"
    )

    image = launcher._resolve_image(WorkerSpec(image="spec:1"), make_base_container())

    assert image == "spec:1"


def test_missing_image_fails_closed(monkeypatch):
    monkeypatch.delenv("LUCENT_WORKER_IMAGE", raising=False)
    launcher = AzureLauncher(subscription_id="s", resource_group="rg", job_name="j")

    with pytest.raises(RuntimeError, match="no image"):
        launcher._resolve_image(WorkerSpec(), SimpleNamespace(image=None))


# ── ensure: spawn only the shortfall, never kill ────────────────────────────


@pytest.mark.asyncio
async def test_ensure_spawns_only_the_shortfall():
    client = FakeContainerAppsClient(executions=[make_execution("e1", status="Running")])
    launcher, client = make_launcher(client)

    started = await launcher.ensure(ORG, 2, WorkerSpec())

    assert started == 1
    assert len(client.started) == 1
    assert client.stopped == []


@pytest.mark.asyncio
async def test_ensure_is_a_noop_when_satisfied():
    client = FakeContainerAppsClient(executions=[make_execution("e1"), make_execution("e2")])
    launcher, client = make_launcher(client)

    assert await launcher.ensure(ORG, 2, WorkerSpec()) == 0
    assert client.started == []


@pytest.mark.asyncio
async def test_ensure_never_kills_a_surplus():
    client = FakeContainerAppsClient(executions=[make_execution(f"e{i}") for i in range(3)])
    launcher, client = make_launcher(client)

    assert await launcher.ensure(ORG, 1, WorkerSpec()) == 0
    assert client.started == []
    assert client.stopped == []


# ── running / list_workers ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_running_counts_only_active_managed_executions():
    client = FakeContainerAppsClient(
        executions=[
            make_execution("running", status="Running"),
            make_execution("processing", status="Processing"),
            make_execution("done", status="Succeeded"),
            make_execution("other-org", org="org-other", status="Running"),
            make_execution("manual", org=None, status="Running"),  # not managed
        ]
    )
    launcher, _ = make_launcher(client)

    assert await launcher.running(ORG) == 2


@pytest.mark.asyncio
async def test_list_workers_is_oldest_first():
    client = FakeContainerAppsClient(
        executions=[
            make_execution("newer", start="2026-01-02T00:00:00Z"),
            make_execution("older", start="2026-01-01T00:00:00Z"),
        ]
    )
    launcher, _ = make_launcher(client)

    workers = await launcher.list_workers(ORG)

    assert [w.name for w in workers] == ["older", "newer"]
    assert all(w.org_id == ORG for w in workers)


# ── stop ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_terminates_the_execution():
    launcher, client = make_launcher()

    await launcher.stop("lucent-worker-123", graceful=True)

    assert client.stopped == ["lucent-worker-123"]


# ── prune ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_is_an_honest_noop():
    """There is no delete-execution API, and Azure only stops a *running*
    execution -- so finished executions cannot be swept, and stopping the running
    ones would break the controller's never-kill contract. prune collects
    nothing, reports 0, and makes no Azure mutation."""
    client = FakeContainerAppsClient(
        executions=[
            make_execution("done", status="Succeeded"),
            make_execution("failed", status="Failed"),
            make_execution("running", status="Running"),
            make_execution("manual", org=None, status="Succeeded"),
        ]
    )
    launcher, client = make_launcher(client)

    assert await launcher.prune(None) == 0
    assert await launcher.prune("org-a") == 0
    assert client.stopped == []


# ── failure handling ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_failure_is_handled_without_raising():
    client = FakeContainerAppsClient(
        fail_start=RuntimeError("(403) Forbidden: missing jobs/start/action")
    )
    launcher, client = make_launcher(client)

    # A rejected start is logged, not raised into the supervisor.
    assert await launcher.ensure(ORG, 1, WorkerSpec()) == 0


@pytest.mark.asyncio
async def test_missing_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("LUCENT_WORKER_JOB_NAME", raising=False)
    monkeypatch.delenv("LUCENT_WORKER_RESOURCE_GROUP", raising=False)
    launcher = AzureLauncher(subscription_id="sub")

    with pytest.raises(RuntimeError, match="not configured"):
        await launcher.running(ORG)


# ── configuration / wiring ───────────────────────────────────────────────────


def test_configuration_defaults_from_environment(monkeypatch):
    monkeypatch.setenv("LUCENT_WORKER_JOB_NAME", "lucent-worker-job")
    monkeypatch.setenv("LUCENT_WORKER_RESOURCE_GROUP", "rg-lucent")
    monkeypatch.setenv("LUCENT_WORKER_SUBSCRIPTION_ID", "sub-xyz")
    monkeypatch.setenv("LUCENT_WORKER_CONTAINER_APP_ENV", "cae-lucent")

    launcher = AzureLauncher(controller_id="ctrl-9")

    assert launcher._job_name == "lucent-worker-job"
    assert launcher._resource_group == "rg-lucent"
    assert launcher._subscription_id == "sub-xyz"
    assert launcher._container_app_env == "cae-lucent"


def test_get_launcher_selects_azure(monkeypatch):
    from daemon.launcher import get_launcher

    monkeypatch.setenv("LUCENT_WORKER_LAUNCHER", "azure")
    assert isinstance(get_launcher(controller_id="c"), AzureLauncher)
