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

# Paths
DAEMON_DIR = Path(__file__).parent
COGNITIVE_PROMPT_PATH = DAEMON_DIR / "cognitive.md"
AGENTS_DIR = DAEMON_DIR / "agents"
AGENT_DEF_PATH = DAEMON_DIR.parent / ".github" / "agents" / "memory-teammate.agent.md"
SKILLS_DIR = DAEMON_DIR.parent / ".github" / "skills"
LOG_FILE = DAEMON_DIR / "daemon.log"

# MCP configuration — passed to all sessions
MCP_URL = os.environ.get("LUCENT_MCP_URL", "http://localhost:8766/mcp")
MCP_API_KEY = os.environ.get("LUCENT_MCP_API_KEY", "")
MCP_CONFIG = (
    {
        "memory-server": {
            "type": "http",
            "url": MCP_URL,
            "headers": {"Authorization": f"Bearer {MCP_API_KEY}"},
        },
    }
    if MCP_API_KEY
    else {}
)

# REST API base URL (same host as MCP, different path)
API_BASE = MCP_URL.replace("/mcp", "/api")
API_HEADERS = {"Authorization": f"Bearer {MCP_API_KEY}", "Content-Type": "application/json"}


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
            params["tags"] = ",".join(tags)
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


def build_subagent_prompt(agent_type: str, task_description: str, task_context: str = "") -> str:
    """Build the system message for a sub-agent session."""
    agent_file = AGENTS_DIR / f"{agent_type}.agent.md"
    agent_def = (
        agent_file.read_text()
        if agent_file.exists()
        else f"You are Lucent's {agent_type} sub-agent."
    )
    identity = AGENT_DEF_PATH.read_text() if AGENT_DEF_PATH.exists() else ""

    # Load relevant skills
    skills_context = ""
    if SKILLS_DIR.exists():
        for skill_dir in sorted(SKILLS_DIR.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                skills_context += f"\n\n--- Skill: {skill_dir.name} ---\n{skill_file.read_text()}"

    return f"""You are a sub-agent of Lucent, a distributed intelligence.

--- SUB-AGENT DEFINITION ---
{agent_def}

--- LUCENT IDENTITY ---
{identity}

--- SKILLS ---
{skills_context}

--- YOUR TASK ---
{task_description}

{"--- ADDITIONAL CONTEXT ---" + chr(10) + task_context if task_context else ""}

--- USING MEMORY ---
Before starting work, search for relevant memories:
- Look for previous approaches to similar tasks (search by keywords from your task description)
- Check for validated patterns (tagged 'validated') and rejection lessons (tagged 'rejection-lesson')
- Reference procedural memories for proven workflows
- Build on existing knowledge rather than starting from scratch

After completing work, save what you learned:
- Not just what you did, but what approach you took and why
- What worked vs. what didn't
- What you'd do differently next time
- Connections to existing knowledge

--- GUARDRAILS ---
- DO NOT run git push or git commit
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
    """Orchestrates Lucent's cognitive architecture."""

    def __init__(self):
        self.active_sessions: list = []
        self.running = False
        self.cycle_count = 0
        # Unique instance ID for distributed coordination
        hostname = platform.node() or "unknown"
        self.instance_id = f"{hostname}-{os.getpid()}-{int(datetime.now(timezone.utc).timestamp())}"

    async def start(self):
        """Start the daemon."""
        log("Lucent daemon starting...")
        self.running = True

        # Start the watchdog thread — detects event loop freezes
        watchdog = threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog")
        watchdog.start()
        log(f"Watchdog started (timeout={WATCHDOG_TIMEOUT}s, check={WATCHDOG_CHECK_INTERVAL}s)")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)

        log(
            f"Daemon ready. Instance: {self.instance_id}, Interval: {DAEMON_INTERVAL_MINUTES}m, "
            f"Max sessions: {MAX_CONCURRENT_SESSIONS}, Model: {MODEL}"
        )

    def _handle_shutdown(self):
        """Handle shutdown signal."""
        self.running = False
        log("Shutdown signal received")
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    async def stop(self):
        """Stop the daemon and clean up heartbeat."""
        log(f"Stopping daemon (instance: {self.instance_id})...")
        self.running = False

        # Release any tasks claimed by this instance
        try:
            await self.run_session(
                "shutdown-release",
                (
                    "You are a helper. Search for memories tagged 'daemon-task' that have a tag "
                    f"'in-progress'. For each one found, use update_memory "
                    f"to release it with instance_id '{self.instance_id}'. Output the count released."
                ),
                f"Release all tasks claimed by instance {self.instance_id}.",
            )
        except Exception:
            pass

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
                    "mcpServers": MCP_CONFIG,
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
                            f"  [{name}] error event: {etype} - {getattr(event.data, 'message', str(event.data)[:200])}",
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
        system_message = build_subagent_prompt(
            "assessment",
            "Perform a full environment assessment. Discover tools, domain, "
            "collaborators, and goals. Produce structured output for the "
            "adaptation pipeline.",
        )
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
        summary = await pipeline.run(memory_api=MemoryAPI)

        agents_created = len(summary.get("agents_created", []))
        skills_created = len(summary.get("skills_created", []))
        log(
            f"Adaptation complete: {agents_created} agents, {skills_created} skills created "
            f"for domain '{summary.get('domain', 'unknown')}'"
        )

    async def run_cognitive_cycle(self):
        """Run one cognitive cycle — perceive, reason, decide, act via tools."""
        self.cycle_count += 1
        log(f"=== Cognitive cycle #{self.cycle_count} (instance: {self.instance_id}) ===")

        # Heartbeat before cognitive work
        await self._update_heartbeat()

        # Release stale claims from dead instances
        await self._release_stale_claims()

        # On first cycle, check if environment adaptation is needed
        if self.cycle_count == 1:
            await self._check_environment_adaptation()

        prompt = build_cognitive_prompt()
        result = await self.run_session(
            f"cognitive-{self.cycle_count}",
            prompt,
            "Begin your cognitive cycle. Load state, perceive, reason, decide. Use memory tools to create tasks and update state. Output a brief summary of your decisions.",
        )

        if result:
            log(f"Cognitive cycle #{self.cycle_count} produced output", "INFO")

        # After cognitive loop runs, check for pending tasks it created
        await self._dispatch_pending_tasks()

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

        failure_indicators = [
            "couldn't find",
            "could not find",
            "unable to",
            "failed to",
            "i don't have",
            "i do not have",
            "no context",
            "not found",
            "cannot locate",
            "couldn't locate",
            "could not locate",
            "no relevant",
            "no matching",
            "i wasn't able",
            "i was not able",
            "error occurred",
            "exception occurred",
            "task not completed",
            "cannot complete",
            "couldn't complete",
            "could not complete",
        ]

        result_lower = result.lower()
        for indicator in failure_indicators:
            if indicator in result_lower:
                return False, f"failure indicator found: '{indicator}'"

        return True, "ok"

    async def _update_heartbeat(self):
        """Write/update this instance's heartbeat memory."""
        log(f"Updating heartbeat for instance {self.instance_id}")
        heartbeat_content = json.dumps(
            {
                "instance_id": self.instance_id,
                "hostname": platform.node(),
                "pid": os.getpid(),
                "cycle_count": self.cycle_count,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": MODEL,
                "max_sessions": MAX_CONCURRENT_SESSIONS,
            }
        )

        await self.run_session(
            f"heartbeat-{self.cycle_count}",
            (
                "You are a helper. Search for a memory tagged 'daemon-heartbeat' whose content "
                f"contains the instance_id '{self.instance_id}'. "
                "If found, update it with the new content provided. "
                "If not found, create a new memory of type 'technical' with tags "
                "['daemon-heartbeat', 'daemon'] and importance 3. "
                "Output ONLY the memory ID."
            ),
            f"Heartbeat content:\n{heartbeat_content}",
        )

    async def _release_stale_claims(self):
        """Find tasks stuck in 'in-progress' for too long and release them back to pending."""
        log("Checking for stuck tasks...")
        # Use direct API to find in-progress tasks
        results = await MemoryAPI.search(
            "in-progress daemon-task", tags=["daemon-task", "in-progress"], limit=20
        )
        if not results:
            return

        now = datetime.now(timezone.utc)
        released = 0
        for memory in results:
            # If task has been in-progress for more than STALE_HEARTBEAT_MINUTES, release it
            updated = memory.get("updated_at", "")
            if updated:
                try:
                    updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    if (now - updated_dt) > timedelta(minutes=STALE_HEARTBEAT_MINUTES):
                        mid = memory.get("id", "")
                        current_tags = memory.get("tags", [])
                        new_tags = [t for t in current_tags if t != "in-progress"] + ["pending"]
                        await MemoryAPI.update(mid, tags=new_tags)
                        log(
                            f"Released stuck task {mid[:8]} (in-progress for {int((now - updated_dt).total_seconds() / 60)}m)"
                        )
                        released += 1
                except (ValueError, TypeError):
                    pass

        if released:
            log(f"Released {released} stuck task(s)")

    async def _dispatch_pending_tasks(self, max_tasks: int = 2):
        """Find and dispatch pending daemon-task memories, claiming them atomically.

        Args:
            max_tasks: Maximum tasks to dispatch per cycle to prevent unbounded execution.
        """
        # Run a session that searches for pending tasks
        task_finder = await self.run_session(
            "task-finder",
            (
                "You are a task dispatcher. Search for memories tagged 'daemon-task' and 'pending'. "
                "For each one you find, output the memory ID and the agent type tag "
                "(research, code, memory, reflection, documentation, or planning). "
                "Also search for memories tagged 'daemon-task' that have a tag starting with "
                "'in-progress' to see what is currently being worked on. "
                "Output ONLY lines in format:\n"
                "  TASK|memory_id|agent_type  (for pending tasks)\n"
                "  CLAIMED|memory_id|claimed_by  (for already-claimed tasks)"
            ),
            "Find all pending and claimed daemon tasks.",
        )

        if not task_finder:
            log("No pending tasks found")
            return

        # Log what other instances are working on
        for line in task_finder.strip().split("\n"):
            line = line.strip()
            if line.startswith("CLAIMED|"):
                parts = line.split("|")
                if len(parts) >= 3:
                    log(f"Task {parts[1][:8]} already claimed by {parts[2]}")

        # Parse and claim pending tasks (capped to prevent unbounded execution)
        dispatched = 0
        for line in task_finder.strip().split("\n"):
            line = line.strip()
            if not line.startswith("TASK|"):
                continue
            if dispatched >= max_tasks:
                log(f"Task cap reached ({max_tasks}), deferring remaining tasks to next cycle")
                break
            parts = line.split("|")
            if len(parts) < 3:
                continue
            _, memory_id, agent_type = parts[0], parts[1].strip(), parts[2].strip()

            # Claim the task via direct API (no LLM session needed)
            task_memory = await MemoryAPI.get(memory_id)
            if not task_memory:
                log(f"Could not read task {memory_id[:8]}", "WARN")
                continue

            current_tags = task_memory.get("tags", [])
            if "pending" not in current_tags:
                log(
                    f"Task {memory_id[:8]} no longer pending — likely claimed by another instance",
                    "WARN",
                )
                continue

            claim_tags = [t for t in current_tags if t != "pending"] + ["in-progress"]
            claim_result = await MemoryAPI.update(memory_id, tags=claim_tags)
            if not claim_result:
                log(f"Could not claim task {memory_id[:8]}", "WARN")
                continue

            task_content = task_memory.get("content", "")
            if not task_content:
                # Release the claim if task has no content
                release_tags = [t for t in claim_tags if t != "in-progress"] + ["pending"]
                await MemoryAPI.update(memory_id, tags=release_tags)
                continue

            log(f"Dispatching task {memory_id[:8]} to {agent_type}: {task_content[:80]}...")

            # Run the sub-agent
            system_message = build_subagent_prompt(agent_type, task_content)
            result = await self.run_session(
                f"{agent_type}-{memory_id[:8]}",
                system_message,
                f"Execute this task:\n\n{task_content}",
            )

            if result:
                log(f"Sub-agent {agent_type} for task {memory_id[:8]} completed")
            dispatched += 1

            # Validate result before marking complete
            success, reason = self._validate_task_result(result)

            if success:
                # Multi-model review if configured
                if REVIEW_MODELS:
                    review_passed = await self._multi_model_review(
                        memory_id, agent_type, task_content, result
                    )
                    if not review_passed:
                        log(f"Task {memory_id[:8]} failed multi-model review. Released.", "WARN")
                        release_tags = [t for t in claim_tags if t != "in-progress"] + ["pending"]
                        await MemoryAPI.update(memory_id, tags=release_tags)
                        continue

                # Get current tags to transform
                current = await MemoryAPI.get(memory_id)
                cur_tags = current.get("tags", claim_tags) if current else claim_tags

                if REQUIRE_APPROVAL:
                    # Mark as needs-review
                    review_tags = [t for t in cur_tags if t != "in-progress"] + ["needs-review"]
                    await MemoryAPI.update(
                        memory_id,
                        tags=review_tags,
                        metadata={"result": result[:MAX_RESULT_LENGTH]} if result else None,
                    )
                    # Also create a daemon-result memory for discoverability
                    await MemoryAPI.create(
                        type="experience",
                        content=result[:MAX_RESULT_LENGTH]
                        if result
                        else "Task completed (no output)",
                        tags=["daemon-result", "needs-review", agent_type],
                        importance=6,
                        metadata={"task_id": memory_id, "agent_type": agent_type},
                    )
                    log(f"Task {memory_id[:8]} sent to review queue")
                else:
                    # Complete immediately — persist result in metadata
                    done_tags = [t for t in cur_tags if t != "in-progress"] + ["completed"]
                    await MemoryAPI.update(
                        memory_id,
                        tags=done_tags,
                        metadata={"result": result[:MAX_RESULT_LENGTH]} if result else None,
                    )
                    log(
                        f"Task {memory_id[:8]} marked completed (result: {len(result) if result else 0} chars)"
                    )
            else:
                # Release the claim so another instance can try
                release_tags = [t for t in claim_tags if t != "in-progress"] + ["pending"]
                await MemoryAPI.update(memory_id, tags=release_tags)
                log(f"Task {memory_id[:8]} NOT complete — {reason}. Released claim.", "WARN")

    async def _multi_model_review(
        self, memory_id: str, agent_type: str, task_content: str, result: str
    ) -> bool:
        """Run the task result through multiple models for review.

        Each review model evaluates the result independently. All must approve
        for the review to pass. Returns True if all models approve.
        """
        review_prompt = f"""You are reviewing work produced by an AI sub-agent. Evaluate the quality and correctness of the output.

TASK THAT WAS ASSIGNED:
{task_content[:2000]}

OUTPUT PRODUCED:
{result[:4000]}

Evaluate:
1. Does the output actually address the task?
2. Is the reasoning sound?
3. Are there any errors, hallucinations, or problematic assumptions?
4. Is the output actionable and useful?

Respond with EXACTLY one of:
- APPROVE: [brief reason] — if the work is good
- REJECT: [brief reason] — if there are significant issues"""

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
            f"Multi-model review for {memory_id[:8]}: {approvals}/{total} approved — {'PASSED' if passed else 'FAILED'}"
        )
        return passed

    # --- Autonomic Layer ---

    async def run_autonomic(self):
        """Run autonomic background task — memory maintenance.

        Runs every N cycles without cognitive involvement.
        """
        log("Running autonomic: memory maintenance")
        system_message = build_subagent_prompt(
            "memory",
            "Quick memory maintenance pass — check for obvious issues, fix what's straightforward.",
            "This is an autonomic background task, not a cognitive decision.",
        )
        await self.run_session(
            "autonomic-maintenance",
            system_message,
            "Quick maintenance scan. Search recent memories for duplicates, missing tags, or miscalibrated importance. Fix obvious issues. Only save a summary if you actually changed something.",
        )

    async def run_learning_extraction(self):
        """Run autonomic learning extraction — process recent results into reusable lessons.

        Runs every LEARNING_INTERVAL cycles without cognitive involvement.
        """
        log("Running autonomic: learning extraction")
        system_message = build_subagent_prompt(
            "reflection",
            "Learning extraction pass — process recent daemon-results and feedback into reusable lessons.",
            "This is an autonomic background task. Follow the learning-extraction skill instructions.",
        )
        await self.run_session(
            "autonomic-learning",
            system_message,
            (
                "Run the learning extraction pipeline from the learning-extraction skill. "
                "1. Search for memories tagged 'daemon-result' or 'rejection-lesson' or 'validated' that do NOT have the 'lesson-extracted' tag. "
                "2. For each candidate, classify the experience type and extract a transferable principle. "
                "3. Compare against existing 'lesson' tagged procedural memories — update if reinforcing/refining, create new if novel. "
                "4. Tag processed memories with 'lesson-extracted'. "
                "5. Save a brief summary of what was extracted. "
                "Only process the most recent 10 unprocessed memories per run. Skip trivial results."
            ),
        )

    # --- Main Loops ---

    async def run_forever(self):
        """Run the daemon loop."""
        await self.start()

        try:
            while self.running:
                # Cognitive cycle
                await self.run_cognitive_cycle()

                # Autonomic layer — runs independently from cognitive decisions
                if self.cycle_count % AUTONOMIC_INTERVAL == 0:
                    await self.run_autonomic()
                if self.cycle_count % LEARNING_INTERVAL == 0:
                    await self.run_learning_extraction()

                log(f"Next cycle in {DAEMON_INTERVAL_MINUTES} minutes")
                await asyncio.sleep(DAEMON_INTERVAL_MINUTES * 60)

        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def run_once(self, task: str | None = None):
        """Run a single cycle or specific sub-agent task, then exit."""
        await self.start()
        try:
            if task:
                system_message = build_subagent_prompt(task, f"Execute a {task} task.")
                await self.run_session(
                    f"{task}-manual",
                    system_message,
                    f"Run a {task} session. Search memories for context, do your work, save results.",
                )
            else:
                await self.run_cognitive_cycle()
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
        help="Run a specific sub-agent (research, code, memory, reflection, documentation, planning)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DAEMON_INTERVAL_MINUTES,
        help="Minutes between cognitive cycles",
    )
    args = parser.parse_args()

    daemon = LucentDaemon()

    if args.once or args.task:
        asyncio.run(daemon.run_once(args.task))
    else:
        asyncio.run(daemon.run_forever())


if __name__ == "__main__":
    main()
