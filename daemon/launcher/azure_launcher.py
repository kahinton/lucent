"""Azure Container Apps Jobs-backed worker launcher.

Workers are **executions** of a single base Container Apps *Job*, started by the
always-on controller. A manual-trigger Job is a good fit for this role: it is
started programmatically through the ARM API, runs at the active rate with no
idle charge and no 60-second minimum (unlike ACI), and no Kubernetes cluster is
required.

Security / identity
-------------------
The controller starts executions with its **managed identity** (through
``DefaultAzureCredential``) -- no stored secret. That identity must hold a
**narrow** custom role scoped to this one Job, granting only
``Microsoft.App/jobs/start/action`` (plus ``Microsoft.App/jobs/executions/read``
if the controller later needs to read execution *contents*). Do **not** grant a
wildcard ``Microsoft.App/*`` or a Contributor built-in: on a Container Apps Job
``start`` resolves the job's secrets into the launched execution, so an
over-scoped identity leaks tenant credentials. Terraform provisions the identity
and the role; this module only uses them.

Worker environment
------------------
The worker env is always built by :func:`daemon.launcher.base.build_worker_env`,
never hand-rolled: it strips binding keys out of caller-supplied env and forces
the authoritative org binding last, so nothing a task or a hostile spec supplies
can change which organization a worker serves.

The execution-template trap
---------------------------
``jobs.start``'s request body **replaces the entire execution template, not a
patch**. Sending a bare env override would silently drop the base Job's other
container settings (resources, extra/init containers, unrelated env). So every
start first fetches the Job, copies its template, mutates only the primary
container's env/image, and resends the whole template.

Identification -- and its honest limits
---------------------------------------
Docker and Kubernetes attach ``io.lucent.worker.{managed,org,controller}`` labels
to each worker resource. Container Apps **executions cannot carry labels or
tags**; the only taggable resource is the Job itself, and one Job serves *every*
org, so per-execution identifiers cannot be expressed as tags at all. What is
left is a split, and it is worth stating exactly what is -- and is not --
represented on Azure:

* **The Job carries one static tag**, ``io.lucent.worker.managed=true``. It is
  set by Terraform on the workers job, *not* by this launcher: the controller's
  role deliberately grants no ``Microsoft.App/jobs/write`` (see rbac.tf), so a
  ``jobs.begin_update`` (an ARM PATCH) would always be refused -- and it would
  be the wrong tool anyway, because the tag is a static property of the
  deployment, not a per-process fact. The launcher only *reads* the Job
  (``jobs/read``, which the role does grant) and warns once if the tag is
  missing; it never attempts the write.
* **Per-execution identity is the forced binding env** carried in the execution
  template: ``LUCENT_DAEMON_ORG`` names the org an execution serves, and its
  presence marks the execution as Lucent-managed. It is read back from
  ``execution.template.containers[*].env`` to attribute an execution to an org.
  Because it is the authoritative value the worker is itself bound to, it cannot
  drift from a separate bookkeeping record.
* **The controller's instance id is not represented on Azure at all.** It is
  per-process (each controller boot is a new id), so it cannot be a static Job
  tag, and executions are not taggable, so it cannot ride with an execution
  either. That is a genuine loss against Docker/Kubernetes labels and is stated
  here rather than papered over; nothing infers anything from it.

Limits of that choice: org attribution is *not* server-side filterable, so
``list_workers`` pages the executions endpoint and filters client-side; and the
executions endpoint returns only the **most recent 100 executions**, so a very
old still-running execution can fall outside the window and be undercounted.
The controller's reconcile loop is level-based and tolerates that (it may start
one extra worker rather than silently under-provision).

Like the other backends, the client is built lazily and every blocking SDK call
is pushed through :func:`asyncio.to_thread`, because this runs on the daemon's
event loop.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from daemon.launcher.base import (
    BINDING_ENV_KEYS,
    LABEL_PREFIX,
    WorkerHandle,
    WorkerLauncher,
    WorkerSpec,
    build_worker_env,
)

# Configuration, all from the controller's environment (or explicit overrides).
# The base Job is identified by name + resource group; the subscription is what
# the ARM client needs. ``LUCENT_WORKER_CONTAINER_APP_ENV`` is the Container Apps
# environment the Job lives in; it is not required by the start/executions API
# (job names are unique within the resource group) and is used only to sanity-
# check the fetched Job on first use.
_JOB_NAME_ENV = "LUCENT_WORKER_JOB_NAME"
_RESOURCE_GROUP_ENV = "LUCENT_WORKER_RESOURCE_GROUP"
_CONTAINER_APP_ENV = "LUCENT_WORKER_CONTAINER_APP_ENV"
_SUBSCRIPTION_ID_ENV = "LUCENT_WORKER_SUBSCRIPTION_ID"
# Conventional ARM variable, used as a fallback when the Lucent-specific one is
# unset, so a controller configured the usual Azure way still works.
_FALLBACK_SUBSCRIPTION_ID_ENV = "AZURE_SUBSCRIPTION_ID"

# The org binding env key. Its presence in an execution's template is what marks
# that execution as a Lucent-managed worker (a manually "Run now" execution has
# the base Job's template, which carries no org binding).
_ORG_ENV_KEY = "LUCENT_DAEMON_ORG"

# ``JobExecutionRunningState`` values. Active = a replica should be up; anything
# else (succeeded/failed/stopped/degraded/unknown) is treated as not active.
# ``unknown`` is deliberately not active: ``list_workers`` under-counts it, which
# is the conservative direction for scaling. There is no "finished" set any more:
# ``prune`` has nothing to do on Azure and does not consult execution states (see
# its docstring).
_ACTIVE_STATES = frozenset({"running", "processing"})


def _wire_env(item: Any) -> dict[str, str]:
    """Convert an env model to its wire (camelCase) shape, preserving secrets."""
    out: dict[str, str] = {"name": getattr(item, "name", "")}
    value = getattr(item, "value", None)
    if value is not None:
        out["value"] = value
    secret = getattr(item, "secret_ref", None) or getattr(item, "secretRef", None)
    if secret:
        out["secretRef"] = secret
    return out


def _wire_resources(resources: Any) -> dict[str, Any] | None:
    """Copy ``cpu`` and ``memory`` only.

    ``ephemeralStorage`` is a **read-only** field on the SDK's
    ``ContainerResources`` (``visibility=["read"]``): it reports the platform's
    per-replica scratch space. It is not a caller-settable knob, so echoing it
    into the ``start`` body would ask the service to set a value the API does not
    accept -- which it may reject the request over or silently ignore. A base Job
    that has ephemeral storage set keeps it at the Job level; it is simply not
    carried into the execution template.
    """
    if resources is None:
        return None
    out: dict[str, Any] = {}
    for attr in ("cpu", "memory"):
        value = getattr(resources, attr, None)
        if value is not None:
            out[attr] = value
    return out or None


def _wire_container(base: Any) -> dict[str, Any]:
    """Copy one base-Job container into the wire shape, preserving every field
    that an execution template (``JobExecutionContainer``) can express.

    That set is exactly ``name``, ``image``, ``command``, ``args``, ``env`` and
    ``resources``. Probes and volume mounts exist on the Job's ``Container``
    model but not on ``JobExecutionContainer``, so they cannot be carried in a
    start body at all -- a platform limit, surfaced here rather than papered
    over. ``env`` is copied faithfully (including ``secretRef`` entries) so an
    init container or sidecar never silently loses a variable.
    """
    out: dict[str, Any] = {
        "name": getattr(base, "name", ""),
        "image": getattr(base, "image", None),
    }
    command = getattr(base, "command", None)
    if command is not None:
        out["command"] = list(command)
    args = getattr(base, "args", None)
    if args is not None:
        out["args"] = list(args)
    out["env"] = [_wire_env(e) for e in (getattr(base, "env", None) or [])]
    resources = _wire_resources(getattr(base, "resources", None))
    if resources is not None:
        out["resources"] = resources
    return out


def _overlay_worker_env(
    base_env: list[dict[str, Any]], worker_env: dict[str, str]
) -> list[dict[str, Any]]:
    """Overlay the per-execution worker env onto a base container's env.

    The base Job is the source of truth for **secret material**. On Azure the
    base container supplies ``DATABASE_URL`` / ``DAEMON_DATABASE_URL`` /
    ``LUCENT_MCP_API_KEY`` as Container Apps **secret references** (Key Vault
    backed), which the platform resolves into the process environment.
    ``build_worker_env`` copies those resolved plaintexts out of ``os.environ``,
    so overlaying them verbatim would replace the base's ``secretRef`` with a
    literal -- and that literal is sent in the ARM ``start`` body, which Azure
    **stores with the execution and returns from ``jobs_executions.list``**,
    persisting the DB credentials in retained execution history. So a key the
    base already supplies by reference is left as a reference: the launcher only
    supplies non-secret, per-execution values.

    The forced binding keys are exempt -- they are never secret, and the
    authoritative org binding must always be applied as a literal (a base
    ``secretRef`` on one of them could otherwise smuggle in a stale org).

    Names are keyed so an overlay replaces a same-named base entry in place
    rather than duplicating it.
    """
    env = {item["name"]: item for item in base_env}
    for key, value in worker_env.items():
        existing = env.get(key)
        if key not in BINDING_ENV_KEYS and existing and existing.get("secretRef"):
            continue  # keep the base's secret reference; never write the literal
        env[key] = {"name": key, "value": value}
    return list(env.values())


class AzureLauncher(WorkerLauncher):
    """Spawns workers as executions of a Container Apps Job."""

    def __init__(
        self,
        *,
        controller_id: str = "",
        subscription_id: str | None = None,
        resource_group: str | None = None,
        job_name: str | None = None,
        container_app_env: str | None = None,
        image: str | None = None,
    ):
        self._api: Any = None
        self._controller_id = controller_id
        self._subscription_id = (
            subscription_id
            or os.environ.get(_SUBSCRIPTION_ID_ENV)
            or os.environ.get(_FALLBACK_SUBSCRIPTION_ID_ENV)
            or ""
        )
        self._resource_group = resource_group or os.environ.get(_RESOURCE_GROUP_ENV) or ""
        self._job_name = job_name or os.environ.get(_JOB_NAME_ENV) or ""
        self._container_app_env = container_app_env or os.environ.get(_CONTAINER_APP_ENV) or ""
        # Explicit override; ``WorkerSpec.image`` still takes precedence, and the
        # base Job's own image is the final fallback (it always has one).
        self._image = image or os.environ.get("LUCENT_WORKER_IMAGE")
        self._tags_checked = False
        self._env_checked = False

    # ── Azure client ──────────────────────────────────────────────────────

    def _client(self) -> Any:
        """Return the lazily built ``ContainerAppsAPIClient``.

        Imports the Azure SDK here rather than at module scope so the package
        (and the unit tests) do not require the SDK to be installed unless the
        Azure backend is actually used.
        """
        if self._api is not None:
            return self._api
        if not self._subscription_id:
            raise RuntimeError(
                "AzureLauncher requires a subscription id: set "
                f"{_SUBSCRIPTION_ID_ENV} (or {_FALLBACK_SUBSCRIPTION_ID_ENV})."
            )
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.appcontainers import ContainerAppsAPIClient
        except ImportError as exc:  # pragma: no cover - exercised only without the dep
            raise RuntimeError(
                "Azure worker launcher requires the 'azure-identity' and "
                "'azure-mgmt-appcontainers' packages"
            ) from exc
        self._api = ContainerAppsAPIClient(
            credential=DefaultAzureCredential(),
            subscription_id=self._subscription_id,
        )
        return self._api

    def _require_config(self) -> None:
        """Fail closed when the base Job is not identified.

        A worker pointed at the wrong (or a missing) job is worse than no worker,
        so this raises rather than guessing.
        """
        missing = [
            name
            for name, value in (
                (_RESOURCE_GROUP_ENV, self._resource_group),
                (_JOB_NAME_ENV, self._job_name),
            )
            if not value
        ]
        if missing:
            raise RuntimeError("AzureLauncher is not configured; set " + ", ".join(missing) + ".")

    # ── Job identification ────────────────────────────────────────────────

    async def _get_job(self) -> Any:
        self._require_config()
        return await asyncio.to_thread(
            self._client().jobs.get, self._resource_group, self._job_name
        )

    def _check_job_tags(self, job: Any) -> None:
        """Warn once if the Job lacks Terraform's static managed tag.

        The ``io.lucent.worker.managed=true`` tag is applied by Terraform to the
        workers job (see container_apps.tf), because it describes the deployment,
        not a running controller. This launcher **cannot** set it: the
        controller's custom role grants no ``Microsoft.App/jobs/write``, so an
        ARM PATCH of the job's tags is always refused -- and widening the role to
        allow it would also let the holder rewrite the job template and its
        secrets, which is exactly what the narrow role exists to prevent. So this
        is a read-only cross-check against ``jobs/read`` (which the role does
        grant): a missing tag usually means a job that was not created by this
        module, and that is worth surfacing once rather than silently trusting.
        """
        if self._tags_checked:
            return
        self._tags_checked = True
        from daemon.runtime.module_proxy import runtime

        tags = dict(getattr(job, "tags", None) or {})
        if tags.get(f"{LABEL_PREFIX}.managed") != "true":
            runtime.log(
                f"Worker job {self._job_name!r} is missing the "
                f"{LABEL_PREFIX}.managed=true tag; Terraform sets it on the job "
                "(the controller's role has no jobs/write and cannot).",
                "WARN",
            )

    def _check_environment(self, job: Any) -> None:
        """Sanity-check that the Job lives in the configured Container Apps env.

        The API does not need the environment name, so this is a warning-only
        cross-check: a mismatch usually means a misconfigured deployment, and it
        is worth surfacing once rather than starting workers in the wrong place.
        """
        if self._env_checked or not self._container_app_env:
            return
        self._env_checked = True
        from daemon.runtime.module_proxy import runtime

        environment_id = str(getattr(getattr(job, "properties", None), "environment_id", "") or "")
        if environment_id and self._container_app_env not in environment_id:
            runtime.log(
                f"Worker job {self._job_name!r} is not in the configured Container "
                f"Apps environment {self._container_app_env!r} (environment_id="
                f"{environment_id!r})",
                "WARN",
            )

    def _resolve_image(self, spec: WorkerSpec, base_container: Any) -> str:
        """Resolve the worker image, failing closed when no source yields one.

        ``WorkerSpec.image`` wins over the controller's ``LUCENT_WORKER_IMAGE``
        (matching Docker/Kubernetes). Azure has a third source the others lack:
        the base Job's own container image, which the platform requires to be
        set anyway -- it is used only as the last fallback.
        """
        image = spec.image or self._image or getattr(base_container, "image", None)
        if not image:
            raise RuntimeError(
                "AzureLauncher cannot start a worker: no image. Set "
                "LUCENT_WORKER_IMAGE or pass WorkerSpec(image=...)."
            )
        return image

    # ── Execution template construction ───────────────────────────────────

    def _build_execution_template(self, job: Any, org_id: str, spec: WorkerSpec) -> dict[str, Any]:
        """Build the execution template from the Job's template, mutated in place.

        This is the whole guard against the start-replaces-template trap: the
        base template is copied wholesale (all containers, init containers,
        resources and unrelated env), then only the primary container's env,
        image and command are changed, and the entire template is resent.
        """
        base_template = getattr(getattr(job, "properties", None), "template", None)
        base_containers = list(getattr(base_template, "containers", None) or [])
        if not base_containers:
            raise RuntimeError(
                "AzureLauncher cannot start a worker: the base job has no container template."
            )
        image = self._resolve_image(spec, base_containers[0])
        worker_env = build_worker_env(org_id, spec.env)

        containers: list[dict[str, Any]] = []
        for index, base in enumerate(base_containers):
            wire = _wire_container(base)
            if index == 0:
                # Mutate only the primary container: resolve the image and
                # command, and overlay the per-execution env. The overlay keeps
                # any base ``secretRef`` (see _overlay_worker_env) so a resolved
                # plaintext secret is never written into the start body. Every
                # other container -- sidecars and, below, init containers -- is
                # copied faithfully, env included.
                wire["image"] = image
                wire["command"] = list(spec.command)
                wire["env"] = _overlay_worker_env(wire["env"], worker_env)
            containers.append(wire)

        template: dict[str, Any] = {"containers": containers}
        init_containers = [
            _wire_container(c) for c in (getattr(base_template, "init_containers", None) or [])
        ]
        if init_containers:
            template["initContainers"] = init_containers
        return template

    # ── Execution discovery ───────────────────────────────────────────────

    async def _list_executions(self) -> list[Any]:
        """List the Job's executions -- bounded to the most recent 100 by Azure."""
        self._require_config()
        client = self._client()
        # The executions endpoint has no queryable org field, so this is always
        # the whole (bounded) history; callers filter client-side.
        result = await asyncio.to_thread(
            client.jobs_executions.list, self._resource_group, self._job_name
        )
        return list(result)

    @staticmethod
    def _status(execution: Any) -> str:
        return str(getattr(execution, "status", "") or "").lower()

    @staticmethod
    def _execution_org(execution: Any) -> str | None:
        """Recover the org an execution serves from its forced binding env.

        Returns ``None`` for an execution that carries no org binding (e.g. a
        manual "Run now"), which is exactly how a non-managed execution is
        excluded.
        """
        template = getattr(execution, "template", None)
        for container in getattr(template, "containers", None) or []:
            for item in getattr(container, "env", None) or []:
                if getattr(item, "name", None) == _ORG_ENV_KEY:
                    return getattr(item, "value", None)
        return None

    # ── WorkerLauncher ────────────────────────────────────────────────────

    async def ensure(self, org_id: str, count: int, spec: WorkerSpec) -> int:
        have = await self.running(org_id)
        shortfall = max(0, count - have)
        if shortfall == 0:
            return 0
        started = 0
        for _ in range(shortfall):
            if await self._start_one(org_id, spec):
                started += 1
        return started

    async def running(self, org_id: str) -> int:
        return len(await self.list_workers(org_id))

    async def list_workers(self, org_id: str) -> list[WorkerHandle]:
        executions = await self._list_executions()
        active = [
            execution
            for execution in executions
            if self._execution_org(execution) == org_id
            and self._status(execution) in _ACTIVE_STATES
        ]
        active.sort(key=lambda execution: str(getattr(execution, "start_time", "") or ""))
        return [
            WorkerHandle(
                worker_id=str(getattr(execution, "name", "")),
                org_id=org_id,
                status=self._status(execution),
                name=str(getattr(execution, "name", "")),
            )
            for execution in active
        ]

    async def stop(self, worker_id: str, *, graceful: bool = True) -> None:
        """Terminate one execution.

        Container Apps exposes a single "stop execution" operation; unlike a
        Kubernetes Job deletion there is no foreground/background propagation
        choice, so ``graceful`` cannot be expressed as a distinct behaviour. The
        platform terminates the execution and the container is given its own
        shutdown window, so the worker's drain path still runs either way.
        """
        self._require_config()
        poller = await asyncio.to_thread(
            self._client().jobs.begin_stop_execution,
            self._resource_group,
            self._job_name,
            worker_id,
        )
        await asyncio.to_thread(poller.result)

    async def prune(self, org_id: str | None = None) -> int:
        """Nothing to collect on Azure -- and that is the honest answer, not a bug.

        Container Apps has **no delete-execution API**: an execution cannot be
        removed, so unlike a Docker container or a Kubernetes Job there is no
        dead record for this sweep to collect. The Job keeps a bounded history of
        the most recent executions and the platform reaps older ones on its own.

        The single mutation the platform offers is ``jobs.stop_execution``, and
        it is asymmetric in a way the previous version of this method got wrong:

        * it terminates a **running** execution (``Running`` / ``Processing``);
          Azure rejects it for any terminal state (``Succeeded`` / ``Failed`` /
          ``Stopped`` / ``Degraded``) with a "not running" error. Stopping
          *finished* executions could therefore never succeed, and the old bare
          ``except`` swallowed the rejection, so every pass swept nothing while
          appearing to work.
        * stopping an *active* execution is precisely what this launcher must not
          do: a worker is never killed out from under an in-flight session (see
          :meth:`WorkerLauncher.ensure`); scale-in is the worker's own idle-exit.

        So there is genuinely nothing for ``prune`` to do here, and it returns 0
        without contacting Azure. A maintainer should not "fix" that by stopping
        executions: it would contradict the never-kill contract *and* still fail
        for the finished executions this sweep was originally aimed at. ``org_id``
        is accepted for interface parity and deliberately ignored.
        """
        return 0

    # ── Internals ─────────────────────────────────────────────────────────

    async def _start_one(self, org_id: str, spec: WorkerSpec) -> bool:
        from daemon.runtime.module_proxy import runtime

        try:
            job = await self._get_job()
            template = self._build_execution_template(job, org_id, spec)
        except Exception as error:
            # Misconfiguration (no Job, no image) is fatal for the caller to see;
            # the supervisor logs it. A start *failure* below is not.
            runtime.log(f"Worker launcher cannot start workers: {error}", "ERROR")
            raise

        self._check_environment(job)
        self._check_job_tags(job)

        try:
            poller = await asyncio.to_thread(
                self._client().jobs.begin_start,
                self._resource_group,
                self._job_name,
                template,
            )
            # Wait for the start LRO so a rejected start (e.g. a missing RBAC
            # action) surfaces here and is handled, not swallowed by the platform.
            execution = await asyncio.to_thread(poller.result)
            name = str(getattr(execution, "name", "") or "")
            runtime.log(f"Started worker execution {name} for org {org_id[:8]}")
            return True
        except Exception as error:
            runtime.log(f"Failed to start worker for org {org_id[:8]}: {error}", "ERROR")
            return False
