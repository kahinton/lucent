"""Worker launcher interface for the on-demand daemon controller.

A *worker* is an ephemeral process that runs the dispatcher role for exactly
one organization, drains that org's queue, and exits when idle. The controller
spawns them through a :class:`WorkerLauncher`, so the same controller logic can
target Docker today and Kubernetes (or anything else) later.

Security note — the org binding is authoritative and never comes from the
caller. :func:`build_worker_env` strips every binding key out of caller-supplied
env and applies the controller's values last, so a task, request, or hostile
spec can never influence which organization a worker runs as.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field

LABEL_PREFIX = "io.lucent.worker"

# Environment keys that decide *which org a worker serves* and *what it does*.
# These are stripped from any caller-supplied env and set by the launcher.
BINDING_ENV_KEYS = frozenset(
    {
        "LUCENT_DAEMON_ORG",
        "LUCENT_DAEMON_ROLES",
        "LUCENT_DAEMON_WORKER",
        "LUCENT_DAEMON_SOURCE_RELOAD",
    }
)

# The only controller environment variables a worker inherits. An allowlist
# (not a denylist) so a new variable the controller gains is never silently
# leaked into workers.
WORKER_ENV_ALLOWLIST = (
    "LUCENT_MCP_URL",
    "LUCENT_MCP_API_KEY",
    "LUCENT_DEFAULT_MODEL",
    "LUCENT_DAEMON_INTERVAL",
    "LUCENT_DAEMON_MODEL",
    "LUCENT_MCP_URL_ALLOWLIST",
    "LUCENT_LLM_ENGINE",
    "LUCENT_MAX_SESSIONS",
    "LUCENT_STALE_HEARTBEAT_MINUTES",
    "LUCENT_DAEMON_WORKER_MIN_WARM",
    "LUCENT_DAEMON_WORKER_IDLE_SECONDS",
    "LUCENT_DAEMON_WORKER_RECONCILE_SECONDS",
    "DAEMON_DATABASE_URL",
    "DATABASE_URL",
    "LUCENT_SANDBOX_BACKEND",
    "LUCENT_SANDBOX_BRIDGE_IMAGE",
    "LUCENT_SANDBOX_K8S_NAMESPACE",
    "OLLAMA_HOST",
    "LUCENT_SECRET_PROVIDER",
    "VAULT_ADDR",
    "VAULT_TOKEN_FILE",
    "DOCKER_HOST",
    "LUCENT_MODE",
    "LUCENT_LOG_LEVEL",
    "LUCENT_LOG_STDERR",
    "LUCENT_LOG_FILE",
    "PYTHONUNBUFFERED",
)


def build_worker_env(org_id: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the environment for a worker container serving ``org_id``.

    Starts from the allowlisted subset of this process's environment, applies
    ``extra`` (with binding keys ignored), then forces the authoritative
    binding last so nothing earlier can override it.
    """
    env: dict[str, str] = {}
    for key in WORKER_ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value

    # Least privilege: the daemon prefers DAEMON_DATABASE_URL (restricted
    # ``lucent_daemon`` role) over the full-privilege DATABASE_URL. Only fall
    # back to the broader URL when the restricted one is absent.
    if "DAEMON_DATABASE_URL" in os.environ:
        env.pop("DATABASE_URL", None)
    elif os.environ.get("DATABASE_URL"):
        env["DATABASE_URL"] = os.environ["DATABASE_URL"]

    for key, value in (extra or {}).items():
        if key in BINDING_ENV_KEYS:
            continue
        env[key] = value

    # Authoritative binding — applied last, never overridable.
    env["LUCENT_DAEMON_ORG"] = org_id
    env["LUCENT_DAEMON_ROLES"] = "dispatcher"
    env["LUCENT_DAEMON_WORKER"] = "1"
    # A worker must never restart itself; the controller owns its lifecycle.
    env["LUCENT_DAEMON_SOURCE_RELOAD"] = "false"
    return env


@dataclass(frozen=True)
class WorkerSpec:
    """How to start a worker. ``None`` fields mean "inherit from the controller"."""

    image: str | None = None
    command: list[str] = field(default_factory=lambda: ["python", "-m", "daemon.daemon"])
    env: dict[str, str] = field(default_factory=dict)
    working_dir: str = "/app"


@dataclass(frozen=True)
class WorkerHandle:
    """A worker container known to the launcher."""

    worker_id: str
    org_id: str
    status: str
    name: str


class WorkerLauncher(abc.ABC):
    """Spawns and tracks ephemeral per-org workers."""

    @abc.abstractmethod
    async def ensure(self, org_id: str, count: int, spec: WorkerSpec) -> int:
        """Ensure at least ``count`` workers are running for ``org_id``.

        Spawns the shortfall and returns how many were started. Never stops a
        running worker: scale-in happens through idle-exit, so a worker is never
        killed out from under an in-flight session.
        """

    @abc.abstractmethod
    async def running(self, org_id: str) -> int:
        """Return the number of running workers for ``org_id``."""

    @abc.abstractmethod
    async def list_workers(self, org_id: str) -> list[WorkerHandle]:
        """Return running workers for ``org_id``, oldest first."""

    @abc.abstractmethod
    async def stop(self, worker_id: str, *, graceful: bool = True) -> None:
        """Stop one worker. Graceful sends SIGTERM so it can drain."""

    @abc.abstractmethod
    async def prune(self, org_id: str | None = None) -> int:
        """Remove exited workers and return how many were removed.

        ``org_id=None`` prunes across every organization: an org that drops off
        the discovered list (work finished, warm floor 0) still has exited
        containers to collect, and nothing else will collect them.
        """
