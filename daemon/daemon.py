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
import json
import os
import platform
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from copilot import CopilotClient, PermissionHandler

# Optional: adaptation module for environment assessment
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from adaptation import AdaptationPipeline, parse_assessment_output

    sys.path.pop(0)
except (ImportError, Exception):
    AdaptationPipeline = None
    parse_assessment_output = None

# ============================================================================
# Exceptions
# ============================================================================


class AgentNotFoundError(Exception):
    """Raised when a task references an agent type with no approved definition."""


# ============================================================================
# Configuration
# ============================================================================

MAX_CONCURRENT_SESSIONS = int(os.environ.get("LUCENT_MAX_SESSIONS", "3"))
DAEMON_INTERVAL_MINUTES = int(os.environ.get("LUCENT_DAEMON_INTERVAL", "15"))
MODEL = os.environ.get("LUCENT_DAEMON_MODEL", "claude-opus-4.6")
STALE_HEARTBEAT_MINUTES = int(os.environ.get("LUCENT_STALE_HEARTBEAT_MINUTES", "30"))
# Overall timeout for an entire run_session call (client start + create + response)
SESSION_TOTAL_TIMEOUT = int(os.environ.get("LUCENT_SESSION_TIMEOUT", "720"))
# Watchdog: kill process if no log activity for this many seconds.
# CopilotClient can block the event loop, defeating asyncio timeouts.
WATCHDOG_TIMEOUT = int(os.environ.get("LUCENT_WATCHDOG_TIMEOUT", "900"))
WATCHDOG_CHECK_INTERVAL = 60
# How many cognitive cycles between autonomic maintenance runs
AUTONOMIC_INTERVAL = int(os.environ.get("LUCENT_AUTONOMIC_INTERVAL", "8"))
# How many cognitive cycles between learning extraction runs
LEARNING_INTERVAL = int(os.environ.get("LUCENT_LEARNING_INTERVAL", str(AUTONOMIC_INTERVAL * 2)))
# Maximum characters stored from sub-agent results
MAX_RESULT_LENGTH = int(os.environ.get("LUCENT_MAX_RESULT_LENGTH", "8000"))

# ── Role-based loop configuration ─────────────────────────────────────────
# Each daemon instance can run a subset of roles. Multi-instance deployments
# can split work across instances (e.g. one cognitive, N dispatchers).
# Roles: dispatcher, cognitive, scheduler, autonomic (or 'all')
DAEMON_ROLES_STR = os.environ.get("LUCENT_DAEMON_ROLES", "all")
# Dispatch loop: how often to poll if PG LISTEN misses a signal
DISPATCH_POLL_SECONDS = int(os.environ.get("LUCENT_DISPATCH_POLL_SECONDS", "60"))
# Scheduler loop: how often to check for due schedules
SCHEDULER_CHECK_SECONDS = int(os.environ.get("LUCENT_SCHEDULER_CHECK_SECONDS", "60"))
# Time-based intervals for independent loops (derive defaults from cycle-count configs)
AUTONOMIC_MINUTES = int(
    os.environ.get("LUCENT_AUTONOMIC_MINUTES", str(AUTONOMIC_INTERVAL * DAEMON_INTERVAL_MINUTES))
)
LEARNING_MINUTES = int(
    os.environ.get("LUCENT_LEARNING_MINUTES", str(LEARNING_INTERVAL * DAEMON_INTERVAL_MINUTES))
)

# Approval flow: when enabled, tasks go to needs-review before completing.
# When disabled, tasks complete immediately after successful execution.
REQUIRE_APPROVAL = os.environ.get("LUCENT_REQUIRE_APPROVAL", "false").lower() in (
    "true",
    "1",
    "yes",
)

# Multi-model review: comma-separated list of models to use for reviewing task output.
# When set, completed tasks are re-evaluated by each model before final completion.
# The cognitive model is always claude-opus-4.6; these are for sub-agent review.
REVIEW_MODELS = [
    m.strip() for m in os.environ.get("LUCENT_REVIEW_MODELS", "").split(",") if m.strip()
]

# Git operations: daemon can commit (but never push without ALLOW_GIT_PUSH)
_git_commit_val = os.environ.get("LUCENT_ALLOW_GIT_COMMIT", "false")
ALLOW_GIT_COMMIT = _git_commit_val.lower() in ("true", "1", "yes")
_git_push_val = os.environ.get("LUCENT_ALLOW_GIT_PUSH", "false")
ALLOW_GIT_PUSH = _git_push_val.lower() in ("true", "1", "yes")

# Paths
DAEMON_DIR = Path(__file__).parent
COGNITIVE_PROMPT_PATH = DAEMON_DIR / "cognitive.md"
AGENT_DEF_PATH = DAEMON_DIR.parent / ".github" / "agents" / "lucent.agent.md"
LOG_FILE = DAEMON_DIR / "daemon.log"
DAEMON_KEY_FILE = DAEMON_DIR / ".daemon_api_key"

# MCP configuration — passed to all sessions
MCP_URL = os.environ.get("LUCENT_MCP_URL", "http://localhost:8766/mcp")
MCP_API_KEY = os.environ.get("LUCENT_MCP_API_KEY", "")

# Database URL for direct key provisioning.
# Prefers DAEMON_DATABASE_URL (restricted lucent_daemon role) over DATABASE_URL
# (full-privilege server role). The restricted role can only manage api_keys.
DATABASE_URL = os.environ.get(
    "DAEMON_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql://lucent:lucent_dev_password@localhost:5433/lucent"),
)

# Key expiry — daemon keys auto-expire and are refreshed each cycle
KEY_TTL_HOURS = 24

# These are set dynamically after key provisioning in LucentDaemon.start()
MCP_CONFIG: dict = {}
API_BASE = MCP_URL.replace("/mcp", "/api")
API_HEADERS: dict = {"Content-Type": "application/json"}


def _build_auth_config(api_key: str) -> tuple[dict, dict]:
    """Build MCP_CONFIG and API_HEADERS from a valid API key."""
    mcp_config = (
        {
            "memory-server": {
                "type": "http",
                "url": MCP_URL,
                "headers": {"Authorization": f"Bearer {api_key}"},
                "tools": ["*"],
            },
        }
        if api_key
        else {}
    )
    api_headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    return mcp_config, api_headers


# ============================================================================
# API Key Provisioning
# ============================================================================

# Tracks the current key's DB id so we can revoke it on shutdown
_current_key_db_id: str | None = None
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

    Uses the restricted lucent_daemon DB role which can only touch api_keys,
    users (SELECT), and organizations (SELECT).  The daemon service user is
    pre-created by migration 017; if it doesn't exist yet, we fall back to
    creating it (works when connected as the full-privilege lucent role).

    Returns the plain-text hs_ key, or None on failure.
    """
    import secrets

    import asyncpg
    import bcrypt

    global _current_key_db_id

    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        log(f"DB connect failed during key provisioning: {e}", "WARN")
        return None

    try:
        # Look up the daemon service user (created by migration 017)
        user = await conn.fetchrow(
            "SELECT id, organization_id FROM users "
            "WHERE external_id = 'daemon-service' AND is_active = true"
        )
        if not user:
            # Fallback: create the user if we have sufficient privileges
            # (only works with the full lucent role, not lucent_daemon)
            try:
                org = await conn.fetchrow(
                    "SELECT id FROM organizations ORDER BY created_at LIMIT 1"
                )
                org_id = str(org["id"]) if org else None
                user = await conn.fetchrow(
                    "INSERT INTO users (external_id, provider, organization_id, "
                    "  email, display_name, role) "
                    "VALUES ('daemon-service', 'local', $1, "
                    "  'daemon@lucent.local', 'Lucent Daemon', 'member') "
                    "RETURNING id, organization_id",
                    org_id,
                )
                log("Created daemon service user (fallback path)")
            except Exception as e:
                log(
                    f"Daemon service user not found and cannot create: {e}. "
                    "Run migration 017 or use the full DATABASE_URL.",
                    "ERROR",
                )
                return None

        user_id = str(user["id"])
        org_id = str(user["organization_id"]) if user["organization_id"] else None

        # Instance-specific key name prevents collisions between daemon instances
        key_name = f"daemon-{instance_id}"

        # Revoke any prior key for THIS instance (unique constraint on name)
        await conn.execute(
            "UPDATE api_keys SET is_active = false, revoked_at = NOW() "
            "WHERE user_id = $1 AND name = $2 AND revoked_at IS NULL",
            user_id,
            key_name,
        )

        # Hard-delete old revoked daemon keys to prevent table bloat.
        # Keep only the 5 most recent revoked keys for audit trail.
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id = $1 "
            "AND name LIKE 'daemon-%' AND revoked_at IS NOT NULL "
            "AND id NOT IN ("
            "  SELECT id FROM api_keys WHERE user_id = $1 "
            "  AND name LIKE 'daemon-%' AND revoked_at IS NOT NULL "
            "  ORDER BY revoked_at DESC LIMIT 5"
            ")",
            user_id,
        )

        # Generate a new hs_ key with 24h expiry
        raw_key = secrets.token_urlsafe(32)
        plain_key = f"hs_{raw_key}"
        key_prefix = plain_key[:11]
        key_hash = bcrypt.hashpw(plain_key.encode(), bcrypt.gensalt()).decode()

        row = await conn.fetchrow(
            "INSERT INTO api_keys "
            "(user_id, organization_id, name, key_prefix, key_hash, scopes, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, NOW() + INTERVAL '1 hour' * $7) "
            "RETURNING id",
            user_id,
            org_id,
            key_name,
            key_prefix,
            key_hash,
            ["read", "write"],
            KEY_TTL_HOURS,
        )
        _current_key_db_id = str(row["id"])
        log(f"Provisioned daemon API key (prefix: {key_prefix}, expires in {KEY_TTL_HOURS}h)")
        return plain_key

    except Exception as e:
        log(f"Key provisioning failed: {e}", "ERROR")
        return None
    finally:
        await conn.close()


async def _revoke_current_key() -> None:
    """Revoke the daemon's current API key (called on shutdown)."""
    import asyncpg

    global _current_key_db_id

    if not _current_key_db_id:
        return

    try:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            await conn.execute(
                "UPDATE api_keys SET is_active = false, revoked_at = NOW() "
                "WHERE id = $1 AND revoked_at IS NULL",
                _current_key_db_id,
            )
            log(f"Revoked daemon API key on shutdown (id: {_current_key_db_id[:8]}...)")
        finally:
            await conn.close()
    except Exception as e:
        log(f"Failed to revoke key on shutdown: {e}", "WARN")
    finally:
        _current_key_db_id = None
        # Clean up cached key file
        if DAEMON_KEY_FILE.exists():
            try:
                DAEMON_KEY_FILE.unlink()
            except OSError:
                pass


async def _verify_api_key(api_key: str) -> bool:
    """Check if an API key is accepted by the server.

    Uses /api/search (available in all modes) rather than /api/users/me
    which only exists in team mode.
    """
    if not api_key or not api_key.startswith("hs_"):
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{API_BASE}/search",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"q": "_verify"},
            )
            return resp.status_code == 200
    except Exception:
        return False


async def ensure_valid_api_key(instance_id: str = "local") -> str:
    """Ensure the daemon has a valid hs_ API key.

    Checks (in order): env var, cached file, provision new.
    Updates global MCP_CONFIG and API_HEADERS.
    Returns the valid key.
    """
    global MCP_API_KEY, MCP_CONFIG, API_HEADERS

    # 1. Check if the env var key works
    if MCP_API_KEY and await _verify_api_key(MCP_API_KEY):
        log("API key from environment is valid")
        MCP_CONFIG, API_HEADERS = _build_auth_config(MCP_API_KEY)
        return MCP_API_KEY

    # 2. Check cached key file
    if DAEMON_KEY_FILE.exists():
        cached_key = DAEMON_KEY_FILE.read_text().strip()
        if cached_key and await _verify_api_key(cached_key):
            log("Using cached API key")
            MCP_API_KEY = cached_key
            MCP_CONFIG, API_HEADERS = _build_auth_config(cached_key)
            return cached_key

    # 3. Provision a new key (instance-scoped, 24h expiry)
    log("No valid API key found — provisioning daemon service account...")
    new_key = await _provision_daemon_api_key(instance_id)
    if new_key:
        # Cache for future restarts (same instance can reuse if not expired)
        DAEMON_KEY_FILE.write_text(new_key)
        DAEMON_KEY_FILE.chmod(0o600)
        MCP_API_KEY = new_key
        MCP_CONFIG, API_HEADERS = _build_auth_config(new_key)
        log("Daemon API key provisioned and cached")
        return new_key

    # 4. Fall back to whatever we have (may not work)
    log("WARNING: Could not provision a valid API key", "WARN")
    MCP_CONFIG, API_HEADERS = _build_auth_config(MCP_API_KEY)
    return MCP_API_KEY


async def _handle_auth_failure(instance_id: str) -> bool:
    """Re-provision API key after an authentication failure.

    Uses a lock to prevent concurrent provisioning from multiple async loops
    (cognitive, scheduler, dispatcher) which would hit the unique constraint.
    Returns True if a new key was provisioned successfully.
    """
    global MCP_API_KEY, MCP_CONFIG, API_HEADERS, _current_key_db_id

    lock = _get_key_lock()
    async with lock:
        # Re-check after acquiring lock — another loop may have already fixed it
        if MCP_API_KEY and await _verify_api_key(MCP_API_KEY):
            return True

        log("Auth failure detected — re-provisioning API key...", "WARN")
        _current_key_db_id = None  # old key is dead

        new_key = await _provision_daemon_api_key(instance_id)
        if new_key:
            DAEMON_KEY_FILE.write_text(new_key)
            DAEMON_KEY_FILE.chmod(0o600)
            MCP_API_KEY = new_key
            MCP_CONFIG, API_HEADERS = _build_auth_config(new_key)
            log("Re-provisioned daemon API key after auth failure")
            return True

        log("Failed to re-provision API key after auth failure", "ERROR")
        return False


# ============================================================================
# Direct API Client (no LLM needed)
# ============================================================================


class MemoryAPI:
    """Direct REST API client for memory operations that don't need an LLM."""

    API_TIMEOUT = 15  # seconds for individual HTTP requests

    @staticmethod
    async def search(
        query: str, tags: list[str] | None = None, type: str | None = None, limit: int = 10
    ) -> list[dict]:
        """Search memories via REST API."""
        params = {"query": query, "limit": limit}
        if tags:
            params["tags"] = tags
        if type:
            params["type"] = type
        try:
            async with httpx.AsyncClient(timeout=MemoryAPI.API_TIMEOUT) as client:
                resp = await client.post(f"{API_BASE}/search", json=params, headers=API_HEADERS)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("memories", data.get("results", []))
        except Exception as e:
            log(f"API search failed: {e}", "WARN")
        return []

    @staticmethod
    async def create(
        type: str, content: str, tags: list[str], importance: int = 5, metadata: dict | None = None
    ) -> dict | None:
        """Create a memory via REST API."""
        body = {"type": type, "content": content, "tags": tags, "importance": importance}
        if metadata:
            body["metadata"] = metadata
        try:
            async with httpx.AsyncClient(timeout=MemoryAPI.API_TIMEOUT) as client:
                resp = await client.post(f"{API_BASE}/memories", json=body, headers=API_HEADERS)
                if resp.status_code in (200, 201):
                    return resp.json()
        except Exception as e:
            log(f"API create failed: {e}", "WARN")
        return None

    @staticmethod
    async def update(
        memory_id: str,
        tags: list[str] | None = None,
        content: str | None = None,
        importance: int | None = None,
        metadata: dict | None = None,
    ) -> dict | None:
        """Update a memory via REST API."""
        body = {}
        if tags is not None:
            body["tags"] = tags
        if content is not None:
            body["content"] = content
        if importance is not None:
            body["importance"] = importance
        if metadata is not None:
            body["metadata"] = metadata
        try:
            async with httpx.AsyncClient(timeout=MemoryAPI.API_TIMEOUT) as client:
                resp = await client.patch(
                    f"{API_BASE}/memories/{memory_id}", json=body, headers=API_HEADERS
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            log(f"API update failed: {e}", "WARN")
        return None

    @staticmethod
    async def get(memory_id: str) -> dict | None:
        """Get a single memory by ID."""
        try:
            async with httpx.AsyncClient(timeout=MemoryAPI.API_TIMEOUT) as client:
                resp = await client.get(f"{API_BASE}/memories/{memory_id}", headers=API_HEADERS)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            log(f"API get failed: {e}", "WARN")
        return None


class RequestAPI:
    """REST API client for the request tracking system."""

    API_TIMEOUT = 15

    @staticmethod
    async def create_request(title: str, description: str | None = None,
                             source: str = "cognitive", priority: str = "medium") -> dict | None:
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
    async def create_task(request_id: str, title: str, agent_type: str | None = None,
                          description: str | None = None, priority: str = "medium",
                          sequence_order: int = 0, model: str | None = None) -> dict | None:
        body = {"title": title, "priority": priority, "sequence_order": sequence_order}
        if agent_type:
            body["agent_type"] = agent_type
        if description:
            body["description"] = description
        if model:
            body["model"] = model
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/requests/{request_id}/tasks", json=body, headers=API_HEADERS)
                if resp.status_code in (200, 201):
                    return resp.json()
        except Exception as e:
            log(f"API create_task failed: {e}", "WARN")
        return None

    @staticmethod
    async def claim_task(task_id: str, instance_id: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/requests/tasks/{task_id}/claim",
                    params={"instance_id": instance_id}, headers=API_HEADERS)
                if resp.status_code in (200, 201):
                    return resp.json()
        except Exception as e:
            log(f"API claim_task failed: {e}", "WARN")
        return None

    @staticmethod
    async def start_task(task_id: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/requests/tasks/{task_id}/start", headers=API_HEADERS)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            log(f"API start_task failed: {e}", "WARN")
        return None

    @staticmethod
    async def complete_task(task_id: str, result: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/requests/tasks/{task_id}/complete",
                    params={"result": result[:8000]}, headers=API_HEADERS)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            log(f"API complete_task failed: {e}", "WARN")
        return None

    @staticmethod
    async def fail_task(task_id: str, error: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/requests/tasks/{task_id}/fail",
                    params={"error": error[:2000]}, headers=API_HEADERS)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            log(f"API fail_task failed: {e}", "WARN")
        return None

    @staticmethod
    async def add_event(task_id: str, event_type: str, detail: str | None = None,
                        metadata: dict | None = None) -> dict | None:
        body = {"event_type": event_type}
        if detail:
            body["detail"] = detail
        if metadata:
            body["metadata"] = metadata
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/requests/tasks/{task_id}/events",
                    json=body, headers=API_HEADERS)
                if resp.status_code in (200, 201):
                    return resp.json()
        except Exception as e:
            log(f"API add_event failed: {e}", "WARN")
        return None

    @staticmethod
    async def link_memory(task_id: str, memory_id: str, relation: str = "created") -> None:
        body = {"memory_id": memory_id, "relation": relation}
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                await client.post(
                    f"{API_BASE}/requests/tasks/{task_id}/memories",
                    json=body, headers=API_HEADERS)
        except Exception as e:
            log(f"API link_memory failed: {e}", "WARN")

    @staticmethod
    async def get_pending_tasks() -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.get(
                    f"{API_BASE}/requests/queue/pending", headers=API_HEADERS)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            log(f"API get_pending_tasks failed: {e}", "WARN")
        return []

    @staticmethod
    async def release_stale(stale_minutes: int = 30) -> int:
        try:
            async with httpx.AsyncClient(timeout=RequestAPI.API_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/requests/queue/release-stale",
                    params={"stale_minutes": stale_minutes}, headers=API_HEADERS)
                if resp.status_code == 200:
                    return resp.json().get("released", 0)
        except Exception as e:
            log(f"API release_stale failed: {e}", "WARN")
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

    # Add src to path so lucent package is importable
    src_dir = str(DAEMON_DIR.parent / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from lucent.logging import configure_logging, get_logger

    configure_logging()
    return get_logger


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


def log(message: str, level: str = "INFO"):
    """Log via the structured logging module (falls back to print before init)."""
    _touch_activity()
    if _logger is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [{level}] {message}")
        return

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


# ============================================================================
# System Message Builders
# ============================================================================


def build_cognitive_prompt() -> str:
    """Build the system message for the cognitive loop session."""
    cognitive_md = COGNITIVE_PROMPT_PATH.read_text() if COGNITIVE_PROMPT_PATH.exists() else ""
    agent_def = AGENT_DEF_PATH.read_text() if AGENT_DEF_PATH.exists() else ""

    return f"""
{cognitive_md}

--- AGENT IDENTITY ---
{agent_def}

--- CURRENT TIME ---
{datetime.now(timezone.utc).isoformat()}
"""


async def load_instance_agent(agent_type: str) -> dict | None:
    """Load an instance agent definition from the database, if one exists."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{API_BASE}/definitions/agents",
                params={"status": "active"},
                headers=API_HEADERS,
            )
            if resp.status_code == 200:
                agents = resp.json()
                for agent in agents:
                    if agent.get("name") == agent_type:
                        # Load full agent with skills and MCP servers
                        detail_resp = await client.get(
                            f"{API_BASE}/definitions/agents/{agent['id']}",
                            headers=API_HEADERS,
                        )
                        if detail_resp.status_code == 200:
                            return detail_resp.json()
    except Exception:
        pass
    return None


async def load_instance_skills_for_agent(agent_id: str) -> list[dict]:
    """Load skills granted to an instance agent."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{API_BASE}/definitions/agents/{agent_id}",
                headers=API_HEADERS,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("skills", [])
    except Exception:
        pass
    return []


def resolve_env_vars(value: str) -> str:
    """Resolve ${ENV_VAR} patterns in a string from environment variables."""
    import re
    def replacer(match):
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))  # Keep original if not found
    return re.sub(r'\$\{([^}]+)\}', replacer, value)


async def build_subagent_prompt(
    agent_type: str,
    task_description: str,
    task_context: str = "",
    agent_definition_id: str | None = None,
) -> str:
    """Build the system message for a sub-agent session.

    Resolution order:
      1. If agent_definition_id is set, load that specific definition from the DB
      2. Otherwise, search DB for an active definition matching agent_type by name
      3. Raise AgentNotFoundError if no approved definition exists

    Only active (human-approved) definitions are used. This ensures a human
    is always in the loop for what roles the daemon can assume.
    """
    agent_def = ""
    skills_context = ""

    # Try loading from DB definitions (approved only)
    db_agent = None
    if agent_definition_id:
        # Direct ID lookup
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{API_BASE}/definitions/agents/{agent_definition_id}",
                    headers=API_HEADERS,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "active":
                        db_agent = data
        except Exception:
            pass

    if not db_agent:
        # Search by name among active definitions
        db_agent = await load_instance_agent(agent_type)

    if db_agent:
        agent_def = db_agent.get("content", "")
        # Load skills granted to this agent
        skill_names = db_agent.get("skill_names", [])
        if skill_names:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    for skill_name in skill_names:
                        resp = await client.get(
                            f"{API_BASE}/definitions/skills",
                            params={"status": "active"},
                            headers=API_HEADERS,
                        )
                        if resp.status_code == 200:
                            for skill in resp.json():
                                if skill.get("name") in skill_names and skill.get("content"):
                                    skills_context += (
                                        f"\n\n--- Skill: {skill['name']} ---\n{skill['content']}"
                                    )
                            break  # Only need one request for all skills
            except Exception:
                pass
        log(f"Using approved DB definition for '{agent_type}' agent (id: {db_agent['id'][:8]})")
    else:
        raise AgentNotFoundError(
            f"No approved agent definition found for '{agent_type}'. "
            f"Create and approve a definition at /definitions before dispatching tasks to this agent."
        )

    identity = AGENT_DEF_PATH.read_text() if AGENT_DEF_PATH.exists() else ""

    return f"""You are a sub-agent of Lucent, a distributed intelligence.

--- SUB-AGENT DEFINITION ---
{agent_def}

--- LUCENT IDENTITY ---
{identity}

{f"--- SKILLS ---{skills_context}" if skills_context else ""}

--- YOUR TASK ---
{task_description}

{"--- ADDITIONAL CONTEXT ---" + chr(10) + task_context if task_context else ""}

--- USING MEMORY ---
Before starting work, search for relevant memories:
- Look for previous approaches to similar tasks (search by keywords from your task description)
- Check for validated patterns (tagged 'validated') and
  rejection lessons (tagged 'rejection-lesson')
- Reference procedural memories for proven workflows
- Build on existing knowledge rather than starting from scratch

After completing work, save what you learned:
- Not just what you did, but what approach you took and why
- What worked vs. what didn't
- What you'd do differently next time
- Connections to existing knowledge

--- OUTPUT ---
Always output your findings and results as text. Do not rely solely on saving
to memory — the dispatch system validates your text output.

--- GUARDRAILS ---
- {"Git commit is ALLOWED — commit meaningful changes with clear messages"
   if ALLOW_GIT_COMMIT else "DO NOT run git commit"}
- {"Git push is ALLOWED" if ALLOW_GIT_PUSH else "DO NOT run git push"}
- DO NOT take irreversible actions without approval
- Tag all memories with 'daemon' so activity is visible
- Write concise, actionable output

--- CURRENT TIME ---
{datetime.now(timezone.utc).isoformat()}
"""


# ============================================================================
# Daemon
# ============================================================================


class LucentDaemon:
    """Orchestrates Lucent's cognitive architecture.

    Runs independent loops based on configured roles:
      - dispatcher:  event-driven task execution (PG LISTEN + polling)
      - cognitive:   periodic planning / goal assessment
      - scheduler:   checks and fires due schedules
      - autonomic:   memory maintenance, learning extraction
    """

    ALL_ROLES = frozenset({"dispatcher", "cognitive", "scheduler", "autonomic"})

    def __init__(self):
        self.active_sessions: list = []
        self.running = False
        self.cycle_count = 0
        # Unique instance ID for distributed coordination
        hostname = platform.node() or "unknown"
        self.instance_id = f"{hostname}-{os.getpid()}-{int(datetime.now(timezone.utc).timestamp())}"

        # Role-based loop configuration
        self.roles = self._parse_roles(DAEMON_ROLES_STR)

        # PG LISTEN infrastructure for event-driven dispatch
        self._listen_conn = None
        self._task_ready = asyncio.Event()

        # Heartbeat memory ID (cached after first create/lookup)
        self._heartbeat_memory_id: str | None = None

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

        # Ensure we have a valid API key before anything else
        await ensure_valid_api_key(self.instance_id)

        # Start the watchdog thread — detects event loop freezes
        watchdog = threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog")
        watchdog.start()
        log(f"Watchdog started (timeout={WATCHDOG_TIMEOUT}s, check={WATCHDOG_CHECK_INTERVAL}s)")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)

        log(
            f"Daemon ready. Instance: {self.instance_id}, "
            f"Roles: {','.join(sorted(self.roles))}, "
            f"Model: {MODEL}, Max sessions: {MAX_CONCURRENT_SESSIONS}"
        )

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

        # Close PG LISTEN connection
        if self._listen_conn and not self._listen_conn.is_closed():
            try:
                await self._listen_conn.close()
            except Exception:
                pass
            self._listen_conn = None

        # Revoke the daemon's API key — clean up after ourselves
        await _revoke_current_key()

        for session in self.active_sessions:
            try:
                await session.destroy()
            except Exception:
                pass
        log("Daemon stopped")
        sys.exit(0)

    # --- Session Management ---

    async def run_session(
        self, name: str, system_message: str, prompt: str, model: str | None = None
    ) -> str | None:
        """Run a single Copilot session with the given system message and prompt.

        Each session gets its own CopilotClient for full isolation.
        Args:
            model: Override the default model. If None, uses MODEL config.
        Returns the assistant's final message text, or None on error.
        """
        if len(self.active_sessions) >= MAX_CONCURRENT_SESSIONS:
            log(f"Skipping '{name}' — at session limit ({MAX_CONCURRENT_SESSIONS})", "WARN")
            return None

        log(f"Starting session: {name}{f' (model: {model})' if model else ''}")

        try:
            return await asyncio.wait_for(
                self._run_session_inner(name, system_message, prompt, model=model),
                timeout=SESSION_TOTAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log(
                f"Session '{name}' HARD TIMEOUT after {SESSION_TOTAL_TIMEOUT}s — "
                "session lifecycle hung (likely during client.start or create_session)",
                "ERROR",
            )
            return None
        except Exception as e:
            log(f"Session '{name}' failed: {e}", "ERROR")
            return None

    async def _run_session_inner(
        self, name: str, system_message: str, prompt: str, model: str | None = None
    ) -> str | None:
        """Inner session runner — separated so the caller can wrap with a hard timeout."""
        client = None

        try:
            client = CopilotClient({"log_level": "warning"})
            await client.start()

            session = await client.create_session(
                {
                    "model": model or MODEL,
                    "system_message": {"content": system_message},
                    "on_permission_request": PermissionHandler.approve_all,
                    "mcp_servers": MCP_CONFIG,
                }
            )
            self.active_sessions.append(session)

            try:
                # Collect response with event streaming for visibility
                response_parts = []
                done = asyncio.Event()

                def on_event(event):
                    etype = event.type.value if hasattr(event.type, "value") else str(event.type)

                    if etype == "assistant.message":
                        content = getattr(event.data, "content", None)
                        if content:
                            response_parts.append(content)
                            log(f"  [{name}] message: {content[:200]}...", "STREAM")
                    elif etype == "assistant.message_delta":
                        pass  # Skip deltas — final message has full content
                    elif etype == "session.idle":
                        done.set()
                    elif "error" in etype.lower():
                        log(
                            f"  [{name}] error event: {etype} - "
                            f"{getattr(event.data, 'message', str(event.data)[:200])}",
                            "ERROR",
                        )
                        done.set()
                    else:
                        # Log all other events for visibility (tool calls, etc.)
                        detail = ""
                        if hasattr(event.data, "tool_name"):
                            detail = f" tool={event.data.tool_name}"
                        elif hasattr(event.data, "name"):
                            detail = f" name={event.data.name}"
                        # Include tool output/result snippets when available
                        if hasattr(event.data, "output"):
                            output = str(event.data.output)[:300]
                            detail += f" output={output}"
                        elif hasattr(event.data, "result"):
                            result_str = str(event.data.result)[:300]
                            detail += f" result={result_str}"
                        log(f"  [{name}] event: {etype}{detail}", "STREAM")

                session.on(on_event)
                await session.send({"prompt": prompt})

                try:
                    await asyncio.wait_for(done.wait(), timeout=600)
                except asyncio.TimeoutError:
                    log(f"Session '{name}' timed out after 10 minutes", "WARN")

                # Cleanup
                try:
                    await session.destroy()
                except Exception:
                    pass
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
                pass

    # --- Cognitive Loop ---

    async def _check_environment_adaptation(self):
        """Check if environment has been assessed. If not, run assessment + adaptation.

        This is the entry point for the adaptation pipeline. On first boot or
        in a new environment, it runs the assessment agent, parses structured
        output, and generates domain-specific agents and skills.
        """
        # Check if an environment memory already exists
        env_memories = await MemoryAPI.search("environment", tags=["environment"], limit=1)
        if env_memories:
            log("Environment profile found — skipping adaptation")
            return

        log("No environment profile found — running adaptation pipeline")

        # Run the assessment agent
        try:
            system_message = await build_subagent_prompt(
                "assessment",
                "Perform a full environment assessment. Discover tools, domain, "
                "collaborators, and goals. Produce structured output for the "
                "adaptation pipeline.",
            )
        except AgentNotFoundError:
            log("No approved 'assessment' agent definition — skipping adaptation. "
                "Create and approve one at /definitions to enable environment assessment.", "WARN")
            return
        assessment_output = await self.run_session(
            "adaptation-assessment",
            system_message,
            "Run a complete environment assessment. At the end of your response, "
            "include the structured <assessment_result> JSON block as described "
            "in your instructions. This is critical — the adaptation pipeline "
            "depends on it.",
        )

        if not assessment_output:
            log("Assessment agent produced no output", "WARN")
            return

        # Parse and run adaptation pipeline
        assessment = parse_assessment_output(assessment_output)
        if assessment is None:
            log(
                "Could not parse structured assessment output — "
                "the assessment agent may not have included <assessment_result> tags",
                "WARN",
            )
            return

        pipeline = AdaptationPipeline(assessment)
        summary = await pipeline.run(
            memory_api=MemoryAPI, api_base=API_BASE, api_headers=API_HEADERS,
        )

        agents_proposed = len(summary.get("agents_proposed", []))
        skills_proposed = len(summary.get("skills_proposed", []))
        log(
            f"Adaptation complete: {agents_proposed} agents, {skills_proposed} skills proposed "
            f"for domain '{summary.get('domain', 'unknown')}' — awaiting human approval"
        )

    async def run_cognitive_cycle(self):
        """Run one cognitive cycle — perceive, reason, decide, act via tools.

        This is the executive planning function. It assesses long-horizon goals,
        reviews state, and creates requests/tasks. It does NOT dispatch tasks —
        that is handled by the dispatch loop.
        """
        self.cycle_count += 1
        log(f"=== Cognitive cycle #{self.cycle_count} (instance: {self.instance_id}) ===")

        # Verify API key is still valid (handles 24h expiry and revocation)
        if not await _verify_api_key(MCP_API_KEY):
            log("API key expired or revoked — re-provisioning...", "WARN")
            if not await _handle_auth_failure(self.instance_id):
                log("Cannot proceed without valid API key — skipping cycle", "ERROR")
                return

        # On first cycle, check if environment adaptation is needed
        if self.cycle_count == 1:
            await self._check_environment_adaptation()

        prompt = build_cognitive_prompt()
        result = await self.run_session(
            f"cognitive-{self.cycle_count}",
            prompt,
            (
                "Begin your cognitive cycle. Load state, perceive, "
                "reason, decide. Use memory tools to create tasks "
                "and update state. Output a brief summary of "
                "your decisions."
            ),
        )

        if result:
            log(f"Cognitive cycle #{self.cycle_count} produced output", "INFO")

    # --- Distributed Coordination ---

    def _validate_task_result(self, result: str) -> tuple[bool, str]:
        """Validate whether a sub-agent result indicates actual work was done.

        Returns (success, reason) — success is True only if the result looks
        like genuine completed work rather than a failure or empty acknowledgment.
        """
        if not result:
            return False, "no output"

        stripped = result.strip()

        if len(stripped) < 100:
            return False, f"output too short ({len(stripped)} chars)"

        # For substantial output (1000+ chars), trust it — the agent did real work
        # even if it mentioned limitations along the way
        if len(stripped) >= 1000:
            return True, "ok"

        # For shorter output, check if it's mostly a failure message
        failure_indicators = [
            "couldn't find", "could not find", "unable to", "failed to",
            "i don't have", "i do not have", "no context",
            "cannot complete", "couldn't complete", "could not complete",
            "task not completed", "error occurred", "exception occurred",
        ]

        result_lower = result.lower()
        for indicator in failure_indicators:
            if indicator in result_lower:
                return False, f"failure indicator found: '{indicator}'"

        return True, "ok"

    async def _update_heartbeat(self):
        """Update instance heartbeat via direct API (no LLM session)."""
        heartbeat_content = json.dumps(
            {
                "instance_id": self.instance_id,
                "hostname": platform.node(),
                "pid": os.getpid(),
                "cycle_count": self.cycle_count,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": MODEL,
                "roles": sorted(self.roles),
                "max_sessions": MAX_CONCURRENT_SESSIONS,
            }
        )

        if self._heartbeat_memory_id:
            await MemoryAPI.update(self._heartbeat_memory_id, content=heartbeat_content)
        else:
            # Search for existing heartbeat from this instance
            results = await MemoryAPI.search(
                self.instance_id, tags=["daemon-heartbeat"], limit=1
            )
            if results:
                self._heartbeat_memory_id = results[0].get("id")
                await MemoryAPI.update(self._heartbeat_memory_id, content=heartbeat_content)
            else:
                result = await MemoryAPI.create(
                    type="technical",
                    content=heartbeat_content,
                    tags=["daemon-heartbeat", "daemon"],
                    importance=3,
                )
                if result:
                    self._heartbeat_memory_id = result.get("id")

    async def _check_due_schedules(self):
        """Fire any scheduled tasks that are due, creating requests for them."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{API_BASE}/schedules/due", headers=API_HEADERS)
                if resp.status_code != 200:
                    return
                due = resp.json()
                if not due:
                    return

            log(f"Found {len(due)} due schedules")

            for sched in due:
                sched_id = str(sched["id"])
                title = sched.get("title", "Scheduled task")
                template = sched.get("task_template") or {}

                # Trigger the schedule via API (creates request + task + records run)
                try:
                    async with httpx.AsyncClient(timeout=15) as client:
                        resp = await client.post(
                            f"{API_BASE}/schedules/{sched_id}/trigger",
                            headers=API_HEADERS,
                        )
                        if resp.status_code in (200, 201):
                            data = resp.json()
                            req_id = data.get("request", {}).get("id", "?")
                            log(f"Triggered schedule {sched_id[:8]} '{title}' → request {str(req_id)[:8]}")
                        else:
                            log(f"Failed to trigger schedule {sched_id[:8]}: {resp.status_code}", "WARN")
                except Exception as e:
                    log(f"Error triggering schedule {sched_id[:8]}: {e}", "WARN")

        except Exception as e:
            log(f"Error checking due schedules: {e}", "WARN")

    async def _dispatch_tracked_tasks(self, max_tasks: int = 2):
        """Dispatch tasks from the new request tracking queue."""
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
            title = task.get("title", "")
            description = task.get("description", title)

            # Claim it atomically
            claimed = await RequestAPI.claim_task(task_id, self.instance_id)
            if not claimed:
                continue

            log(f"Dispatching tracked task {task_id[:8]} to {agent_type}: {title[:80]}...")

            # Mark running
            await RequestAPI.start_task(task_id)
            await RequestAPI.add_event(task_id, "agent_dispatched",
                                       f"Dispatched to {agent_type} agent",
                                       {"agent_type": agent_type, "instance_id": self.instance_id})

            # Build and run the sub-agent
            agent_def_id = task.get("agent_definition_id")
            try:
                system_message = await build_subagent_prompt(
                    agent_type, description, agent_definition_id=str(agent_def_id) if agent_def_id else None,
                )
            except AgentNotFoundError as exc:
                log(f"Tracked task {task_id[:8]} failed: {exc}", "WARN")
                await RequestAPI.fail_task(task_id, str(exc))
                await RequestAPI.add_event(task_id, "agent_not_found",
                                           f"No approved definition for agent '{agent_type}'")
                continue

            result = await self.run_session(
                f"{agent_type}-{task_id[:8]}",
                system_message,
                f"Execute this task:\n\n{description}",
                model=task_model,
            )
            dispatched += 1

            # Validate
            success, reason = self._validate_task_result(result)

            if success:
                # Multi-model review if configured
                if REVIEW_MODELS:
                    review_passed = await self._multi_model_review(
                        task_id, agent_type, description, result
                    )
                    if not review_passed:
                        log(f"Tracked task {task_id[:8]} failed multi-model review", "WARN")
                        await RequestAPI.fail_task(task_id, "Failed multi-model review")
                        continue

                await RequestAPI.complete_task(task_id, result)
                log(f"Tracked task {task_id[:8]} completed ({len(result) if result else 0} chars)")
            else:
                await RequestAPI.fail_task(task_id, reason)
                log(f"Tracked task {task_id[:8]} failed: {reason}", "WARN")

    async def _multi_model_review(
        self, memory_id: str, agent_type: str, task_content: str, result: str
    ) -> bool:
        """Run the task result through multiple models for review.

        Each review model evaluates the result independently. All must approve
        for the review to pass. Returns True if all models approve.
        """
        review_prompt = (
            "You are reviewing work produced by an AI sub-agent. "
            "Evaluate the quality and correctness of the output."
            f"\n\nTASK THAT WAS ASSIGNED:\n{task_content[:2000]}"
            f"\n\nOUTPUT PRODUCED:\n{result[:4000]}"
            "\n\nEvaluate:\n"
            "1. Does the output actually address the task?\n"
            "2. Is the reasoning sound?\n"
            "3. Are there any errors, hallucinations, "
            "or problematic assumptions?\n"
            "4. Is the output actionable and useful?\n\n"
            "Respond with EXACTLY one of:\n"
            "- APPROVE: [brief reason] — if the work is good\n"
            "- REJECT: [brief reason] — if there are "
            "significant issues"
        )

        approvals = 0
        rejections = 0

        for review_model in REVIEW_MODELS:
            log(f"  Review of task {memory_id[:8]} by {review_model}...")
            review_result = await self.run_session(
                f"review-{memory_id[:8]}-{review_model.split('/')[-1][:10]}",
                "You are a quality reviewer. Be concise and decisive.",
                review_prompt,
                model=review_model,
            )

            if review_result and "APPROVE" in review_result.upper():
                approvals += 1
                log(f"  Review by {review_model}: APPROVED")
            else:
                rejections += 1
                reason = review_result[:200] if review_result else "no response"
                log(f"  Review by {review_model}: REJECTED — {reason}", "WARN")

        total = approvals + rejections
        if total == 0:
            log(f"No review models responded for task {memory_id[:8]}", "WARN")
            return True  # Don't block on review failures

        passed = rejections == 0
        log(
            f"Multi-model review for {memory_id[:8]}: "
            f"{approvals}/{total} approved "
            f"— {'PASSED' if passed else 'FAILED'}"
        )
        return passed

    # --- Autonomic Layer ---

    async def run_autonomic(self):
        """Run autonomic background task — memory maintenance.

        Runs every N cycles without cognitive involvement.
        """
        log("Running autonomic: memory maintenance")
        try:
            system_message = await build_subagent_prompt(
                "memory",
                (
                    "Quick memory maintenance pass — check for "
                    "obvious issues, fix what's straightforward."
                ),
                (
                    "This is an autonomic background task, "
                    "not a cognitive decision."
                ),
            )
        except AgentNotFoundError:
            log("No approved 'memory' agent — skipping autonomic maintenance", "WARN")
            return
        await self.run_session(
            "autonomic-maintenance",
            system_message,
            (
                "Quick maintenance scan. Search recent memories "
                "for duplicates, missing tags, or miscalibrated "
                "importance. Fix obvious issues. Only save a "
                "summary if you actually changed something."
            ),
        )

    async def run_learning_extraction(self):
        """Run autonomic learning extraction — process recent results into reusable lessons.

        Runs every LEARNING_INTERVAL cycles without cognitive involvement.
        """
        log("Running autonomic: learning extraction")
        try:
            system_message = await build_subagent_prompt(
                "reflection",
                (
                    "Learning extraction pass — process recent "
                    "daemon-results and feedback into "
                    "reusable lessons."
                ),
                (
                    "This is an autonomic background task. "
                    "Follow the learning-extraction skill "
                    "instructions."
                ),
            )
        except AgentNotFoundError:
            log("No approved 'reflection' agent — skipping learning extraction", "WARN")
            return

        await self.run_session(
            "autonomic-learning",
            system_message,
            (
                "Run the learning extraction pipeline from the learning-extraction skill. "
                "1. Search for memories tagged 'daemon-result' "
                "or 'rejection-lesson' or 'validated' that do "
                "NOT have the 'lesson-extracted' tag. "
                "2. For each candidate, classify the experience "
                "type and extract a transferable principle. "
                "3. Compare against existing 'lesson' tagged "
                "procedural memories — update if "
                "reinforcing/refining, create new if novel. "
                "4. Tag processed memories with 'lesson-extracted'. "
                "5. Save a brief summary of what was extracted. "
                "Only process the most recent 10 unprocessed "
                "memories per run. Skip trivial results."
            ),
        )

    # --- PG LISTEN for event-driven dispatch ---

    async def _setup_listen(self):
        """Establish a persistent PG connection for LISTEN/NOTIFY.

        Returns True if LISTEN is active, False if we'll rely on polling only.
        """
        import asyncpg

        if self._listen_conn and not self._listen_conn.is_closed():
            return True  # already connected

        try:
            self._listen_conn = await asyncpg.connect(DATABASE_URL)
            await self._listen_conn.add_listener("task_ready", self._on_task_ready)
            log("PG LISTEN established on 'task_ready' channel")
            return True
        except Exception as e:
            log(f"PG LISTEN setup failed (dispatch will use polling only): {e}", "WARN")
            self._listen_conn = None
            return False

    def _on_task_ready(self, conn, pid, channel, payload):
        """Callback when PG NOTIFY fires on task_ready channel."""
        self._task_ready.set()

    # --- Independent loop implementations ---

    async def _dispatch_loop(self):
        """Event-driven task dispatch loop.

        Wakes on PG NOTIFY signals or polls as a fallback.  Claims and
        executes pending tasks from the tracked-task queue.  Multiple
        instances can run this loop safely — claim_task is atomic.
        """
        log(f"Dispatch loop started (poll fallback: {DISPATCH_POLL_SECONDS}s)")
        await self._setup_listen()
        heartbeat_interval = 300  # 5 minutes
        last_heartbeat = 0

        while self.running:
            try:
                # Wait for a NOTIFY signal or polling timeout
                try:
                    await asyncio.wait_for(
                        self._task_ready.wait(), timeout=DISPATCH_POLL_SECONDS
                    )
                except asyncio.TimeoutError:
                    pass  # polling fallback — this is normal
                self._task_ready.clear()

                # Verify API key is still valid
                if not await _verify_api_key(MCP_API_KEY):
                    if not await _handle_auth_failure(self.instance_id):
                        log("Dispatch: no valid API key, retrying in 30s", "WARN")
                        await asyncio.sleep(30)
                        continue

                # Periodic heartbeat
                now = time.time()
                if now - last_heartbeat >= heartbeat_interval:
                    await self._update_heartbeat()
                    last_heartbeat = now

                # Release stale tasks from dead instances
                stale = await RequestAPI.release_stale(STALE_HEARTBEAT_MINUTES)
                if stale:
                    log(f"Released {stale} stale tracked tasks")

                # Dispatch all available tasks
                await self._dispatch_tracked_tasks()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"Dispatch loop error: {e}", "ERROR")
                await asyncio.sleep(5)
                # Reconnect LISTEN if connection dropped
                if self._listen_conn is None or self._listen_conn.is_closed():
                    await self._setup_listen()

    async def _cognitive_loop(self):
        """Periodic planning cycle — long-horizon goal assessment.

        Runs the cognitive LLM session on a fixed interval.  Creates
        requests/tasks for the dispatch loop to pick up.
        """
        log(f"Cognitive loop started (interval: {DAEMON_INTERVAL_MINUTES}m)")

        while self.running:
            try:
                await self.run_cognitive_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"Cognitive loop error: {e}", "ERROR")

            # Sleep until next cycle
            await asyncio.sleep(DAEMON_INTERVAL_MINUTES * 60)

    async def _scheduler_loop(self):
        """Check for due schedules and fire them.

        Runs on a short interval so schedules fire close to their target time.
        """
        log(f"Scheduler loop started (interval: {SCHEDULER_CHECK_SECONDS}s)")

        while self.running:
            try:
                # Verify API key
                if not await _verify_api_key(MCP_API_KEY):
                    if not await _handle_auth_failure(self.instance_id):
                        await asyncio.sleep(30)
                        continue

                await self._check_due_schedules()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"Scheduler loop error: {e}", "ERROR")

            await asyncio.sleep(SCHEDULER_CHECK_SECONDS)

    async def _autonomic_loop(self):
        """Periodic maintenance: memory consolidation, learning extraction.

        Runs on longer intervals since these are background housekeeping tasks.
        """
        log(
            f"Autonomic loop started (maintenance: {AUTONOMIC_MINUTES}m, "
            f"learning: {LEARNING_MINUTES}m)"
        )

        # Give cognitive loop a head start on first boot
        await asyncio.sleep(min(300, AUTONOMIC_MINUTES * 60))

        last_maintenance = time.time()
        last_learning = time.time()

        while self.running:
            try:
                now = time.time()

                if now - last_maintenance >= AUTONOMIC_MINUTES * 60:
                    # Verify API key
                    if await _verify_api_key(MCP_API_KEY) or await _handle_auth_failure(
                        self.instance_id
                    ):
                        await self.run_autonomic()
                        last_maintenance = now

                if now - last_learning >= LEARNING_MINUTES * 60:
                    if await _verify_api_key(MCP_API_KEY) or await _handle_auth_failure(
                        self.instance_id
                    ):
                        await self.run_learning_extraction()
                        last_learning = now

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"Autonomic loop error: {e}", "ERROR")

            await asyncio.sleep(60)  # check every minute

    async def _reload_watcher(self):
        """Periodically check for source file changes and trigger auto-reload."""
        while self.running:
            try:
                if self._should_reload():
                    log("Source files changed — restarting daemon to pick up new code")
                    self.running = False
                    self._restart_self()
                    return
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"Reload watcher error: {e}", "WARN")
            await asyncio.sleep(30)

    # --- Main Loops ---

    async def run_forever(self):
        """Run enabled daemon loops concurrently.

        Each role spawns an independent asyncio task:
          - dispatcher:  event-driven (PG LISTEN + polling fallback)
          - cognitive:   interval-based planning
          - scheduler:   short-interval schedule checks
          - autonomic:   long-interval maintenance
        """
        await self.start()
        self._source_mtimes = self._snapshot_source_files()

        log(f"Daemon roles enabled: {', '.join(sorted(self.roles))}")

        try:
            loops: list[asyncio.Task] = []

            if "dispatcher" in self.roles:
                loops.append(asyncio.create_task(self._dispatch_loop(), name="dispatch"))
            if "cognitive" in self.roles:
                loops.append(asyncio.create_task(self._cognitive_loop(), name="cognitive"))
            if "scheduler" in self.roles:
                loops.append(asyncio.create_task(self._scheduler_loop(), name="scheduler"))
            if "autonomic" in self.roles:
                loops.append(asyncio.create_task(self._autonomic_loop(), name="autonomic"))

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
            pass
        finally:
            await self.stop()

    def _snapshot_source_files(self) -> dict[str, float]:
        """Capture mtimes of all daemon source files."""
        files = {}
        watch_paths = [
            DAEMON_DIR / "daemon.py",
            COGNITIVE_PROMPT_PATH,
            AGENT_DEF_PATH,
        ]
        # Watch adaptation module if it exists
        adapt_path = DAEMON_DIR / "adaptation.py"
        if adapt_path.exists():
            watch_paths.append(adapt_path)

        for p in watch_paths:
            if p.exists():
                files[str(p)] = p.stat().st_mtime
        return files

    def _should_reload(self) -> bool:
        """Check if any watched source files have changed since startup."""
        current = self._snapshot_source_files()
        for path, mtime in current.items():
            old_mtime = self._source_mtimes.get(path)
            if old_mtime is None or mtime > old_mtime:
                log(f"File changed: {Path(path).name} (mtime {old_mtime} -> {mtime})")
                return True
        # Also check for new files that didn't exist at startup
        for path in current:
            if path not in self._source_mtimes:
                log(f"New file detected: {Path(path).name}")
                return True
        return False

    def _restart_self(self):
        """Replace the current process with a fresh one using the same args."""
        log("Executing self-restart...")
        # Flush logs
        sys.stdout.flush()
        sys.stderr.flush()
        # Re-exec with the same Python and arguments
        os.execv(sys.executable, [sys.executable] + sys.argv)

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
    get_logger_fn = _configure_daemon_logging()
    _logger = get_logger_fn("daemon")

    parser = argparse.ArgumentParser(description="Lucent Daemon — Cognitive Architecture")
    parser.add_argument("--once", action="store_true", help="Run one cognitive cycle and exit")
    parser.add_argument(
        "--task",
        type=str,
        help=(
            "Run a specific sub-agent "
            "(research, code, memory, reflection, "
            "documentation, planning)"
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
