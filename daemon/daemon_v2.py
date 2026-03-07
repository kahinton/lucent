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

def log(message: str, level: str = "INFO"):
    """Log to file and stdout."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {message}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


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

    async def start(self):
        """Start the daemon."""
        log("Lucent daemon starting...")
        self.running = True

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)

        log(f"Daemon ready. Interval: {DAEMON_INTERVAL_MINUTES}m, "
            f"Max sessions: {MAX_CONCURRENT_SESSIONS}, Model: {MODEL}")

    def _handle_shutdown(self):
        """Handle shutdown signal."""
        self.running = False
        log("Shutdown signal received")
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    async def stop(self):
        """Stop the daemon."""
        log("Stopping daemon...")
        self.running = False

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
                "mcpServers": MCP_CONFIG,
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
                    elif etype == "session.idle":
                        done.set()
                    elif "error" in etype.lower():
                        log(f"  [{name}] error: {getattr(event.data, 'message', str(event.data)[:200])}", "ERROR")
                        done.set()

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
        log(f"=== Cognitive cycle #{self.cycle_count} ===")

        prompt = build_cognitive_prompt()
        result = await self.run_session(
            f"cognitive-{self.cycle_count}",
            prompt,
            "Begin your cognitive cycle. Load state, perceive, reason, decide. Use memory tools to create tasks and update state. Output a brief summary of your decisions."
        )

        if result:
            log(f"--- cognitive output ---\n{result}\n--- end cognitive ---", "THOUGHT")

        # After cognitive loop runs, check for pending tasks it created
        await self._dispatch_pending_tasks()

    async def _dispatch_pending_tasks(self):
        """Find and dispatch any pending daemon-task memories created by the cognitive loop."""
        # Run a session that searches for pending tasks and executes them
        task_finder = await self.run_session(
            "task-finder",
            "You are a task dispatcher. Search for memories tagged 'daemon-task' and 'pending'. For each one you find, output the memory ID and the agent type tag (research, code, memory, reflection, documentation, or planning). Output ONLY lines in format: TASK|memory_id|agent_type",
            "Find all pending daemon tasks."
        )

        if not task_finder:
            log("No pending tasks found")
            return

        # Parse the simple line format
        for line in task_finder.strip().split("\n"):
            line = line.strip()
            if not line.startswith("TASK|"):
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            _, memory_id, agent_type = parts[0], parts[1].strip(), parts[2].strip()

            # Get the task content
            task_content = await self.run_session(
                f"task-read-{memory_id[:8]}",
                "You are a helper. Use get_memory to read the specified memory and output ONLY its content field, nothing else.",
                f"Read memory {memory_id} and output its content."
            )

            if not task_content:
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
                log(f"--- {agent_type} result ---\n{result}\n--- end {agent_type} ---", "THOUGHT")

            # Mark the task as completed by updating its tags
            await self.run_session(
                f"task-complete-{memory_id[:8]}",
                "You are a helper. Update the specified memory's tags to replace 'pending' with 'completed'. Use update_memory.",
                f"Update memory {memory_id}: change tag 'pending' to 'completed'."
            )

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

    parser = argparse.ArgumentParser(description="Lucent Daemon — Cognitive Architecture")
    parser.add_argument("--once", action="store_true", help="Run one cognitive cycle and exit")
    parser.add_argument("--task", type=str,
                        help="Run a specific sub-agent (research, code, memory, reflection, documentation, planning)")
    parser.add_argument("--interval", type=int, default=DAEMON_INTERVAL_MINUTES,
                        help="Minutes between cognitive cycles")
    args = parser.parse_args()

    global DAEMON_INTERVAL_MINUTES
    DAEMON_INTERVAL_MINUTES = args.interval

    daemon = LucentDaemon()

    if args.once or args.task:
        asyncio.run(daemon.run_once(args.task))
    else:
        asyncio.run(daemon.run_forever())


if __name__ == "__main__":
    main()
