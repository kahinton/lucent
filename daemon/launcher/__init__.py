"""Worker launcher backends for the on-demand daemon controller.

``get_launcher`` returns the backend named by ``LUCENT_WORKER_LAUNCHER``
(default ``docker``). ``docker`` spawns workers as sibling containers for local
dev parity with the Compose stack; ``kubernetes`` spawns them as Jobs on a
cluster. Both plug in here without touching the controller.
"""

from __future__ import annotations

import os

from daemon.launcher.base import (
    BINDING_ENV_KEYS,
    LABEL_PREFIX,
    WORKER_ENV_ALLOWLIST,
    WorkerHandle,
    WorkerLauncher,
    WorkerSpec,
    build_worker_env,
)

__all__ = [
    "BINDING_ENV_KEYS",
    "LABEL_PREFIX",
    "WORKER_ENV_ALLOWLIST",
    "WorkerHandle",
    "WorkerLauncher",
    "WorkerSpec",
    "build_worker_env",
    "get_launcher",
]


def get_launcher(*, controller_id: str = "") -> WorkerLauncher:
    """Build the configured worker launcher."""
    kind = os.environ.get("LUCENT_WORKER_LAUNCHER", "docker").strip().lower()
    if kind == "docker":
        from daemon.launcher.docker_launcher import DockerLauncher

        return DockerLauncher(controller_id=controller_id)
    if kind == "kubernetes":
        from daemon.launcher.kubernetes_launcher import KubernetesLauncher

        return KubernetesLauncher(controller_id=controller_id)
    raise ValueError(f"Unknown worker launcher: {kind!r}")
