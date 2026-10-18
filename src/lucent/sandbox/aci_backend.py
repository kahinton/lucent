"""Azure Container Instances (ACI) backend for sandbox execution.

A sandbox is an ACI *container group*: two Linux containers (the user image and
the trusted MCP-bridge sidecar) sharing one network namespace, with an
``emptyDir`` mounted at ``/workspace``.  The group is deployed with a private IP
into the VNet subnet named by ``LUCENT_SANDBOX_ACI_SUBNET_ID``.

Exec is the hard part.  ACI exposes exec only through
``containers.execute_command()``, which returns a ``ContainerExecResponse`` with
a WebSocket URI and a password -- no argv, no exit code, and no separate
stdout/stderr.  ``_exec_via_websocket`` implements the CLI's protocol (send the
password as the first frame, then read frames), drives ``/bin/sh -c``, and
appends an exit-code sentinel to the command.  Consequences, deliberately
documented rather than hidden:

* stderr is merged into stdout (the stream is a terminal, not a pipe);
* output passes through a PTY, so binary output and very large output are
  unreliable;
* exit codes are synthesized from the sentinel and are lost if the stream is
  truncated;
* a timed-out command is abandoned, not killed (ACI gives us no signal channel);
* a command that replaces or kills its shell (``exec ...``, ``kill $$``) never
  reaches the sentinel and is reported as exit 1 with a diagnostic; wrap such
  commands so the shell survives.

The sentinel is a **per-execution random nonce**, not a fixed public string.
It is emitted on its own terminal line as the last thing the wrapper prints, and
the parser accepts it only when it is the final line of the accumulated stream.
A fixed marker would be forgeable and, worse, a command that prints the marker
(``git diff``/``grep`` over this repository literally prints the old one) would
truncate its own output.  Because the nonce is 128 random bits, output cannot
collide with it by accident.  Residual risk: the wrapper's ``/bin/sh -c``
argument is visible to the command via ``/proc/self/cmdline``, so a command that
deliberately inspects its own argv can still print a valid sentinel.  That is
unavoidable on ACI's exec transport (a command already runs arbitrary code) and
is documented rather than papered over.

Output is bounded.  The reader accumulates at most ``_MAX_EXEC_OUTPUT_BYTES``
(16 MiB) of terminal data; a larger stream is closed and reported as an explicit
failure with the partial output, so a sandbox running ``yes`` cannot exhaust the
Lucent process memory.  The API permits a 3600 s timeout, which makes an
unbounded buffer a denial-of-service vector.

The exec layer is isolated behind ``_exec_via_websocket`` so an AKS-backed
replacement can be swapped in without touching lifecycle, files, or the bridge.

**Security:** the composed environment is split by ``bridge_runtime``.  The user
container receives ``primary_env`` only; the task-scoped API key and bridge
routing reach only the sidecar through ``bridge_env``.  On ACI the key is sent
as an ``EnvironmentVariable.secure_value`` so it is write-only in the group
definition.

Managed identity is attached only when ACI forces it.  Every image in the group
is considered: the user image and, when the MCP bridge is enabled, the bridge
sidecar image.  A group whose images are all public carries no identity.  A
group with any image from a private Azure Container Registry (``*.azurecr.io``)
carries the configured user-assigned pull identity and one
``imageRegistryCredentials`` entry per distinct ACR server: ACI requires the
*same* identity in both the group ``identity`` block and
``imageRegistryCredentials[].identity``, and a private pull otherwise fails
with ``InvalidImageRegistryIdentity``.  This is a platform constraint, not a
design choice.  If an ACR image is present and no identity is configured,
``_group_spec`` fails closed rather than letting the group deploy and fail at
pull time.  The exposure is real and must not be understated: untrusted code in
*any* container of the group can reach IMDS and obtain a token for that
identity.  The mitigation is scope, not secrecy -- the identity must be a
dedicated pull identity holding only ``AcrPull`` on the container registry and
no other role anywhere, so a token grants registry read (pull images) and
nothing else: no storage, no database, no Key Vault, no application API, no
tenant data.  Never use the app, controller or provisioner identity.  The
backend cannot verify role assignments; Terraform provisions the identity with
exactly ``AcrPull`` and the live PoC checks that no other role exists.  See
``docs/sandbox-azure-poc.md``.

Standard ACI container groups do not support security-context fields other
than ``privileged`` (``runAsUser``, ``runAsGroup``, ``capabilities``,
``seccompProfile`` and ``allowPrivilegeEscalation`` are rejected with
``SecurityContextNotSupported``).  The backend therefore sets only
``privileged: false``; non-root execution, capability drops and seccomp must
be baked into the image.  A root image retains ``NET_RAW`` even with
``privileged: false``, and the MCP bridge sidecar shares the group's network
namespace, so a root user container could sniff the bridge's ``Authorization``
header.  To keep the non-root requirement honest, ``create`` runs ``id -u`` in
the user container after the group is ready and fails closed on uid 0 -- or
when the uid cannot be determined.  ``LUCENT_SANDBOX_ACI_ALLOW_ROOT=1``
(or ``allow_root=True``) is an explicit, loudly logged operator opt-in for
images that genuinely cannot run non-root.  The bridge URL must be ``https``
for the same reason: ``http`` would expose the task key on the shared network
namespace.

Egress is subnet-scoped.  ACI has no per-container-group NetworkPolicy.  The
backend never claims more isolation than it enforces:

* ``none`` does **not** mean "no network".  The group is private (no public IP),
  but egress from the subnet is controlled by the subnet NSG.
* ``bridge`` is the same: a private group; the ``bridge`` sidecar needs
  outbound access and egress isolation is still the subnet NSG.
* ``allowlist`` cannot be honoured per sandbox.  ``create`` fails closed unless
  the operator explicitly acknowledges the subnet-level deviation with
  ``LUCENT_SANDBOX_ACI_ALLOW_SUBNET_EGRESS=1``, and even then the backend logs
  that ``allowed_hosts`` is **not** enforced per group.  ``allowlist`` is never
  silently downgraded to another mode.

Every group carries ``lucent-network-mode`` and
``lucent-network-enforcement=subnet-nsg`` tags so callers can report the actual
enforcement boundary.  The module does not pretend ACI provides per-group
network isolation.

Networking details: the group is always ``Private`` with no DNS name label, so
it has no public endpoint.  ACI nevertheless requires ``ipAddress.ports`` to be
non-empty even for a private group, so one unused high port is declared; nothing
in the group binds it.  ACI has no container-level ``workingDir``, so the
configured ``working_dir`` is created (``mkdir -p``) before the keepalive,
clone and setup commands run, and is applied as the initial directory of the
keepalive command and of every exec session.  Because container-group config is
immutable and the in-process ``_configs`` cache does not survive a worker
restart, ``exec`` reloads ``working_dir`` from the group's tags when the cache
is cold.

**Files:** ``write_file``/``read_file`` are confined to ``/workspace``.  A
host-side lexical check is only a prefilter (the host cannot resolve the
container filesystem), so the authoritative check resolves the path *inside*
the container with ``realpath -e``/``readlink`` and rejects any result outside
``/workspace``; ``write_file`` additionally rejects a symlink at the final
component, which a shell redirect would otherwise follow.  A TOCTOU window
remains between resolution and the base64 write/read (ACI gives no
``O_NOFOLLOW`` handle), which is documented rather than denied.

**Stop destroys data:** ACI container groups cannot be paused or resumed, so
``stop`` is mapped to destroy and ``preserves_state`` is ``False``.  Callers
must not report a stopped ACI sandbox as resumable.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import ipaddress
import logging
import os
import re
import secrets
import shlex
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from lucent.sandbox.backend import SandboxBackend
from lucent.sandbox.bridge_runtime import (
    bridge_command,
    bridge_env,
    bridge_image,
    bridge_port,
    mcp_enabled,
    primary_env,
)
from lucent.sandbox.models import ExecResult, SandboxConfig, SandboxInfo, SandboxStatus

logger = logging.getLogger(__name__)

RESOURCE_GROUP_ENV = "LUCENT_SANDBOX_ACI_RESOURCE_GROUP"
SUBNET_ID_ENV = "LUCENT_SANDBOX_ACI_SUBNET_ID"
IDENTITY_ID_ENV = "LUCENT_SANDBOX_ACI_IDENTITY_ID"
LOCATION_ENV = "LUCENT_SANDBOX_ACI_LOCATION"
ALLOW_SUBNET_EGRESS_ENV = "LUCENT_SANDBOX_ACI_ALLOW_SUBNET_EGRESS"
ALLOW_ROOT_ENV = "LUCENT_SANDBOX_ACI_ALLOW_ROOT"
# Conventional ARM variable, used because Terraform sets it on the app.
SUBSCRIPTION_ID_ENV = "AZURE_SUBSCRIPTION_ID"

TAG_MANAGED = "lucent-managed"
TAG_ID = "lucent-sandbox-id"
TAG_NAME = "lucent-name"
TAG_ORGANIZATION = "lucent-organization-id"
TAG_TASK = "lucent-task-id"
TAG_REQUEST = "lucent-request-id"
TAG_WORKING_DIR = "lucent-working-dir"
TAG_BRIDGE_PORT = "lucent-bridge-port"
# Honest reporting of what ACI actually enforces.  Network policy is applied
# by the subnet NSG, never per container group.
TAG_NETWORK_MODE = "lucent-network-mode"
TAG_NETWORK_ENFORCEMENT = "lucent-network-enforcement"
NETWORK_ENFORCEMENT_SUBNET = "subnet-nsg"

USER_CONTAINER = "sandbox"
BRIDGE_CONTAINER = "mcp-bridge"

# ACI requires the container-group minimum even when the caller asks for less.
_MIN_CPU = 1.0
_MIN_MEMORY_GB = 1.0
# Documented ACI limits for a Linux container group.  CPU/memory are
# best-effort: clamp rather than let the platform reject an otherwise valid
# sandbox.  (Regional limits can differ; the live PoC records the real ceiling.)
_MAX_GROUP_CPU = 4.0
_MAX_GROUP_MEMORY_GB = 16.0
_BRIDGE_CPU = 0.25
_BRIDGE_MEMORY_GB = 0.25

# A private ACI group must declare at least one port.  Nothing in this group
# listens on it; it exists only to satisfy the API.  The group is Private with
# no DNS name label, so this is not a public endpoint: the port is
# VNet-internal and never publicly reachable.
_DUMMY_PORT = 65535

_GIT_ASKPASS_PATH = "/tmp/lucent-git-askpass.sh"
# Per-execution nonce: the prefix is fixed so a human can recognise the line,
# but the 128-bit suffix is unguessable and never reused, so user output cannot
# accidentally match it.  The regex is terminal-line anchored *and* anchored to
# the end of the stream; the parser must never accept a marker merely because it
# appears somewhere in the output.
EXEC_SENTINEL_PREFIX = "__LUCENT_ACI_EXIT_"
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ENV_KEY_RE = re.compile(r"(?i)(key|token|secret|password|passwd|credential)")
_URL_USERINFO_RE = re.compile(r"(?i)\b(https?://)[^/\s@]+@")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|apikey|key|sig|signature|password|passwd|secret)=)"
    r"[^&\s'\"]+"
)

# A sandbox running `yes` must not be able to exhaust the Lucent process.
_MAX_EXEC_OUTPUT_BYTES = 16 * 1024 * 1024

_WORKSPACE_ROOT = "/workspace"

# Base64 in an exec command is the only reliable write path ACI gives us.
# `execute_command` ultimately hands the command to `/bin/sh -c`, and Linux
# caps a single argv string at 128 KiB (MAX_ARG_STRLEN), so writes are chunked
# below that.  The overall file cap keeps transfers bounded in both directions.
_MAX_FILE_BYTES = 1 * 1024 * 1024
_WRITE_CHUNK_BYTES = 48 * 1024


def _memory_in_gb(value: str | float | int | None) -> float:
    """Convert a Docker-style memory string to ACI's gigabytes."""
    if value is None:
        return _MIN_MEMORY_GB
    if isinstance(value, (int, float)):
        return max(float(value), _MIN_MEMORY_GB)
    text = str(value).strip().lower()
    if not text:
        return _MIN_MEMORY_GB
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([a-z]*)", text)
    if not match:
        logger.warning("Unrecognised memory limit %r; using the ACI minimum", value)
        return _MIN_MEMORY_GB
    amount = float(match.group(1))
    unit = match.group(2)
    multipliers = {
        "": 1.0,
        "b": 1.0 / (1024**3),
        "k": 1.0 / (1024**2),
        "kb": 1.0 / (1024**2),
        "ki": 1.0 / (1024**2),
        "m": 1.0 / 1024,
        "mb": 1.0 / 1024,
        "mi": 1.0 / 1024,
        "g": 1.0,
        "gb": 1.0,
        "gi": 1.0,
        "t": 1024.0,
        "tb": 1024.0,
        "ti": 1024.0,
    }
    return max(amount * multipliers.get(unit, 1.0), _MIN_MEMORY_GB)


def _effective_cpu(value: float | None) -> float:
    try:
        cpu = float(value or 0)
    except (TypeError, ValueError):
        cpu = 0.0
    return max(cpu, _MIN_CPU)


def _registry_server(image: str) -> str | None:
    """Return the image's registry host, or ``None`` for Docker Hub images."""
    parts = (image or "").split("/")
    if len(parts) < 2:
        return None
    first = parts[0]
    if "." in first or ":" in first or first == "localhost":
        return first
    return None


def _is_not_found(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 404 or type(exc).__name__ in {"ResourceNotFoundError", "ResourceNotFound"}


class ExecOutputLimitError(RuntimeError):
    """Raised when one exec stream exceeds the output cap.

    Carries the partial output so the caller can return an explicit truncated
    result instead of silently growing the buffer.
    """

    def __init__(self, partial_output: str, limit: int):
        super().__init__(
            f"ACI exec output exceeded the {limit}-byte limit; stream closed"
        )
        self.partial_output = partial_output
        self.limit = limit


def _new_exec_sentinel() -> str:
    """Return a fresh, unguessable exit-code sentinel for one execution."""
    return f"{EXEC_SENTINEL_PREFIX}{secrets.token_hex(16)}__"


def _sentinel_regex(sentinel: str) -> re.Pattern[str]:
    """Match ``sentinel`` only as the final terminal line of the stream.

    The leading ``(?:^|\\n)`` requires a line boundary and ``\\Z`` requires the
    match to end the accumulated output (trailing whitespace/newlines are
    tolerated because a PTY emits CRLF).  A sentinel that merely appears in the
    middle of output does not match.
    """
    return re.compile(
        r"(?:^|\n)" + re.escape(sentinel) + r"(-?\d+)[ \t\r\n]*\Z"
    )


def _normalise_terminal_output(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_sentinel(output: str, sentinel: str) -> tuple[str, int | None]:
    """Split terminal output into (user output, synthesized exit code).

    Returns ``(output, None)`` unless ``sentinel`` is the final terminal line.
    This is deliberately not ``in``/``finditer``: an occurrence in the middle of
    the stream is user output, not an exit code.
    """
    match = _sentinel_regex(sentinel).search(output)
    if not match:
        return output, None
    # The match consumed the line break the wrapper injected before the
    # sentinel, so user output may legitimately still end with its own newline.
    return output[: match.start()], int(match.group(1))


def _redact_text(text: str, config: SandboxConfig) -> str:
    """Redact secret values from text destined for logs or persisted state."""
    redacted = text
    if config.git_credentials:
        redacted = redacted.replace(config.git_credentials, "***")
        _, token = AzureContainerInstancesBackend._parse_git_credentials(
            config.git_credentials
        )
        if token:
            redacted = redacted.replace(token, "***")
    if config.repo_url:
        clean_url = AzureContainerInstancesBackend._sanitize_repo_url(config.repo_url)
        if clean_url and clean_url != config.repo_url:
            redacted = redacted.replace(config.repo_url, clean_url)
    for key, value in config.env_vars.items():
        if not value:
            continue
        # Long values are treated as secrets regardless of the key name;
        # short values only when the key itself looks secret, to avoid
        # redacting ordinary words such as "true".
        if len(value) >= 16 or _SECRET_ENV_KEY_RE.search(key):
            redacted = redacted.replace(value, "***")
    redacted = _URL_USERINFO_RE.sub(r"\1***@", redacted)
    redacted = _QUERY_SECRET_RE.sub(r"\1***", redacted)
    return redacted


class AzureContainerInstancesBackend(SandboxBackend):
    """Runs sandboxes as private ACI container groups."""

    # ACI cannot suspend a container group: stop() maps to destroy(), so state
    # (including /workspace) is lost.  Managers/API must report this accurately
    # instead of presenting stop as resumable.
    preserves_state = False

    def __init__(
        self,
        *,
        subscription_id: str | None = None,
        resource_group: str | None = None,
        subnet_id: str | None = None,
        identity_id: str | None = None,
        location: str | None = None,
        allow_subnet_egress: bool | None = None,
        allow_root: bool | None = None,
        client: Any | None = None,
        ready_timeout: float = 300.0,
        bridge_timeout: float = 20.0,
        poll_interval: float = 2.0,
    ):
        self._subscription_id = subscription_id or os.environ.get(SUBSCRIPTION_ID_ENV, "")
        self._resource_group = resource_group or os.environ.get(RESOURCE_GROUP_ENV, "")
        self._subnet_id = subnet_id or os.environ.get(SUBNET_ID_ENV, "")
        self._identity_id = identity_id or os.environ.get(IDENTITY_ID_ENV, "")
        self._location = location or os.environ.get(LOCATION_ENV, "")
        if allow_subnet_egress is None:
            allow_subnet_egress = os.environ.get(ALLOW_SUBNET_EGRESS_ENV, "") == "1"
        self._allow_subnet_egress = bool(allow_subnet_egress)
        if allow_root is None:
            allow_root = os.environ.get(ALLOW_ROOT_ENV, "") == "1"
        self._allow_root = bool(allow_root)
        self._client_instance = client
        self._ready_timeout = ready_timeout
        self._bridge_timeout = bridge_timeout
        self._poll_interval = poll_interval
        self._configs: dict[str, SandboxConfig] = {}
        self._group_names: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Client and configuration
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        """Return the lazily built ACI management client.

        Imports the Azure SDK here so an install that never selects this
        backend does not pay for the import.
        """
        if self._client_instance is not None:
            return self._client_instance
        if not self._subscription_id:
            raise RuntimeError(
                f"ACI sandbox backend requires a subscription id: set {SUBSCRIPTION_ID_ENV}."
            )
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.containerinstance import ContainerInstanceManagementClient
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                "ACI sandbox backend requires 'azure-identity' and "
                "'azure-mgmt-containerinstance'"
            ) from exc
        self._client_instance = ContainerInstanceManagementClient(
            credential=DefaultAzureCredential(),
            subscription_id=self._subscription_id,
        )
        return self._client_instance

    def _validate_config(self, config: SandboxConfig) -> None:
        missing = [
            name
            for name, value in (
                (RESOURCE_GROUP_ENV, self._resource_group),
                (SUBNET_ID_ENV, self._subnet_id),
                (LOCATION_ENV, self._location),
            )
            if not value
        ]
        if missing:
            raise RuntimeError("ACI sandbox backend is not configured; set " + ", ".join(missing))
        if config.dockerfile:
            raise RuntimeError(
                "The ACI sandbox backend cannot build a Dockerfile; provide a prebuilt image"
            )
        if config.network_mode not in {"none", "allowlist", "bridge"}:
            raise RuntimeError(
                f"Unsupported sandbox network mode for ACI: {config.network_mode!r}"
            )
        if config.network_mode == "allowlist" and not self._allow_subnet_egress:
            raise RuntimeError(
                "ACI cannot enforce a per-sandbox egress allowlist: NSGs are subnet-wide, "
                "and a container group cannot carry its own network policy. Refusing to "
                "create the sandbox. Set "
                f"{ALLOW_SUBNET_EGRESS_ENV}=1 only to acknowledge that the whole subnet "
                "controls egress for this sandbox."
            )
        if config.network_mode == "allowlist":
            # Never let the opt-in turn into a claim that allowed_hosts is
            # enforced.  It is not: ACI has no per-group network policy.
            logger.warning(
                "ACI sandbox %s requested allowlist egress; %s=1 acknowledges the "
                "subnet-wide NSG deviation. allowed_hosts=%r is NOT enforced per "
                "container group: the subnet NSG governs all egress.",
                config.name or "sandbox",
                ALLOW_SUBNET_EGRESS_ENV,
                list(config.allowed_hosts),
            )
        elif config.network_mode == "none":
            logger.warning(
                "ACI sandbox %s network_mode='none' does NOT mean no network: the "
                "group is private (no public IP) but ACI has no per-group network "
                "policy, so egress is governed entirely by the subnet NSG.",
                config.name or "sandbox",
            )
        else:  # bridge
            logger.warning(
                "ACI sandbox %s network_mode='bridge' is subnet policy, not per-group "
                "isolation: the sidecar must reach the Lucent server and egress is "
                "governed by the subnet NSG.",
                config.name or "sandbox",
            )
        if mcp_enabled(config):
            self._validate_bridge_url(config)

    @staticmethod
    def _validate_bridge_url(config: SandboxConfig) -> None:
        url = config.env_vars.get("LUCENT_API_URL", "")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RuntimeError(
                "The MCP bridge requires LUCENT_API_URL to be an "
                "absolute http(s) Lucent server URL."
            )
        host = parsed.hostname.lower()
        if host == "host.docker.internal" or host == "localhost":
            raise RuntimeError(
                "LUCENT_API_URL points at the Docker-only default "
                f"({url!r}); set it to the Lucent server URL reachable from the ACI subnet."
            )
        try:
            if ipaddress.ip_address(host).is_loopback:
                raise RuntimeError(
                    "LUCENT_API_URL must not be a loopback address on ACI."
                )
        except ValueError:
            pass
        if parsed.scheme != "https":
            # The bridge shares the group's network namespace.  Over http, any
            # code in the user container (in particular a root image retaining
            # NET_RAW) could sniff the bridge's Authorization header.
            raise RuntimeError(
                "The MCP bridge requires an https LUCENT_API_URL on ACI: the "
                "sidecar shares the sandbox's network namespace, so http would "
                "expose the task-scoped API key to sandbox code. Use https or "
                "terminate TLS at a reachable front end."
            )

    # ------------------------------------------------------------------
    # Manifest / container-group specification
    # ------------------------------------------------------------------

    @staticmethod
    def _group_name(config: SandboxConfig, sandbox_id: str) -> str:
        base = (config.name or "lucent-sandbox").lower()
        safe = re.sub(r"[^a-z0-9-]+", "-", base).strip("-") or "lucent-sandbox"
        return f"{safe[:45].strip('-')}-{sandbox_id[:8]}"[:63].strip("-")

    @staticmethod
    def _keepalive_command(working_dir: str) -> list[str]:
        """Keep the user container alive and exit 0 when ACI stops it.

        ACI has no container-level ``workingDir`` and no way to create one at
        deploy time, so the keepalive creates the configured directory before
        changing into it.  ``create`` also creates it explicitly before clone
        and setup, because setup commands can arrive before the keepalive's
        first iteration.
        """
        directory = shlex.quote(working_dir or _WORKSPACE_ROOT)
        return [
            "sh",
            "-c",
            f"mkdir -p {directory} 2>/dev/null; "
            f"cd {directory} 2>/dev/null || cd /; "
            "trap 'exit 0' TERM INT; while :; do sleep 30; done",
        ]

    @staticmethod
    def _is_acr_image(image: str) -> bool:
        """True for a private Azure Container Registry image reference."""
        server = _registry_server(image)
        return bool(server and server.lower().endswith(".azurecr.io"))

    @staticmethod
    def _group_images(config: SandboxConfig) -> list[str]:
        """Every image the group will pull, user container first."""
        images = [config.image]
        if mcp_enabled(config):
            images.append(bridge_image())
        return [image for image in images if image]

    def _acr_servers(self, config: SandboxConfig) -> list[str]:
        """Distinct ``*.azurecr.io`` servers across all group images.

        Credentials must cover the bridge sidecar as well as the user image:
        the Terraform default bridge image lives in the same ACR, and a public
        user image with a private bridge otherwise deploys with no identity and
        fails the pull with ``InvalidImageRegistryIdentity``.  When images span
        two ACR servers, the same identity must hold ``AcrPull`` on each (or the
        failing pull still reports the same error).
        """
        servers: list[str] = []
        seen: set[str] = set()
        for image in self._group_images(config):
            server = _registry_server(image)
            if not server or not server.lower().endswith(".azurecr.io"):
                continue
            key = server.lower()
            if key not in seen:
                seen.add(key)
                servers.append(server)
        return servers

    def _group_spec(self, sandbox_id: str, name: str, config: SandboxConfig) -> dict[str, Any]:
        """Build the platform-neutral container-group specification.

        Kept as a plain dict so the security-relevant shape (env split, private
        networking, conditional ACR-pull identity, volumes) can be asserted
        without the SDK.
        """
        if config.docker_bind_mounts:
            logger.warning(
                "ACI has no host bind mounts; ignoring %d bind mount(s) for %s",
                len(config.docker_bind_mounts),
                config.name or "sandbox",
            )
        if config.extra_hosts:
            logger.warning(
                "ACI cannot inject /etc/hosts entries; ignoring extra_hosts for %s",
                config.name or "sandbox",
            )
        if config.disk_limit:
            # ACI emptyDir has no quota.  This is not "best effort": there is
            # nothing to apply, and the operator needs to know.
            logger.warning(
                "ACI cannot enforce disk_limit=%r: the /workspace emptyDir has no "
                "size quota. The sandbox is limited only by the node/group disk.",
                config.disk_limit,
            )
        bridge = mcp_enabled(config)
        max_user_cpu = _MAX_GROUP_CPU - (_BRIDGE_CPU if bridge else 0.0)
        max_user_memory = _MAX_GROUP_MEMORY_GB - (
            _BRIDGE_MEMORY_GB if bridge else 0.0
        )
        cpu = min(_effective_cpu(config.cpu_limit), max_user_cpu)
        memory = min(_memory_in_gb(config.memory_limit), max_user_memory)
        if cpu != _effective_cpu(config.cpu_limit) or memory != _memory_in_gb(
            config.memory_limit
        ):
            logger.warning(
                "ACI sandbox %s resource request clamped to %.2f vCPU / %.2f GB "
                "(group maximum includes the bridge sidecar)",
                config.name or "sandbox",
                cpu,
                memory,
            )
        requests = {"cpu": cpu, "memory_in_gb": memory}
        user_container: dict[str, Any] = {
            "name": USER_CONTAINER,
            "image": config.image,
            "command": self._keepalive_command(config.working_dir),
            "ports": [{"port": _DUMMY_PORT, "protocol": "TCP"}],
            "environment_variables": [
                {"name": key, "value": value} for key, value in primary_env(config).items()
            ],
            "resources": {"requests": dict(requests), "limits": dict(requests)},
            "volume_mounts": [{"name": "workspace", "mount_path": "/workspace"}],
            # Standard ACI supports only `privileged`; everything else is
            # rejected by the service, so the rest must live in the image.
            # This sets only privileged: false -- the image must ship a
            # non-root USER (sandbox-images/Dockerfile.base does).
            "security_context": {"privileged": False},
        }
        containers = [user_container]
        if mcp_enabled(config):
            bridge_environment = []
            for key, value in bridge_env(config).items():
                if key == "LUCENT_SANDBOX_MCP_API_KEY":
                    # secureValue is write-only: the task key must not be
                    # readable from the group definition by ARM readers.
                    bridge_environment.append({"name": key, "secure_value": value})
                else:
                    bridge_environment.append({"name": key, "value": value})
            containers.append(
                {
                    "name": BRIDGE_CONTAINER,
                    "image": bridge_image(),
                    "command": bridge_command(),
                    "environment_variables": bridge_environment,
                    "resources": {
                        "requests": {"cpu": _BRIDGE_CPU, "memory_in_gb": _BRIDGE_MEMORY_GB},
                        "limits": {"cpu": _BRIDGE_CPU, "memory_in_gb": _BRIDGE_MEMORY_GB},
                    },
                    "security_context": {"privileged": False},
                }
            )

        tags = {
            TAG_MANAGED: "true",
            TAG_ID: sandbox_id,
            TAG_NAME: (config.name or name)[:256],
            TAG_WORKING_DIR: (config.working_dir or _WORKSPACE_ROOT)[:256],
            TAG_BRIDGE_PORT: str(bridge_port(config)),
            TAG_NETWORK_MODE: config.network_mode,
            TAG_NETWORK_ENFORCEMENT: NETWORK_ENFORCEMENT_SUBNET,
        }
        if config.organization_id:
            tags[TAG_ORGANIZATION] = config.organization_id[:256]
        if config.task_id:
            tags[TAG_TASK] = config.task_id[:256]
        if config.request_id:
            tags[TAG_REQUEST] = config.request_id[:256]

        spec: dict[str, Any] = {
            "name": name,
            "location": self._location,
            "tags": tags,
            "os_type": "Linux",
            "restart_policy": "Never",
            # Private VNet address, never a public IP or DNS label.  The port is
            # the API's mandatory placeholder; nothing listens on it.
            "ip_address": {
                "type": "Private",
                "ports": [{"port": _DUMMY_PORT, "protocol": "TCP"}],
            },
            "subnet_ids": [self._subnet_id],
            "volumes": [{"name": "workspace", "empty_dir": {}}],
            "containers": containers,
        }
        acr_servers = self._acr_servers(config)
        if acr_servers:
            if not self._identity_id:
                # Fail closed: deploying would succeed but the pull would fail
                # with InvalidImageRegistryIdentity, which is a confusing and
                # partially-created failure.  Name every ACR image involved.
                acr_images = [
                    image
                    for image in self._group_images(config)
                    if self._is_acr_image(image)
                ]
                raise RuntimeError(
                    "ACI sandbox group includes ACR image(s) "
                    f"{acr_images!r} but {IDENTITY_ID_ENV} is unset. ACI requires "
                    "the same user-assigned identity in the group and in each "
                    "imageRegistryCredentials entry; the pull would fail with "
                    "InvalidImageRegistryIdentity. Configure the dedicated "
                    "AcrPull identity, or use public images."
                )
            # One credential per distinct registry server covers a public user
            # image with a private bridge sidecar and two different ACRs.
            spec["image_registry_credentials"] = [
                {"server": server, "identity": self._identity_id}
                for server in acr_servers
            ]
            # ACI requires the same user-assigned identity on the group for a
            # private ACR pull; without it the deployment fails with
            # InvalidImageRegistryIdentity.  This identity is dedicated to
            # image pulls and holds only AcrPull on the registry.  Untrusted
            # code in any container of the group can reach IMDS and obtain a
            # token for it, which is why its role scope is the mitigation.
            spec["identity"] = {
                "type": "UserAssigned",
                "user_assigned_identities": {self._identity_id: {}},
            }
        return spec

    def _build_group(self, spec: dict[str, Any]) -> Any:
        """Convert the plain specification into the Azure SDK model."""
        from azure.mgmt.containerinstance.models import (
            Container,
            ContainerGroup,
            ContainerGroupIdentity,
            ContainerGroupSubnetId,
            ContainerPort,
            EnvironmentVariable,
            ImageRegistryCredential,
            IpAddress,
            ResourceIdentityType,
            ResourceLimits,
            ResourceRequests,
            ResourceRequirements,
            SecurityContextDefinition,
            UserAssignedIdentities,
            Volume,
            VolumeMount,
        )

        containers = []
        for item in spec["containers"]:
            requests = item["resources"]["requests"]
            limits = item["resources"].get("limits", requests)
            security = item.get("security_context")
            containers.append(
                Container(
                    name=item["name"],
                    image=item["image"],
                    command=item.get("command"),
                    ports=[
                        ContainerPort(port=port["port"], protocol=port.get("protocol"))
                        for port in item.get("ports", [])
                    ],
                    environment_variables=[
                        EnvironmentVariable(
                            name=env["name"],
                            value=env.get("value"),
                            secure_value=env.get("secure_value"),
                        )
                        for env in item.get("environment_variables", [])
                    ],
                    resources=ResourceRequirements(
                        requests=ResourceRequests(
                            cpu=requests["cpu"], memory_in_gb=requests["memory_in_gb"]
                        ),
                        limits=ResourceLimits(
                            cpu=limits["cpu"], memory_in_gb=limits["memory_in_gb"]
                        ),
                    ),
                    volume_mounts=[
                        VolumeMount(
                            name=mount["name"],
                            mount_path=mount["mount_path"],
                            read_only=mount.get("read_only"),
                        )
                        for mount in item.get("volume_mounts", [])
                    ],
                    security_context=(
                        SecurityContextDefinition(privileged=False) if security else None
                    ),
                )
            )

        registry_credentials = [
            ImageRegistryCredential(server=cred["server"], identity=cred["identity"])
            for cred in spec.get("image_registry_credentials", [])
        ]
        identity_spec = spec.get("identity")
        identity = (
            ContainerGroupIdentity(
                type=ResourceIdentityType.USER_ASSIGNED,
                user_assigned_identities={
                    identity_id: UserAssignedIdentities()
                    for identity_id in identity_spec["user_assigned_identities"]
                },
            )
            if identity_spec
            else None
        )
        return ContainerGroup(
            containers=containers,
            os_type=spec["os_type"],
            location=spec["location"],
            tags=spec["tags"],
            restart_policy=spec["restart_policy"],
            ip_address=IpAddress(
                type=spec["ip_address"]["type"],
                ports=[
                    ContainerPort(port=port["port"], protocol=port.get("protocol"))
                    for port in spec["ip_address"]["ports"]
                ],
            ),
            subnet_ids=[ContainerGroupSubnetId(id=subnet) for subnet in spec["subnet_ids"]],
            volumes=[
                Volume(name=volume["name"], empty_dir=volume.get("empty_dir"))
                for volume in spec["volumes"]
            ],
            image_registry_credentials=registry_credentials or None,
            identity=identity,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def create(self, config: SandboxConfig) -> SandboxInfo:
        """Create the container group.  Never raises; failures are FAILED."""
        sandbox_id = str(uuid.uuid4())
        name = self._group_name(config, sandbox_id)
        # SandboxInfo is handed back by get_live(); it must not retain the
        # task-scoped key or git credentials even though no serializer leaks
        # them today.
        scrubbed_config = self._scrub_config(config)
        info = SandboxInfo(
            id=sandbox_id,
            name=name,
            status=SandboxStatus.CREATING,
            config=scrubbed_config,
            created_at=datetime.now(timezone.utc),
        )
        try:
            self._validate_config(config)
            spec = self._group_spec(sandbox_id, name, config)
            client = self._client()
            group = self._build_group(spec)
            poller = await asyncio.to_thread(
                client.container_groups.begin_create_or_update,
                self._resource_group,
                name,
                group,
            )
            if poller is not None and hasattr(poller, "result"):
                await asyncio.to_thread(poller.result, self._ready_timeout)

            self._configs[sandbox_id] = scrubbed_config
            self._group_names[sandbox_id] = name
            info.container_id = name
            await self._wait_until_ready(name)
            # ACI cannot drop privileges or create a container workingDir, so
            # enforce the non-root requirement and create the directory here,
            # before clone/setup can depend on it.
            await self._verify_non_root(sandbox_id)
            await self._ensure_working_dir(sandbox_id, config.working_dir)

            if config.repo_url:
                await self._prepare_repo(sandbox_id, config)
            for command in config.setup_commands:
                result = await self.exec(
                    sandbox_id, command, cwd=config.working_dir or None, timeout=300
                )
                if result.exit_code != 0:
                    logger.warning(
                        "Setup command failed in ACI sandbox %s: %s (exit %d)",
                        sandbox_id[:12],
                        command,
                        result.exit_code,
                    )
            if mcp_enabled(config):
                await self._wait_for_bridge(sandbox_id, bridge_port(config))

            info.status = SandboxStatus.READY
            info.ready_at = datetime.now(timezone.utc)
            logger.info("ACI sandbox ready: %s (%s)", name, sandbox_id[:12])
        except Exception as exc:
            info.status = SandboxStatus.FAILED
            # The manager persists info.error; redact secret values and
            # tokenised URIs before they can reach a log or a database.
            info.error = self._redact_error(str(exc), config)
            logger.error("Failed to create ACI sandbox %s: %s", name, info.error)
            try:
                await self.destroy(sandbox_id)
            except Exception:
                logger.warning(
                    "Failed to clean up ACI sandbox %s", sandbox_id[:12], exc_info=True
                )
        return info

    async def _wait_until_ready(self, name: str) -> None:
        client = self._client()
        deadline = time.monotonic() + self._ready_timeout
        last_state = ""
        last_detail = ""
        while time.monotonic() < deadline:
            group = await asyncio.to_thread(
                client.container_groups.get, self._resource_group, name
            )
            provisioning = str(getattr(group, "provisioning_state", "") or "").lower()
            state, exit_code, detail = self._container_state(group)
            last_state, last_detail = state, detail
            if provisioning in {"failed", "canceled"}:
                raise RuntimeError(
                    f"ACI container group entered {provisioning or 'failed'} state"
                    + (f": {detail}" if detail else "")
                )
            if state == "running":
                return
            if state == "terminated":
                if exit_code not in (None, 0):
                    raise RuntimeError(
                        f"Sandbox container exited with code {exit_code}"
                        + (f": {detail}" if detail else "")
                    )
                raise RuntimeError("Sandbox container exited before becoming ready")
            await asyncio.sleep(self._poll_interval)
        raise TimeoutError(
            f"Timed out waiting for ACI container group {name} "
            f"(last state {last_state or 'unknown'!r}: {last_detail or 'no detail'})"
        )

    async def _verify_non_root(self, sandbox_id: str) -> None:
        """Enforce the non-root guarantee that ACI cannot configure.

        ``privileged: false`` is the only security context ACI accepts; the
        image's ``USER`` is the sole guarantee of non-root, and a root image
        keeps ``NET_RAW`` while sharing the bridge sidecar's network namespace.
        Run ``id -u`` after the group is ready and fail closed.  The explicit
        ``allow_root`` opt-in is the only way past it.
        """
        result = await self._exec_in_container(
            sandbox_id,
            USER_CONTAINER,
            # Prefer the real binary over anything the image put on PATH.
            "(/usr/bin/id -u 2>/dev/null || /bin/id -u 2>/dev/null || id -u)",
            timeout=30,
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        uid = lines[-1] if lines else ""
        if result.exit_code != 0 or not uid.isdigit():
            if self._allow_root:
                logger.warning(
                    "ACI sandbox %s did not report a numeric uid (%r); proceeding "
                    "only because %s is set",
                    sandbox_id[:12],
                    (result.stderr or result.stdout or "no output")[:200],
                    ALLOW_ROOT_ENV,
                )
                return
            raise RuntimeError(
                "Could not verify that the ACI sandbox runs as non-root: 'id -u' "
                f"failed ({result.stderr or result.stdout or 'no output'!r}). ACI "
                "cannot set runAsUser, so the image is the only guarantee; refusing "
                f"to create the sandbox. Set {ALLOW_ROOT_ENV}=1 only to accept a "
                "root-capable image."
            )
        if int(uid) == 0:
            if self._allow_root:
                logger.warning(
                    "ACI sandbox %s runs as root (uid 0); %s is set. The user "
                    "container shares the bridge sidecar's network namespace and "
                    "retains NET_RAW, so the task key is exposed to sandbox code.",
                    sandbox_id[:12],
                    ALLOW_ROOT_ENV,
                )
                return
            raise RuntimeError(
                "ACI sandbox image runs as root (uid 0). ACI cannot set runAsUser "
                "or drop capabilities, so the image must ship a non-root USER; a "
                "root sandbox also shares the bridge sidecar's network namespace "
                "and could sniff the task key. Refusing to create the sandbox. "
                f"Set {ALLOW_ROOT_ENV}=1 only to accept this risk explicitly."
            )

    async def _ensure_working_dir(self, sandbox_id: str, working_dir: str | None) -> None:
        """Create the configured working directory before clone/setup.

        ``cd <dir> || cd /`` in the keepalive does not create anything, so a
        template with ``working_dir=/workspace/app`` would otherwise clone and
        run setup in the wrong place (or fail).  Run without a cwd: the
        directory may not exist yet.
        """
        directory = working_dir or _WORKSPACE_ROOT
        result = await self._exec_in_container(
            sandbox_id,
            USER_CONTAINER,
            f"mkdir -p -- {shlex.quote(directory)}",
            timeout=30,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"Could not create the sandbox working directory {directory!r}: "
                f"{(result.stderr or result.stdout or 'no output')[:200]}"
            )

    async def _wait_for_bridge(self, sandbox_id: str, port: int) -> None:
        """Poll the sidecar's /health endpoint from inside the group."""
        health = (
            "import urllib.request; "
            f"urllib.request.urlopen('http://127.0.0.1:{int(port)}/health', timeout=1)"
        )
        deadline = time.monotonic() + self._bridge_timeout
        while time.monotonic() < deadline:
            result = await self._exec_in_container(
                sandbox_id, BRIDGE_CONTAINER, ["python3", "-c", health], timeout=10
            )
            if result.exit_code == 0:
                return
            await asyncio.sleep(1)
        raise TimeoutError("Timed out waiting for the ACI MCP bridge sidecar")

    async def _prepare_repo(self, sandbox_id: str, config: SandboxConfig) -> None:
        install = await self.exec(
            sandbox_id,
            "command -v git >/dev/null 2>&1 || "
            "((command -v apt-get >/dev/null 2>&1 && apt-get update -qq && "
            "apt-get install -y -qq git >/dev/null 2>&1) || "
            "(command -v apk >/dev/null 2>&1 && apk add --no-cache git >/dev/null 2>&1) || "
            "(command -v yum >/dev/null 2>&1 && yum install -y -q git >/dev/null 2>&1))",
            timeout=120,
        )
        if install.exit_code != 0:
            raise RuntimeError(
                "Git not available in the sandbox image and auto-install failed. "
                "Use an image with git pre-installed, or set setup_commands to install it."
            )

        clone_env = None
        if config.git_credentials and config.repo_url and config.repo_url.startswith("https://"):
            await self._ensure_git_askpass_script(sandbox_id)
            clone_env = self._build_git_auth_env(config.git_credentials)

        result = await self.exec(
            sandbox_id,
            self._clone_command(config),
            cwd=config.working_dir or None,
            env=clone_env,
            timeout=180,
        )
        if result.exit_code != 0:
            detail = self._sanitize_git_output(result.stderr or result.stdout, config)
            raise RuntimeError(f"Git clone failed: {detail}")

    async def _ensure_git_askpass_script(self, sandbox_id: str) -> None:
        script = (
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *Username*) printf "%s\\n" "${LUCENT_GIT_USERNAME:-x-access-token}" ;;\n'
            '  *Password*) printf "%s\\n" "${LUCENT_GIT_TOKEN:-}" ;;\n'
            '  *) printf "\\n" ;;\n'
            "esac\n"
        )
        await self._write_file_unchecked(sandbox_id, _GIT_ASKPASS_PATH, script.encode())
        await self.exec(sandbox_id, f"chmod 700 {shlex.quote(_GIT_ASKPASS_PATH)}", timeout=10)

    @staticmethod
    def _clone_command(config: SandboxConfig) -> str:
        url = AzureContainerInstancesBackend._sanitize_repo_url(config.repo_url)
        parts = ["git", "clone", "--depth=1"]
        if config.branch:
            parts.extend(["-b", config.branch])
        if url:
            parts.append(url)
        parts.append(".")
        return " ".join(shlex.quote(part) for part in parts)

    @staticmethod
    def _parse_git_credentials(credentials: str) -> tuple[str, str]:
        if ":" in credentials:
            username, token = credentials.split(":", 1)
            if username and token:
                return username, token
        return "x-access-token", credentials

    @classmethod
    def _build_git_auth_env(cls, credentials: str) -> dict[str, str]:
        username, token = cls._parse_git_credentials(credentials)
        return {
            "GIT_ASKPASS": _GIT_ASKPASS_PATH,
            "GIT_TERMINAL_PROMPT": "0",
            "LUCENT_GIT_USERNAME": username,
            "LUCENT_GIT_TOKEN": token,
        }

    @staticmethod
    def _sanitize_repo_url(repo_url: str | None) -> str | None:
        if not repo_url:
            return repo_url
        try:
            parsed = urlsplit(repo_url)
            if not parsed.scheme or not parsed.netloc or "@" not in parsed.netloc:
                return repo_url
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
        except Exception:
            return repo_url

    @classmethod
    def _sanitize_git_output(cls, text: str, config: SandboxConfig) -> str:
        sanitized = text
        if config.git_credentials:
            sanitized = sanitized.replace(config.git_credentials, "***")
            _, token = cls._parse_git_credentials(config.git_credentials)
            sanitized = sanitized.replace(token, "***")
        if config.repo_url:
            clean_url = cls._sanitize_repo_url(config.repo_url)
            if clean_url and clean_url != config.repo_url:
                sanitized = sanitized.replace(config.repo_url, clean_url)
        return _redact_text(sanitized, config)

    @classmethod
    def _redact_error(cls, text: str, config: SandboxConfig) -> str:
        """Remove known secret values and tokenised URIs from an error string.

        The manager persists ``SandboxInfo.error``, so an SDK message that
        echoes an env value or a credentialed URL must not reach the database
        or logs verbatim.
        """
        return _redact_text(text, config)

    @classmethod
    def _scrub_config(cls, config: SandboxConfig) -> SandboxConfig:
        """Return a copy of ``config`` safe to retain and hand back.

        Removes the task-scoped bridge key and git credentials, redacts
        secret-looking env values, and strips credentials from ``repo_url``.
        Execution paths keep using the original local ``config``; only the
        cache and ``SandboxInfo`` receive this copy.
        """
        env_vars: dict[str, str] = {}
        for key, value in config.env_vars.items():
            if key == "LUCENT_SANDBOX_MCP_API_KEY":
                continue
            if value and _SECRET_ENV_KEY_RE.search(key):
                env_vars[key] = "***"
            else:
                env_vars[key] = value
        try:
            repo_url = cls._sanitize_repo_url(config.repo_url)
            if repo_url:
                repo_url = _QUERY_SECRET_RE.sub(r"\1***", repo_url)
            return dataclasses.replace(
                config,
                env_vars=env_vars,
                git_credentials=None,
                repo_url=repo_url,
            )
        except Exception:
            # create() must never raise; a scrub failure must not let the
            # original secret-bearing config escape either.
            logger.warning("Failed to deep-copy sandbox config for scrubbing", exc_info=True)
            return SandboxConfig(
                name=config.name,
                image=config.image,
                working_dir=config.working_dir,
                task_id=config.task_id,
                request_id=config.request_id,
                organization_id=config.organization_id,
                mcp_bridge_port=config.mcp_bridge_port,
            )

    async def get(self, sandbox_id: str) -> SandboxInfo | None:
        group = await asyncio.to_thread(self._find_group, sandbox_id)
        if group is None:
            return None
        return self._info_from_group(sandbox_id, group)

    async def rebind_bridge(self, sandbox_id: str, config: SandboxConfig) -> bool:
        """Container-group containers are immutable, so reuse cannot rebind.

        Returning False makes the manager revoke the freshly minted key and
        create a fresh sandbox, which is the only way to avoid attributing the
        new task's events to the previous bridge identity.
        """
        return False

    async def stop(self, sandbox_id: str) -> None:
        """Destroy the group: ACI cannot suspend/resume, so state is lost.

        ``preserves_state`` is ``False`` for this reason.  The workspace is an
        ``emptyDir`` tied to the container group and is deleted with it.
        """
        await self.destroy(sandbox_id)

    async def destroy(self, sandbox_id: str) -> None:
        self._configs.pop(sandbox_id, None)
        try:
            group = await asyncio.to_thread(self._find_group, sandbox_id)
            if group is None:
                return
            client = self._client()
            try:
                poller = await asyncio.to_thread(
                    client.container_groups.begin_delete,
                    self._resource_group,
                    group.name,
                )
                if poller is not None and hasattr(poller, "result"):
                    await asyncio.to_thread(poller.result)
                logger.info("Destroyed ACI sandbox: %s", sandbox_id[:12])
            except Exception as exc:
                if _is_not_found(exc):
                    return
                raise
        finally:
            self._group_names.pop(sandbox_id, None)

    async def list_all(self) -> list[SandboxInfo]:
        client = self._client()
        groups = await asyncio.to_thread(
            lambda: list(client.container_groups.list_by_resource_group(self._resource_group))
        )
        results = []
        for group in groups:
            tags = getattr(group, "tags", None) or {}
            if tags.get(TAG_MANAGED) != "true":
                continue
            sandbox_id = tags.get(TAG_ID)
            if sandbox_id:
                results.append(self._info_from_group(sandbox_id, group))
        return results

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_managed_group(group: Any, sandbox_id: str) -> bool:
        tags = getattr(group, "tags", None) or {}
        return tags.get(TAG_MANAGED) == "true" and tags.get(TAG_ID) == sandbox_id

    def _find_group(self, sandbox_id: str) -> Any | None:
        """Find a managed group by its Lucent id, surviving process restarts."""
        client = self._client()
        cached = self._group_names.get(sandbox_id)
        if cached:
            try:
                group = client.container_groups.get(self._resource_group, cached)
                if self._is_managed_group(group, sandbox_id):
                    return group
                # Never trust a cached name alone: a recycled/reused group name
                # must not let another group's state be attributed to this id.
                logger.warning(
                    "Cached ACI group name %r no longer carries the managed tags "
                    "for sandbox %s; rescanning",
                    cached,
                    sandbox_id[:12],
                )
                self._group_names.pop(sandbox_id, None)
            except Exception as exc:
                if not _is_not_found(exc):
                    raise
                self._group_names.pop(sandbox_id, None)
        try:
            groups = client.container_groups.list_by_resource_group(self._resource_group)
        except Exception as exc:
            # An absent resource group means the sandbox is absent too.
            if _is_not_found(exc):
                return None
            raise
        for group in groups:
            if self._is_managed_group(group, sandbox_id):
                self._group_names[sandbox_id] = group.name
                return group
        return None

    @staticmethod
    def _container_state(group: Any) -> tuple[str, int | None, str]:
        containers = getattr(group, "containers", None) or []
        for container in containers:
            if getattr(container, "name", None) != USER_CONTAINER:
                continue
            current = getattr(getattr(container, "instance_view", None), "current_state", None)
            state = str(getattr(current, "state", "") or "").lower()
            exit_code = getattr(current, "exit_code", None)
            detail = str(getattr(current, "detail_status", "") or "")
            return state, exit_code, detail
        return "", None, ""

    @classmethod
    def _status_from_group(cls, group: Any) -> SandboxStatus:
        provisioning = str(getattr(group, "provisioning_state", "") or "").lower()
        state, exit_code, _ = cls._container_state(group)
        if provisioning in {"failed", "canceled"}:
            return SandboxStatus.FAILED
        if state == "running":
            return SandboxStatus.READY
        if state == "terminated":
            return SandboxStatus.STOPPED if exit_code in (None, 0) else SandboxStatus.FAILED
        if state == "waiting":
            return SandboxStatus.CREATING
        if provisioning == "deleting":
            return SandboxStatus.STOPPED
        if provisioning == "succeeded" and not state:
            # No instance view yet: the group is deployed but not observably up.
            return SandboxStatus.CREATING
        return SandboxStatus.CREATING

    def _config_from_tags(self, sandbox_id: str, group: Any) -> SandboxConfig:
        tags = getattr(group, "tags", None) or {}
        image = ""
        for container in getattr(group, "containers", None) or []:
            if getattr(container, "name", None) == USER_CONTAINER:
                image = getattr(container, "image", "") or ""
                break
        try:
            port = int(tags.get(TAG_BRIDGE_PORT, "8765"))
        except (TypeError, ValueError):
            port = 8765
        return SandboxConfig(
            name=tags.get(TAG_NAME),
            image=image or "unknown",
            working_dir=tags.get(TAG_WORKING_DIR) or "/workspace",
            task_id=tags.get(TAG_TASK),
            request_id=tags.get(TAG_REQUEST),
            organization_id=tags.get(TAG_ORGANIZATION),
            mcp_bridge_port=port,
        )

    def _info_from_group(self, sandbox_id: str, group: Any) -> SandboxInfo:
        status = self._status_from_group(group)
        _, _, detail = self._container_state(group)
        provisioning = str(getattr(group, "provisioning_state", "") or "")
        error = None
        if status == SandboxStatus.FAILED:
            error = detail or f"ACI container group provisioning state: {provisioning or 'failed'}"
        return SandboxInfo(
            id=sandbox_id,
            name=getattr(group, "name", sandbox_id),
            status=status,
            config=self._configs.get(sandbox_id) or self._config_from_tags(sandbox_id, group),
            container_id=getattr(group, "name", None),
            error=error,
        )

    # ------------------------------------------------------------------
    # Exec
    # ------------------------------------------------------------------

    async def exec(
        self,
        sandbox_id: str,
        command: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> ExecResult:
        """Execute a command over ACI's terminal-emulated exec WebSocket.

        stderr is merged into stdout; the exit code is synthesized from a
        sentinel.  See the module docstring.
        """
        container_cwd = cwd
        if container_cwd is None:
            config = self._configs.get(sandbox_id)
            if config is None:
                # _configs is in-process only.  After a worker restart (or a
                # sandbox adopted from list_all/get), recover working_dir from
                # the group's tags rather than silently running in the shell
                # default.
                group = await asyncio.to_thread(self._find_group, sandbox_id)
                if group is not None:
                    config = self._config_from_tags(sandbox_id, group)
                    self._configs[sandbox_id] = config
            if config is not None:
                container_cwd = config.working_dir or None
        return await self._exec_in_container(
            sandbox_id,
            USER_CONTAINER,
            command,
            cwd=container_cwd,
            env=env,
            timeout=timeout,
        )

    async def _exec_in_container(
        self,
        sandbox_id: str,
        container_name: str,
        command: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> ExecResult:
        started = time.monotonic()
        # One nonce per execution.  It is created here so the wrapper and the
        # parser share it; nothing about it is caller-controlled.
        sentinel = _new_exec_sentinel()
        try:
            group = await asyncio.to_thread(self._find_group, sandbox_id)
            if group is None:
                return ExecResult(1, "", "Sandbox not found")
            script = self._wrap_command(
                command, cwd=cwd, env=env, sentinel=sentinel
            )
            uri, password = await asyncio.to_thread(
                self._request_exec, group.name, container_name, script
            )
            output = await asyncio.wait_for(
                self._exec_via_websocket(
                    uri,
                    password,
                    timeout=timeout,
                    stop_marker=sentinel,
                ),
                timeout=timeout + 5,
            )
        except TimeoutError:
            duration_ms = int((time.monotonic() - started) * 1000)
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                duration_ms=duration_ms,
                timed_out=True,
            )
        except ExecOutputLimitError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            # Explicit truncated failure, not a silently growing buffer and
            # not a fake exit code from the partial tail.
            return ExecResult(1, exc.partial_output, str(exc), duration_ms)
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            # An SDK/transport error can echo the wrapped command, which
            # carries exported values (git tokens, for example).
            message = str(exc)
            for value in (env or {}).values():
                if value and len(value) >= 6:
                    message = message.replace(value, "***")
            return ExecResult(1, "", message, duration_ms)

        output = _normalise_terminal_output(output)
        stdout, exit_code = _strip_sentinel(output, sentinel)
        stderr = ""
        if exit_code is None:
            # Terminal emulation can drop the tail of a large stream.  Do not
            # pretend success; report the output we did receive.
            exit_code = 1
            stderr = "ACI exec stream ended without an exit sentinel"
        return ExecResult(
            exit_code,
            stdout,
            stderr,
            int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _wrap_command(
        command: str | list[str],
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        sentinel: str | None = None,
    ) -> str:
        """Build a single ``/bin/sh -c`` command line with an exit sentinel.

        ``sentinel`` is a per-execution random nonce supplied by
        ``_exec_in_container``.  The default exists only so direct callers and
        tests can build a wrapper without threading one through, and still gets
        a fresh random value.
        """
        sentinel = sentinel or _new_exec_sentinel()
        shell = command if isinstance(command, str) else shlex.join(command)
        prefix = ""
        for key, value in (env or {}).items():
            if not _ENV_KEY_RE.fullmatch(key):
                raise ValueError(f"Invalid environment variable name: {key!r}")
            prefix += f"export {key}={shlex.quote(value)}; "
        if cwd:
            prefix += f"cd {shlex.quote(cwd)} || exit $?; "
        script = (
            f"( {prefix}{shell}\n)\n"
            "__lucent_aci_rc=$?\n"
            # The nonce is passed as a printf argument, never interpolated into
            # the format string, so a nonce can never become a format specifier.
            f"printf '\\n%s%s\\n' {shlex.quote(sentinel)} \"$__lucent_aci_rc\"\n"
            "exit 0"
        )
        return f"/bin/sh -c {shlex.quote(script)}"

    def _request_exec(self, group_name: str, container_name: str, command: str) -> tuple[str, str]:
        """Ask ACI for one exec WebSocket ticket."""
        from azure.mgmt.containerinstance.models import (
            ContainerExecRequest,
            ContainerExecRequestTerminalSize,
        )

        client = self._client()
        response = client.containers.execute_command(
            self._resource_group,
            group_name,
            container_name,
            ContainerExecRequest(
                command=command,
                terminal_size=ContainerExecRequestTerminalSize(rows=24, cols=512),
            ),
        )
        return response.web_socket_uri, response.password

    async def _open_websocket(self, uri: str) -> Any:
        """Open the exec WebSocket.  A seam for tests and future transports."""
        import websockets

        return await websockets.connect(
            uri,
            # Bound a single frame too: the cumulative check in the reader
            # handles a long stream, this stops one giant frame from being
            # allocated before that check runs.
            max_size=_MAX_EXEC_OUTPUT_BYTES,
            open_timeout=10,
            close_timeout=2,
        )

    async def _exec_via_websocket(
        self,
        uri: str,
        password: str,
        *,
        timeout: int = 300,
        stop_marker: str | None = None,
    ) -> str:
        """Drive one ACI exec stream and return the raw terminal output.

        Protocol (matching ``az container exec``): open the socket, send the
        password as the first frame, then read until the process exits or the
        nonce sentinel ends the stream.  Output frames may be text or UTF-8
        bytes.

        The stream is capped at ``_MAX_EXEC_OUTPUT_BYTES`` cumulative bytes.
        Exceeding it closes the socket and raises ``ExecOutputLimitError`` with
        the partial output; the buffer never grows without bound.  A stream
        that merely *contains* the sentinel mid-output keeps reading: only a
        sentinel at the end of the accumulated terminal output terminates it.
        """
        if not uri:
            raise RuntimeError("ACI exec did not return a WebSocket URI")
        if not password:
            raise RuntimeError("ACI exec did not return a WebSocket password")
        websocket = await self._open_websocket(uri)
        chunks: list[str] = []
        total_bytes = 0
        stop_re = _sentinel_regex(stop_marker) if stop_marker else None
        try:
            await websocket.send(password)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            tail = ""
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(f"ACI exec timed out after {timeout}s")
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                except TimeoutError:
                    raise
                except Exception as exc:
                    if type(exc).__name__.startswith("ConnectionClosed"):
                        # The websockets library closes with 1009 when a single
                        # frame exceeds max_size.  Treat that as our explicit
                        # output-limit failure rather than a normal EOF.
                        if getattr(exc, "code", None) == 1009:
                            raise ExecOutputLimitError(
                                "".join(chunks), _MAX_EXEC_OUTPUT_BYTES
                            ) from exc
                        break
                    raise
                if isinstance(message, (bytes, bytearray)):
                    raw = bytes(message)
                    chunk = raw.decode("utf-8", errors="replace")
                    chunk_bytes = len(raw)
                else:
                    chunk = str(message)
                    chunk_bytes = len(chunk.encode("utf-8", errors="replace"))
                if total_bytes + chunk_bytes > _MAX_EXEC_OUTPUT_BYTES:
                    raise ExecOutputLimitError(
                        "".join(chunks), _MAX_EXEC_OUTPUT_BYTES
                    )
                total_bytes += chunk_bytes
                chunks.append(chunk)
                if stop_re is not None:
                    # Keep enough tail to catch a sentinel split across frames,
                    # including the line boundary that precedes it.
                    tail = (tail + chunk)[-(len(stop_marker) + 64):]
                    if stop_re.search(tail):
                        break
        finally:
            try:
                await websocket.close()
            except Exception:
                logger.debug("Failed to close ACI exec WebSocket", exc_info=True)
        return "".join(chunks)

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        # Host-side lexical prefilter; the container resolves the real path
        # (and any symlinks) below.
        lexical = self._validate_workspace_path(path)
        resolved = self._assert_inside_workspace(
            await self._container_realpath(sandbox_id, lexical), path
        )
        # Check the size before streaming the file so an oversized file is
        # refused instead of being pulled through the terminal.
        size_result = await self.exec(
            sandbox_id, f"wc -c < {shlex.quote(resolved)}", timeout=30
        )
        if size_result.exit_code != 0:
            raise FileNotFoundError(size_result.stderr or path)
        try:
            size = int(size_result.stdout.strip().split()[0])
        except (IndexError, ValueError):
            size = None
        if size is not None and size > _MAX_FILE_BYTES:
            raise ValueError(
                f"File exceeds the ACI transport limit of {_MAX_FILE_BYTES} bytes: {path}"
            )

        result = await self.exec(
            sandbox_id, f"base64 {shlex.quote(resolved)}", timeout=60
        )
        if result.exit_code != 0:
            raise FileNotFoundError(result.stderr or path)
        # Guard before decoding: base64 output over the terminal can be large.
        if len(result.stdout) > _MAX_FILE_BYTES * 2:
            raise ValueError(
                f"File exceeds the ACI transport limit of {_MAX_FILE_BYTES} bytes: {path}"
            )
        try:
            data = base64.b64decode("".join(result.stdout.split()), validate=True)
        except Exception as exc:
            raise OSError(f"Invalid base64 from ACI sandbox for {path}: {exc}") from exc
        if len(data) > _MAX_FILE_BYTES:
            raise ValueError(
                f"File exceeds the ACI transport limit of {_MAX_FILE_BYTES} bytes: {path}"
            )
        return data

    async def write_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        # Refuse an oversized payload before issuing any container command.
        if len(content) > _MAX_FILE_BYTES:
            raise ValueError(
                f"File exceeds the ACI transport limit of {_MAX_FILE_BYTES} bytes: {path}"
            )
        # Lexical prefilter first (cheap, refuses obvious escapes), then the
        # authoritative in-container resolution of the parent directory and a
        # symlink check on the final component.
        safe_path = self._validate_workspace_path(path)
        await self._verify_write_target(sandbox_id, safe_path, path)
        await self._write_file_unchecked(sandbox_id, safe_path, content)

    async def _container_realpath(
        self, sandbox_id: str, path: str, *, must_exist: bool = True
    ) -> str:
        """Resolve ``path`` inside the container, following symlinks.

        The host cannot resolve the container filesystem, so this is the only
        real confinement check.  ``realpath -e``/``readlink -e`` require the
        path to exist; fall back to ``readlink -f`` for a create target whose
        final component does not exist yet.
        """
        quoted = shlex.quote(path)
        if must_exist:
            command = (
                f"realpath -e -- {quoted} 2>/dev/null || "
                f"readlink -e -- {quoted} 2>/dev/null"
            )
        else:
            command = (
                f"realpath -m -- {quoted} 2>/dev/null || "
                f"readlink -f -- {quoted} 2>/dev/null"
            )
        result = await self.exec(sandbox_id, command, timeout=30)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        resolved = lines[-1] if lines else ""
        if result.exit_code != 0 or not resolved.startswith("/"):
            raise FileNotFoundError(result.stderr or path)
        return resolved

    async def _verify_write_target(
        self, sandbox_id: str, absolute: str, original: str
    ) -> None:
        """Ensure a write cannot leave ``/workspace`` via a symlink."""
        parent = os.path.dirname(absolute) or "/"
        created = await self.exec(
            sandbox_id, f"mkdir -p -- {shlex.quote(parent)}", timeout=30
        )
        if created.exit_code != 0:
            raise OSError(
                f"Cannot create parent directory for {original}: "
                f"{(created.stderr or created.stdout)[:200]}"
            )
        resolved_parent = await self._container_realpath(sandbox_id, parent)
        self._assert_inside_workspace(resolved_parent, original)
        # A symlink at the final component would be followed by the shell
        # redirect (`> link`) and write outside the workspace.  Reject it
        # rather than resolving it: the write is a replacement, not a
        # read-through.
        link_check = await self.exec(
            sandbox_id,
            f"if [ -L {shlex.quote(absolute)} ]; then echo yes; fi",
            timeout=10,
        )
        if "yes" in link_check.stdout.split():
            raise ValueError(
                f"Path escapes workspace root through a symlink: {original}"
            )

    async def _write_file_unchecked(
        self, sandbox_id: str, path: str, content: bytes
    ) -> None:
        data = bytes(content)
        if len(data) > _MAX_FILE_BYTES:
            raise ValueError(
                f"File exceeds the ACI transport limit of {_MAX_FILE_BYTES} bytes: {path}"
            )
        absolute = path if path.startswith("/") else f"/{path}"
        parent = os.path.dirname(absolute) or "/"
        # One command per chunk: a single base64 argument larger than 128 KiB
        # cannot be passed through /bin/sh -c (MAX_ARG_STRLEN).
        chunks = [
            data[offset : offset + _WRITE_CHUNK_BYTES]
            for offset in range(0, len(data), _WRITE_CHUNK_BYTES)
        ] or [b""]
        for index, chunk in enumerate(chunks):
            encoded = base64.b64encode(chunk).decode("ascii")
            redirect = ">" if index == 0 else ">>"
            command = (
                f"printf %s {shlex.quote(encoded)} | base64 -d {redirect} "
                f"{shlex.quote(absolute)}"
            )
            if index == 0:
                command = f"mkdir -p {shlex.quote(parent)} && {command}"
            result = await self.exec(sandbox_id, command, timeout=120)
            if result.exit_code != 0:
                raise OSError(
                    f"File write failed for {path}: "
                    f"{(result.stderr or result.stdout)[:200]}"
                )

    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> list[dict]:
        result = await self.exec(
            sandbox_id,
            f"find {shlex.quote(path)} -mindepth 1 -maxdepth 1 "
            "-printf '%f\\t%y\\t%s\\n'",
            timeout=30,
        )
        if result.exit_code == 0:
            return self._parse_find_output(path, result.stdout)
        # Busybox find has no -printf.
        fallback = await self.exec(
            sandbox_id, f"ls -la {shlex.quote(path)}", timeout=30
        )
        if fallback.exit_code != 0:
            raise OSError(fallback.stderr or result.stderr or f"Cannot list {path}")
        return self._parse_ls_output(path, fallback.stdout)

    @staticmethod
    def _parse_find_output(path: str, output: str) -> list[dict]:
        files = []
        for line in output.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            name, file_type, size = parts
            is_dir = file_type == "d"
            files.append(
                {
                    "name": name,
                    "path": os.path.join(path, name),
                    "size": int(size) if size.isdigit() else 0,
                    "type": "directory" if is_dir else "file",
                    "is_dir": is_dir,
                }
            )
        return files

    @staticmethod
    def _parse_ls_output(path: str, output: str) -> list[dict]:
        files = []
        for line in output.splitlines():
            parts = line.split(None, 8)
            if len(parts) != 9 or parts[0] == "total":
                continue
            mode, size, name = parts[0], parts[4], parts[8]
            if name in {".", ".."}:
                continue
            is_dir = mode.startswith("d")
            files.append(
                {
                    "name": name,
                    "path": os.path.join(path, name),
                    "size": int(size) if size.isdigit() else 0,
                    "type": "directory" if is_dir else "file",
                    "is_dir": is_dir,
                }
            )
        return files

    @staticmethod
    def _assert_inside_workspace(resolved: str, original: str,
                                 workspace_root: str = _WORKSPACE_ROOT) -> str:
        """Raise unless an in-container resolved path is inside the workspace."""
        root = os.path.normpath(workspace_root)
        candidate = os.path.normpath(resolved)
        if candidate != root and not candidate.startswith(root + os.sep):
            raise ValueError(f"Path escapes workspace root: {original}")
        return candidate

    @staticmethod
    def _validate_workspace_path(path: str, workspace_root: str = _WORKSPACE_ROOT) -> str:
        """Lexically normalize a path and require it under ``/workspace``.

        This is a *host-side prefilter only*.  ``os.path.realpath`` on the host
        cannot resolve container symlinks (and would consult the wrong
        filesystem), so it must not be used as the confinement check.  The
        authoritative check is :meth:`_container_realpath` /
        :meth:`_assert_inside_workspace` inside the sandbox.
        """
        if not path:
            raise ValueError("Path must not be empty")
        normalized = os.path.normpath(path)
        candidate = (
            normalized
            if os.path.isabs(normalized)
            else os.path.normpath(os.path.join(workspace_root, normalized))
        )
        return AzureContainerInstancesBackend._assert_inside_workspace(
            candidate, path, workspace_root
        )
