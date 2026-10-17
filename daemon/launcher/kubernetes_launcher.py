"""Kubernetes-backed worker launcher.

Workers run as ephemeral **Jobs** in a cluster, created by the always-on
controller. A Job (not a bare Pod) is used so the platform gives us a real
completion record: ``backoffLimit`` bounds retries, ``ttlSecondsAfterFinished``
reaps finished Jobs even if ``prune()`` never runs, and ``activeDeadlineSeconds``
is sized above the session timeout so a long session is never killed mid-flight.

The controller runs in-cluster (or against a kubeconfig in dev), so the client is
built lazily -- ``load_incluster_config`` first, ``load_kube_config`` as the dev
fallback -- and every blocking client call is pushed through
``asyncio.to_thread`` because this runs on the daemon's event loop.

Security mirrors the sandbox backend: the worker environment comes from
:func:`daemon.launcher.base.build_worker_env` (the authoritative org binding is
applied last and can never be overridden by a caller), the pod does not mount a
service-account token, runs with the ``RuntimeDefault`` seccomp profile, and
drops all capabilities. The org binding is carried both as the
``io.lucent.worker.org`` label and as an environment variable, so the launcher
can find a worker's Job without ever trusting caller-supplied env.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from typing import Any

from daemon.launcher.base import (
    LABEL_PREFIX,
    WorkerHandle,
    WorkerLauncher,
    WorkerSpec,
    build_worker_env,
)

# Namespace the workers run in. The Helm chart sets this to its release
# namespace; the default mirrors the sandbox backend's default so a dev cluster
# works without extra configuration.
_DEFAULT_NAMESPACE = "lucent-workers"

# The worker hard-times-out a session at ``daemon.session_timeout_seconds``
# (default 3600). ``activeDeadlineSeconds`` and the pod's drain grace are set
# comfortably above that so the platform never kills a session mid-flight; the
# daemon's own drain budget is session + 60s.
_DEFAULT_SESSION_TIMEOUT_SECONDS = 3600
_DEADLINE_BUFFER_SECONDS = 300

# How long a finished Job's record (and logs) survive before the platform reaps
# it. ``prune()`` is the primary sweep; this is the backstop.
_JOB_TTL_SECONDS = 900


def _session_timeout_seconds() -> int:
    """The controller's session timeout, with a safe fallback."""
    raw = os.environ.get("LUCENT_SESSION_TIMEOUT")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return _DEFAULT_SESSION_TIMEOUT_SECONDS


def _worker_deadline_seconds() -> int:
    """Job ``activeDeadlineSeconds`` / pod drain grace: above the session timeout."""
    return _session_timeout_seconds() + _DEADLINE_BUFFER_SECONDS


def _sanitize_name(value: str) -> str:
    """Coerce ``value`` to a DNS-1123 label fragment (lowercase alnum and '-')."""
    cleaned = "".join(c if c.isalnum() or c == "-" else "-" for c in value.lower())
    return cleaned.strip("-")


class KubernetesLauncher(WorkerLauncher):
    """Spawns workers as Kubernetes Jobs on a cluster."""

    def __init__(
        self,
        *,
        controller_id: str = "",
        namespace: str | None = None,
        image: str | None = None,
        kubeconfig: str | None = None,
    ):
        self._api: Any = None
        self._controller_id = controller_id
        self._namespace = (
            namespace or os.environ.get("LUCENT_WORKER_NAMESPACE") or _DEFAULT_NAMESPACE
        )
        # Explicit override; ``WorkerSpec.image`` still takes precedence.
        self._image = image or os.environ.get("LUCENT_WORKER_IMAGE")
        self._kubeconfig = kubeconfig

    # ── Kubernetes client ─────────────────────────────────────────────────

    def _clients(self) -> Any:
        """Return the (lazily built) ``BatchV1Api`` client."""
        if self._api is not None:
            return self._api
        try:
            from kubernetes import client, config
        except ImportError as exc:  # pragma: no cover - exercised only without the dep
            raise RuntimeError(
                "Kubernetes worker launcher requires the 'kubernetes' package"
            ) from exc
        if self._kubeconfig:
            config.load_kube_config(config_file=self._kubeconfig)
        else:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
        self._api = client.BatchV1Api()
        return self._api

    # ── Manifest construction ─────────────────────────────────────────────

    def _labels(self, org_id: str) -> dict[str, str]:
        labels = {
            f"{LABEL_PREFIX}.managed": "true",
            f"{LABEL_PREFIX}.org": org_id,
        }
        if self._controller_id:
            labels[f"{LABEL_PREFIX}.controller"] = self._controller_id
        return labels

    def _resolve_image(self, spec: WorkerSpec) -> str:
        """Resolve the worker image, failing closed when neither source yields one.

        ``WorkerSpec.image`` wins when both are present (matching the Docker
        backend); otherwise the controller's ``LUCENT_WORKER_IMAGE`` is used.
        """
        image = spec.image or self._image
        if not image:
            raise RuntimeError(
                "KubernetesLauncher cannot start a worker: no image. Set "
                "LUCENT_WORKER_IMAGE or pass WorkerSpec(image=...)."
            )
        return image

    def _job_manifest(self, org_id: str, name: str, spec: WorkerSpec) -> dict[str, Any]:
        """Build a Job manifest carrying the forced binding env and labels."""
        labels = self._labels(org_id)
        worker_env = build_worker_env(org_id, spec.env)
        env = [{"name": key, "value": value} for key, value in worker_env.items()]
        deadline = _worker_deadline_seconds()
        container = {
            "name": name,
            "image": self._resolve_image(spec),
            "command": list(spec.command),
            "workingDir": spec.working_dir,
            "env": env,
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "runAsNonRoot": True,
            },
        }
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                # A worker owns its own lifecycle; never restart a crashed one.
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": _JOB_TTL_SECONDS,
                "activeDeadlineSeconds": deadline,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        # Long enough for SIGTERM drain (the daemon blocks up to
                        # session + 60s); a quick idle-exit returns sooner.
                        "terminationGracePeriodSeconds": deadline,
                        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [container],
                    },
                },
            },
        }

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
        jobs = await self._list_jobs(org_id=org_id)
        running = [job for job in jobs if self._is_active(job)]
        running.sort(key=lambda job: str(job.metadata.creation_timestamp or ""))
        return [
            WorkerHandle(
                worker_id=job.metadata.name,
                org_id=org_id,
                status="running",
                name=job.metadata.name,
            )
            for job in running
        ]

    async def stop(self, worker_id: str, *, graceful: bool = True) -> None:
        """Delete a worker's Job.

        Graceful uses foreground propagation so the pod is deleted *first*: it
        receives SIGTERM and drains (the daemon's signal path waits for in-flight
        sessions), and only then is the Job record removed. A non-graceful stop
        deletes in the background with zero grace so the pod vanishes at once.
        """
        api = self._clients()
        if graceful:
            await asyncio.to_thread(
                api.delete_namespaced_job,
                worker_id,
                self._namespace,
                propagation_policy="Foreground",
            )
        else:
            await asyncio.to_thread(
                api.delete_namespaced_job,
                worker_id,
                self._namespace,
                propagation_policy="Background",
                grace_period_seconds=0,
            )

    async def prune(self, org_id: str | None = None) -> int:
        """Remove finished Jobs.

        ``org_id=None`` sweeps across **every** organization: an org that has
        just stopped needing workers is exactly the one whose finished Jobs are
        left behind, and it is by definition absent from the controller's
        discovered-org list. Running Jobs are always spared.
        """
        api = self._clients()
        jobs = await self._list_jobs(org_id=org_id)
        removed = 0
        for job in jobs:
            if not self._is_finished(job):
                continue
            try:
                await asyncio.to_thread(
                    api.delete_namespaced_job,
                    job.metadata.name,
                    self._namespace,
                    propagation_policy="Background",
                )
                removed += 1
            except Exception:
                continue
        return removed

    # ── Internals ─────────────────────────────────────────────────────────

    async def _list_jobs(self, *, org_id: str | None) -> list[Any]:
        api = self._clients()
        selector = [f"{LABEL_PREFIX}.managed=true"]
        if org_id:
            selector.append(f"{LABEL_PREFIX}.org={org_id}")
        result = await asyncio.to_thread(
            api.list_namespaced_job,
            self._namespace,
            label_selector=",".join(selector),
        )
        return list(result.items)

    @staticmethod
    def _is_active(job: Any) -> bool:
        return bool(getattr(job.status, "active", None))

    @staticmethod
    def _is_finished(job: Any) -> bool:
        """Whether a Job has finished (succeeded or failed) and is not running."""
        status = job.status
        if getattr(status, "active", None):
            return False
        for condition in getattr(status, "conditions", None) or []:
            if condition.type in ("Complete", "Failed") and condition.status == "True":
                return True
        succeeded = getattr(status, "succeeded", None) or 0
        failed = getattr(status, "failed", None) or 0
        return bool(succeeded or failed)

    async def _start_one(self, org_id: str, spec: WorkerSpec) -> bool:
        from daemon.runtime.module_proxy import runtime

        try:
            self._resolve_image(spec)
        except Exception as error:
            runtime.log(f"Worker launcher cannot start workers: {error}", "ERROR")
            raise

        name = f"lucent-worker-{_sanitize_name(org_id[:8])}-{secrets.token_hex(3)}"
        manifest = self._job_manifest(org_id, name, spec)
        api = self._clients()

        try:
            await asyncio.to_thread(api.create_namespaced_job, self._namespace, manifest)
            runtime.log(f"Started worker {name} for org {org_id[:8]}")
            return True
        except Exception as error:
            # A duplicate Job is not a failure: another reconcile pass created it
            # first. Tolerate the conflict idempotently rather than failing both.
            status = getattr(error, "status", None)
            text = str(error).lower()
            if status == 409 or "already exists" in text or "conflict" in text:
                runtime.log(f"Worker {name} already exists; skipping spawn", "DEBUG")
                return False
            runtime.log(f"Failed to start worker for org {org_id[:8]}: {error}", "ERROR")
            return False
