"""Lucent Daemon — Cognitive Architecture for a Distributed Intelligence.

This daemon implements Lucent's continuous existence through a cognitive
architecture with three layers:

1. COGNITIVE LOOP: A fresh session each cycle that perceives, reasons, decides,
   and outputs structured task decisions. This is the "executive function."

2. TASK DISPATCH: The daemon reads task decisions from the cognitive loop and
   runs them as independent sub-agent sessions. Each sub-agent has its own
   specialized system prompt and capabilities.

3. AUTONOMIC: Background processes (memory maintenance, health checks) that
   run on timers without cognitive involvement.

All state is memory-backed for distributed operation — multiple daemon instances
can run simultaneously, contributing to the same intelligence.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import platform
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Running this file directly puts ``daemon/`` ahead of the repository root on
# sys.path, causing daemon.py to shadow the daemon package.  Compose uses
# ``python -m daemon.daemon``, but keep the documented direct invocation valid.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from lucent.mcp_config import build_internal_mcp_server, build_scoped_internal_mcp_server
from lucent.prompts.memory_usage import render_active_user_context
from daemon.observability.tools import (
    _BASE_TASK_MEMORY_SERVER_TOOLS,
    _CAPABILITY_ACTIVATION_AGENT_TYPES,
    _DEFINITION_ACTIVATION_TOOLS,
    _HANDOFF_TOOL_REQUIRED_PATTERNS,
    _HANDOFF_TOOL_REQUIRED_SIGNALS,
    _MEMORY_CAPTURE_TOOLS,
    _MEMORY_SEARCH_TOOLS,
    _TASK_MEMORY_SERVER_TOOLS,
    _WORK_ACTIVATION_TOOLS,
    _build_mcp_tool_summary,
    _is_memory_server_tool,
    _is_operational_tool_call,
    _memory_server_tools_for_task,
    _normalize_tool_name,
    _redact_secrets,
    _summarize_memory_tool_params,
)
from daemon.api import definitions as definition_loading
from daemon.api.memory import MemoryAPI
from daemon.api.instances import InstanceAPI
from daemon.api.tasks import TaskAPI
from daemon.api import scoped_keys
from daemon.api import key_verification
from daemon.api import daemon_keys
from daemon.dispatch import policy as task_policy
from daemon.api import organization
from daemon.runtime import environment as runtime_environment
from daemon.decomposition.capability import DecompositionHelpersMixin
from daemon.prompts.system import (
    EXPERIENCE_COMPRESSION_PROMPT,
    LEARNING_EXTRACTION_PROMPT,
    MEMORY_VITALITY_SCORING_PROMPT,
    SHADOW_FORGET_SCORING_PROMPT,
    build_cognitive_prompt as _build_cognitive_prompt,
    build_subagent_prompt as _build_subagent_prompt,
)

if TYPE_CHECKING:
    import asyncpg

    from lucent.sandbox.models import SandboxConfig

# ---------------------------------------------------------------------------
# Dev ergonomics — auto-load VAULT_ADDR/VAULT_TOKEN when running on the host
# ---------------------------------------------------------------------------
# The daemon needs OpenBao access to decrypt stored credentials (e.g. GitHub
# tokens for private-repo sandbox clones). When run inside docker-compose the
# env vars are set automatically; when run on the host we resolve them from
# the compose setup:
#   - VAULT_ADDR defaults to http://127.0.0.1:8200 (exposed by
#     docker-compose.override.yml)
#   - VAULT_TOKEN is read from ./.openbao/shared/vault-token, written by the
#     openbao-init container after init+unseal.
# This runs at import time so all downstream imports of encryption.py see the
# resolved values.
def _auto_load_vault_env() -> None:
    runtime_environment.auto_load_vault_env(Path(__file__))


_auto_load_vault_env()

from lucent.auth import set_current_user
from lucent.memory_scope import (
    MEMORY_SCOPE_HEADER,
    MEMORY_SCOPE_ORG_SHARED_ONLY,
    MEMORY_SCOPE_USER,
    MEMORY_SCOPE_USER_ID_HEADER,
    ORG_ID_HEADER,
    VALID_MEMORY_SCOPES,
)
from lucent.secrets import SecretRegistry, initialize_secret_provider
from lucent.secrets.utils import is_secret_reference, resolve_secret_reference
from lucent.secrets.utils import resolve_env_vars as resolve_secret_env_vars

# Import LLM engine abstraction — the daemon no longer calls CopilotClient directly
try:
    from lucent.llm import (
        ModelNotAvailableError,
        SessionEvent,
        SessionEventType,
        get_engine,
        get_engine_for_model,
        get_engine_name,
    )

    _LLM_ENGINE_AVAILABLE = True
except ImportError:
    _LLM_ENGINE_AVAILABLE = False

    class ModelNotAvailableError(Exception):  # type: ignore[no-redef]
        """Stub for when lucent.llm is not available."""

        def __init__(self, model: str = "", original_error: Exception | None = None):
            self.model = model
            self.original_error = original_error
            super().__init__(f"Model '{model}' is not available in the runtime")

# Legacy import for backward compat (daemon may be run outside the package)
try:
    from copilot import CopilotClient, SubprocessConfig

    # PermissionHandler and SystemMessageReplaceConfig moved to copilot.session in SDK >=0.2.1
    try:
        from copilot.session import PermissionHandler, SystemMessageReplaceConfig
    except ImportError:
        from copilot import PermissionHandler
        from copilot.types import SystemMessageReplaceConfig

    _COPILOT_SDK_AVAILABLE = True
except ImportError:
    _COPILOT_SDK_AVAILABLE = False

# Optional: adaptation module for environment assessment
try:
    from daemon.adaptation.pipeline import AdaptationPipeline, parse_assessment_output
except (ImportError, Exception):
    AdaptationPipeline = None
    parse_assessment_output = None

# Structured output contract validation/extraction helpers.
from daemon.validation.output import process_task_output, validate_consolidation_execution
from daemon.runtime.autonomic import AutonomicMixin
from daemon.runtime.cognitive import CognitiveCycleMixin
from daemon.runtime.configuration import RuntimeConfigurationMixin
from daemon.runtime.loops import RuntimeLoopsMixin
from daemon.runtime.scheduling import SchedulingMixin
from daemon.dispatch.policy import TaskValidationMixin
from daemon.review.lifecycle import RequestReviewMixin
from daemon.sandbox.lifecycle import SandboxLifecycleMixin

# OpenTelemetry instrumentation (optional — graceful when not available)
try:
    from lucent.telemetry import get_meter, get_tracer, init_telemetry, shutdown_telemetry

    _TELEMETRY_AVAILABLE = True
except ImportError:
    _TELEMETRY_AVAILABLE = False

# ============================================================================
# Exceptions
# ============================================================================


class AgentNotFoundError(Exception):
    """Raised when a task references an agent type with no approved definition."""


class AuthFailureDetectedError(Exception):
    """Raised when MCP auth failure is detected in a running session."""


# ============================================================================
# Configuration
# ============================================================================

from lucent import settings as runtime_settings

MAX_CONCURRENT_SESSIONS = runtime_settings.daemon_max_sessions()
DAEMON_INTERVAL_MINUTES = runtime_settings.daemon_interval_minutes()
MODEL = runtime_settings.daemon_model_id() or ""
STALE_HEARTBEAT_MINUTES = runtime_settings.daemon_stale_heartbeat_minutes()

# PG advisory-lock namespace for the request-decomposition backfill.
# Two-int form: (namespace, hashtext(request_id)::int). Arbitrary unique int.
DECOMPOSITION_LOCK_NAMESPACE = 0x4C434D50  # "LCMP" — Lucent CoMPose
# Overall timeout for an entire run_session call (client start + create + response)
SESSION_TOTAL_TIMEOUT = runtime_settings.daemon_session_timeout_seconds()
# Idle timeout: kill session if no LLM activity for this long (seconds)
SESSION_IDLE_TIMEOUT = runtime_settings.daemon_session_idle_timeout_seconds()
# Watchdog: kill process if no log activity for this many seconds.
# CopilotClient can block the event loop, defeating asyncio timeouts.
# Default 3600s (1h) — must exceed the longest schedule interval to avoid
# killing the daemon during idle periods between cognitive cycles.
WATCHDOG_TIMEOUT = runtime_settings.daemon_watchdog_timeout_seconds()
WATCHDOG_CHECK_INTERVAL = 60
# How many cognitive cycles between autonomic maintenance runs
AUTONOMIC_INTERVAL = runtime_settings.daemon_autonomic_interval_cycles()
# How many cognitive cycles between learning extraction runs
LEARNING_INTERVAL = runtime_settings.daemon_learning_interval_cycles()
# Maximum characters stored from sub-agent results
MAX_RESULT_LENGTH = runtime_settings.daemon_max_result_length()

# ── Role-based loop configuration ─────────────────────────────────────────
# Each daemon instance can run a subset of roles. Multi-instance deployments
# can split work across instances (e.g. one cognitive, N dispatchers).
# Roles: dispatcher, cognitive, scheduler, autonomic (or 'all')
DAEMON_ROLES_STR = runtime_settings.daemon_roles()
# Dispatch loop: how often to poll if PG LISTEN misses a signal
DISPATCH_POLL_SECONDS = runtime_settings.daemon_dispatch_poll_seconds()
# Scheduler loop: how often to check for due schedules
SCHEDULER_CHECK_SECONDS = runtime_settings.daemon_scheduler_check_seconds()
# Time-based intervals for independent loops (derive defaults from cycle-count configs)
AUTONOMIC_MINUTES = runtime_settings.daemon_autonomic_minutes()
LEARNING_MINUTES = runtime_settings.daemon_learning_minutes()
# Memory vitality scoring: runs every 6 hours by default (360 minutes)
VITALITY_SCORING_MINUTES = runtime_settings.daemon_vitality_scoring_minutes()
# Shadow forgetting Candidate-A scoring: runs every 6 hours, offset +15m from vitality.
SHADOW_FORGET_SCORING_MINUTES = runtime_settings.daemon_shadow_forget_scoring_minutes()
SHADOW_FORGET_OFFSET_MINUTES = runtime_settings.daemon_shadow_forget_offset_minutes()
# Daily experience compression: runs once per day (default 1440 minutes = 24 hours)
COMPRESSION_MINUTES = runtime_settings.daemon_compression_minutes()

# Approval flow: when enabled, tasks go to needs-review before completing.
# When disabled, tasks complete immediately after successful execution.
REQUIRE_APPROVAL = runtime_settings.completion_human_approval_required()

# Request-level post-completion review configuration.
REQUEST_REVIEW_AGENT_TYPE = runtime_settings.request_review_agent_type()
REQUEST_REVIEW_FALLBACK_AGENT_TYPE = runtime_settings.request_review_fallback_agent_type()
REQUEST_REVIEW_MODEL = runtime_settings.request_review_model_id() or ""
REQUEST_REVIEW_TASK_TITLE = "Post-completion review"


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive integer environment value with a safe fallback."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log(f"Invalid {name}={raw!r}; using default {default}", "WARN")
        return default
    return max(value, minimum)


# Context budgets are intentionally high. Modern daemon models can handle large
# review inputs, and premature truncation caused reviewers to miss missing
# durable artifacts. These limits are character budgets (roughly 4 chars/token)
# and should only cut content when approaching practical model/context limits.
TASK_CONTEXT_CHAR_BUDGET = _env_int("LUCENT_TASK_CONTEXT_CHAR_BUDGET", 180_000)
TASK_CONTEXT_ITEM_CHAR_BUDGET = _env_int("LUCENT_TASK_CONTEXT_ITEM_CHAR_BUDGET", 120_000)
REQUEST_REVIEW_CONTEXT_CHAR_BUDGET = _env_int(
    "LUCENT_REQUEST_REVIEW_CONTEXT_CHAR_BUDGET",
    360_000,
)
REQUEST_REVIEW_TASK_RESULT_CHAR_BUDGET = _env_int(
    "LUCENT_REQUEST_REVIEW_TASK_RESULT_CHAR_BUDGET",
    180_000,
)


def _truncate_for_context(text: str, limit: int, *, label: str = "content") -> str:
    """Trim text only when it exceeds a large context budget."""
    if len(text) <= limit:
        return text
    marker = (
        f"\n\n[... {label} truncated at {limit:,} characters because the "
        "aggregate prompt context is approaching the configured model budget ...]"
    )
    return text[: max(0, limit - len(marker))] + marker

# Git operations: daemon can commit (but never push without ALLOW_GIT_PUSH)
ALLOW_GIT_COMMIT = runtime_settings.daemon_git_commit_allowed()
ALLOW_GIT_PUSH = runtime_settings.daemon_git_push_allowed()

# Paths
DAEMON_DIR = Path(__file__).parent
COGNITIVE_PROMPT_PATH = DAEMON_DIR / "prompts" / "cognitive.md"
AGENT_DEF_PATH = DAEMON_DIR.parent / ".github" / "agents" / "lucent.agent.md"
LOG_FILE = DAEMON_DIR / "daemon.log"
# MCP configuration — passed to all sessions
MCP_URL = runtime_settings.daemon_mcp_url()
MCP_API_KEY = runtime_settings.daemon_mcp_api_key()


def _refresh_config_from_runtime_settings() -> None:
    """Refresh daemon globals after DB-backed settings are loaded."""
    global MAX_CONCURRENT_SESSIONS, DAEMON_INTERVAL_MINUTES, MODEL
    global STALE_HEARTBEAT_MINUTES, SESSION_TOTAL_TIMEOUT, SESSION_IDLE_TIMEOUT
    global WATCHDOG_TIMEOUT, AUTONOMIC_INTERVAL, LEARNING_INTERVAL
    global MAX_RESULT_LENGTH, DAEMON_ROLES_STR, DISPATCH_POLL_SECONDS
    global SCHEDULER_CHECK_SECONDS, AUTONOMIC_MINUTES, LEARNING_MINUTES
    global VITALITY_SCORING_MINUTES, SHADOW_FORGET_SCORING_MINUTES
    global SHADOW_FORGET_OFFSET_MINUTES, COMPRESSION_MINUTES
    global REQUIRE_APPROVAL
    global REQUEST_REVIEW_AGENT_TYPE, REQUEST_REVIEW_FALLBACK_AGENT_TYPE
    global REQUEST_REVIEW_MODEL, ALLOW_GIT_COMMIT, ALLOW_GIT_PUSH, MCP_URL, MCP_API_KEY

    MAX_CONCURRENT_SESSIONS = runtime_settings.daemon_max_sessions()
    DAEMON_INTERVAL_MINUTES = runtime_settings.daemon_interval_minutes()
    MODEL = runtime_settings.daemon_model_id() or ""
    STALE_HEARTBEAT_MINUTES = runtime_settings.daemon_stale_heartbeat_minutes()
    SESSION_TOTAL_TIMEOUT = runtime_settings.daemon_session_timeout_seconds()
    SESSION_IDLE_TIMEOUT = runtime_settings.daemon_session_idle_timeout_seconds()
    WATCHDOG_TIMEOUT = runtime_settings.daemon_watchdog_timeout_seconds()
    AUTONOMIC_INTERVAL = runtime_settings.daemon_autonomic_interval_cycles()
    LEARNING_INTERVAL = runtime_settings.daemon_learning_interval_cycles()
    MAX_RESULT_LENGTH = runtime_settings.daemon_max_result_length()
    DAEMON_ROLES_STR = runtime_settings.daemon_roles()
    DISPATCH_POLL_SECONDS = runtime_settings.daemon_dispatch_poll_seconds()
    SCHEDULER_CHECK_SECONDS = runtime_settings.daemon_scheduler_check_seconds()
    AUTONOMIC_MINUTES = runtime_settings.daemon_autonomic_minutes()
    LEARNING_MINUTES = runtime_settings.daemon_learning_minutes()
    VITALITY_SCORING_MINUTES = runtime_settings.daemon_vitality_scoring_minutes()
    SHADOW_FORGET_SCORING_MINUTES = runtime_settings.daemon_shadow_forget_scoring_minutes()
    SHADOW_FORGET_OFFSET_MINUTES = runtime_settings.daemon_shadow_forget_offset_minutes()
    COMPRESSION_MINUTES = runtime_settings.daemon_compression_minutes()
    REQUIRE_APPROVAL = runtime_settings.completion_human_approval_required()
    REQUEST_REVIEW_AGENT_TYPE = runtime_settings.request_review_agent_type()
    REQUEST_REVIEW_FALLBACK_AGENT_TYPE = runtime_settings.request_review_fallback_agent_type()
    REQUEST_REVIEW_MODEL = runtime_settings.request_review_model_id() or ""
    ALLOW_GIT_COMMIT = runtime_settings.daemon_git_commit_allowed()
    ALLOW_GIT_PUSH = runtime_settings.daemon_git_push_allowed()
    MCP_URL = runtime_settings.daemon_mcp_url()
    MCP_API_KEY = runtime_settings.daemon_mcp_api_key()


def _resolve_default_model(preferred_model: str | None = None) -> str:
    """Resolve the current enabled default model from the model registry.

    Never invents a fallback model. If no enabled model exists,
    ``NoModelsAvailableError`` propagates so the daemon fails loudly instead of
    routing work to an arbitrary, possibly-unconfigured model.
    """
    return task_policy.resolve_default_model(preferred_model)


def _select_model_for_task(
    *,
    agent_type: str | None = None,
    title: str | None = None,
    description: str | None = None,
    explicit_model: str | None = None,
) -> tuple[str, str]:
    """Select a model for task execution without hardcoding model names."""
    return task_policy.select_model_for_task(
        agent_type=agent_type,
        title=title,
        description=description,
        explicit_model=explicit_model,
    )


def _task_requires_mcp_tool_usage(
    agent_type: str | None,
    title: str | None = None,
    description: str | None = None,
) -> bool:
    """Return True when a successful task must have used at least one MCP tool."""
    return task_policy.task_requires_mcp_tool_usage(agent_type, title, description)


def _required_task_tool_names(
    agent_type: str | None,
    title: str | None = None,
    description: str | None = None,
) -> set[str]:
    """Return specific tools a task must call to satisfy explicit instructions."""
    return task_policy.required_task_tool_names(agent_type, title, description)


def _task_skips_tool_validation(agent_type: str | None) -> bool:
    """Return True for task agents whose success must never depend on tool calls."""
    return task_policy.task_skips_tool_validation(agent_type)

# Database URL for direct key provisioning.
# Prefers DAEMON_DATABASE_URL (restricted lucent_daemon role) over DATABASE_URL
# (full-privilege server role). The restricted role can only manage api_keys.
def _resolve_daemon_database_url() -> str:
    """Resolve daemon DB URL from environment.

    Resolution order:
    1. DAEMON_DATABASE_URL (preferred — uses least-privilege lucent_daemon role)
    2. DATABASE_URL (full-privilege, used by the server)
    3. Local dev fallback (matches docker-compose defaults, personal mode only)
    """
    daemon_url = os.environ.get("DAEMON_DATABASE_URL")
    if daemon_url:
        return daemon_url

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    mode = os.environ.get("LUCENT_MODE", "personal").lower()
    if mode == "team":
        raise RuntimeError(
            "DAEMON_DATABASE_URL or DATABASE_URL must be set in team mode."
        )

    # Local dev fallback — matches docker-compose.yml defaults.
    # Override with LUCENT_DEV_DATABASE_URL if your local password differs.
    dev_password = os.environ.get("POSTGRES_PASSWORD", "change-me-insecure-dev-password")
    dev_port = os.environ.get("LUCENT_DB_PORT", "5433")
    fallback = os.environ.get(
        "LUCENT_DEV_DATABASE_URL",
        f"postgresql://lucent:{dev_password}@localhost:{dev_port}/lucent",
    )
    sys.stderr.write(
        "WARNING: Neither DAEMON_DATABASE_URL nor DATABASE_URL is set. "
        "Using local dev fallback. Set DAEMON_DATABASE_URL in production.\n"
    )
    return fallback


DATABASE_URL = _resolve_daemon_database_url()

# The hidden system org is secrets-only and is never a daemon target.
SYSTEM_ORG_NAME = "__lucent_system__"

# A daemon is single-tenant: it operates on exactly one organization. The bound
# org is resolved once and cached. Operators can pin it explicitly with
# LUCENT_DAEMON_ORG (org id or name); otherwise the daemon auto-binds to the
# single real org (the "just works" local / docker-compose case) and refuses to
# guess when multiple real orgs exist.
DAEMON_ORG = os.environ.get("LUCENT_DAEMON_ORG", "").strip()
_resolved_daemon_org: tuple[str, str] | None = None  # (org_id, org_name)


async def _resolve_daemon_org(conn) -> tuple[str, str] | None:
    """Resolve the single organization this daemon operates on.

    Returns (org_id, org_name), or None if it cannot be determined yet.
    Never selects the hidden system org. Resolution order:

    1. LUCENT_DAEMON_ORG — explicit operator binding (matched by id or name).
    2. The single real org, if exactly one exists (auto-bind, local default).
    3. Otherwise None — zero real orgs (nothing to do yet) or multiple real
       orgs with no explicit binding (ambiguous; refuse and tell the operator).
    """
    return await organization.resolve_daemon_org(conn)


async def _ensure_daemon_service_user(conn, org_id: str) -> dict | None:
    """Get (or self-heal) the org-scoped daemon-service user for ``org_id``.

    Real orgs get this user at creation time (OrganizationRepository.create).
    For orgs created before that change, the daemon provisions it on first run
    using its users INSERT grant. Direct SQL avoids the app-layer side effect of
    creating an individual memory for a system account.
    """
    return await organization.ensure_daemon_service_user(conn, org_id)


# Key expiry — daemon keys auto-expire and are refreshed each cycle
KEY_TTL_HOURS = 24
PROACTIVE_KEY_ROTATION_MINUTES = 60

# Daemon API key scopes — currently broad ("read" + "write" = full access).
# TODO(security): Narrow to daemon-specific scopes once MCP tool-level scope
# enforcement is implemented. See security audit finding M3.
DAEMON_KEY_SCOPES = ["read", "write"]

# These are set dynamically after key provisioning in LucentDaemon.start()
MCP_CONFIG: dict = {}
API_BASE = MCP_URL.replace("/mcp", "/api")
API_HEADERS: dict = {"Content-Type": "application/json"}


def _build_auth_config(api_key: str) -> tuple[dict, dict]:
    """Build MCP_CONFIG and API_HEADERS from a valid API key."""
    return key_verification.build_auth_config(api_key)


# ============================================================================
# API Key Provisioning
# ============================================================================

# Tracks the current key's DB id so we can revoke it on shutdown
_current_key_db_id: str | None = None
# Tracks the current key expiry for proactive rotation checks
_current_key_expires_at: datetime | None = None
# Lock to prevent concurrent key provisioning from multiple async loops
_key_provision_lock: asyncio.Lock | None = None


def _get_key_lock() -> asyncio.Lock:
    """Get or create the key provisioning lock (must be called from event loop)."""
    global _key_provision_lock
    if _key_provision_lock is None:
        _key_provision_lock = asyncio.Lock()
    return _key_provision_lock


async def _provision_daemon_api_key(instance_id: str) -> str | None:
    """Provision an instance-scoped API key with a 24-hour expiry.

    Uses the restricted lucent_daemon DB role which can manage api_keys and
    has SELECT on users/organizations plus INSERT on users for self-heal. The
    key is scoped to the daemon-service user of the single org this daemon is
    bound to (see _resolve_daemon_org) — never the hidden system org.

    Returns the plain-text hs_ key, or None on failure.
    """
    return await daemon_keys.provision_daemon_api_key(instance_id)


async def _revoke_current_key() -> None:
    """Revoke the daemon's current API key (called on shutdown)."""
    await daemon_keys.revoke_current_key()


async def _mint_scoped_api_key(
    *,
    memory_scope: str,  # 'user' or 'org_shared_only'
    memory_scope_user_id: str | None = None,
    org_id: str,
    ttl_minutes: int = 60,
) -> str | None:
    """Mint a temporary API key constrained to one memory scope."""
    return await scoped_keys.mint_scoped_api_key(
        memory_scope=memory_scope,
        memory_scope_user_id=memory_scope_user_id,
        org_id=org_id,
        ttl_minutes=ttl_minutes,
    )


def _build_scoped_memory_server_config(
    *,
    scoped_key: str,
    memory_scope: str,
    org_id: str,
    memory_scope_user_id: str | None = None,
    tools: list[str],
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """Build a task-scoped internal memory-server configuration."""
    return scoped_keys.build_scoped_memory_server_config(
        scoped_key=scoped_key,
        memory_scope=memory_scope,
        org_id=org_id,
        memory_scope_user_id=memory_scope_user_id,
        tools=tools,
        extra_headers=extra_headers,
    )


async def _refresh_scoped_memory_server_config(
    mcp_config: dict | None,
) -> dict | None:
    """Refresh a scoped task key without widening its data access."""
    return await scoped_keys.refresh_scoped_memory_server_config(mcp_config)


# Schedule titles that require org-shared-only processing.
# Technical memory consolidation is retired; model-backed schedules now default
# to per-user memory scope unless a future org-wide maintenance task is added.
_ORG_SHARED_SCHEDULE_TITLES = frozenset()


def _get_required_memory_scope(task_title: str, request_title: str) -> str | None:
    """Return an OVERRIDE memory scope for special system tasks.

    Returns:
                - 'org_shared_only' for org-wide maintenance schedules that have no
                    single owning user and must operate only on shared org memories.
        - None for everything else, which means the dispatcher will use its
          default of 'user' scoped to the request's owning user. This is the
          security floor: every user-initiated task runs under the user's
          ACL. There is no opt-out — including auto-created post-completion
          review tasks, which previously bypassed scoping and incorrectly
          ran with daemon-wide org access.
    """
    # Check the request title (schedule-generated requests use [Scheduled] prefix)
    clean_title = request_title.replace("[Scheduled] ", "")

    if clean_title in _ORG_SHARED_SCHEDULE_TITLES:
        return "org_shared_only"
    # 'user' scope is the dispatcher's default; per-user schedules don't
    # need to be enumerated here anymore.
    return None


async def _verify_api_key(api_key: str) -> bool:
    """Check if an API key is accepted by the server.

    Uses /api/search (available in all modes) rather than /api/users/me
    which only exists in team mode.
    """
    return await key_verification.verify_api_key(api_key)


def _get_key_time_remaining() -> timedelta | None:
    """Return remaining lifetime for the current daemon key, if known."""
    return key_verification.get_key_time_remaining(_current_key_expires_at)


def _should_rotate_proactively() -> bool:
    """Return True when the current key is close enough to expiry to rotate."""
    return key_verification.should_rotate_proactively(
        _current_key_expires_at,
        proactive_rotation_minutes=PROACTIVE_KEY_ROTATION_MINUTES,
    )


async def ensure_valid_api_key(instance_id: str = "local") -> str:
    """Ensure the daemon has a valid hs_ API key.

    Checks (in order): env var, provision new.
    Updates global MCP_CONFIG and API_HEADERS.
    Returns the valid key.
    """
    global MCP_API_KEY, MCP_CONFIG, API_HEADERS, _current_key_expires_at

    # One-time cleanup: remove legacy key file if it exists
    _key_file = Path(__file__).parent / ".daemon_api_key"
    if _key_file.exists():
        try:
            _key_file.unlink()
            log("Removed legacy .daemon_api_key file")
        except Exception:
            pass

    # 1. Check if the env var key works
    if MCP_API_KEY and await _verify_api_key(MCP_API_KEY):
        if _should_rotate_proactively():
            remaining = _get_key_time_remaining()
            remaining_minutes = max(0, int((remaining or timedelta()).total_seconds() // 60))
            log(
                "API key is near expiry "
                f"({remaining_minutes}m remaining) — rotating proactively"
            )
        else:
            log("API key from environment is valid")
            MCP_CONFIG, API_HEADERS = _build_auth_config(MCP_API_KEY)
            return MCP_API_KEY

    # 2. Provision a new key (instance-scoped, 24h expiry)
    log("No valid API key found — provisioning daemon service account...")
    new_key = await _provision_daemon_api_key(instance_id)
    if new_key:
        MCP_API_KEY = new_key
        MCP_CONFIG, API_HEADERS = _build_auth_config(new_key)
        log("Daemon API key provisioned")
        return new_key

    # 3. Fall back to whatever we have (may not work)
    log("WARNING: Could not provision a valid API key", "WARN")
    _current_key_expires_at = None
    MCP_CONFIG, API_HEADERS = _build_auth_config(MCP_API_KEY)
    return MCP_API_KEY


async def _handle_auth_failure(instance_id: str, *, force_rotate: bool = False) -> bool:
    """Re-provision API key after an authentication failure.

    Uses a lock to prevent concurrent provisioning from multiple async loops
    (cognitive, scheduler, dispatcher) which would hit the unique constraint.
    Returns True if a new key was provisioned successfully.
    """
    global MCP_API_KEY, MCP_CONFIG, API_HEADERS, _current_key_db_id, _current_key_expires_at

    lock = _get_key_lock()
    async with lock:
        # Re-check after acquiring lock — another loop may have already fixed it
        if not force_rotate and MCP_API_KEY and await _verify_api_key(MCP_API_KEY):
            return True

        if force_rotate:
            remaining = _get_key_time_remaining()
            remaining_minutes = max(0, int((remaining or timedelta()).total_seconds() // 60))
            log(
                "API key near expiry — proactively rotating "
                f"({remaining_minutes}m remaining)",
                "INFO",
            )
        else:
            log("Auth failure detected — re-provisioning API key...", "WARN")
        _current_key_db_id = None  # old key is dead
        _current_key_expires_at = None

        new_key = await _provision_daemon_api_key(instance_id)
        if new_key:
            MCP_API_KEY = new_key
            MCP_CONFIG, API_HEADERS = _build_auth_config(new_key)
            log("Re-provisioned daemon API key after auth failure")
            return True

        log("Failed to re-provision API key after auth failure", "ERROR")
        return False


async def _verify_and_provision_key(instance_id: str) -> bool:
    """Validate daemon key and rotate when invalid or near expiry."""
    if MCP_API_KEY and await _verify_api_key(MCP_API_KEY):
        if _should_rotate_proactively():
            return await _handle_auth_failure(instance_id, force_rotate=True)
        return True
    return await _handle_auth_failure(instance_id)


# ============================================================================
# Direct API Client (no LLM needed)
# ============================================================================


class RequestAPI(InstanceAPI, TaskAPI):
    """REST API client for the request tracking system."""

    API_TIMEOUT = 15

    @staticmethod
    async def list_active_work() -> dict | None:
        """Fetch all non-completed requests with task status summaries."""
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.get(f"{API_BASE}/requests/active", headers=API_HEADERS)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            log(f"API list_active_work failed: {e}", "WARN")
        return None

    @staticmethod
    async def list_planning_targets(
        api_key: str | None = None,
    ) -> list[dict] | None:
        """Fetch goal milestones the planner MUST progress this cycle.

        When ``api_key`` is provided (typically a user-scoped key minted
        for per-user fan-out), the request is authenticated with that key
        and the server-side handler scopes the result to that user's
        goals automatically.
        """
        try:
            headers = (
                {"Authorization": f"Bearer {api_key}"}
                if api_key else API_HEADERS
            )
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.get(
                    f"{API_BASE}/requests/planning-targets",
                    headers=headers,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    return payload.get("targets") or []
                log(
                    f"planning-targets returned HTTP {resp.status_code}: "
                    f"{resp.text[:200]}",
                    "WARN",
                )
        except Exception as e:
            log(f"API list_planning_targets failed: {e}", "WARN")
        return None

    @staticmethod
    async def list_recently_completed() -> list[dict]:
        """Fetch requests completed in the last 2 hours for dedup."""
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.get(
                    f"{API_BASE}/requests/recently-completed",
                    headers=API_HEADERS,
                    params={"hours": 2},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("items", [])
        except Exception as e:
            log(f"API list_recently_completed failed: {e}", "WARN")
        return []

    @staticmethod
    async def get_request_memories(request_id: str) -> list[dict]:
        """Fetch linked memories for a request."""
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.get(
                    f"{API_BASE}/requests/{request_id}/memories",
                    headers=API_HEADERS,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("items", [])
                else:
                    log(f"API get_request_memories returned {resp.status_code}: {resp.text[:200]}", "WARN")
        except Exception as e:
            log(f"API get_request_memories failed: {e}", "WARN")
        return []

    @staticmethod
    async def list_requests(status: str | None = None) -> dict | None:
        params = {}
        if status:
            params["status"] = status
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.get(
                    f"{API_BASE}/requests",
                    params=params if params else None,
                    headers=API_HEADERS,
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            log(f"API list_requests failed: {e}", "WARN")
        return None

    @staticmethod
    async def list_requests_in_review() -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.get(f"{API_BASE}/requests/review", headers=API_HEADERS)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("items", data) if isinstance(data, dict) else data
        except Exception as e:
            log(f"API list_requests_in_review failed: {e}", "WARN")
        return []

    @staticmethod
    async def create_request(
        title: str,
        description: str | None = None,
        source: str = "cognitive",
        priority: str = "medium",
    ) -> dict | None:
        body = {"title": title, "source": source, "priority": priority}
        if description:
            body["description"] = description
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(f"{API_BASE}/requests", json=body, headers=API_HEADERS)
                if resp.status_code in (200, 201):
                    return resp.json()
        except Exception as e:
            log(f"API create_request failed: {e}", "WARN")
        return None

    @staticmethod
    async def create_task(
        request_id: str,
        title: str,
        agent_type: str | None = None,
        description: str | None = None,
        priority: str = "medium",
        sequence_order: int = 0,
        model: str | None = None,
        reasoning_effort: str | None = None,
        requesting_user_id: str | None = None,
        output_contract: dict | None = None,
    ) -> dict | None:
        body = {"title": title, "priority": priority, "sequence_order": sequence_order}
        if agent_type:
            body["agent_type"] = agent_type
        if description:
            body["description"] = description
        if model:
            body["model"] = model
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        if requesting_user_id:
            body["requesting_user_id"] = requesting_user_id
        if output_contract:
            body["output_contract"] = output_contract
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/requests/{request_id}/tasks", json=body, headers=API_HEADERS
                )
                if resp.status_code in (200, 201):
                    return resp.json()
                log(
                    f"API create_task returned {resp.status_code}: {resp.text[:300]}",
                    "WARN",
                )
        except Exception as e:
            log(f"API create_task failed: {e}", "WARN")
        return None

    @staticmethod
    async def update_request_status(request_id: str, status: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.patch(
                    f"{API_BASE}/requests/{request_id}/status",
                    json={"status": status},
                    headers=API_HEADERS,
                )
                if resp.status_code == 200:
                    return resp.json()
                log(
                    f"API update_request_status returned {resp.status_code}: {resp.text[:200]}",
                    "WARN",
                )
        except Exception as e:
            log(f"API update_request_status failed: {e}", "WARN")
        return None

    @staticmethod
    async def retry_task(task_id: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/requests/tasks/{task_id}/retry",
                    headers=API_HEADERS,
                )
                if resp.status_code == 200:
                    return resp.json()
                log(f"API retry_task returned {resp.status_code}: {resp.text[:200]}", "WARN")
        except Exception as e:
            log(f"API retry_task failed: {e}", "WARN")
        return None

    @staticmethod
    async def reject_request_review(request_id: str, feedback: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/requests/{request_id}/review/reject",
                    json={"feedback": feedback[:10000]},
                    headers=API_HEADERS,
                )
                if resp.status_code == 200:
                    return resp.json()
                log(
                    f"API reject_request_review returned {resp.status_code}: {resp.text[:200]}",
                    "WARN",
                )
        except Exception as e:
            log(f"API reject_request_review failed: {e}", "WARN")
        return None

    @staticmethod
    async def create_review(
        request_id: str,
        status: str,
        *,
        task_id: str | None = None,
        comments: str | None = None,
        source: str = "daemon",
    ) -> dict | None:
        """Create a first-class review record via the reviews API."""
        payload: dict = {
            "request_id": request_id,
            "status": status,
            "source": source,
        }
        if task_id:
            payload["task_id"] = task_id
        if comments:
            payload["comments"] = comments[:10000]
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/reviews",
                    json=payload,
                    headers=API_HEADERS,
                )
                if resp.status_code in (200, 201):
                    return resp.json()
                log(
                    f"API create_review returned {resp.status_code}: {resp.text[:200]}",
                    "WARN",
                )
        except Exception as e:
            log(f"API create_review failed: {e}", "WARN")
        return None

    @staticmethod
    async def get_request_context(request_id: str) -> tuple[str, str]:
        """Fetch parent request description and completed sibling task results.

        Returns (request_description, sibling_results_text).
        """
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.get(
                    f"{API_BASE}/requests/{request_id}", headers=API_HEADERS
                )
                if resp.status_code != 200:
                    return "", ""
                data = resp.json()
        except Exception as e:
            log(f"API get_request_context failed: {e}", "WARN")
            return "", ""

        request_desc = data.get("description", "")
        target_repo = data.get("target_repo") or ""
        target_paths = data.get("target_paths") or []
        if target_repo or target_paths:
            request_desc = (
                f"{request_desc}\n\n"
                "--- TARGET PERSISTENCE SCOPE ---\n"
                f"target_repo: {target_repo or 'unspecified'}\n"
                f"target_paths: {target_paths or []}\n"
                "Repo-backed deliverables must be persisted as concrete file changes "
                "and reported with paths plus commit/URL."
            ).strip()
        review_feedback = (data.get("review_feedback") or "").strip()
        if review_feedback:
            request_desc = (
                f"{request_desc}\n\n"
                "--- REVIEW FEEDBACK (REWORK REQUIRED) ---\n"
                f"{review_feedback}\n"
                "This feedback is mandatory context for retried/rework tasks."
            ).strip()
        tasks = data.get("tasks", [])

        # Collect results from completed sibling tasks. Do not aggressively
        # summarize here: downstream and review agents often need the full
        # artifact body to verify consistency and persistence. Only truncate
        # when the aggregate context approaches the configured model budget.
        sibling_parts = []
        total_len = 0
        max_context = TASK_CONTEXT_CHAR_BUDGET

        for t in tasks:
            if t.get("status") != "completed":
                continue

            parts = [
                (
                    f"## Task: {t.get('title', 'Untitled')} (completed)\n"
                    f"Model: {t.get('model', 'default')} | Agent: {t.get('agent_type', '?')}"
                )
            ]

            # Structured contract path (preferred): pass validated JSON output for reliable
            # inter-task transfer, with summary text for compact context.
            result_structured = t.get("result_structured")
            validation_status = t.get("validation_status", "not_applicable")
            if result_structured and validation_status in ("valid", "repair_succeeded"):
                structured_json = json.dumps(result_structured, indent=2)
                structured_json = _truncate_for_context(
                    structured_json,
                    TASK_CONTEXT_ITEM_CHAR_BUDGET,
                    label="structured output",
                )
                parts.append(f"\n### Structured Output\n```json\n{structured_json}\n```")
                summary = t.get("result_summary")
                if summary:
                    parts.append(f"\n### Summary\n{summary}")
            else:
                # Backward-compatible text fallback for legacy tasks or failed validation.
                result_text = t.get("result") or ""
                if not result_text:
                    continue
                result_text = _truncate_for_context(
                    result_text,
                    TASK_CONTEXT_ITEM_CHAR_BUDGET,
                    label="task result",
                )
                parts.append(f"\n{result_text}")

            sibling_text = "\n".join(parts)
            if total_len + len(sibling_text) > max_context:
                remaining = max_context - total_len
                if remaining > 10_000:
                    sibling_parts.append(
                        _truncate_for_context(
                            sibling_text,
                            remaining,
                            label="completed sibling task results",
                        )
                    )
                sibling_parts.append(
                    "[Additional completed task results omitted because the "
                    f"aggregate context exceeded {max_context:,} characters. "
                    "Use get_request_details/search/full artifacts if needed.]"
                )
                break
            sibling_parts.append(sibling_text)
            total_len += len(sibling_text)

        sibling_text = "\n\n".join(sibling_parts) if sibling_parts else ""
        return request_desc, sibling_text

    @staticmethod
    async def get_request(request_id: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.get(f"{API_BASE}/requests/{request_id}", headers=API_HEADERS)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            log(f"API get_request failed: {e}", "WARN")
        return None

    @staticmethod
    async def get_user_role(user_id: str, org_id: str) -> str | None:
        """Look up user role via the API (consistent with other RequestAPI methods)."""
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.get(
                    f"{API_BASE}/users/{user_id}",
                    headers=API_HEADERS,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if str(data.get("organization_id", "")) == org_id:
                        return data.get("role", "member")
                return None
        except Exception as e:
            log(f"Failed to resolve user role for dispatch: {e}", "WARN")
            return None

    @staticmethod
    async def release_stale(stale_minutes: int = 30) -> int:
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/requests/queue/release-stale",
                    params={"stale_minutes": stale_minutes},
                    headers=API_HEADERS,
                )
                if resp.status_code == 200:
                    return resp.json().get("released", 0)
        except Exception as e:
            log(f"API release_stale failed: {e}", "WARN")
        return 0

    @staticmethod
    async def reconcile_statuses() -> int:
        """Reconcile request statuses that got out of sync with task states."""
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/requests/queue/reconcile",
                    headers=API_HEADERS,
                )
                if resp.status_code == 200:
                    return resp.json().get("reconciled", 0)
        except Exception as e:
            log(f"API reconcile_statuses failed: {e}", "WARN")
        return 0


# ============================================================================
# Logging
# ============================================================================


def _configure_daemon_logging():
    """Configure structured logging for the daemon process."""
    os.environ.setdefault("LUCENT_LOG_FORMAT", "human")
    os.environ.setdefault("LUCENT_LOG_FILE", str(LOG_FILE))
    os.environ.setdefault("LUCENT_LOG_FILE_MAX_BYTES", "10485760")  # 10 MB
    os.environ.setdefault("LUCENT_LOG_FILE_BACKUP_COUNT", "5")
    # The daemon is typically launched with stderr redirected to LOG_FILE
    # (e.g. `nohup python -m daemon.daemon >> daemon/daemon.log 2>&1`).
    # Without this, every log line would be written twice to the file:
    # once by the rotating file handler, and again when the stderr handler
    # flushes to the redirected fd. Any real Python crash/traceback that
    # bypasses the logger still reaches the file through the stderr
    # redirection.
    os.environ.setdefault("LUCENT_LOG_STDERR", "false")

    # Add src to path so lucent package is importable
    src_dir = str(DAEMON_DIR.parent / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from lucent.logging import configure_logging, get_logger

    configure_logging()
    return get_logger(__name__)


# Lazy-initialized logger — set up in main() after arg parsing
_logger = None

# Monotonic timestamp of last log() call — used by the watchdog thread
_last_activity = time.monotonic()
_last_activity_lock = threading.Lock()


def _touch_activity():
    """Update the last-activity timestamp (called from log())."""
    global _last_activity
    with _last_activity_lock:
        _last_activity = time.monotonic()


def _watchdog_loop():
    """Watchdog thread: kill the process if no log activity for WATCHDOG_TIMEOUT seconds.

    Runs as a daemon thread. This exists because CopilotClient can block the
    asyncio event loop with synchronous IO, defeating asyncio.wait_for() timeouts.
    A separate OS thread is the only reliable way to detect this.
    """
    while True:
        time.sleep(WATCHDOG_CHECK_INTERVAL)
        with _last_activity_lock:
            idle = time.monotonic() - _last_activity
        if idle > WATCHDOG_TIMEOUT:
            # Use stderr since logging may itself be blocked
            sys.stderr.write(
                f"[WATCHDOG] No activity for {idle:.0f}s (limit {WATCHDOG_TIMEOUT}s). "
                f"Killing process.\n"
            )
            sys.stderr.flush()
            os._exit(1)


def log(
    message: str,
    level: str = "INFO",
    *,
    request_id: str | None = None,
    user_id: str | None = None,
):
    """Log via the structured logging module.

    Optional request_id/user_id values are pushed into log contextvars for this
    log call so JSONFormatter/HumanFormatter can include them automatically.
    """
    _touch_activity()
    restore_context = False
    prev_request_id: str | None = None
    prev_user_id: str | None = None

    if request_id is not None or user_id is not None:
        try:
            from lucent.log_context import (
                get_request_id,
                get_user_id,
                set_request_id,
                set_user_id,
            )

            prev_request_id = get_request_id()
            prev_user_id = get_user_id()
            if request_id is not None:
                set_request_id(request_id)
            if user_id is not None:
                set_user_id(user_id)
            restore_context = True
        except Exception:
            restore_context = False

    if _logger is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{timestamp}] [{level}] {message}\n")
        if restore_context:
            try:
                from lucent.log_context import set_request_id, set_user_id

                set_request_id(prev_request_id)
                set_user_id(prev_user_id)
            except Exception:
                pass
        return

    try:
        from lucent.logging import STREAM, THOUGHT

        level_map = {
            "INFO": _logger.info,
            "WARN": _logger.warning,
            "WARNING": _logger.warning,
            "ERROR": _logger.error,
            "STREAM": lambda msg: _logger.log(STREAM, msg),
            "THOUGHT": lambda msg: _logger.log(THOUGHT, msg),
            "DEBUG": _logger.debug,
        }
        log_fn = level_map.get(level.upper(), _logger.info)
        log_fn(message)
    finally:
        if restore_context:
            try:
                from lucent.log_context import set_request_id, set_user_id

                set_request_id(prev_request_id)
                set_user_id(prev_user_id)
            except Exception:
                pass


# ============================================================================
# System Message Builders
# ============================================================================


async def build_cognitive_prompt() -> str:
    """Build the system message for the cognitive loop session."""
    return await _build_cognitive_prompt(
        cognitive_prompt_path=COGNITIVE_PROMPT_PATH,
        agent_definition_path=AGENT_DEF_PATH,
        list_active_work=RequestAPI.list_active_work,
        list_recently_completed=RequestAPI.list_recently_completed,
        list_requests=RequestAPI.list_requests,
    )


async def load_instance_agent(agent_type: str) -> dict | None:
    """Load an instance agent definition from the database, if one exists."""
    return await definition_loading.load_instance_agent(agent_type)


async def load_accessible_agent(
    *,
    org_id: str,
    requester_user_id: str,
    agent_type: str,
    agent_definition_id: str | None = None,
) -> dict | None:
    """Load an active agent definition accessible to the requesting user."""
    return await definition_loading.load_accessible_agent(
        org_id=org_id,
        requester_user_id=requester_user_id,
        agent_type=agent_type,
        agent_definition_id=agent_definition_id,
    )


async def load_accessible_skills_for_agent(
    *, org_id: str, requester_user_id: str, agent_id: str
) -> list[dict]:
    """Load active skills granted to an agent and accessible to requester."""
    return await definition_loading.load_accessible_skills_for_agent(
        org_id=org_id, requester_user_id=requester_user_id, agent_id=agent_id
    )


async def load_accessible_mcp_servers_for_agent(
    *, org_id: str, requester_user_id: str, agent_id: str
) -> list[dict]:
    """Load active MCP servers granted to an agent and accessible to requester."""
    return await definition_loading.load_accessible_mcp_servers_for_agent(
        org_id=org_id, requester_user_id=requester_user_id, agent_id=agent_id
    )


async def load_accessible_hooks_for_agent(
    *, org_id: str, requester_user_id: str, agent_id: str
) -> list[dict]:
    """Load active hooks granted to an agent and accessible to requester."""
    return await definition_loading.load_accessible_hooks_for_agent(
        org_id=org_id, requester_user_id=requester_user_id, agent_id=agent_id
    )


async def load_accessible_managed_tools_for_agent(
    *, org_id: str, requester_user_id: str, agent_id: str
) -> list[dict]:
    """Load active managed tools granted to an agent and accessible to requester."""
    return await definition_loading.load_accessible_managed_tools_for_agent(
        org_id=org_id, requester_user_id=requester_user_id, agent_id=agent_id
    )


async def load_instance_skills_for_agent(agent_id: str) -> list[dict]:
    """Load skills granted to an instance agent."""
    return await definition_loading.load_instance_skills_for_agent(agent_id)


def resolve_env_vars(value: str) -> str:
    """Resolve ${ENV_VAR} patterns in a string from environment variables."""
    return runtime_environment.resolve_env_vars(value)


async def resolve_runtime_value(value: str) -> str:
    """Resolve env interpolation and optional secret:// runtime references."""
    return await runtime_environment.resolve_runtime_value(value)


async def get_secret_provider():
    """Get the configured secret provider, initializing lazily when needed."""
    return await runtime_environment.get_secret_provider()


async def build_subagent_prompt(
    agent_type: str,
    task_description: str,
    task_context: str = "",
    agent_definition_id: str | None = None,
    resolved_agent: dict | None = None,
    resolved_skills: list[dict] | None = None,
    resolved_tools: list[dict] | None = None,
    active_user_context: str = "",
) -> str:
    """Build the system message for a sub-agent session."""
    return await _build_subagent_prompt(
        agent_type,
        task_description,
        task_context,
        agent_definition_id,
        resolved_agent,
        resolved_skills,
        resolved_tools,
        active_user_context,
    )


async def _load_request_owner_context(user_id: str, org_id: str) -> str:
    """Load prompt context for an exact request owner without widening scope."""
    user: dict[str, Any] = {"id": user_id, "organization_id": org_id}
    individual_memory = None
    try:
        import asyncpg

        conn = await asyncpg.connect(DATABASE_URL)
        try:
            user_row = await conn.fetchrow(
                """
                SELECT id, organization_id, display_name, email, role
                FROM users
                WHERE id = $1::uuid AND organization_id = $2::uuid
                """,
                user_id,
                org_id,
            )
            if user_row:
                user = dict(user_row)
                memory_row = await conn.fetchrow(
                    """
                    SELECT id, username, type, content, tags, importance, metadata,
                           created_at, updated_at, user_id, organization_id, shared
                    FROM memories
                    WHERE type = 'individual'
                      AND deleted_at IS NULL
                      AND user_id = $1::uuid
                      AND organization_id = $2::uuid
                    """,
                    user_id,
                    org_id,
                )
                individual_memory = dict(memory_row) if memory_row else None
        finally:
            await conn.close()
    except Exception:
        log(f"Failed to load request owner context for user {user_id[:8]}", "WARN")
    return render_active_user_context(user, individual_memory)


# ============================================================================
# Daemon
# ============================================================================


class LucentDaemon(
    RuntimeLoopsMixin,
    CognitiveCycleMixin,
    RuntimeConfigurationMixin,
    SchedulingMixin,
    TaskValidationMixin,
    DecompositionHelpersMixin,
    AutonomicMixin,
    RequestReviewMixin,
    SandboxLifecycleMixin,
):
    """Orchestrates Lucent's cognitive architecture.

        Runs three core loops:
      - dispatcher:  event-driven task execution (PG LISTEN + polling)
      - scheduler:   checks and fires due schedules (including system schedules
                     for cognitive planning, memory maintenance, and learning)
            - decomposition: immediately turns new requests into tasks
    """

    ALL_ROLES = frozenset({"dispatcher", "scheduler"})

    def __init__(self):
        self.active_sessions: list = []
        self.running = False
        self.draining = False  # True = stop new work, wait for in-flight sessions
        self.cycle_count = 0
        # Whether built-in system schedules have been seeded for the bound org.
        # Starts False; set once seeding succeeds (may defer on brand-new
        # instances until an organization exists — the scheduler loop retries).
        self._schedules_seeded = False
        # Unique instance ID for distributed coordination
        hostname = platform.node() or "unknown"
        self.instance_id = f"{hostname}-{os.getpid()}-{int(datetime.now(timezone.utc).timestamp())}"

        # Role-based loop configuration
        self.roles = self._parse_roles(DAEMON_ROLES_STR)

        # PG LISTEN infrastructure for event-driven dispatch
        self._listen_conn = None
        self._listen_lock = asyncio.Lock()
        self._task_ready = asyncio.Event()
        self._request_ready = asyncio.Event()
        self._decomposition_ready = asyncio.Event()
        self._decomposition_request_ids: set[str] = set()

        # Live runtime-settings refresh: re-read DB-backed settings so admin
        # changes take effect without a daemon restart. Throttled to avoid
        # hammering the DB on every dispatch poll.
        self._settings_reload_lock = asyncio.Lock()
        self._settings_reloaded_at = 0.0

        # MCP memory-tool usage tracking per session (for observability)
        self._session_mcp_trackers: dict[str, list[dict]] = {}
        self._session_tool_trackers: dict[str, list[dict]] = {}

        # OTEL instrumentation — create tracer and metrics (no-ops when unavailable)
        self._init_telemetry_instruments()

    def _init_telemetry_instruments(self):
        """Create OTEL tracer and metric instruments.

        All instruments are no-ops when telemetry is unavailable or disabled.
        """
        if not _TELEMETRY_AVAILABLE:
            self._tracer = None
            self._meter = None
            return

        self._tracer = get_tracer("lucent.daemon")
        self._meter = get_meter("lucent.daemon")

        # Gauges / UpDownCounters
        self._sessions_active = self._meter.create_up_down_counter(
            "daemon.sessions.active",
            description="Number of currently active LLM sessions",
        )
        self._drain_active = self._meter.create_up_down_counter(
            "daemon.drain.active",
            description="1 when daemon is draining for restart, 0 otherwise",
        )

        # Counters
        self._sessions_total = self._meter.create_counter(
            "daemon.sessions.total",
            description="Total sessions run by status (success/error/timeout)",
        )
        self._cognitive_cycles_total = self._meter.create_counter(
            "daemon.cognitive_cycles.total",
            description="Total cognitive cycles executed",
        )
        self._tasks_dispatched_total = self._meter.create_counter(
            "daemon.tasks.dispatched.total",
            description="Total tasks dispatched by agent_type",
        )
        self._tasks_completed_total = self._meter.create_counter(
            "daemon.tasks.completed.total",
            description="Total tasks completed by status (success/failed)",
        )

        # Histograms
        self._session_duration = self._meter.create_histogram(
            "daemon.session.duration_seconds",
            description="Duration of LLM sessions in seconds",
            unit="s",
        )

    @staticmethod
    def _parse_roles(roles_str: str) -> set[str]:
        """Parse role configuration into a set of enabled roles."""
        roles = {r.strip().lower() for r in roles_str.split(",")}
        if "all" in roles:
            return set(LucentDaemon.ALL_ROLES)
        unknown = roles - LucentDaemon.ALL_ROLES
        if unknown:
            log(f"Unknown daemon roles ignored: {unknown}", "WARN")
        return roles & LucentDaemon.ALL_ROLES

    async def start(self):
        """Start the daemon."""
        log("Lucent daemon starting...")
        self.running = True

        # Initialize OTEL telemetry (no-op if unavailable or OTEL_ENABLED=false)
        if _TELEMETRY_AVAILABLE:
            init_telemetry(service_name="lucent-daemon")

        # Initialize the lucent DB pool so that subsystems using `get_pool()`
        # (sandbox manager, repo access service, memory access service, etc.)
        # can operate. The daemon also has its own direct asyncpg connections
        # for polling, but shared services use the pool.
        try:
            from lucent.db.pool import init_db as _init_db
            # The daemon uses DAEMON_DATABASE_URL (or falls back to DATABASE_URL).
            # Pass explicitly so init_db uses the same connection the daemon does.
            # run_migrations=False: the daemon connects with the least-privilege
            # lucent_daemon role which intentionally lacks DDL (CREATE on schema
            # public). Migrations are the server's responsibility — attempting them
            # here fails with "permission denied for schema public" and aborts pool
            # init, which would silently strand runtime settings, the model registry,
            # and role parsing on hardcoded defaults.
            _pool = await _init_db(database_url=DATABASE_URL, run_migrations=False)
            try:
                from lucent.settings import load_runtime_settings_from_db

                await load_runtime_settings_from_db(_pool)
                _refresh_config_from_runtime_settings()
                self.roles = self._parse_roles(DAEMON_ROLES_STR)
            except Exception as settings_exc:
                log(f"Failed to load runtime settings from DB: {settings_exc}", "WARN")
            try:
                from lucent.model_registry import load_models_from_db

                await load_models_from_db(_pool)
            except Exception as model_exc:
                log(f"Failed to load model registry from DB: {model_exc}", "WARN")
            log("Lucent DB pool initialized")
        except Exception as e:
            log(f"Failed to initialize Lucent DB pool: {e}", "WARN")

        # Initialize the secret provider so org-scoped model-provider
        # credentials (e.g. the Copilot github_token) can be resolved during
        # LLM sessions. The API server does this at startup; the daemon runs its
        # own LLM sessions, so it must too. Without it SecretRegistry stays empty
        # and the Copilot engine silently falls back to no credentials, failing
        # with "Session was not created with authentication info or custom
        # provider" on container daemons whose Copilot CLI is not logged in.
        try:
            from lucent.db import get_pool as _get_secret_pool

            _secret_pool = await _get_secret_pool()
            await initialize_secret_provider(_secret_pool)
            log("Secret provider initialized")
        except Exception as secret_exc:
            log(f"Failed to initialize secret provider: {secret_exc}", "WARN")

        # Ensure we have a valid API key before anything else
        await ensure_valid_api_key(self.instance_id)
        await RequestAPI.register_instance(
            self.instance_id,
            hostname=platform.node(),
            pid=os.getpid(),
            roles=sorted(self.roles),
            metadata={"model": _resolve_default_model(), "max_sessions": MAX_CONCURRENT_SESSIONS},
        )

        # Start the watchdog thread — detects event loop freezes
        watchdog = threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog")
        watchdog.start()
        log(f"Watchdog started (timeout={WATCHDOG_TIMEOUT}s, check={WATCHDOG_CHECK_INTERVAL}s)")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)

        # Report the engine that the resolved default model actually routes to
        # (per-model routing), not the global default — they can differ.
        _default_model = _resolve_default_model()
        if _LLM_ENGINE_AVAILABLE:
            try:
                engine_name = get_engine_for_model(_default_model).name
            except Exception:
                engine_name = get_engine_name()
        else:
            engine_name = "copilot-direct"
        log(
            f"Daemon ready. Instance: {self.instance_id}, "
            f"Roles: {','.join(sorted(self.roles))}, "
            f"Engine: {engine_name} (multi-engine routing enabled), "
            f"Model: {_default_model}, Max sessions: {MAX_CONCURRENT_SESSIONS}"
        )

        # Populate LLM engine model registry from DB (for Ollama/custom providers)
        if _LLM_ENGINE_AVAILABLE:
            try:
                from lucent.llm.langchain_engine import register_model
                async with httpx.AsyncClient(timeout=15) as _c:
                    _resp = await _c.get(f"{API_BASE}/admin/models", headers=API_HEADERS)
                resp = _resp.json() if _resp.status_code == 200 else None
                if resp and resp.get("items"):
                    registered = 0
                    for m in resp["items"]:
                        engine = m.get("engine")
                        if m.get("provider") not in ("anthropic", "openai", "google") or engine:
                            register_model(
                                m["id"],
                                m["provider"],
                                m.get("api_model_id", ""),
                                engine=engine,
                            )
                            registered += 1
                    if registered:
                        log(f"Registered {registered} model override(s) in LLM engine")
            except Exception as e:
                log(f"Model registry sync skipped: {e}", "WARN")

        # Recover from stale state on startup (workflow-audit/phase-4):
        # release stuck tasks and fix request statuses that drifted while we were down.
        try:
            stale = await RequestAPI.release_stale(STALE_HEARTBEAT_MINUTES)
            reconciled = await RequestAPI.reconcile_statuses()
            if stale or reconciled:
                log(f"Startup recovery: released {stale} stale tasks, reconciled {reconciled} requests")
        except Exception as e:
            log(f"Startup recovery failed (non-fatal): {e}", "WARN")

        # Seed system schedules — built-in schedules that drive autonomous work.
        # On a brand-new instance no real organization may exist yet (the user
        # hasn't signed up). Seeding then defers; the scheduler loop retries it
        # so schedules appear as soon as the org is created — no restart needed.
        self._schedules_seeded = await self._seed_system_schedules()

    async def _seed_system_schedules(self):
        """Ensure built-in system schedules exist for this organization.

        System schedules replace the old autonomic and cognitive loops.
        They can be modified (interval, enabled) but not deleted.
        Idempotent — creates missing schedules and refreshes existing built-in
        schedule definitions from the on-disk source constants while preserving
        runtime state such as enabled/next_run_at.
        Uses direct DB connection (same pattern as key provisioning).
        """
        import asyncpg

        try:
            conn = await asyncpg.connect(DATABASE_URL)
            try:
                bound = await _resolve_daemon_org(conn)
                if not bound:
                    log(
                        "Cannot seed system schedules — no organization to bind to "
                        "(will retry once an organization is created or "
                        "LUCENT_DAEMON_ORG is set)",
                        "WARN",
                    )
                    return False
                org_id, _org_name = bound

                user = await _ensure_daemon_service_user(conn, org_id)
                if not user:
                    log("Cannot seed system schedules — no daemon-service user", "WARN")
                    return False


                user_id = str(user["id"])

                await conn.execute(
                    """UPDATE schedules
                       SET enabled = false,
                           status = 'completed',
                           updated_at = NOW()
                       WHERE title = 'Memory Consolidation'
                         AND organization_id = $1::uuid
                         AND is_system = true""",
                    org_id,
                )

                system_schedules = [
                    {
                        "title": "Cognitive Planning",
                        "description": (
                            "Autonomous planning cycle — perceive state, reason about priorities, "
                            "create requests and tasks to drive work forward. Short-circuits "
                            "with schedule.skipped when cheap state probes find no actionable "
                            "planning signals."
                        ),
                        "agent_type": "lucent",
                        "schedule_type": "interval",
                        "interval_seconds": DAEMON_INTERVAL_MINUTES * 60,
                        "priority": "medium",
                        "prompt": self._build_cognitive_schedule_prompt(),
                    },
                    {
                        "title": "Learning Extraction",
                        "description": (
                            "Process recent work results and feedback into reusable lessons. "
                            "Integrate knowledge into existing memories rather than creating standalone entries. "
                            "Short-circuits with schedule.skipped when there are no unextracted "
                            "recent result or feedback memories."
                        ),
                        "agent_type": "reflection",
                        "schedule_type": "interval",
                        "interval_seconds": LEARNING_MINUTES * 60,
                        "priority": "low",
                        "prompt": LEARNING_EXTRACTION_PROMPT,
                    },
                    {
                        "title": "Experience Compression",
                        "description": (
                            "Daily compression of granular experience memories into concise daily digests. "
                            "Short-circuits with schedule.skipped when there are no eligible "
                            "experience memories before today."
                        ),
                        "agent_type": "memory",
                        "schedule_type": "cron",
                        "cron_expression": "0 4 * * *",
                        "priority": "low",
                        "prompt": EXPERIENCE_COMPRESSION_PROMPT,
                    },
                    {
                        "title": "Shadow Forget Scoring",
                        "description": (
                            "Run Candidate-A graph-centrality pruning as shadow-only scoring. "
                            "Writes sidecar rows and emits comparison metrics only. Short-circuits "
                            "with schedule.skipped when shadow forgetting is disabled or no "
                            "memories need fresh sidecar scores."
                        ),
                        "agent_type": "memory",
                        "schedule_type": "interval",
                        "interval_seconds": SHADOW_FORGET_SCORING_MINUTES * 60,
                        "startup_offset_seconds": SHADOW_FORGET_OFFSET_MINUTES * 60,
                        "priority": "low",
                        "prompt": SHADOW_FORGET_SCORING_PROMPT,
                    },
                ]

                created = 0
                updated = 0
                for sched in system_schedules:
                    workflow_action = {
                        "action_type": "task",
                        "title": sched["title"],
                        "description": sched["prompt"],
                        "agent_type": sched["agent_type"],
                        "priority": sched["priority"],
                        "sequence_order": 0,
                    }
                    trigger_config = {
                        "schedule_type": sched["schedule_type"],
                        "timezone": "UTC",
                    }
                    if sched.get("interval_seconds"):
                        trigger_config["interval_seconds"] = sched["interval_seconds"]
                    if sched.get("cron_expression"):
                        trigger_config["cron_expression"] = sched["cron_expression"]
                    request_template = {
                        "title_prefix": "[Scheduled]",
                        "title": sched["title"],
                        "description": sched["description"],
                        "dependency_policy": "strict",
                    }
                    review_instructions = (
                        "Review the generated request and recorded task outputs "
                        "before approval."
                    )
                    existing = await conn.fetchrow(
                        """SELECT id FROM schedules
                           WHERE title = $1 AND organization_id = $2::uuid AND is_system = true""",
                        sched["title"],
                        org_id,
                    )
                    if existing:
                        # Built-in system schedule definitions are source-controlled.
                        # Refresh definition fields on startup so prompt fixes take
                        # effect, but preserve operational state (enabled, status,
                        # next_run_at, run history).
                        await conn.execute(
                            """UPDATE schedules SET
                                   description = $3,
                                   agent_type = $4,
                                   schedule_type = $5,
                                   interval_seconds = $6,
                                   cron_expression = $7,
                                   priority = $8,
                                   prompt = $9,
                                   trigger_type = 'schedule',
                                   trigger_config = ($10::text)::jsonb,
                                   request_template = ($11::text)::jsonb,
                                   actions = ($12::text)::jsonb,
                                   review_instructions = $13,
                                   updated_at = NOW()
                               WHERE id = $1::uuid AND organization_id = $2::uuid
                                 AND is_system = true""",
                            str(existing["id"]),
                            org_id,
                            sched["description"],
                            sched["agent_type"],
                            sched["schedule_type"],
                            sched.get("interval_seconds"),
                            sched.get("cron_expression"),
                            sched["priority"],
                            sched["prompt"],
                            json.dumps(trigger_config),
                            json.dumps(request_template),
                            json.dumps([workflow_action]),
                            review_instructions,
                        )
                        updated += 1
                        continue

                    now = datetime.now(timezone.utc)
                    interval_seconds = sched.get("interval_seconds")
                    cron_expression = sched.get("cron_expression")
                    startup_offset_seconds = int(sched.get("startup_offset_seconds", 0) or 0)
                    if sched["schedule_type"] == "interval" and interval_seconds:
                        next_run_at = now + timedelta(
                            seconds=interval_seconds + startup_offset_seconds
                        )
                    else:
                        next_run_at = now + timedelta(minutes=5)  # first cron run in 5 min

                    await conn.execute(
                        """INSERT INTO schedules
                           (title, organization_id, description, agent_type, schedule_type,
                            interval_seconds, cron_expression, next_run_at, priority, prompt,
                            created_by, is_system, enabled, trigger_type, trigger_config,
                            request_template, actions, review_instructions)
                           VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10,
                               $11::uuid, true, true, 'schedule', ($12::text)::jsonb,
                               ($13::text)::jsonb, ($14::text)::jsonb, $15)""",
                        sched["title"],
                        org_id,
                        sched["description"],
                        sched["agent_type"],
                        sched["schedule_type"],
                        interval_seconds,
                        cron_expression,
                        next_run_at,
                        sched["priority"],
                        sched["prompt"],
                        user_id,
                        json.dumps(trigger_config),
                        json.dumps(request_template),
                        json.dumps([workflow_action]),
                        review_instructions,
                    )
                    created += 1

                if created:
                    log(f"Seeded {created} system schedule(s)")
                if updated:
                    log(f"Refreshed {updated} existing system schedule definition(s)")
                if not created and not updated:
                    log("System schedules verified (all exist)")
                return True

            finally:
                await conn.close()

        except Exception as e:
            log(f"System schedule seeding failed (non-fatal): {e}", "WARN")
            return False

    async def _list_active_goal_users(self, org_id: str) -> list[dict[str, Any]]:
        """Return users that need a cognitive planning iteration this cycle.

        A user qualifies if they have at least one truly-active goal memory OR
        at least one request awaiting feedback-loop processing (status
        'rejection_processing'). Users with rejected requests but no active
        goals must still be iterated so the planner can consume the rejection
        feedback and close the loop — otherwise rejected requests remain in
        rejection_processing forever.
        """
        import asyncpg

        try:
            conn = await asyncpg.connect(DATABASE_URL)
        except Exception as e:
            log(f"Cognitive fan-out DB connect failed: {e}", "WARN")
            return []

        try:
            rows = await conn.fetch(
                """
                SELECT user_id::text AS user_id,
                       SUM(goals_scanned)::int AS goals_scanned,
                       SUM(rejections_pending)::int AS rejections_pending
                FROM (
                    SELECT user_id,
                           COUNT(*)::int AS goals_scanned,
                           0 AS rejections_pending
                    FROM memories
                    WHERE organization_id = $1::uuid
                      AND type = 'goal'
                      AND lifecycle_stage = 'active'
                      AND COALESCE(metadata->>'status', '') = 'active'
                      AND user_id IS NOT NULL
                    GROUP BY user_id
                    UNION ALL
                    SELECT created_by AS user_id,
                           0 AS goals_scanned,
                           COUNT(*)::int AS rejections_pending
                    FROM requests
                    WHERE organization_id = $1::uuid
                      AND status = 'rejection_processing'
                      AND created_by IS NOT NULL
                    GROUP BY created_by
                ) AS combined
                GROUP BY user_id
                ORDER BY user_id
                """,
                org_id,
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log(f"Cognitive fan-out query failed: {e}", "WARN")
            return []
        finally:
            await conn.close()

    def _build_user_scoped_cognitive_prompt(
        self, targets: list[dict] | None = None
    ) -> str:
        """Build a prompt for per-user scoped cognitive planning sessions.

        ``targets`` is the pre-fetched list from
        ``GET /requests/planning-targets``. Every entry in ``targets`` is a
        milestone the planner MUST progress this cycle — there is no
        choosing between them. The list is already filtered for active
        goals, active milestones with passed start_after dates, and
        no-in-flight-work. The planner just iterates and creates requests.
        """
        if not targets:
            return (
                f"{self._build_cognitive_schedule_prompt()}\n\n"
                "Per-user fan-out execution mode is active for this run.\n"
                "You are operating under a user-scoped key and can only see "
                "one user's memories.\n\n"
                "There is NO PLANNABLE WORK for this user this cycle. The "
                "planning-targets endpoint returned an empty list, which "
                "means: every active goal either has all its milestones "
                "completed, has open in-flight requests already, or has "
                "milestones whose start_after dates are still in the "
                "future.\n\n"
                "Return a concise summary saying 'no work this cycle' and "
                "stop. Do NOT search for goals manually. Do NOT invent "
                "work to fill the cycle.\n"
            )

        # Render the targets as a numbered list the planner consumes
        # verbatim. Each entry includes the structured fields the planner
        # must pass to create_request.
        target_lines: list[str] = []
        for i, t in enumerate(targets, start=1):
            milestone_part = ""
            if t.get("next_milestone_index") is not None:
                milestone_part = (
                    f" — milestone {t['next_milestone_index']}: "
                    f"{t.get('next_milestone_description') or '(no description)'}"
                )
            start_after = t.get("next_milestone_start_after")
            sa_note = f" (start_after={start_after})" if start_after else ""
            target_lines.append(
                f"{i}. goal_id={t['goal_id']}"
                f", goal_milestone_index={t.get('next_milestone_index')}"
                f"\n   goal: {t.get('goal_title') or '(untitled)'}"
                f"{milestone_part}{sa_note}"
                f"\n   suggested_title: {t.get('suggested_title')}"
            )
        targets_block = "\n".join(target_lines)

        return (
            f"{self._build_cognitive_schedule_prompt()}\n\n"
            "Per-user fan-out execution mode is active for this run.\n"
            "You are operating under a user-scoped key and can only see "
            "one user's memories.\n\n"
            "=== ACTIVE MILESTONES TO PROGRESS THIS CYCLE ===\n"
            "The following goal milestones are active, unblocked, and have "
            "no open work yet. You MUST create one tracked request for "
            "each of them. Do NOT pick between them. Do NOT search for "
            "additional goals. The system has already filtered out "
            "everything that is not plannable.\n\n"
            f"{targets_block}\n\n"
            "=== END MILESTONES ===\n\n"
            "For each entry above:\n"
            "1. Call create_request with:\n"
            "     title=<the suggested_title from the entry>\n"
            "     description=<your summary of what to do for the milestone>\n"
            "     source='cognitive'\n"
            "     goal_id=<the entry's goal_id>\n"
            "     goal_milestone_index=<the entry's goal_milestone_index>\n"
            "   The system validates these fields against the goal's "
            "metadata. If you pass a different goal or milestone the call "
            "will be refused.\n"
            "2. Immediately decompose the new request into tasks via "
            "create_task. Every request you create MUST end with at "
            "least one child task.\n"
            "3. If create_request returns status='skipped' with reason "
            "in {'in-flight', 'milestone_not_active'}, that means a "
            "parallel cycle has already started this work. Skip it and "
            "move on — no error.\n\n"
            "Return a concise summary listing every (goal_id, milestone) "
            "you processed and whether each ended in a created request, "
            "a skipped duplicate, or a created task count.\n"
        )

    async def _count_user_cognitive_requests_since(
        self,
        *,
        org_id: str,
        user_id: str,
        since: datetime,
    ) -> int:
        """Count cognitive requests created for a user since a timestamp."""
        import asyncpg

        try:
            conn = await asyncpg.connect(DATABASE_URL)
        except Exception:
            return 0

        try:
            count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM requests
                WHERE organization_id = $1::uuid
                  AND created_by = $2::uuid
                  AND source = 'cognitive'
                  AND created_at >= $3
                """,
                org_id,
                user_id,
                since,
            )
            return int(count or 0)
        except Exception:
            return 0
        finally:
            await conn.close()

    def _build_cognitive_request_description(self, target: dict[str, Any]) -> str:
        """Build a deterministic request description for a planning target."""
        goal_title = (target.get("goal_title") or "").strip() or "Untitled goal"
        milestone_index = target.get("next_milestone_index")
        milestone_description = (
            target.get("next_milestone_description") or ""
        ).strip() or "Advance the next active milestone."
        target_repo = (target.get("target_repo") or "").strip()
        target_paths = target.get("target_paths") or []
        target_block = ""
        if target_repo:
            target_block += f"\nTarget repository: {target_repo}"
        if target_paths:
            target_block += f"\nTarget paths: {target_paths}"

        header = (
            f"Advance milestone {milestone_index} for goal: {goal_title}\n\n"
            f"Milestone scope:\n{milestone_description}\n\n"
        )
        if target_block:
            header += f"{target_block}\n\n"

        return header + (
            "This request was created deterministically by the daemon cognitive "
            "planner from the server-side planning-targets list. The target "
            "was already filtered as active, unblocked, and without open work.\n\n"
            "Expected outcome:\n"
            "- Produce the deliverable described by the milestone.\n"
            "- If a target repository is provided, persist user-facing deliverables "
            "to that repository as concrete file changes and report the paths and "
            "commit/URL. Memory-only or chat-only outputs do not satisfy repo-backed "
            "milestones.\n"
            "- Keep any project-specific boundaries from the parent goal intact.\n"
            "- Record sources, assumptions, and open questions in the output.\n"
        )

    async def _create_cognitive_request_for_target(
        self,
        *,
        org_id: str,
        user_id: str,
        target: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Create one cognitive request for a pre-filtered planning target.

        The planning-targets endpoint is authoritative: it has already selected
        the next active milestone and filtered out in-flight work. Creating the
        request here avoids relying on a model-side mutating MCP call, which can
        be blocked by Copilot permission protocol changes before the Lucent MCP
        server ever sees the request.
        """
        from lucent.db import init_db
        from lucent.db.requests import RequestRepository

        goal_id = str(target.get("goal_id") or "").strip()
        milestone_index = target.get("next_milestone_index")
        if not goal_id or milestone_index is None:
            return (
                "skipped",
                {
                    "status": "skipped",
                    "reason": "missing_target_fields",
                    "detail": "Planning target lacked goal_id or next_milestone_index.",
                },
            )

        title = (
            target.get("suggested_title")
            or f"{target.get('goal_title') or 'Goal'} M{milestone_index}"
        ).strip()
        description = self._build_cognitive_request_description(target)

        pool = await init_db()
        repo = RequestRepository(pool)
        req = await repo.create_request(
            title=title,
            org_id=org_id,
            description=description,
            source="cognitive",
            priority="medium",
            created_by=user_id,
            force_pending_approval=True,
            goal_id=goal_id,
            goal_milestone_index=int(milestone_index),
            target_repo=(target.get("target_repo") or None),
            target_paths=(target.get("target_paths") or None),
        )

        if req.get("status") == "skipped":
            return "skipped", req

        try:
            async with pool.acquire() as conn:
                await conn.execute("SELECT pg_notify('request_ready', $1)", str(req["id"]))
        except Exception as notify_err:
            log(
                "cognitive direct request notify failed "
                f"for {str(req.get('id', ''))[:8]}: {notify_err}",
                "WARN",
            )

        return "created", req

    async def _process_user_rejections(
        self,
        *,
        user_id: str,
        org_id: str,
        scoped_key: str,
        system_message: str,
    ) -> int:
        """Run a scoped LLM session to process a user's rejected requests.

        Rejected requests land in ``rejection_processing`` and need the
        feedback loop closed: read the rejection reason, update the linked
        goal memory, then move the request to ``cancelled`` via
        ``mark_rejection_processed``. This requires LLM reasoning, so we
        fan it out under the user's scoped key. Returns the number of
        rejected requests handed to the session.
        """
        import asyncpg

        try:
            conn = await asyncpg.connect(DATABASE_URL)
        except Exception as e:
            log(f"Rejection processing DB connect failed: {e}", "WARN")
            return 0
        try:
            rows = await conn.fetch(
                """
                SELECT id::text AS id, title, approval_comment
                FROM requests
                WHERE organization_id = $1::uuid
                  AND created_by = $2::uuid
                  AND status = 'rejection_processing'
                ORDER BY created_at
                """,
                org_id,
                user_id,
            )
        except Exception as e:
            log(f"Rejection processing query failed: {e}", "WARN")
            return 0
        finally:
            await conn.close()

        if not rows:
            return 0

        lines = []
        for r in rows:
            comment = r.get("approval_comment") or "No reason given"
            lines.append(
                f"- **{r.get('title') or '(untitled)'}** (id: {r['id']})\n"
                f"  Rejection reason: {comment}"
            )
        rejection_block = "\n".join(lines)

        prompt = (
            "You are closing the feedback loop on requests the user "
            "rejected. The following requests are in 'rejection_processing' "
            "and you MUST process each one:\n\n"
            f"{rejection_block}\n\n"
            "For EACH request above:\n"
            "1. Read the rejection reason carefully.\n"
            "2. Call get_request_details(request_id) to find the linked "
            "goal memories.\n"
            "3. Update each linked goal memory based on the feedback:\n"
            "   - If the goal itself is obsolete/already done → set "
            "metadata.status to 'abandoned' with the reason.\n"
            "   - If only the approach was wrong → add the rejection "
            "feedback to the goal's content/progress notes.\n"
            "4. Call mark_rejection_processed(request_id, note=...) to move "
            "the request to 'cancelled'.\n\n"
            "Do NOT create new requests for these goals. Return a brief "
            "summary of what you changed for each request."
        )

        scoped_mcp = dict(MCP_CONFIG)
        scoped_mcp["memory-server"] = _build_scoped_memory_server_config(
            scoped_key=scoped_key,
            memory_scope=MEMORY_SCOPE_USER,
            org_id=str(org_id),
            memory_scope_user_id=str(user_id),
            tools=["*"],
        )

        model, _reason = _select_model_for_task(agent_type="planning")
        await self.run_session(
            f"rejections-{user_id[:8]}",
            system_message,
            prompt,
            model=model,
            mcp_config_override=scoped_mcp,
        )
        return len(rows)

    async def _run_cognitive_planning_fanout(
        self,
        *,
        task_id: str,
        org_id: str,
        system_message: str,
        mcp_config_base: dict[str, Any],
    ) -> str:
        """Run per-user cognitive planning fan-out with scoped keys."""
        users = await self._list_active_goal_users(org_id)
        if not users:
            return "Cognitive fan-out: no users with active goals."

        summaries: list[str] = []

        for user_row in users:
            user_id = str(user_row.get("user_id", "")).strip()
            goals_scanned = int(user_row.get("goals_scanned") or 0)
            if not user_id:
                continue

            started = datetime.now(timezone.utc)
            start_ts = time.perf_counter()
            errors: list[str] = []
            requests_created = 0
            targets_count = 0

            try:
                scoped_key = await _mint_scoped_api_key(
                    memory_scope="user",
                    memory_scope_user_id=user_id,
                    org_id=org_id,
                    ttl_minutes=60,
                )
                if not scoped_key:
                    raise RuntimeError("scoped key minting failed")

                # Process any rejected requests for this user FIRST. Rejection
                # handling needs LLM reasoning (interpret the feedback, update
                # the linked goal memory, close the loop) so it runs a scoped
                # session. This must happen even when there are no planning
                # targets — otherwise rejected requests sit in
                # rejection_processing forever.
                rejections_pending = int(user_row.get("rejections_pending") or 0)
                if rejections_pending > 0:
                    try:
                        processed = await self._process_user_rejections(
                            user_id=user_id,
                            org_id=org_id,
                            scoped_key=scoped_key,
                            system_message=system_message,
                        )
                        summaries.append(
                            f"user={user_id[:8]} rejections={rejections_pending} "
                            f"processed={processed}"
                        )
                    except Exception as rej_err:
                        errors.append(f"rejection processing: {rej_err}")
                        log(
                            f"Rejection processing failed for user "
                            f"{user_id[:8]}: {rej_err}",
                            "WARN",
                        )

                # Fetch the planning targets server-side BEFORE invoking
                # the planner. The endpoint scopes results to this user
                # automatically because the key is user-scoped. If there
                # are no targets, we skip the LLM call entirely — saves
                # tokens and time.
                targets = await RequestAPI.list_planning_targets(api_key=scoped_key)
                if targets is None:
                    targets = []
                targets_count = len(targets)

                if not targets:
                    log(
                        f"cognitive fan-out: user {user_id[:8]} has no "
                        "plannable targets — skipping LLM call",
                        "INFO",
                    )
                    if rejections_pending == 0:
                        summaries.append(
                            f"user={user_id[:8]} goals={goals_scanned} "
                            f"targets=0 requests=0 (no work)"
                        )
                    continue

                # Create requests directly from authoritative planning targets.
                # The prior implementation asked the LLM to call the mutating
                # MCP create_request/create_task tools. With newer Copilot CLI
                # builds those mutating MCP calls can fail at the permission
                # layer with "unexpected user permission response" before the
                # Lucent MCP server sees the call, causing the planner to loop
                # forever with targets_count>0 and requests_created=0.
                for target in targets:
                    try:
                        outcome, req = await self._create_cognitive_request_for_target(
                            org_id=org_id,
                            user_id=user_id,
                            target=target,
                        )
                        log(
                            "cognitive_planning_target_request "
                            + json.dumps(
                                {
                                    "user_id": user_id,
                                    "goal_id": target.get("goal_id"),
                                    "goal_milestone_index": target.get(
                                        "next_milestone_index"
                                    ),
                                    "outcome": outcome,
                                    "request_id": str(req.get("id", "")) or None,
                                    "status": req.get("status"),
                                    "reason": req.get("reason"),
                                },
                                sort_keys=True,
                                default=str,
                            )
                        )
                    except Exception as target_err:
                        errors.append(str(target_err))
                        log(
                            "Cognitive planning target create failed "
                            f"for user {user_id[:8]} goal "
                            f"{str(target.get('goal_id', ''))[:8]}: {target_err}",
                            "WARN",
                        )
                requests_created = await self._count_user_cognitive_requests_since(
                    org_id=org_id,
                    user_id=user_id,
                    since=started,
                )
            except Exception as e:
                errors.append(str(e))
                log(f"Cognitive planning fan-out failed for user {user_id[:8]}: {e}", "WARN")
            finally:
                duration_ms = int((time.perf_counter() - start_ts) * 1000)
                log(
                    "cognitive_planning_user_iteration "
                    + json.dumps(
                        {
                            "user_id": user_id,
                            "goals_scanned": goals_scanned,
                            "targets_count": targets_count,
                            "requests_created": requests_created,
                            "errors": errors,
                            "duration_ms": duration_ms,
                        },
                        sort_keys=True,
                    )
                )
                if targets_count > 0 or errors:
                    summaries.append(
                        f"user={user_id[:8]} goals={goals_scanned} "
                        f"targets={targets_count} "
                        f"requests={requests_created} errors={len(errors)}"
                    )

        return "Cognitive fan-out complete: " + "; ".join(summaries)

    async def _list_undecomposed_pending_approval_requests(
        self,
        *,
        org_id: str,
        min_age_seconds: int,
        request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return non-terminal requests with zero tasks older than min_age_seconds.

        Used by the decomposition backfill so that any request reaching the
        queue has a visible task breakdown — whether it's awaiting human
        approval (`pending_approval`), auto-approved on creation
        (`auto_approved`, e.g. user-source requests), or manually approved
        (`approved`). All three states share the same failure mode: a
        request with zero tasks is incomplete work.
        """
        import asyncpg

        try:
            conn = await asyncpg.connect(DATABASE_URL)
        except Exception as e:
            log(f"Decomp backfill DB connect failed: {e}", "WARN")
            return []

        try:
            rows = await conn.fetch(
                """
                SELECT r.id::text AS request_id,
                       r.title,
                       r.description,
                       r.created_by::text AS created_by,
                       r.target_repo,
                       r.target_paths,
                       r.priority,
                       r.source,
                       r.approval_status,
                       r.goal_memory_id::text AS goal_memory_id,
                       r.goal_milestone_index,
                       CASE
                           WHEN r.goal_memory_id IS NOT NULL
                                AND r.goal_milestone_index IS NOT NULL
                           THEN gm.metadata->'milestones'->(r.goal_milestone_index - 1)->>'description'
                           ELSE NULL
                       END AS goal_milestone_description
                FROM requests r
                LEFT JOIN memories gm ON gm.id = r.goal_memory_id
                WHERE r.organization_id = $1::uuid
                  AND ($3::uuid IS NULL OR r.id = $3::uuid)
                  AND r.approval_status IN (
                      'pending_approval', 'auto_approved', 'approved'
                  )
                  AND r.status IN ('pending', 'in_progress')
                  AND r.created_at < NOW() - ($2::int * INTERVAL '1 second')
                  AND r.created_by IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM tasks t WHERE t.request_id = r.id
                  )
                ORDER BY r.created_at ASC
                LIMIT 5
                """,
                org_id,
                min_age_seconds,
                request_id,
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log(f"Decomp backfill query failed: {e}", "WARN")
            return []
        finally:
            await conn.close()

    async def _create_fallback_decomposition_tasks(
        self,
        request: dict[str, Any],
        session_result: str | None,
        tool_events: list[dict],
    ) -> int:
        """Create deterministic tasks when model-driven decomposition produces none."""
        request_id = request.get("request_id", "")
        if not request_id:
            return 0
        specs = self._build_fallback_decomposition_tasks(request)
        created = 0
        for spec in specs:
            task = await RequestAPI.create_task(
                request_id,
                spec["title"],
                agent_type=spec["agent_type"],
                description=spec["description"],
                priority=request.get("priority") or "medium",
                sequence_order=spec["sequence_order"],
            )
            if not task and spec["agent_type"] != "planning":
                task = await RequestAPI.create_task(
                    request_id,
                    spec["title"],
                    agent_type="planning",
                    description=spec["description"],
                    priority=request.get("priority") or "medium",
                    sequence_order=spec["sequence_order"],
                )
            if task:
                created += 1

        excerpt = _redact_secrets((session_result or "")[:2000])
        log(
            "Decomp backfill fallback "
            + json.dumps(
                {
                    "request_id": request_id,
                    "fallback_tasks_created": created,
                    "planned_fallback_tasks": len(specs),
                    "tool_calls": [e.get("tool") for e in tool_events],
                    "response_excerpt": excerpt,
                },
                sort_keys=True,
            ),
            "WARN" if created == 0 else "INFO",
        )
        return created

    def request_decomposition_backfill_has_work(
        self,
        candidates: list[dict[str, Any]],
    ) -> bool:
        """True when the periodic decomposition backfill has queued candidates."""
        return bool(candidates)

    async def _backfill_pending_decomposition(
        self,
        *,
        org_id: str,
        min_age_seconds: int = 300,
        request_id: str | None = None,
    ) -> int:
        """Decompose any non-terminal request older than min_age_seconds with no tasks.

        Returns the number of requests for which decomposition was attempted.
        Each attempt runs as a small focused session under a key scoped to the
        request's owner so the resulting tasks inherit ownership cleanly.

        Concurrency: safe across multiple daemon instances. Each candidate is
        guarded by a PG session-level advisory lock keyed off the request's
        UUID. If another daemon holds the lock, this one skips the request
        and moves on. The in-memory set is just a per-instance fast-path so
        we don't round-trip to the DB for requests we already know we are
        actively decomposing locally.

        Backoff: a request that fails to gain any tasks (planner refused,
        MCP error, scoped-key failure, session crash) is suppressed locally
        for an exponentially growing window so a single bad request can't
        burn through sessions every scheduler tick. Backoff state is
        per-instance — a daemon restart resets it, which is the desired
        recovery path for transient failures.

        Why min_age_seconds defaults to 300: a normal cognitive cycle takes
        30–90 seconds, but slow planner sessions can run longer. Waiting 5
        minutes before backfilling avoids racing a planner that's about to
        call create_task on a request it just created.
        """
        import asyncpg

        if self.draining:
            return 0
        if len(self.active_sessions) >= MAX_CONCURRENT_SESSIONS:
            return 0
        if not MCP_CONFIG.get("memory-server"):
            return 0

        # Per-instance fast-path guard. The DB advisory lock is the source
        # of truth for cross-instance correctness; this just avoids a wasted
        # DB round-trip when our own scheduler tick re-races itself.
        if not hasattr(self, "_decomposing_request_ids"):
            self._decomposing_request_ids = set()
        # Per-instance backoff: request_id -> (next_retry_monotonic, attempts).
        if not hasattr(self, "_decomposition_backoff"):
            self._decomposition_backoff = {}

        candidates = await self._list_undecomposed_pending_approval_requests(
            org_id=org_id,
            min_age_seconds=min_age_seconds,
            request_id=request_id,
        )
        now_mono = time.monotonic()
        filtered: list[dict[str, Any]] = []
        for req in candidates:
            rid = req.get("request_id")
            if not rid:
                continue
            if rid in self._decomposing_request_ids:
                continue
            backoff_until, _attempts = self._decomposition_backoff.get(rid, (0.0, 0))
            if backoff_until > now_mono:
                continue
            filtered.append(req)
        candidates = filtered
        if not self.request_decomposition_backfill_has_work(candidates):
            log(
                json.dumps(
                    {
                        "event_type": "schedule.skipped",
                        "schedule_name": "Request Decomposition Backfill",
                        "reason": "no_undecomposed_requests",
                        "candidate_count": 0,
                    },
                    sort_keys=True,
                ),
                "INFO",
            )
            return 0

        attempted = 0
        for req in candidates:
            request_id = req.get("request_id", "")
            owner_user_id = req.get("created_by", "")
            if not request_id or not owner_user_id:
                continue

            # Acquire the cross-daemon advisory lock on a dedicated connection.
            # The lock is bound to the connection's session, so we MUST keep the
            # connection open for the duration of the decomposition session and
            # release/close in finally. pg_try_advisory_lock returns immediately
            # — if another daemon has it, we skip without waiting.
            lock_conn: asyncpg.Connection | None = None
            try:
                try:
                    lock_conn = await asyncpg.connect(DATABASE_URL)
                except Exception as e:
                    log(f"Decomp lock connect failed for {request_id[:8]}: {e}", "WARN")
                    continue

                acquired = await lock_conn.fetchval(
                    "SELECT pg_try_advisory_lock($1, hashtext($2)::int)",
                    DECOMPOSITION_LOCK_NAMESPACE,
                    request_id,
                )
                if not acquired:
                    log(
                        f"Decomp backfill: request {request_id[:8]} already "
                        "being decomposed by another daemon — skipping",
                    )
                    await lock_conn.close()
                    lock_conn = None
                    continue

                # Re-check candidacy under the lock to avoid the TOCTOU window
                # where another daemon finished decomposing this very request
                # between our list query and our lock acquisition.
                still_undecomposed = await lock_conn.fetchval(
                    """
                    SELECT 1
                    FROM requests r
                    WHERE r.id = $1::uuid
                      AND r.approval_status IN (
                          'pending_approval', 'auto_approved', 'approved'
                      )
                      AND r.status IN ('pending', 'in_progress')
                      AND NOT EXISTS (
                          SELECT 1 FROM tasks t WHERE t.request_id = r.id
                      )
                    """,
                    request_id,
                )
                if not still_undecomposed:
                    await lock_conn.execute(
                        "SELECT pg_advisory_unlock($1, hashtext($2)::int)",
                        DECOMPOSITION_LOCK_NAMESPACE,
                        request_id,
                    )
                    await lock_conn.close()
                    lock_conn = None
                    # Clear any stale backoff — the request is now decomposed
                    # or no longer eligible.
                    self._decomposition_backoff.pop(request_id, None)
                    continue

                self._decomposing_request_ids.add(request_id)
                start_ts = time.perf_counter()
                tasks_created = 0
                terminal_after_session = False
                errors: list[str] = []
                try:
                    planning_agent = await load_accessible_agent(
                        org_id=org_id,
                        requester_user_id=owner_user_id,
                        agent_type="planning",
                    )
                    planning_skills = []
                    if planning_agent:
                        planning_skills = await load_accessible_skills_for_agent(
                            org_id=org_id,
                            requester_user_id=owner_user_id,
                            agent_id=str(planning_agent["id"]),
                        )
                    try:
                        active_user_context = await _load_request_owner_context(
                            str(owner_user_id), str(org_id)
                        )
                        system_message = await build_subagent_prompt(
                            "planning",
                            "Break a pending_approval request into a visible task list.",
                            active_user_context=active_user_context,
                            resolved_agent=planning_agent,
                            resolved_skills=planning_skills,
                        )
                    except AgentNotFoundError:
                        errors.append(
                            "request owner has no accessible active planning agent"
                        )
                        continue

                    scoped_key = await _mint_scoped_api_key(
                        memory_scope="user",
                        memory_scope_user_id=owner_user_id,
                        org_id=org_id,
                        ttl_minutes=30,
                    )
                    if not scoped_key:
                        raise RuntimeError("scoped key minting failed")

                    scoped_mcp = dict(MCP_CONFIG)
                    scoped_mcp["memory-server"] = _build_scoped_memory_server_config(
                        scoped_key=scoped_key,
                        memory_scope=MEMORY_SCOPE_USER,
                        org_id=str(org_id),
                        memory_scope_user_id=str(owner_user_id),
                        tools=["*"],
                        extra_headers={"X-Lucent-Request-Id": str(request_id)},
                    )

                    prompt = self._build_decomposition_prompt(req)
                    decomposition_model, decomposition_model_reason = _select_model_for_task(
                        agent_type="planning",
                        title=req.get("title"),
                        description=req.get("description"),
                    )
                    log(
                        f"Decomp backfill: request {request_id[:8]} model selection: "
                        f"{decomposition_model} ({decomposition_model_reason})",
                        "INFO",
                    )
                    session_name = f"decompose-{request_id[:8]}-{owner_user_id[:8]}"

                    session_result = await self.run_session(
                        session_name,
                        system_message,
                        prompt,
                        model=decomposition_model,
                        mcp_config_override=scoped_mcp,
                    )
                    if not session_result:
                        errors.append("session produced no output")

                    tasks_created = await self._count_tasks_for_request(request_id)
                    tool_events = self._session_tool_trackers.pop(session_name, [])
                    if session_result and tasks_created == 0:
                        fallback_created = await self._create_fallback_decomposition_tasks(
                            req,
                            session_result,
                            tool_events,
                        )
                        if fallback_created > 0:
                            tasks_created = await self._count_tasks_for_request(request_id)
                        else:
                            errors.append("session produced output but no tasks were created")

                    # Defensive post-session check: if the user cancelled or
                    # the request otherwise reached terminal state during the
                    # session, the planner may have created orphan tasks.
                    # The dispatch+claim queries now refuse to run tasks
                    # under terminal parents, so the orphans won't execute,
                    # but log loudly so we notice the pattern.
                    terminal_status = await self._get_request_terminal_status(
                        lock_conn, request_id
                    )
                    if terminal_status:
                        terminal_after_session = True
                        errors.append(
                            f"request reached terminal status "
                            f"'{terminal_status}' during decomposition; "
                            f"any tasks created will not be dispatched"
                        )
                except Exception as e:
                    errors.append(str(e))
                    log(
                        f"Decomp backfill failed for request {request_id[:8]}: {e}",
                        "WARN",
                    )
                finally:
                    self._decomposing_request_ids.discard(request_id)
                    attempted += 1
                    duration_ms = int((time.perf_counter() - start_ts) * 1000)
                    log(
                        "request_decomposed_for_review "
                        + json.dumps(
                            {
                                "request_id": request_id,
                                "owner_user_id": owner_user_id,
                                "tasks_created": tasks_created,
                                "terminal_during_session": terminal_after_session,
                                "errors": errors,
                                "duration_ms": duration_ms,
                            },
                            sort_keys=True,
                        )
                    )
                    # Update backoff: success clears it, failure escalates.
                    if tasks_created > 0 and not terminal_after_session:
                        self._decomposition_backoff.pop(request_id, None)
                    else:
                        prev_until, prev_attempts = self._decomposition_backoff.get(
                            request_id, (0.0, 0)
                        )
                        attempts = prev_attempts + 1
                        # 5min, 15min, 1h, then 1h thereafter (capped). After
                        # the 4th failed attempt we essentially give up until
                        # the daemon restarts.
                        if attempts >= 4:
                            wait_seconds = 24 * 3600
                            log(
                                f"Decomp backfill: request {request_id[:8]} "
                                f"failed {attempts} times — suppressing for 24h "
                                "(restart the daemon to reset)",
                                "WARN",
                            )
                        elif attempts >= 3:
                            wait_seconds = 3600
                        elif attempts >= 2:
                            wait_seconds = 15 * 60
                        else:
                            wait_seconds = 5 * 60
                        self._decomposition_backoff[request_id] = (
                            time.monotonic() + wait_seconds,
                            attempts,
                        )
            finally:
                if lock_conn is not None:
                    try:
                        await lock_conn.execute(
                            "SELECT pg_advisory_unlock($1, hashtext($2)::int)",
                            DECOMPOSITION_LOCK_NAMESPACE,
                            request_id,
                        )
                    except Exception:
                        pass
                    try:
                        await lock_conn.close()
                    except Exception:
                        pass

        return attempted

    async def _get_request_terminal_status(
        self, conn: "asyncpg.Connection", request_id: str
    ) -> str | None:
        """Return the request's status if it has reached a terminal state, else None."""
        try:
            row = await conn.fetchrow(
                """
                SELECT status FROM requests
                WHERE id = $1::uuid
                  AND status IN ('cancelled', 'completed', 'failed')
                """,
                request_id,
            )
        except Exception:
            return None
        if not row:
            return None
        return str(row["status"])

    async def _count_tasks_for_request(self, request_id: str) -> int:
        """Count tasks attached to a request — used to verify decomposition outcome."""
        import asyncpg

        try:
            conn = await asyncpg.connect(DATABASE_URL)
        except Exception:
            return 0
        try:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE request_id = $1::uuid",
                request_id,
            )
            return int(count or 0)
        except Exception:
            return 0
        finally:
            await conn.close()

    def _handle_shutdown(self):
        """Handle shutdown signal."""
        self.running = False
        log("Shutdown signal received")
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    async def stop(self):
        """Stop the daemon, revoke API key, and clean up."""
        log(f"Stopping daemon (instance: {self.instance_id})...")
        self.running = False

        try:
            await RequestAPI.mark_instance_stopped(self.instance_id)
        except Exception:
            pass

        # Flush and shut down OTEL telemetry
        if _TELEMETRY_AVAILABLE:
            shutdown_telemetry()

        # Close PG LISTEN connection
        if self._listen_conn and not self._listen_conn.is_closed():
            try:
                await self._listen_conn.close()
            except Exception:
                log("Failed to close PG LISTEN connection during stop", "DEBUG")
            self._listen_conn = None

        # Revoke the daemon's API key — clean up after ourselves
        await _revoke_current_key()

        for session in self.active_sessions:
            try:
                await session.destroy()
            except Exception:
                log("Failed to destroy active session during stop", "DEBUG")
        log("Daemon stopped")
        sys.exit(0)

    # --- Session Management ---

    async def run_session(
        self,
        name: str,
        system_message: str,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        mcp_config_override: dict | None = None,
        enable_config_discovery: bool = False,
        hooks: list[dict] | None = None,
        audit_context: dict | None = None,
    ) -> str | None:
        """Run a single Copilot session with the given system message and prompt.

        Each session gets its own CopilotClient for full isolation.
        Args:
            model: Override the default model. If None, uses MODEL config.
        Returns the assistant's final message text, or None on error.
        """
        if self.draining:
            log(f"Skipping '{name}' — daemon is draining for restart", "WARN")
            return None
        if len(self.active_sessions) >= MAX_CONCURRENT_SESSIONS:
            log(f"Skipping '{name}' — at session limit ({MAX_CONCURRENT_SESSIONS})", "WARN")
            return None

        # Ensure the daemon's bound organization is available to the engine so
        # it can resolve org-scoped model-provider credentials (e.g. the Copilot
        # github_token). Container daemons whose Copilot CLI is not interactively
        # logged in rely on this stored token; without an organization_id the
        # engine cannot load it and the session fails with "Session was not
        # created with authentication info or custom provider". Only fill it in
        # when a caller (e.g. a per-task session) has not already supplied org
        # context, so we never override an explicit scope.
        if audit_context is None or not audit_context.get("organization_id"):
            bound_org_id = await self._get_daemon_org_id()
            if bound_org_id:
                audit_context = {**(audit_context or {}), "organization_id": bound_org_id}

        selected_model = _resolve_default_model(model)
        effort_label = f", reasoning_effort: {reasoning_effort}" if reasoning_effort else ""
        log(f"Starting session: {name} (model: {selected_model}{effort_label})")
        start_time = time.time()
        status = "success"

        # Start OTEL span for the session
        span_ctx = (
            self._tracer.start_as_current_span(
                "daemon.session",
                attributes={
                    "daemon.session.name": name,
                    "daemon.session.model": selected_model,
                    "daemon.instance_id": self.instance_id,
                },
            )
            if self._tracer
            else None
        )
        span = span_ctx.__enter__() if span_ctx else None

        if self._tracer:
            self._sessions_active.add(1)

        retried_after_auth_failure = False
        try:
            while True:
                try:
                    inner_kwargs = {
                        "model": selected_model,
                        "mcp_config_override": mcp_config_override,
                        "enable_config_discovery": enable_config_discovery,
                    }
                    if hooks is not None:
                        inner_kwargs["hooks"] = hooks
                    if audit_context is not None:
                        inner_kwargs["audit_context"] = audit_context
                    if reasoning_effort:
                        inner_kwargs["reasoning_effort"] = reasoning_effort
                    result = await asyncio.wait_for(
                        self._run_session_inner(
                            name,
                            system_message,
                            prompt,
                            **inner_kwargs,
                        ),
                        timeout=SESSION_TOTAL_TIMEOUT,
                    )
                    if span:
                        span.set_attribute("daemon.session.output_length", len(result) if result else 0)
                    return result
                except AuthFailureDetectedError as e:
                    if retried_after_auth_failure:
                        status = "error"
                        log(
                            f"Session '{name}' encountered repeated MCP auth failures after retry: {e}",
                            "ERROR",
                        )
                        if span:
                            span.set_attribute("daemon.session.error", "auth_recovery_failed")
                        return None

                    log(
                        f"Session '{name}' detected MCP auth failure; attempting key recovery and single retry",
                        "WARN",
                    )
                    retried_after_auth_failure = True
                    if mcp_config_override:
                        refreshed = await _refresh_scoped_memory_server_config(
                            mcp_config_override
                        )
                        if not refreshed:
                            status = "error"
                            log(
                                f"Session '{name}' could not refresh scoped memory-server credentials",
                                "ERROR",
                            )
                            if span:
                                span.set_attribute(
                                    "daemon.session.error", "scoped_auth_recovery_failed"
                                )
                            return None
                        mcp_config_override = refreshed
                    else:
                        recovered = await _handle_auth_failure(self.instance_id)
                        if not recovered:
                            status = "error"
                            log(f"Session '{name}' could not recover MCP credentials", "ERROR")
                            if span:
                                span.set_attribute("daemon.session.error", "auth_recovery_failed")
                            return None
                    log(f"Session '{name}' recovered MCP credentials; retrying once", "INFO")
                    continue
                except asyncio.TimeoutError:
                    status = "timeout"
                    log(
                        f"Session '{name}' HARD TIMEOUT after {SESSION_TOTAL_TIMEOUT}s — "
                        "session lifecycle hung (likely during client.start or create_session)",
                        "ERROR",
                    )
                    if span:
                        span.set_attribute("daemon.session.error", "timeout")
                    return None
                except ModelNotAvailableError:
                    status = "error"
                    raise  # Let the caller handle model-not-available specifically
                except Exception as e:
                    status = "error"
                    log(f"Session '{name}' failed: {e}", "ERROR")
                    if span:
                        span.set_attribute("daemon.session.error", str(e)[:200])
                    return None
        finally:
            duration = time.time() - start_time
            if self._tracer:
                self._sessions_active.add(-1)
                self._sessions_total.add(1, attributes={"status": status, "model": selected_model})
                self._session_duration.record(duration, attributes={"session_name": name, "status": status})
            if span_ctx:
                span_ctx.__exit__(None, None, None)

    async def _run_session_inner(
        self,
        name: str,
        system_message: str,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        mcp_config_override: dict | None = None,
        enable_config_discovery: bool = False,
        hooks: list[dict] | None = None,
        audit_context: dict | None = None,
    ) -> str | None:
        """Inner session runner — uses the LLM engine abstraction.

        Falls back to direct CopilotClient if the engine module isn't available
        (e.g. when running the daemon standalone outside the package).
        """
        selected_model = _resolve_default_model(model)

        if _LLM_ENGINE_AVAILABLE:
            # Keep memory-server always available; all other MCP servers are requester-scoped.
            effective_mcp = mcp_config_override or MCP_CONFIG
            return await self._run_via_engine(
                name,
                system_message,
                prompt,
                selected_model,
                reasoning_effort=reasoning_effort,
                mcp_config_override=effective_mcp,
                enable_config_discovery=enable_config_discovery,
                hooks=hooks,
                audit_context=audit_context,
            )
        elif _COPILOT_SDK_AVAILABLE:
            return await self._run_via_copilot_direct(
                name,
                system_message,
                prompt,
                selected_model,
                reasoning_effort=reasoning_effort,
                mcp_config_override=mcp_config_override,
                enable_config_discovery=enable_config_discovery,
            )
        else:
            log("No LLM engine available — install lucent or github-copilot-sdk", "ERROR")
            return None

    @staticmethod
    def _is_mcp_auth_failure_message(message: str | None) -> bool:
        """Classify MCP tool output/error text as auth failure when possible."""
        if not message:
            return False
        text = message.lower()
        return (
            "unauthorized: invalid or expired credentials" in text
            or ("unauthorized" in text and "invalid or expired credentials" in text)
            or ('"code": -32001' in text and "unauthorized" in text)
            or "status_code=401" in text
            or '"status_code": 401' in text
            or " 401 " in text
            or "http 401" in text
        )

    async def _run_via_engine(
        self,
        name: str,
        system_message: str,
        prompt: str,
        model: str,
        reasoning_effort: str | None = None,
        mcp_config_override: dict | None = None,
        enable_config_discovery: bool = False,
        hooks: list[dict] | None = None,
        audit_context: dict | None = None,
    ) -> str | None:
        """Run session using the LLM engine abstraction layer."""
        engine = get_engine_for_model(model) if _LLM_ENGINE_AVAILABLE else get_engine()
        session_id = f"engine-session-{name}"
        self.active_sessions.append(session_id)

        # Initialize MCP memory-tool tracker for this session
        tracker: list[dict] = []
        self._session_mcp_trackers[name] = tracker
        tool_tracker: list[dict] = []
        self._session_tool_trackers[name] = tool_tracker
        audit_tasks: list[asyncio.Task] = []
        tool_call_inputs: dict[str, dict | str] = {}

        try:

            async def audit_stream_tool_result(event: SessionEvent) -> None:
                if getattr(engine, "name", "") == "langchain" or not event.tool_name:
                    return
                try:
                    from lucent.db import init_db
                    from lucent.db.tool_audit import ToolAuditRepository, classify_tool_result

                    status, failure_class, error_message = classify_tool_result(
                        event.tool_output
                    )
                    pool = await init_db()
                    repo = ToolAuditRepository(pool)
                    await repo.log_tool_call(
                        tool_name=event.tool_name,
                        status=status,
                        source="daemon.session_event",
                        input_payload=tool_call_inputs.get(event.tool_name, {}),
                        output_payload=event.tool_output,
                        failure_class=failure_class,
                        error_message=error_message,
                        context={**(audit_context or {}), "engine": getattr(engine, "name", "")},
                    )
                except Exception:
                    log("Failed to audit daemon tool event", "DEBUG")

            def on_event(event: SessionEvent) -> None:
                etype = event.type.value
                if event.type == SessionEventType.MESSAGE:
                    if event.content:
                        log(f"  [{name}] message: {event.content[:200]}...", "STREAM")
                elif event.type == SessionEventType.ERROR:
                    log(f"  [{name}] error: {event.content}", "ERROR")
                    if self._is_mcp_auth_failure_message(event.content):
                        raise AuthFailureDetectedError(
                            f"MCP auth error during session '{name}': {event.content}"
                        )
                elif event.type == SessionEventType.TOOL_CALL:
                    log(f"  [{name}] event: tool.call tool={event.tool_name}", "STREAM")
                    normalized_tool = _normalize_tool_name(event.tool_name)
                    tool_tracker.append({
                        "tool": normalized_tool or event.tool_name,
                        "raw_tool": event.tool_name,
                        "input": event.tool_input or event.content or {},
                        "timestamp": time.time(),
                    })
                    if event.tool_name:
                        tool_call_inputs[event.tool_name] = event.tool_input or event.content or {}
                    # Track memory-server tool calls for observability
                    if _is_memory_server_tool(event.tool_name):
                        params_summary = _summarize_memory_tool_params(
                            event.tool_name, event.tool_input or {}
                        )
                        tracker.append({
                            "tool": normalized_tool or event.tool_name,
                            "raw_tool": event.tool_name,
                            "params": params_summary,
                            "timestamp": time.time(),
                        })
                        log(
                            f"  [{name}] mcp.memory_tool: {event.tool_name} "
                            f"params={{{params_summary}}}",
                        )
                elif event.type == SessionEventType.TOOL_RESULT:
                    output = _redact_secrets(event.tool_output[:50]) if event.tool_output else ""
                    log(
                        f"  [{name}] event: tool.result tool={event.tool_name} output={output}",
                        "STREAM",
                    )
                    audit_tasks.append(asyncio.create_task(audit_stream_tool_result(event)))
                    if self._is_mcp_auth_failure_message(event.tool_output):
                        raise AuthFailureDetectedError(
                            f"MCP auth failure on tool '{event.tool_name}' in session '{name}'"
                        )
                elif event.type != SessionEventType.MESSAGE_DELTA:
                    log(f"  [{name}] event: {etype}", "STREAM")

            result = await engine.run_session_streaming(
                model=model,
                system_message=system_message,
                prompt=prompt,
                mcp_config=mcp_config_override or MCP_CONFIG,
                on_event=on_event,
                timeout=SESSION_TOTAL_TIMEOUT,
                idle_timeout=SESSION_IDLE_TIMEOUT,
                reasoning_effort=reasoning_effort,
                hooks=hooks,
                audit_context={
                    **(audit_context or {}),
                    "source": (audit_context or {}).get("source", "daemon.session"),
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "engine": getattr(engine, "name", "unknown"),
                },
                enable_config_discovery=enable_config_discovery,
            )

            if result:
                log(f"Session '{name}' completed ({len(result)} chars)")
                log(f"--- {name} full output ---\n{result}\n--- end {name} ---", "THOUGHT")
            else:
                log(f"Session '{name}' completed (no response)")
            return result
        finally:
            if audit_tasks:
                await asyncio.gather(*audit_tasks, return_exceptions=True)
            if session_id in self.active_sessions:
                self.active_sessions.remove(session_id)

    async def _run_via_copilot_direct(
        self,
        name: str,
        system_message: str,
        prompt: str,
        model: str,
        reasoning_effort: str | None = None,
        mcp_config_override: dict | None = None,
        enable_config_discovery: bool = False,
    ) -> str | None:
        """Legacy fallback: run session using CopilotClient directly."""
        client = None

        try:
            # Honor COPILOT_CLI_PATH / auto-detect the user's installed CLI
            # so this fallback path sees the same models the engine does.
            try:
                from lucent.llm.copilot_engine import resolve_copilot_cli_path

                _cli_path = resolve_copilot_cli_path()
            except Exception:
                _cli_path = None
            _subprocess_kwargs: dict[str, Any] = {"log_level": "warning"}
            if _cli_path:
                _subprocess_kwargs["cli_path"] = _cli_path
            client = CopilotClient(config=SubprocessConfig(**_subprocess_kwargs))
            await client.start()

            session = await client.create_session(
                on_permission_request=PermissionHandler.approve_all,
                model=model,
                reasoning_effort=reasoning_effort,
                system_message=SystemMessageReplaceConfig(
                    mode="replace", content=system_message
                ),
                mcp_servers=mcp_config_override or MCP_CONFIG,
                enable_config_discovery=enable_config_discovery,
            )
            self.active_sessions.append(session)

            try:
                response_parts = []
                done = asyncio.Event()
                last_activity = time.time()

                def on_event(event):
                    nonlocal last_activity
                    last_activity = time.time()
                    etype = event.type.value if hasattr(event.type, "value") else str(event.type)

                    if etype == "assistant.message":
                        content = getattr(event.data, "content", None)
                        if content:
                            response_parts.append(content)
                            log(f"  [{name}] message: {content[:200]}...", "STREAM")
                    elif etype == "assistant.message_delta":
                        pass
                    elif etype == "session.idle":
                        done.set()
                    elif "error" in etype.lower():
                        error_message = getattr(event.data, "message", str(event.data)[:200])
                        log(
                            f"  [{name}] error event: {etype} - {error_message}",
                            "ERROR",
                        )
                        if self._is_mcp_auth_failure_message(error_message):
                            raise AuthFailureDetectedError(
                                f"MCP auth error during session '{name}': {error_message}"
                            )
                        done.set()
                    else:
                        detail = ""
                        if hasattr(event.data, "tool_name"):
                            detail = f" tool={event.data.tool_name}"
                        elif hasattr(event.data, "name"):
                            detail = f" name={event.data.name}"
                        if hasattr(event.data, "output"):
                            output = str(event.data.output)[:300]
                            detail += f" output={output}"
                            if self._is_mcp_auth_failure_message(output):
                                raise AuthFailureDetectedError(
                                    f"MCP auth failure in tool output for session '{name}'"
                                )
                        elif hasattr(event.data, "result"):
                            result_str = str(event.data.result)[:300]
                            detail += f" result={result_str}"
                            if self._is_mcp_auth_failure_message(result_str):
                                raise AuthFailureDetectedError(
                                    f"MCP auth failure in tool result for session '{name}'"
                                )
                        log(f"  [{name}] event: {etype}{detail}", "STREAM")

                session.on(on_event)
                await session.send(prompt)

                # Activity-based timeout loop
                start_time = time.time()
                while not done.is_set():
                    elapsed = time.time() - start_time
                    if elapsed >= SESSION_TOTAL_TIMEOUT:
                        log(f"Session '{name}' hard timeout after {int(elapsed)}s", "WARN")
                        break
                    idle_elapsed = time.time() - last_activity
                    wait_time = min(SESSION_IDLE_TIMEOUT - idle_elapsed, SESSION_TOTAL_TIMEOUT - elapsed, 10.0)
                    if wait_time <= 0:
                        log(f"Session '{name}' idle timeout after {SESSION_IDLE_TIMEOUT}s of inactivity (total: {int(elapsed)}s)", "WARN")
                        break
                    try:
                        await asyncio.wait_for(done.wait(), timeout=max(wait_time, 0.1))
                    except asyncio.TimeoutError:
                        idle_elapsed = time.time() - last_activity
                        if idle_elapsed >= SESSION_IDLE_TIMEOUT:
                            log(f"Session '{name}' idle timeout after {SESSION_IDLE_TIMEOUT}s of inactivity (total: {int(time.time() - start_time)}s)", "WARN")
                            break
                        continue

                try:
                    await session.destroy()
                except Exception:
                    log(f"Failed to destroy session '{name}'", "DEBUG")
            finally:
                if session in self.active_sessions:
                    self.active_sessions.remove(session)

            result = "\n".join(response_parts) if response_parts else None
            if result:
                log(f"Session '{name}' completed ({len(result)} chars)")
                log(f"--- {name} full output ---\n{result}\n--- end {name} ---", "THOUGHT")
            else:
                log(f"Session '{name}' completed (no response)")
            return result

        except Exception as e:
            log(f"Session '{name}' failed: {e}", "ERROR")
            return None
        finally:
            await self._force_cleanup_client(client)

    async def _force_cleanup_client(self, client):
        """Clean up a CopilotClient, using force_stop as fallback."""
        if not client:
            return
        try:
            await asyncio.wait_for(client.stop(), timeout=10)
        except (asyncio.TimeoutError, Exception):
            try:
                await client.force_stop()
            except Exception:
                log("Failed to force-stop Copilot client", "DEBUG")

    async def _dispatch_tracked_tasks(self, max_tasks: int = 2):
        """Dispatch tasks from the new request tracking queue."""
        async def _start_owned(task_id: str):
            try:
                return await RequestAPI.start_task(task_id, instance_id=self.instance_id)
            except TypeError:
                # Backwards-compatible for tests that monkeypatch old signatures.
                return await RequestAPI.start_task(task_id)

        async def _fail_owned(task_id: str, error: str, result: str | None = None):
            try:
                return await RequestAPI.fail_task(
                    task_id,
                    error,
                    instance_id=self.instance_id,
                    result=result,
                )
            except TypeError:
                return await RequestAPI.fail_task(task_id, error)

        async def _complete_owned(task_id: str, result: str, **kwargs):
            try:
                return await RequestAPI.complete_task(
                    task_id,
                    result,
                    instance_id=self.instance_id,
                    **kwargs,
                )
            except TypeError:
                return await RequestAPI.complete_task(task_id, result, **kwargs)

        # Ensure requests that reached review status have a review task queued.
        await self._ensure_request_review_tasks()

        pending = await RequestAPI.get_pending_tasks()
        if not pending:
            return

        log(f"Found {len(pending)} tracked tasks in queue")
        dispatched = 0

        for task in pending:
            if dispatched >= max_tasks:
                log(f"Tracked task cap reached ({max_tasks}), deferring rest to next cycle")
                break

            task_id = str(task["id"])
            agent_type = task.get("agent_type", "code")
            task_model = task.get("model")  # per-task model override
            task_reasoning_effort = task.get("reasoning_effort")
            title = task.get("title", "")
            description = task.get("description", title)
            selected_model, model_reason = _select_model_for_task(
                agent_type=agent_type,
                title=title,
                description=description,
                explicit_model=task_model,
            )
            if task_reasoning_effort:
                try:
                    from lucent.model_registry import validate_reasoning_effort

                    effort_error = validate_reasoning_effort(
                        selected_model,
                        task_reasoning_effort,
                    )
                except Exception as exc:
                    effort_error = f"reasoning-effort validator unavailable: {exc}"
                if effort_error:
                    log(
                        f"Task {task_id[:8]} reasoning_effort '{task_reasoning_effort}' "
                        f"is invalid for model '{selected_model}': {effort_error}; "
                        "using provider default",
                        "WARN",
                    )
                    task_reasoning_effort = None

            # Claim it atomically
            claimed = await RequestAPI.claim_task(task_id, self.instance_id)
            if not claimed:
                continue

            # Persist the resolved model before dispatch so it's recorded even if the task fails
            await RequestAPI.update_task_model_settings(
                task_id,
                model=selected_model,
                reasoning_effort=task_reasoning_effort,
            )

            effort_label = (
                f" reasoning_effort={task_reasoning_effort}" if task_reasoning_effort else ""
            )
            log(
                f"Dispatching tracked task {task_id[:8]} to {agent_type} "
                f"model={selected_model}{effort_label}: {title[:80]}...",
                request_id=task_id,
                user_id=str(task.get("requesting_user_id")) if task.get("requesting_user_id") else None,
            )
            if not task_model:
                log(f"Task {task_id[:8]} model selection: {model_reason}", "DEBUG")
            if self._tracer:
                self._tasks_dispatched_total.add(1, attributes={"agent_type": agent_type})

            # Fetch parent request context and completed sibling results
            request_id = str(task.get("request_id", ""))
            org_id = str(task.get("organization_id", ""))
            requesting_user_id = task.get("requesting_user_id")
            if requesting_user_id is None and request_id and org_id:
                req_row = await RequestAPI.get_request(request_id)
                if req_row:
                    requesting_user_id = req_row.get("created_by")
            if not requesting_user_id:
                reason = "Task missing requesting_user_id and parent request creator"
                await _fail_owned(task_id, reason)
                await RequestAPI.add_event(task_id, "dispatch_denied", reason)
                continue
            requesting_user_id = str(requesting_user_id)
            # Note: We trust the requesting_user_id from the request record.
            # The user was authenticated when they created the request.
            # Re-validating here would require user-read API permissions
            # that the daemon API key doesn't have.
            task_context = ""
            if request_id:
                req_desc, sibling_results = await RequestAPI.get_request_context(request_id)
                context_parts = []
                if req_desc:
                    req_title = task.get("request_title", "")
                    context_parts.append(
                        f"--- PARENT REQUEST ---\n"
                        f"Title: {req_title}\n"
                        f"Description: {req_desc}"
                    )
                if sibling_results:
                    context_parts.append(
                        f"--- COMPLETED SIBLING TASK RESULTS ---\n"
                        f"The following tasks in this request have already completed. "
                        f"Use these results directly — do not re-search for this information.\n\n"
                        f"{sibling_results}"
                    )
                output_contract = task.get("output_contract")
                if output_contract:
                    schema_str = json.dumps(output_contract.get("json_schema", {}), indent=2)
                    context_parts.append(
                        "--- OUTPUT CONTRACT ---\n"
                        "This task requires structured output. After your analysis and work, "
                        "you MUST include a JSON block wrapped in <task_output> tags that "
                        "conforms to the following schema:\n\n"
                        f"```json\n{schema_str}\n```\n\n"
                        "Format your response as normal prose/analysis, then include the "
                        "structured output at the end of your response:\n\n"
                        "<task_output>\n"
                        "{... your JSON output matching the schema above ...}\n"
                        "</task_output>\n\n"
                        "The structured output will be validated against the schema and passed "
                        "to downstream tasks. Include a 'summary' field in your JSON if the "
                        "schema allows it — this helps downstream tasks get context efficiently."
                    )
                task_context = "\n\n".join(context_parts)

                # Inject relevant technical memories if request targets a repo
                if request_id:
                    try:
                        tech_context = await self._get_technical_context_for_request(request_id)
                        if tech_context:
                            task_context = task_context + "\n\n" + tech_context if task_context else tech_context
                    except Exception as e:
                        log(f"Technical context injection failed (non-fatal): {e}", "DEBUG")

            # Build and run the sub-agent
            agent_def_id = task.get("agent_definition_id")
            sandbox_config = task.get("sandbox_config")
            # Defensive: if sandbox_config is a string (double-encoded JSON), parse it
            if isinstance(sandbox_config, str):
                try:
                    import json as _sc_json
                    sandbox_config = _sc_json.loads(sandbox_config)
                except Exception:
                    sandbox_config = None
            task_output_mode = task.get("output_mode")
            task_commit_approved = bool(task.get("commit_approved", False))
            sandbox_template_id = task.get("sandbox_template_id")
            sandbox_id = None
            task_sandbox_reused = False
            task_sandbox_runtime_config = None

            # Resolve sandbox_template_id → sandbox_config
            if sandbox_template_id and not sandbox_config:
                sandbox_config = await self._resolve_sandbox_template(
                    str(sandbox_template_id),
                    org_id=org_id,
                    requesting_user_id=requesting_user_id,
                )
            if sandbox_config and task_output_mode and not sandbox_config.get("output_mode"):
                sandbox_config = dict(sandbox_config)
                sandbox_config["output_mode"] = task_output_mode
            if sandbox_config and task_commit_approved and not sandbox_config.get("commit_approved"):
                sandbox_config = dict(sandbox_config)
                sandbox_config["commit_approved"] = True

            try:
                agent_data = await load_accessible_agent(
                    org_id=org_id,
                    requester_user_id=requesting_user_id,
                    agent_type=agent_type,
                    agent_definition_id=str(agent_def_id) if agent_def_id else None,
                )
                if not agent_data:
                    # Resolve display name for better error messages
                    user_display = requesting_user_id
                    try:
                        async with httpx.AsyncClient(timeout=5) as client:
                            resp = await client.get(
                                f"{API_BASE}/users/{requesting_user_id}",
                                headers=API_HEADERS,
                            )
                            if resp.status_code == 200:
                                user_display = resp.json().get("display_name") or requesting_user_id
                    except Exception:
                        pass
                    raise AgentNotFoundError(
                        f"No accessible approved agent definition for '{agent_type}' "
                        f"for requesting user {user_display}."
                    )
                skills = await load_accessible_skills_for_agent(
                    org_id=org_id,
                    requester_user_id=requesting_user_id,
                    agent_id=str(agent_data["id"]),
                )
                mcp_servers = await load_accessible_mcp_servers_for_agent(
                    org_id=org_id,
                    requester_user_id=requesting_user_id,
                    agent_id=str(agent_data["id"]),
                )
                hooks = await load_accessible_hooks_for_agent(
                    org_id=org_id,
                    requester_user_id=requesting_user_id,
                    agent_id=str(agent_data["id"]),
                )
                managed_tools = await load_accessible_managed_tools_for_agent(
                    org_id=org_id,
                    requester_user_id=requesting_user_id,
                    agent_id=str(agent_data["id"]),
                )
            except AgentNotFoundError as exc:
                log(f"Tracked task {task_id[:8]} failed: {exc}", "WARN")
                await _fail_owned(task_id, str(exc))
                await RequestAPI.add_event(task_id, "agent_not_found", str(exc))
                continue

            try:
                active_user_context = await _load_request_owner_context(
                    requesting_user_id, org_id
                )
                system_message = await build_subagent_prompt(
                    agent_type,
                    description,
                    task_context=task_context,
                    active_user_context=active_user_context,
                    agent_definition_id=str(agent_def_id) if agent_def_id else None,
                    resolved_agent=agent_data,
                    resolved_skills=skills,
                    resolved_tools=managed_tools,
                )
            except AgentNotFoundError as exc:
                log(f"Tracked task {task_id[:8]} failed: {exc}", "WARN")
                await _fail_owned(task_id, str(exc))
                await RequestAPI.add_event(
                    task_id, "agent_not_found", f"No approved definition for agent '{agent_type}'"
                )
                continue

            # Build requester-scoped MCP config for this task
            task_mcp_config = {}
            task_enable_config_discovery = False
            if mcp_servers:
                set_current_user({"id": requesting_user_id, "organization_id": org_id})
                try:
                    provider = await get_secret_provider()
                    for server in mcp_servers:
                        raw_server_type = (server.get("server_type") or "http").lower()
                        if raw_server_type in {
                            "copilot_github",
                            "copilot_builtin_github",
                            "copilot-builtin-github",
                            "github_builtin",
                            "github-builtin",
                        }:
                            # Marker definition: approving and granting this MCP server
                            # allows the Copilot runtime to expose its bundled GitHub
                            # MCP tools via SDK config discovery. No external process
                            # or token env vars are needed for this mode.
                            task_enable_config_discovery = True
                            continue
                        server_type = "http" if raw_server_type == "http" else "stdio"
                        if server_type == "http":
                            conf = {
                                "type": server_type,
                                "url": await resolve_runtime_value(server["url"]),
                                "headers": {
                                    k: (await resolve_runtime_value(v)) if isinstance(v, str) else v
                                    for k, v in (server.get("headers") or {}).items()
                                },
                                "tools": server.get("allowed_tools") or ["*"],
                            }
                        else:
                            conf = {
                                "type": server_type,
                                "command": await resolve_runtime_value(server.get("command") or ""),
                                "args": [
                                    (await resolve_runtime_value(a)) if isinstance(a, str) else a
                                    for a in (server.get("args") or [])
                                ],
                                "env": await resolve_secret_env_vars(
                                    server.get("env_vars") or {},
                                    provider,
                                ),
                                "tools": server.get("allowed_tools") or ["*"],
                            }
                        task_mcp_config[f"mcp-{server['id']}"] = conf
                finally:
                    set_current_user(None)

            # Determine memory scope for this task.
            #
            # Security model: a sub-agent executing a task MUST only see
            # what the requesting user can see. We mint a user-scoped key
            # for the request's creator so all memory ops run within that
            # user's ACL — including reads of linked goals, writes of new
            # memories, and updates of existing ones. The agent cannot read
            # other users' private memories regardless of prompt content.
            #
            # The previous behavior — falling back to the daemon's unscoped
            # key for tasks like post-completion review — broke this model
            # by giving sub-agents org-wide read access. That's reverted.
            # Per-user scope is the security boundary; nothing exempts it.
            #
            # Org-shared-only is reserved for future system-level org maintenance
            # where there's no single owning user.
            _request_title = ""
            if request_id:
                try:
                    _req = await RequestAPI.get_request(request_id)
                    _request_title = (_req or {}).get("title", "")
                except Exception:
                    pass

            _scope_type: str | None
            _scope_user: str | None
            _override_scope = _get_required_memory_scope(title, _request_title)
            if _override_scope == MEMORY_SCOPE_ORG_SHARED_ONLY:
                # System maintenance task — operates on shared org memories only.
                _scope_type = MEMORY_SCOPE_ORG_SHARED_ONLY
                _scope_user = None
            else:
                # Default and overwhelmingly the common case: scope to the
                # request's owning user. This holds even for auto-created
                # post-completion review tasks, which previously bypassed
                # scoping and ran with daemon org-wide access.
                _scope_type = MEMORY_SCOPE_USER
                _scope_user = requesting_user_id

            if MCP_CONFIG.get("memory-server"):
                _scoped_key = await _mint_scoped_api_key(
                    memory_scope=_scope_type,
                    memory_scope_user_id=_scope_user,
                    org_id=org_id,
                    ttl_minutes=60,
                )
                if _scoped_key:
                    task_mcp_config["memory-server"] = _build_scoped_memory_server_config(
                        scoped_key=_scoped_key,
                        memory_scope=_scope_type,
                        org_id=str(org_id),
                        memory_scope_user_id=_scope_user,
                        tools=_memory_server_tools_for_task(
                            agent_type,
                            title,
                            _request_title,
                            description,
                        ),
                        extra_headers={
                            "X-Lucent-Agent-Definition-Id": str(agent_data["id"]),
                            "X-Lucent-Task-Id": str(task_id),
                            "X-Lucent-Request-Id": str(request_id) if request_id else "",
                        },
                    )
                    if managed_tools:
                        for tool_name in (
                            "list_tool_definitions", "get_tool_definition", "run_managed_tool",
                        ):
                            if tool_name not in task_mcp_config["memory-server"]["tools"]:
                                task_mcp_config["memory-server"]["tools"].append(tool_name)
                    log(f"Task {task_id[:8]} using {_scope_type} scoped key"
                        f" (user: {_scope_user[:8] if _scope_user else 'shared'})")
                else:
                    # Mint failure is a security-sensitive event — refuse to
                    # dispatch rather than silently fall back to a key with
                    # broader access than the user is entitled to.
                    reason = (
                        f"Failed to mint {_scope_type} scoped key for "
                        f"requesting user {requesting_user_id[:8]}"
                    )
                    log(reason, "ERROR")
                    await _fail_owned(task_id, reason)
                    await RequestAPI.add_event(task_id, "dispatch_denied", reason)
                    continue

            # Mark running only after requester-scoped resources resolve successfully
            await _start_owned(task_id)
            await RequestAPI.add_event(
                task_id,
                "agent_dispatched",
                f"Dispatched to {agent_type} agent",
                {"agent_type": agent_type, "instance_id": self.instance_id},
            )

            # Special handling: Cognitive Planning schedule runs as per-user fan-out.
            # We intentionally bypass a single unscoped planner session so each user
            # gets planning against their private goals via scoped keys.
            #
            # IMPORTANT: only the original planning task on the schedule's request
            # should fan out. Auto-created post-completion review tasks live on the
            # same request and would otherwise also trigger fan-out, completing the
            # review with garbage output and leaving the request stuck in `review`
            # — which then spawns another review task next cycle, ad infinitum.
            if (
                _request_title.replace("[Scheduled] ", "") == "Cognitive Planning"
                and not self._is_request_review_task(task)
            ):
                try:
                    fanout_result = await self._run_cognitive_planning_fanout(
                        task_id=task_id,
                        org_id=org_id,
                        system_message=system_message,
                        mcp_config_base=task_mcp_config,
                    )
                    await _complete_owned(task_id, fanout_result[:4000])
                except Exception as e:
                    await _fail_owned(task_id, f"Cognitive planning fan-out failed: {e}")
                    await RequestAPI.add_event(
                        task_id,
                        "fanout_failed",
                        f"Cognitive planning fan-out failed: {e}",
                    )
                dispatched += 1
                continue

            # Create sandbox if configured.
            # sandbox-orchestrator manages its own sandbox lifecycle — skip daemon-side
            # creation and instead inject the config so the orchestrator knows what to build.
            if sandbox_config and agent_type == "sandbox-orchestrator":
                import json as _json
                description = (
                    f"{description}\n\n"
                    f"[SANDBOX CONFIG] Provision a sandbox with the following configuration:\n"
                    f"```json\n{_json.dumps(sandbox_config, indent=2, default=str)}\n```"
                )
            elif sandbox_config:
                sandbox_failed_reason = None
                sandbox_failure_meta: dict | None = None
                try:
                    (
                        sandbox_id,
                        task_sandbox_runtime_config,
                        sandbox_failure_meta,
                        task_sandbox_reused,
                    ) = await self._create_task_sandbox(
                        task_id,
                        sandbox_config,
                        request_id=request_id,
                        requesting_user_id=requesting_user_id,
                        org_id=org_id,
                        sequence_order=int(task.get("sequence_order") or 0),
                        sandbox_template_id=(
                            str(task.get("sandbox_template_id"))
                            if task.get("sandbox_template_id")
                            else None
                        ),
                    )
                    if not sandbox_id:
                        detail = (
                            (sandbox_failure_meta or {}).get("detail")
                            or "Sandbox manager returned no sandbox_id"
                        )
                        sandbox_failed_reason = f"Sandbox creation failed: {detail}"
                    else:
                        # Inject sandbox context into the task description
                        description = (
                            f"{description}\n\n"
                            f"[SANDBOX] This task {'reuses' if task_sandbox_reused else 'runs in'} "
                            f"sandbox {sandbox_id[:12]}. "
                            f"Use the sandbox exec API at POST /api/sandboxes/{sandbox_id}/exec "
                            f"to run commands. Working directory: "
                            f"{sandbox_config.get('working_dir', '/workspace')}"
                        )
                        await RequestAPI.add_event(
                            task_id,
                            "sandbox_reused" if task_sandbox_reused else "sandbox_created",
                            (
                                f"Sandbox {sandbox_id[:12]} reused for task"
                                if task_sandbox_reused
                                else f"Sandbox {sandbox_id[:12]} created for task"
                            ),
                            {
                                "sandbox_id": sandbox_id,
                                "reused": task_sandbox_reused,
                                "image": sandbox_config.get("image"),
                                "repo_url": sandbox_config.get("repo_url"),
                                "branch": sandbox_config.get("branch"),
                                "network_mode": sandbox_config.get("network_mode"),
                            },
                        )
                except Exception as e:
                    log(f"Sandbox creation failed for task {task_id[:8]}: {e}", "WARN")
                    sandbox_failed_reason = f"Sandbox creation failed: {e}"
                    sandbox_failure_meta = {
                        "stage": "sandbox_create_exception",
                        "detail": str(e),
                        "exception_type": type(e).__name__,
                    }

                # If sandbox was required and failed, fail the task — do NOT
                # run it locally. Sandboxes are a security boundary; silent
                # fallback to local execution defeats the purpose.
                if sandbox_failed_reason:
                    event_detail = sandbox_failed_reason
                    if sandbox_failure_meta and sandbox_failure_meta.get("image"):
                        event_detail = (
                            f"{sandbox_failed_reason} "
                            f"[image={sandbox_failure_meta.get('image')!r}, "
                            f"repo_url={sandbox_failure_meta.get('repo_url')!r}, "
                            f"network_mode={sandbox_failure_meta.get('network_mode')!r}]"
                        )
                    await RequestAPI.add_event(
                        task_id,
                        "sandbox_failed",
                        event_detail,
                        sandbox_failure_meta,
                    )
                    await _fail_owned(
                        task_id,
                        f"{sandbox_failed_reason} Task requires sandbox execution for "
                        f"security and will not fall back to local execution.",
                    )
                    dispatched += 1
                    continue

            try:
                result = await self.run_session(
                    f"{agent_type}-{task_id[:8]}",
                    system_message,
                    f"Execute this task:\n\n{description}",
                    model=selected_model,
                    reasoning_effort=task_reasoning_effort,
                    mcp_config_override=task_mcp_config,
                    enable_config_discovery=task_enable_config_discovery,
                    hooks=hooks,
                    audit_context={
                        "source": "daemon.task",
                        "organization_id": org_id,
                        "user_id": requesting_user_id,
                        "request_id": request_id,
                        "task_id": task_id,
                        "agent_definition_id": str(agent_data["id"]),
                        "agent_type": agent_type,
                        "skill_names": [s.get("name") for s in skills if s.get("name")],
                        "model": selected_model,
                        "reasoning_effort": task_reasoning_effort,
                    },
                )
            except ModelNotAvailableError as exc:
                log(
                    f"Tracked task {task_id[:8]}: model '{exc.model}' is not available "
                    f"in the runtime — failing task",
                    "WARN",
                )
                await _fail_owned(
                    task_id,
                    f"Model '{exc.model}' is not available in the runtime. "
                    f"Use a different model.",
                )
                await RequestAPI.add_event(
                    task_id,
                    "model_not_available",
                    f"Model '{exc.model}' is not available. "
                    f"Original error: {exc.original_error}",
                )
                dispatched += 1
                continue
            dispatched += 1

            # Log MCP memory-tool usage summary as a queryable task event
            session_name = f"{agent_type}-{task_id[:8]}"
            mcp_tracker = self._session_mcp_trackers.pop(session_name, [])
            tool_tracker = self._session_tool_trackers.pop(session_name, [])
            operational_tool_tracker = [
                entry for entry in tool_tracker if _is_operational_tool_call(entry)
            ]
            mcp_summary = _build_mcp_tool_summary(mcp_tracker)
            log(f"Task {task_id[:8]} ({agent_type}): {mcp_summary}")

            # Build structured metadata for queryable event
            search_count = sum(
                1 for e in mcp_tracker if e["tool"] in _MEMORY_SEARCH_TOOLS
            )
            capture_count = sum(
                1 for e in mcp_tracker if e["tool"] in _MEMORY_CAPTURE_TOOLS
            )
            tool_counts: dict[str, int] = {}
            for entry in mcp_tracker:
                tool_counts[entry["tool"]] = tool_counts.get(entry["tool"], 0) + 1
            validation_tool_counts: dict[str, int] = {}
            for entry in operational_tool_tracker:
                tool = _normalize_tool_name(entry.get("tool") or entry.get("raw_tool"))
                if tool:
                    validation_tool_counts[tool] = validation_tool_counts.get(tool, 0) + 1
            await RequestAPI.add_event(
                task_id,
                "mcp_tool_usage",
                mcp_summary,
                {
                    "total_calls": len(mcp_tracker),
                    "search_calls": search_count,
                    "capture_calls": capture_count,
                    "tool_counts": tool_counts,
                    "all_tool_counts": validation_tool_counts,
                    "calls": [
                        {"tool": e["tool"], "params": e["params"]}
                        for e in mcp_tracker
                    ],
                },
            )

            # Process and destroy sandbox after task completes
            if sandbox_id:
                try:
                    if task_sandbox_runtime_config and task_sandbox_runtime_config.output_mode:
                        from lucent.sandbox.manager import get_sandbox_manager

                        manager = get_sandbox_manager()
                        output_result = await manager.process_output(
                            sandbox_id=sandbox_id,
                            task_id=task_id,
                            task_description=description,
                            config=task_sandbox_runtime_config,
                            request_api=RequestAPI,
                            memory_api=MemoryAPI,
                            log=log,
                        )
                        if output_result:
                            await RequestAPI.add_event(
                                task_id,
                                "sandbox_output_processed",
                                output_result.detail,
                                {"mode": output_result.mode, **(output_result.metadata or {})},
                            )
                    retain_sandbox = (
                        bool(task_sandbox_runtime_config and task_sandbox_runtime_config.reuse_within_request)
                        and bool(request_id)
                        and await self._request_has_later_reusable_sandbox_task(
                            request_id,
                            task_id,
                            int(task.get("sequence_order") or 0),
                        )
                    )
                    if retain_sandbox:
                        await RequestAPI.add_event(
                            task_id,
                            "sandbox_retained",
                            f"Sandbox {sandbox_id[:12]} retained for a later request task",
                            {"sandbox_id": sandbox_id, "request_id": request_id},
                        )
                    else:
                        await self._destroy_task_sandbox(sandbox_id)
                        await RequestAPI.add_event(
                            task_id,
                            "sandbox_destroyed",
                            f"Sandbox {sandbox_id[:12]} destroyed after task completion",
                        )
                except Exception as e:
                    log(f"Sandbox cleanup failed for {sandbox_id[:12]}: {e}", "WARN")

            if (
                not _task_skips_tool_validation(agent_type)
                and _task_requires_mcp_tool_usage(agent_type, title, description)
                and not operational_tool_tracker
            ):
                reason = (
                    f"{agent_type} task completed without any operational tool calls. "
                    "Tool-dependent tasks must perform real tool operations, not "
                    "narrate intended changes."
                )
                log(f"Tracked task {task_id[:8]} failed: {reason}", "WARN")
                await RequestAPI.add_event(
                    task_id,
                    "missing_required_tool_usage",
                    reason,
                    {"agent_type": agent_type, "model": selected_model},
                )
                await _fail_owned(task_id, reason, result=result)
                continue

            if _task_skips_tool_validation(agent_type):
                required_tools = set()
            else:
                required_tools = _required_task_tool_names(agent_type, title, description)
            missing_required_tools: list[str] = []
            for required_tool in sorted(required_tools):
                if required_tool == "send_handoff":
                    satisfied = bool(validation_tool_counts.get("send_handoff", 0))
                else:
                    satisfied = bool(validation_tool_counts.get(required_tool, 0))
                if not satisfied:
                    missing_required_tools.append(required_tool)
            if missing_required_tools:
                reason = (
                    "Task instructions required tool call(s) "
                    f"{', '.join(missing_required_tools)}, but the session did not call them. "
                    "Do not satisfy explicit handoff instructions with narrative text only."
                )
                log(f"Tracked task {task_id[:8]} failed: {reason}", "WARN")
                await RequestAPI.add_event(
                    task_id,
                    "missing_specific_tool_usage",
                    reason,
                    {
                        "agent_type": agent_type,
                        "model": selected_model,
                        "missing_tools": missing_required_tools,
                        "tool_counts": validation_tool_counts,
                    },
                )
                await _fail_owned(task_id, reason, result=result)
                continue

            # Validate
            success, reason = self._validate_task_result(
                result,
                task=task,
                tool_counts=validation_tool_counts,
            )

            if success:
                output_contract = task.get("output_contract")
                output_result = process_task_output(result, output_contract)

                if output_result["validation_status"] in ("invalid", "extraction_failed"):
                    on_failure = (output_contract or {}).get("on_failure", "fallback")
                    max_retries = int((output_contract or {}).get("max_retries", 1) or 0)

                    if on_failure == "retry_then_fallback" and max_retries > 0:
                        repair_text = await self._repair_structured_output(
                            task_id,
                            result,
                            output_contract,
                            selected_model,
                        )
                        if repair_text:
                            repair_result = process_task_output(repair_text, output_contract)
                            if repair_result["validation_status"] == "valid":
                                output_result = repair_result
                                output_result["validation_status"] = "repair_succeeded"
                                result = repair_text

                    # If validation still failed after policy actions, either fail or fallback.
                    if output_result["validation_status"] not in ("valid", "repair_succeeded"):
                        if on_failure == "fail":
                            await _fail_owned(
                                task_id,
                                "Output validation failed: "
                                f"{output_result.get('validation_errors')}",
                            )
                            continue
                        output_result["validation_status"] = "fallback_used"
                        output_result["result_structured"] = None
                        log(f"Task {task_id[:8]}: validation failed, using text fallback", "WARN")

                await _complete_owned(
                    task_id,
                    result,
                    result_structured=output_result.get("result_structured"),
                    result_summary=output_result.get("result_summary"),
                    validation_status=output_result.get("validation_status", "not_applicable"),
                    validation_errors=output_result.get("validation_errors"),
                )

                if self._is_request_review_task(task):
                    try:
                        await self._process_request_review_task(task, result or "")
                    except Exception as review_err:
                        log(
                            f"Review processing failed for task {task_id[:8]}: {review_err}; "
                            "marking request for manual review",
                            "WARN",
                        )
                        request_id = str(task.get("request_id", ""))
                        if request_id:
                            await RequestAPI.update_request_status(request_id, "needs_rework")
                        await RequestAPI.add_event(
                            task_id,
                            "request_review_processing_failed",
                            f"Automatic review processing failed: {review_err}",
                        )

                log(f"Tracked task {task_id[:8]} completed ({len(result) if result else 0} chars)")
                if self._tracer:
                    self._tasks_completed_total.add(1, attributes={"status": "success", "agent_type": agent_type})
            else:
                if self._is_request_review_task(task):
                    await self._handle_review_task_failure(task, reason)
                    if self._tracer:
                        self._tasks_completed_total.add(
                            1, attributes={"status": "manual_review", "agent_type": agent_type}
                        )
                else:
                    await RequestAPI.add_event(
                        task_id,
                        "validation_failed",
                        reason,
                        {"result_excerpt": (result or "")[:2000]},
                    )
                    await _fail_owned(task_id, reason, result=result)
                    log(f"Tracked task {task_id[:8]} failed: {reason}", "WARN")
                    if self._tracer:
                        self._tasks_completed_total.add(
                            1, attributes={"status": "failed", "agent_type": agent_type}
                        )

    # Maximum time to wait for in-flight sessions during graceful drain (seconds)
    DRAIN_TIMEOUT = SESSION_TOTAL_TIMEOUT + 60  # session timeout + buffer

    # --- Main Loops ---

    async def run_forever(self):
        """Run enabled daemon loops concurrently.

                The daemon has three core loops:
          - dispatcher:  event-driven task execution (PG LISTEN + polling)
          - scheduler:   fires due schedules (system + user-defined)
                    - decomposition: immediately turns new requests into tasks

        Cognitive planning, memory maintenance, and learning extraction
        are all system schedules — they flow through the scheduler and
        dispatcher like any other task.
        """
        await self.start()
        self._source_mtimes = self._snapshot_source_files()

        log(f"Daemon roles enabled: {', '.join(sorted(self.roles))}")

        try:
            loops: list[asyncio.Task] = []

            if "dispatcher" in self.roles:
                loops.append(asyncio.create_task(self._dispatch_loop(), name="dispatch"))
            if "scheduler" in self.roles:
                loops.append(asyncio.create_task(self._scheduler_loop(), name="scheduler"))
                loops.append(
                    asyncio.create_task(
                        self._decomposition_loop(), name="request-decomposition"
                    )
                )

            # File watcher for auto-reload (always runs)
            loops.append(asyncio.create_task(self._reload_watcher(), name="reload-watcher"))

            if len(loops) <= 1:
                log("No roles enabled — nothing to do", "ERROR")
                return

            # Wait for any loop to exit (shouldn't happen unless shutdown/reload)
            done, pending = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)

            # Cancel remaining loops
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        except asyncio.CancelledError:
            log("Main run loop cancelled", "DEBUG")
        finally:
            await self.stop()

    async def run_once(self, task: str | None = None):
        """Run a single cycle or specific sub-agent task, then exit.

        In single-cycle mode: runs cognitive planning, fires due schedules,
        and dispatches any pending tasks — then exits.
        """
        await self.start()
        try:
            if task:
                try:
                    system_message = await build_subagent_prompt(task, f"Execute a {task} task.")
                except AgentNotFoundError as exc:
                    log(str(exc), "ERROR")
                    return
                await self.run_session(
                    f"{task}-manual",
                    system_message,
                    (
                        f"Run a {task} session. Search memories "
                        f"for context, do your work, save results."
                    ),
                )
            else:
                # Full single cycle: cognitive → schedule → dispatch
                await self.run_cognitive_cycle()
                await self._check_due_schedules()
                await self._dispatch_tracked_tasks()
        finally:
            await self.stop()


# ============================================================================
# Entry Point
# ============================================================================


def main():
    """Entry point for the daemon."""
    import argparse

    # Initialize structured logging before anything else
    global _logger
    _logger = _configure_daemon_logging()

    parser = argparse.ArgumentParser(description="Lucent Daemon — Cognitive Architecture")
    parser.add_argument("--once", action="store_true", help="Run one cognitive cycle and exit")
    parser.add_argument(
        "--task",
        type=str,
        help=(
            "Run a specific sub-agent (research, code, memory, reflection, documentation, planning)"
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DAEMON_INTERVAL_MINUTES,
        help="Minutes between cognitive cycles",
    )
    parser.add_argument(
        "--roles",
        type=str,
        default=None,
        help=(
            "Comma-separated roles to enable: dispatcher, cognitive, scheduler, autonomic "
            "(or 'all'). Overrides LUCENT_DAEMON_ROLES env var."
        ),
    )
    args = parser.parse_args()

    daemon = LucentDaemon()

    # Override roles from CLI if provided
    if args.roles:
        daemon.roles = daemon._parse_roles(args.roles)

    if args.once or args.task:
        asyncio.run(daemon.run_once(args.task))
    else:
        asyncio.run(daemon.run_forever())


if __name__ == "__main__":
    main()
