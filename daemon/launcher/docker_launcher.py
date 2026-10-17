"""Docker-backed worker launcher.

A worker container is a sibling of the controller: same image, same networks,
same mounts. That is why the launcher locates the controller's *own* container
and clones those three things rather than taking them from configuration —
the dev bind mounts (``./daemon``, ``./src``) and the compose network are
exactly what a worker needs, and copying them cannot drift.

Only the environment is constructed rather than copied, and only from an
allowlist (see :mod:`daemon.launcher.base`).
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
from typing import Any

from daemon.launcher.base import (
    LABEL_PREFIX,
    WorkerHandle,
    WorkerLauncher,
    WorkerSpec,
    build_worker_env,
)

# Bind mounts cloned from the controller are read/write unless Docker reported
# them read-only (e.g. the shared OpenBao token directory).
_MOUNT_MODE_RW = "rw"
_MOUNT_MODE_RO = "ro"


class DockerLauncher(WorkerLauncher):
    """Spawns workers as Docker containers alongside the controller."""

    def __init__(
        self,
        *,
        controller_id: str = "",
        image: str | None = None,
        network: str | None = None,
    ):
        self._client: Any = None
        self._controller_id = controller_id
        # Explicit overrides, used when the controller cannot locate its own
        # container (e.g. it runs outside Docker).
        self._image_override = image or os.environ.get("LUCENT_WORKER_IMAGE")
        self._network_override = network or os.environ.get("LUCENT_WORKER_NETWORK")
        self._self_spec: dict[str, Any] | None = None

    # ── Docker client ────────────────────────────────────────────────────

    def _docker(self) -> Any:
        if self._client is None:
            import docker

            base_url = os.environ.get("DOCKER_HOST")
            if base_url:
                self._client = docker.DockerClient(base_url=base_url)
            else:
                self._client = docker.from_env()
        return self._client

    # ── Self-location ─────────────────────────────────────────────────────

    def _self_container(self) -> Any | None:
        """Return the controller's own container, or None if it cannot be found."""
        client = self._docker()
        candidates = [socket.gethostname()]
        if os.environ.get("HOSTNAME"):
            candidates.insert(0, os.environ["HOSTNAME"])
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return client.containers.get(candidate)
            except Exception:
                continue
        return None

    def _resolve_self_spec(self) -> dict[str, Any]:
        """Resolve the image, networks, and mounts a worker should inherit.

        Falls back to explicit overrides only when self-location fails; if
        neither is available it raises, because a worker with an unknown image
        or network is worse than no worker at all.
        """
        if self._self_spec is not None:
            return self._self_spec

        image = self._image_override
        network = self._network_override
        mounts: dict[str, dict[str, str]] = {}

        container = self._self_container()
        if container is not None:
            attrs = container.attrs
            image = image or attrs.get("Image")
            networks = list((attrs.get("NetworkSettings") or {}).get("Networks") or {})
            if not network and networks:
                network = networks[0]
            for mount in attrs.get("Mounts") or []:
                if mount.get("Type") != "bind":
                    continue
                # docker-py keys `volumes` by host path with `bind` = the path
                # inside the container. Getting this backwards silently mounts
                # an empty directory over the wrong path.
                mounts[str(mount["Source"])] = {
                    "bind": str(mount["Destination"]),
                    "mode": _MOUNT_MODE_RW if mount.get("RW") else _MOUNT_MODE_RO,
                }

        if not image or not network:
            raise RuntimeError(
                "DockerLauncher cannot locate its own container to clone. "
                "Set LUCENT_WORKER_IMAGE and LUCENT_WORKER_NETWORK to spawn "
                "workers explicitly."
            )

        self._self_spec = {"image": image, "network": network, "mounts": mounts}
        return self._self_spec

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
        client = self._docker()
        containers = await asyncio.to_thread(
            client.containers.list,
            all=False,
            filters={"label": self._label_filters(org_id)},
        )
        containers.sort(key=lambda c: str(c.attrs.get("Created", "")))
        return [
            WorkerHandle(
                worker_id=c.id,
                org_id=org_id,
                status=str(c.status),
                name=c.name,
            )
            for c in containers
        ]

    async def stop(self, worker_id: str, *, graceful: bool = True) -> None:
        container = await asyncio.to_thread(self._docker().containers.get, worker_id)
        if graceful:
            await asyncio.to_thread(container.stop, timeout=30)
        else:
            await asyncio.to_thread(container.kill)
        await asyncio.to_thread(container.remove, force=True)

    async def prune(self, org_id: str | None = None) -> int:
        """Remove exited workers.

        ``auto_remove`` is off so that ``running()`` is deterministic and a dead
        worker's logs survive for inspection — which makes pruning mandatory
        rather than optional. Called across all orgs each pass, because an org
        that no longer needs workers is exactly the one whose containers are left
        behind.
        """
        client = self._docker()
        filters = [f"{LABEL_PREFIX}.managed=true"]
        if org_id:
            filters.append(f"{LABEL_PREFIX}.org={org_id}")
        containers = await asyncio.to_thread(
            client.containers.list, all=True, filters={"label": filters}
        )
        removed = 0
        for container in containers:
            if container.status == "running":
                continue
            try:
                await asyncio.to_thread(container.remove, force=True)
                removed += 1
            except Exception:
                continue
        return removed

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _label_filters(org_id: str) -> list[str]:
        return [f"{LABEL_PREFIX}.managed=true", f"{LABEL_PREFIX}.org={org_id}"]

    async def _start_one(self, org_id: str, spec: WorkerSpec) -> bool:
        from daemon.runtime.module_proxy import runtime

        try:
            resolved = await asyncio.to_thread(self._resolve_self_spec)
        except Exception as error:
            runtime.log(f"Worker launcher cannot start workers: {error}", "ERROR")
            raise

        name = f"lucent-worker-{org_id[:8]}-{secrets.token_hex(3)}"
        labels = {
            f"{LABEL_PREFIX}.managed": "true",
            f"{LABEL_PREFIX}.org": org_id,
        }
        if self._controller_id:
            labels[f"{LABEL_PREFIX}.controller"] = self._controller_id

        kwargs: dict[str, Any] = {
            "image": spec.image or resolved["image"],
            "command": spec.command,
            "detach": True,
            "auto_remove": False,
            "working_dir": spec.working_dir,
            "name": name,
            "environment": build_worker_env(org_id, spec.env),
            "labels": labels,
            "volumes": resolved["mounts"],
            "network": resolved["network"],
            "restart_policy": {"Name": "no"},
            "stop_signal": "SIGTERM",
            "security_opt": ["no-new-privileges"],
        }

        try:
            await asyncio.to_thread(self._docker().containers.run, **kwargs)
            runtime.log(f"Started worker {name} for org {org_id[:8]}")
            return True
        except Exception as error:
            # A duplicate name is not a failure — the worker already exists.
            if "Conflict" in str(error) or "already in use" in str(error):
                runtime.log(f"Worker {name} already exists; skipping spawn", "DEBUG")
                return False
            runtime.log(f"Failed to start worker for org {org_id[:8]}: {error}", "ERROR")
            return False
