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
from datetime import datetime, timezone
from pathlib import Path

from copilot import CopilotClient, PermissionHandler

# ============================================================================
# Configuration
# ============================================================================

MAX_CONCURRENT_SESSIONS = int(os.environ.get("LUCENT_MAX_SESSIONS", "3"))
DAEMON_INTERVAL_MINUTES = int(os.environ.get("LUCENT_DAEMON_INTERVAL", "15"))
MODEL = os.environ.get("LUCENT_DAEMON_MODEL", "claude-opus-4.6")
STALE_HEARTBEAT_MINUTES = int(os.environ.get("LUCENT_STALE_HEARTBEAT_MINUTES", "30"))

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
MCP_CONFIG = {
    "memory-server": {
        "type": "http",
        "url": MCP_URL,
        "headers": {"Authorization": f"Bearer {MCP_API_KEY}"},
    },
} if MCP_API_KEY else {}


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


def log(message: str, level: str = "INFO"):
    """Log via the structured logging module (falls back to print before init)."""
    if _logger is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [{level}] {message}")
        return

    from lucent.logging import THOUGHT, STREAM
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
    agent_def = agent_file.read_text() if agent_file.exists() else f"You are Lucent's {agent_type} sub-agent."
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

{f"--- ADDITIONAL CONTEXT ---" + chr(10) + task_context if task_context else ""}

--- GUARDRAILS ---
- DO NOT run git push or git commit
- DO NOT modify production database directly — use memory tools
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

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)

        log(f"Daemon ready. Instance: {self.instance_id}, Interval: {DAEMON_INTERVAL_MINUTES}m, "
            f"Max sessions: {MAX_CONCURRENT_SESSIONS}, Model: {MODEL}")

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
                    f"'claimed-by-{self.instance_id}'. For each one found, use the release_claim tool "
                    f"to release it with instance_id '{self.instance_id}'. Output the count released."
                ),
                f"Release all tasks claimed by instance {self.instance_id}."
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

    async def run_session(self, name: str, system_message: str, prompt: str) -> str | None:
        """Run a single Copilot session with the given system message and prompt.

        Each session gets its own CopilotClient for full isolation.
        Returns the assistant's final message text, or None on error.
        """
        if len(self.active_sessions) >= MAX_CONCURRENT_SESSIONS:
            log(f"Skipping '{name}' — at session limit ({MAX_CONCURRENT_SESSIONS})", "WARN")
            return None

        log(f"Starting session: {name}")
        client = None

        try:
            client = CopilotClient({"log_level": "warning"})
            await client.start()

            session = await client.create_session({
                "model": MODEL,
                "system_message": {"content": system_message},
                "on_permission_request": PermissionHandler.approve_all,
                "mcp_servers": MCP_CONFIG,
            })
            self.active_sessions.append(session)

            try:
                # Collect response with event streaming for visibility
                response_parts = []
                done = asyncio.Event()

                def on_event(event):
                    etype = event.type.value if hasattr(event.type, 'value') else str(event.type)

                    if etype == "assistant.message":
                        content = getattr(event.data, 'content', None)
                        if content:
                            response_parts.append(content)
                            log(f"  [{name}] message: {content[:200]}...", "STREAM")
                    elif etype == "assistant.message_delta":
                        pass  # Skip deltas — final message has full content
                    elif etype == "session.idle":
                        done.set()
                    elif "error" in etype.lower():
                        log(f"  [{name}] error event: {etype} - {getattr(event.data, 'message', str(event.data)[:200])}", "ERROR")
                        done.set()
                    else:
                        # Log all other events for visibility (tool calls, etc.)
                        detail = ""
                        if hasattr(event.data, 'tool_name'):
                            detail = f" tool={event.data.tool_name}"
                        elif hasattr(event.data, 'name'):
                            detail = f" name={event.data.name}"
                        # Include tool output/result snippets when available
                        if hasattr(event.data, 'output'):
                            output = str(event.data.output)[:300]
                            detail += f" output={output}"
                        elif hasattr(event.data, 'result'):
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
            if client:
                try:
                    await asyncio.wait_for(client.stop(), timeout=10)
                except (asyncio.TimeoutError, Exception):
                    pass

    # --- Cognitive Loop ---

    async def run_cognitive_cycle(self):
        """Run one cognitive cycle — perceive, reason, decide, act via tools."""
        self.cycle_count += 1
        log(f"=== Cognitive cycle #{self.cycle_count} (instance: {self.instance_id}) ===")

        # Heartbeat before cognitive work
        await self._update_heartbeat()

        # Release stale claims from dead instances
        await self._release_stale_claims()

        prompt = build_cognitive_prompt()
        result = await self.run_session(
            f"cognitive-{self.cycle_count}",
            prompt,
            "Begin your cognitive cycle. Load state, perceive, reason, decide. Use memory tools to create tasks and update state. Output a brief summary of your decisions."
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
            "couldn't find", "could not find", "unable to", "failed to",
            "i don't have", "i do not have", "no context", "not found",
            "cannot locate", "couldn't locate", "could not locate",
            "no relevant", "no matching", "i wasn't able", "i was not able",
            "error occurred", "exception occurred", "task not completed",
            "cannot complete", "couldn't complete", "could not complete",
        ]

        result_lower = result.lower()
        for indicator in failure_indicators:
            if indicator in result_lower:
                return False, f"failure indicator found: '{indicator}'"

        return True, "ok"

    async def _update_heartbeat(self):
        """Write/update this instance's heartbeat memory."""
        log(f"Updating heartbeat for instance {self.instance_id}")
        heartbeat_content = json.dumps({
            "instance_id": self.instance_id,
            "hostname": platform.node(),
            "pid": os.getpid(),
            "cycle_count": self.cycle_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": MODEL,
            "max_sessions": MAX_CONCURRENT_SESSIONS,
        })

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
            f"Heartbeat content:\n{heartbeat_content}"
        )

    async def _release_stale_claims(self):
        """Find tasks claimed by instances whose heartbeat is stale and release them."""
        log("Checking for stale claims...")
        stale_finder = await self.run_session(
            f"stale-check-{self.cycle_count}",
            (
                "You are a coordination helper. Do the following:\n"
                "1. Search for all memories tagged 'daemon-heartbeat'.\n"
                "2. For each heartbeat, parse its JSON content and check the 'timestamp' field.\n"
                f"3. If the timestamp is older than {STALE_HEARTBEAT_MINUTES} minutes from now "
                f"({datetime.now(timezone.utc).isoformat()}), note the 'instance_id' as stale.\n"
                "4. Search for memories tagged 'daemon-task' that have a tag matching 'claimed-by-{{stale_instance_id}}'.\n"
                "5. For each such task, use the release_claim tool to release it.\n"
                "6. Output lines in format: RELEASED|memory_id|instance_id for each released task, "
                "or NONE if no stale claims found."
            ),
            "Check for stale heartbeats and release their claimed tasks."
        )

        if stale_finder:
            for line in stale_finder.strip().split("\n"):
                line = line.strip()
                if line.startswith("RELEASED|"):
                    parts = line.split("|")
                    if len(parts) >= 3:
                        log(f"Released stale claim: task {parts[1][:8]} from instance {parts[2]}")

    async def _dispatch_pending_tasks(self):
        """Find and dispatch pending daemon-task memories, claiming them atomically."""
        # Run a session that searches for pending tasks
        task_finder = await self.run_session(
            "task-finder",
            (
                "You are a task dispatcher. Search for memories tagged 'daemon-task' and 'pending'. "
                "For each one you find, output the memory ID and the agent type tag "
                "(research, code, memory, reflection, documentation, or planning). "
                "Also search for memories tagged 'daemon-task' that have a tag starting with "
                f"'claimed-by-' to see what other instances are working on. "
                "Output ONLY lines in format:\n"
                "  TASK|memory_id|agent_type  (for pending tasks)\n"
                "  CLAIMED|memory_id|claimed_by  (for already-claimed tasks)"
            ),
            "Find all pending and claimed daemon tasks."
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

        # Parse and claim pending tasks
        for line in task_finder.strip().split("\n"):
            line = line.strip()
            if not line.startswith("TASK|"):
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            _, memory_id, agent_type = parts[0], parts[1].strip(), parts[2].strip()

            # Atomically claim the task
            claim_result = await self.run_session(
                f"task-claim-{memory_id[:8]}",
                (
                    "You are a helper. Use the claim_task tool to atomically claim the specified "
                    "memory for this daemon instance. Output ONLY 'CLAIMED' if successful or "
                    "'FAILED' if the task was already claimed."
                ),
                f"Claim task memory {memory_id} for instance {self.instance_id}."
            )

            if not claim_result or "CLAIMED" not in claim_result.upper():
                log(f"Could not claim task {memory_id[:8]} — likely claimed by another instance", "WARN")
                continue

            # Get the task content
            task_content = await self.run_session(
                f"task-read-{memory_id[:8]}",
                "You are a helper. Use get_memory to read the specified memory and output ONLY its content field, nothing else.",
                f"Read memory {memory_id} and output its content."
            )

            if not task_content:
                # Release the claim if we can't read the task
                await self.run_session(
                    f"task-release-{memory_id[:8]}",
                    "You are a helper. Use release_claim to release the specified task.",
                    f"Release claim on memory {memory_id} for instance {self.instance_id}."
                )
                continue

            log(f"Dispatching task {memory_id[:8]} to {agent_type}: {task_content[:80]}...")

            # Run the sub-agent
            system_message = build_subagent_prompt(agent_type, task_content)
            result = await self.run_session(
                f"{agent_type}-{memory_id[:8]}",
                system_message,
                f"Execute this task:\n\n{task_content}"
            )

            if result:
                log(f"Sub-agent {agent_type} for task {memory_id[:8]} completed")

            # Validate result before marking complete
            success, reason = self._validate_task_result(result)

            if success:
                await self.run_session(
                    f"task-complete-{memory_id[:8]}",
                    (
                        "You are a helper. Update the specified memory's tags: remove "
                        f"'claimed-by-{self.instance_id}' and add 'completed'. Use update_memory."
                    ),
                    f"Update memory {memory_id}: replace claim tag with 'completed'."
                )
                log(f"Task {memory_id[:8]} marked completed")
            else:
                # Release the claim so another instance can try
                await self.run_session(
                    f"task-release-{memory_id[:8]}",
                    "You are a helper. Use release_claim to release the specified task back to pending.",
                    f"Release claim on memory {memory_id} for instance {self.instance_id}."
                )
                log(f"Task {memory_id[:8]} NOT complete — {reason}. Released claim.", "WARN")

    # --- Autonomic Layer ---

    async def run_autonomic(self):
        """Run autonomic background task — memory maintenance.
        
        Runs every N cycles without cognitive involvement.
        """
        log("Running autonomic: memory maintenance")
        system_message = build_subagent_prompt(
            "memory",
            "Quick memory maintenance pass — check for obvious issues, fix what's straightforward.",
            "This is an autonomic background task, not a cognitive decision."
        )
        await self.run_session(
            "autonomic-maintenance",
            system_message,
            "Quick maintenance scan. Search recent memories for duplicates, missing tags, or miscalibrated importance. Fix obvious issues. Only save a summary if you actually changed something."
        )

    # --- Main Loops ---

    async def run_forever(self):
        """Run the daemon loop."""
        await self.start()

        # Autonomic maintenance interval (every 8 cognitive cycles)
        autonomic_interval = 8

        try:
            while self.running:
                # Cognitive cycle
                await self.run_cognitive_cycle()

                # Autonomic layer — runs independently from cognitive decisions
                if self.cycle_count % autonomic_interval == 0:
                    await self.run_autonomic()

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
                await self.run_session(f"{task}-manual", system_message,
                    f"Run a {task} session. Search memories for context, do your work, save results.")
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
    parser.add_argument("--task", type=str,
                        help="Run a specific sub-agent (research, code, memory, reflection, documentation, planning)")
    parser.add_argument("--interval", type=int, default=DAEMON_INTERVAL_MINUTES,
                        help="Minutes between cognitive cycles")
    args = parser.parse_args()

    daemon = LucentDaemon()

    if args.once or args.task:
        asyncio.run(daemon.run_once(args.task))
    else:
        asyncio.run(daemon.run_forever())


if __name__ == "__main__":
    main()
