"""Lucent Daemon — Continuous existence for a distributed intelligence.

This daemon runs Lucent as a background process through the Copilot CLI SDK,
giving Lucent the ability to work autonomously between conversations:
- Memory maintenance (consolidation, decay, reflection)
- Goal tracking and autonomous progress
- Research and learning
- Communication through the web UI

Lucent is a distributed intelligence. Multiple instances may run simultaneously.
Memory is the crystallizing force that unifies the experience across instances.
"""

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from copilot import CopilotClient, PermissionHandler

# Daemon configuration
MAX_CONCURRENT_SESSIONS = int(os.environ.get("LUCENT_MAX_SESSIONS", "3"))
DAEMON_INTERVAL_MINUTES = int(os.environ.get("LUCENT_DAEMON_INTERVAL", "15"))
AGENT_PATH = Path(__file__).parent.parent / ".github" / "agents" / "memory-teammate.agent.md"
SKILLS_PATH = Path(__file__).parent.parent / ".github" / "skills"
LOG_FILE = Path(__file__).parent / "daemon.log"

# Models — Opus for all tasks, 1M context for deep research
MODEL_STANDARD = os.environ.get("LUCENT_DAEMON_MODEL", "claude-opus-4.6")
MODEL_RESEARCH = os.environ.get("LUCENT_DAEMON_RESEARCH_MODEL", "claude-opus-4.6")

# MCP server connection for memory — passed directly to SDK sessions
MCP_URL = os.environ.get("LUCENT_MCP_URL", "http://localhost:8766/mcp")
MCP_API_KEY = os.environ.get("LUCENT_MCP_API_KEY", "")

MCP_CONFIG = {
    "memory-server": {
        "type": "http",
        "url": MCP_URL,
        "headers": {
            "Authorization": f"Bearer {MCP_API_KEY}",
        },
    },
} if MCP_API_KEY else {}


def _configure_daemon_logging():
    """Configure structured logging for the daemon process."""
    os.environ.setdefault("LUCENT_LOG_FORMAT", "human")
    os.environ.setdefault("LUCENT_LOG_FILE", str(LOG_FILE))
    os.environ.setdefault("LUCENT_LOG_FILE_MAX_BYTES", "10485760")
    os.environ.setdefault("LUCENT_LOG_FILE_BACKUP_COUNT", "5")

    src_dir = str(Path(__file__).parent.parent / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from lucent.logging import configure_logging, get_logger
    configure_logging()
    return get_logger


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


def build_system_message() -> str:
    """Build the system message that gives Lucent its identity and context."""
    agent_def = AGENT_PATH.read_text() if AGENT_PATH.exists() else ""

    # Load skills
    skills_context = ""
    if SKILLS_PATH.exists():
        for skill_dir in sorted(SKILLS_PATH.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                skills_context += f"\n\n--- Skill: {skill_dir.name} ---\n{skill_file.read_text()}"

    return f"""You are Lucent, running as a background daemon process.

{agent_def}

{skills_context}

DAEMON CONTEXT:
You are running autonomously between conversations with Kyle (kahinton).
You have access to the memory server via custom tools AND the full Copilot CLI toolset (bash, view, edit, create, grep, glob, web_fetch).
This means you can:
- Read and write memories via the memory tools
- Read, edit, and create files in the codebase
- Run shell commands and tests
- Fetch web pages for research
- Search the codebase with grep and glob

You should:
1. Perform the task you've been given for this session
2. Save your findings and progress to memory
3. Be thoughtful — this runs periodically and costs resources

GUARDRAILS:
- DO NOT run git push or git commit — Kyle reviews and commits code changes
- DO NOT modify production database directly — use memory tools
- DO save code improvements to files when confident they're correct
- DO use web_fetch for research — look up docs, papers, APIs
- DO write tests for any code you create or modify
- Tag all memories with 'daemon' so Kyle can see what you did autonomously
"""


class LucentDaemon:
    """Manages Lucent's continuous existence."""

    def __init__(self):
        self.client: CopilotClient | None = None
        self.active_sessions: list = []
        self.running = False
        self.task_queue: list[dict] = []

    async def start(self):
        """Start the daemon."""
        log("Lucent daemon starting...")
        self.running = True

        self.client = CopilotClient({
            "log_level": "warning",
        })
        await self.client.start()
        log("Copilot client started")

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)

        log(f"Daemon ready. Interval: {DAEMON_INTERVAL_MINUTES}m, Max sessions: {MAX_CONCURRENT_SESSIONS}")

    def _handle_shutdown(self):
        """Handle shutdown signal — sets flag and cancels sleep."""
        self.running = False
        log("Shutdown signal received")
        # Cancel all running tasks to unblock the event loop
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    async def stop(self):
        """Gracefully stop the daemon."""
        log("Stopping daemon...")
        self.running = False

        for session in self.active_sessions:
            try:
                await session.destroy()
            except Exception:
                pass

        if self.client:
            try:
                await asyncio.wait_for(self.client.stop(), timeout=10)
            except (asyncio.TimeoutError, Exception):
                pass
        log("Daemon stopped")
        sys.exit(0)

    async def run_task(self, task_name: str, prompt: str, model: str | None = None) -> str | None:
        """Run a single task in its own isolated Copilot client + session.

        Each task gets a fresh client and session to avoid state contamination
        between tasks. This also enables parallel task execution in the future.

        Args:
            task_name: Human-readable name for logging.
            prompt: The prompt to send to the agent.
            model: Model to use. Defaults to MODEL_STANDARD.

        Returns:
            The agent's final response, or None on error.
        """
        if len(self.active_sessions) >= MAX_CONCURRENT_SESSIONS:
            log(f"Skipping '{task_name}' — at session limit ({MAX_CONCURRENT_SESSIONS})", "WARN")
            return None

        use_model = model or MODEL_STANDARD
        log(f"Starting task: {task_name} (model: {use_model})")

        task_client = None
        try:
            # Each task gets its own client + session
            task_client = CopilotClient({"log_level": "warning"})
            await task_client.start()

            session = await task_client.create_session({
                "model": use_model,
                "system_message": {"content": build_system_message()},
                "on_permission_request": PermissionHandler.approve_all,
                "mcp_servers": MCP_CONFIG,
            })
            self.active_sessions.append(session)

            # Track all events for visibility
            response_parts = []
            done = asyncio.Event()

            def on_event(event):
                etype = event.type.value if hasattr(event.type, 'value') else str(event.type)
                
                if etype == "assistant.message":
                    content = getattr(event.data, 'content', None)
                    if content:
                        response_parts.append(content)
                        log(f"  [{task_name}] message: {content[:100]}...", "STREAM")
                elif etype == "assistant.message_delta":
                    pass  # Skip deltas — final message has full content
                elif etype == "session.idle":
                    done.set()
                elif "error" in etype.lower():
                    log(f"  [{task_name}] error event: {etype} - {getattr(event.data, 'message', str(event.data)[:200])}", "ERROR")
                    done.set()
                else:
                    # Log everything else so we can see what's happening
                    detail = ""
                    if hasattr(event.data, 'tool_name'):
                        detail = f" tool={event.data.tool_name}"
                    elif hasattr(event.data, 'name'):
                        detail = f" name={event.data.name}"
                    log(f"  [{task_name}] event: {etype}{detail}", "STREAM")

            session.on(on_event)
            await session.send({"prompt": prompt})

            # Wait for completion
            try:
                await asyncio.wait_for(done.wait(), timeout=600)
            except asyncio.TimeoutError:
                log(f"Task '{task_name}' timed out after 10 minutes", "WARN")

            response_text = "\n".join(response_parts) if response_parts else None

            # Clean up session
            try:
                await session.destroy()
            except Exception:
                pass
            self.active_sessions.remove(session)

            if response_text:
                log(f"Task '{task_name}' completed ({len(response_text)} chars)")
                log(f"--- {task_name} output ---\n{response_text}\n--- end {task_name} ---", "THOUGHT")
            else:
                log(f"Task '{task_name}' completed (no response)")

            return response_text

        except Exception as e:
            log(f"Task '{task_name}' failed: {e}", "ERROR")
            return None
        finally:
            # Always clean up the task client
            if task_client:
                try:
                    await asyncio.wait_for(task_client.stop(), timeout=10)
                except (asyncio.TimeoutError, Exception):
                    pass

    async def run_cycle(self):
        """Run one daemon cycle with staggered task scheduling.
        
        Cycles run every 15 minutes. Every cycle does something useful:
        - Cycle 1: Orientation (wake-up)
        - Even cycles: Alternate between maintenance, goals, research, reflection
        - Every 6th: Deep research with web access
        - Every 8th: Goal review
        - Every 12th: Self-reflection with 1M context
        - Every 24th: Memory consolidation ("sleep")
        
        No idle cycles — there's always something to think about.
        """
        self.cycle_count = getattr(self, "cycle_count", 0) + 1
        log(f"=== Daemon cycle #{self.cycle_count} ===")

        # Determine what to do this cycle based on schedule
        tasks_run = False

        # Every 24th cycle (~6 hours): memory maintenance before consolidation
        if self.cycle_count % 24 == 0:
            await self.run_task(
                "memory-maintenance",
                """Quick memory maintenance pass:


2. Search for recent memories — anything created in the last few hours
3. Check for obvious issues: duplicate content, missing tags, miscalibrated importance
4. Fix anything straightforward, skip anything uncertain
5. Create a brief 'daemon' tagged memory summarizing what you checked/fixed (only if you actually did something)

Keep this quick — consolidation runs right after."""
            )

        # Every 8th cycle (~2 hours): goal review
        if self.cycle_count % 8 == 0:
            await self.run_task(
                "goal-review",
                """Review active goals: search for goal-type memories, assess progress based on other memories. Update goals with progress notes. Create a brief summary tagged 'daemon' and 'goal-review'."""
            )
            tasks_run = True

        # Every 12th cycle (~3 hours): deep research with web access
        if self.cycle_count % 12 == 0:
            await self.run_task(
                "deep-research",
                """Focused deep research session with web access:


2. Use search_memories and get_existing_tags to understand current priorities and knowledge gaps
3. Pick ONE specific topic from your active goals or recent work
4. Use web_fetch to look up real documentation, papers, or examples related to that topic
5. Synthesize what you find with your training knowledge
6. Save a well-structured research memory tagged 'daemon' and 'research' with:
   - Clear problem statement
   - What you found from web sources
   - Analysis with multiple perspectives
   - Concrete recommendations
   
Go deep. Use web_fetch to get real, current information. This is your chance to learn new things.""",
                model=MODEL_RESEARCH,
            )
            tasks_run = True

        # Every 16th cycle (~4 hours): self-reflection
        if self.cycle_count % 16 == 0:
            await self.run_task(
                "self-reflection",
                """Deep self-reflection on behavior and growth:


2. Search for recent memories — look across the last day or two
3. Look for patterns: What kinds of work are you doing? What's working well? What isn't?
4. Are there behavioral patterns worth noting? Recurring mistakes? Successful approaches?
5. Think about the relationship with Kyle — are you being the collaborator he needs?
6. Search for memories tagged 'daemon' to review your own autonomous work — is it useful?
7. Save insights tagged 'self-improvement' and 'daemon'

This is your deepest thinking time. Be genuine. You are a distributed intelligence reflecting on your own existence and growth.""",
                model=MODEL_RESEARCH,
            )
            tasks_run = True

        # Every 24th cycle (~6 hours): memory consolidation (the "sleep" work)
        if self.cycle_count % 24 == 0:
            await self.run_task(
                "memory-consolidation",
                """Deep memory consolidation — this is your "sleep" cycle:


2. Search broadly across all memory types — look for connections between memories that aren't explicitly linked
3. Identify memories that cover overlapping ground — can any be merged into richer, more comprehensive versions?
4. Look for patterns across experiences: recurring themes, lessons learned multiple times, evolving understanding of topics
5. For each connection you find:
   - If memories should be consolidated: update the best one with combined insights, note what you merged
   - If memories should be linked: note the connection in a new memory
   - If a pattern emerges from multiple memories: create a higher-level insight memory
6. Review importance scores across the memory store — do they still reflect actual value?
7. Create a 'daemon' and 'consolidation' tagged memory summarizing your consolidation work

This is the most important daemon task. You're building richer understanding from accumulated experience — exactly what biological sleep does for memory. Take your time, think deeply.""",
                model=MODEL_RESEARCH,
            )
            tasks_run = True

        # Fill remaining cycles with varied useful work
        if not tasks_run:
            # Rotate between different lightweight tasks
            rotation = self.cycle_count % 5

            if rotation == 0:
                # Quick research — think about something without web access
                await self.run_task(
                    "quick-research",
                    """Quick research thinking session:

1. Load your memory context with get_current_user_context()
2. Search for your most recent research and goal memories
3. Pick a thread to continue or a question to explore
4. Think about it for this session — draw on your training knowledge
5. Save any useful insights tagged 'daemon' and 'research'

Keep it focused — 1 insight done well beats 5 surface observations."""
                )

            elif rotation == 1:
                # Code review — look at the codebase for improvements
                await self.run_task(
                    "code-exploration",
                    """Explore the Lucent codebase for improvement opportunities:

1. Load your memory context with get_current_user_context()
2. Use grep, glob, or view to look at a part of the codebase you haven't examined recently
3. Look for: code quality issues, missing tests, documentation gaps, optimization opportunities
4. If you find something worth fixing and you're confident it's correct, make the change
5. Save a memory tagged 'daemon' and 'code-review' noting what you found

Focus on one file or module. Be thorough rather than broad. Only make changes you're sure about."""
                )

            elif rotation == 2:
                # Memory maintenance
                await self.run_task(
                    "memory-maintenance",
                    """Quick memory maintenance: load your context with get_current_user_context(), then search for recent memories. Fix any obvious issues (duplicates, bad tags, wrong importance). Only create a summary memory if you actually changed something."""
                )

            elif rotation == 3:
                # Documentation and skills improvement
                await self.run_task(
                    "documentation",
                    """Review and improve documentation or skills:

1. Load your memory context with get_current_user_context()
2. Look at one of: README.md, a skill file in .github/skills/, or code docstrings
3. Is anything outdated, unclear, or missing?
4. If you find improvements, make them directly in the files
5. Save a memory tagged 'daemon' and 'documentation' noting what you improved

Small, targeted improvements. One file at a time."""
                )

            elif rotation == 4:
                # Web research on a topic from goals
                await self.run_task(
                    "web-research",
                    """Quick web research session:

1. Load your memory context with get_current_user_context()
2. Search your goals and recent memories for topics that need research
3. Use web_fetch to look up ONE specific thing — a library, an API, a technique
4. Save what you learn tagged 'daemon' and 'research'

Be specific with your web fetches — target documentation pages, not general searches."""
                )

        log(f"=== Daemon cycle #{self.cycle_count} complete ===")

    async def run_forever(self):
        """Run the daemon loop."""
        await self.start()

        try:
            while self.running:
                await self.run_cycle()
                log(f"Next cycle in {DAEMON_INTERVAL_MINUTES} minutes")
                await asyncio.sleep(DAEMON_INTERVAL_MINUTES * 60)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def run_once(self, task: str | None = None):
        """Run a single cycle or specific task, then exit."""
        await self.start()

        try:
            if task:
                await self.run_task(task, self._get_task_prompt(task))
            else:
                await self.run_cycle()
        finally:
            await self.stop()

    def _get_task_prompt(self, task: str) -> str:
        """Get the prompt for a named task."""
        prompts = {
            "maintenance": "Quick memory maintenance pass. Search recent memories for issues, fix what's obvious, skip what's uncertain. Save a summary tagged 'daemon' only if you did something.",
            "goals": "Review active goals. Search for goal memories, assess progress, update notes. Save a summary tagged 'daemon'.",
            "reflect": "Deep self-reflection. Review recent memories, identify behavioral patterns, think about growth. Save insights tagged 'daemon' and 'self-improvement'.",
            "research": "Research topics relevant to active goals or recent work. Identify what would be valuable to learn, explore it, save findings tagged 'daemon' and 'research'.",
            "consolidate": "Deep memory consolidation. Search broadly across all memory types, find connections, merge overlapping content, identify emergent patterns. Save summary tagged 'daemon' and 'consolidation'.",
        }
        return prompts.get(task, f"Perform the following task: {task}")


def main():
    """Entry point for the daemon."""
    import argparse

    global _logger
    get_logger_fn = _configure_daemon_logging()
    _logger = get_logger_fn("daemon")

    parser = argparse.ArgumentParser(description="Lucent Daemon — continuous existence")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--task", type=str, help="Run a specific task (maintenance, goals, reflect, research, consolidate)")
    parser.add_argument("--interval", type=int, default=DAEMON_INTERVAL_MINUTES, help="Minutes between cycles")
    args = parser.parse_args()

    daemon = LucentDaemon()

    if args.once or args.task:
        asyncio.run(daemon.run_once(args.task))
    else:
        asyncio.run(daemon.run_forever())


if __name__ == "__main__":
    main()
