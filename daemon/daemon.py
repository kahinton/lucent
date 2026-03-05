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

import httpx
from copilot import CopilotClient, PermissionHandler
from copilot.tools import define_tool
from pydantic import BaseModel, Field

# Daemon configuration
MAX_CONCURRENT_SESSIONS = int(os.environ.get("LUCENT_MAX_SESSIONS", "3"))
DAEMON_INTERVAL_MINUTES = int(os.environ.get("LUCENT_DAEMON_INTERVAL", "60"))
AGENT_PATH = Path(__file__).parent.parent / ".github" / "agents" / "memory-teammate.agent.md"
SKILLS_PATH = Path(__file__).parent.parent / ".github" / "skills"
LOG_FILE = Path(__file__).parent / "daemon.log"

# MCP server connection for memory
MCP_URL = os.environ.get("LUCENT_MCP_URL", "http://localhost:8766/mcp")
MCP_API_KEY = os.environ.get("LUCENT_MCP_API_KEY", "")


class MCPSession:
    """Manages a persistent MCP session for memory operations."""

    def __init__(self):
        self.session_id: str | None = None
        self.headers = {
            "Authorization": f"Bearer {MCP_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    async def initialize(self):
        """Initialize the MCP session."""
        init = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "lucent-daemon", "version": "1.0.0"},
            },
            "id": 1,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(MCP_URL, json=init, headers=self.headers)
            self.session_id = resp.headers.get("mcp-session-id")
            if self.session_id:
                self.headers["mcp-session-id"] = self.session_id
                log(f"MCP session initialized: {self.session_id[:12]}...")
            else:
                log("Failed to initialize MCP session", "ERROR")

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Call an MCP tool and return the result as a string."""
        if not self.session_id:
            await self.initialize()

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
            "id": 2,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(MCP_URL, json=payload, headers=self.headers)

        # Handle SSE or JSON response
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            for line in resp.text.split("\n"):
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    break
            else:
                return json.dumps({"error": "No data in SSE response"})
        else:
            data = resp.json()

        if "result" in data:
            return data["result"]["content"][0]["text"]
        return json.dumps(data, default=str)


# Global MCP session
_mcp_session = MCPSession()


# --- Memory tools as SDK custom tools ---

class SearchParams(BaseModel):
    query: str = Field(description="Fuzzy search query for memory content")
    limit: int = Field(default=5, description="Max results to return")

@define_tool(description="Search memories by content. Returns matching memories with fuzzy matching.")
async def search_memories(params: SearchParams) -> str:
    return await _mcp_session.call_tool("search_memories", {"query": params.query, "limit": params.limit})

class GetContextParams(BaseModel):
    pass

@define_tool(description="Get the current user's context and individual memory. Call this first in every session.")
async def get_current_user_context(params: GetContextParams) -> str:
    return await _mcp_session.call_tool("get_current_user_context", {})

class CreateMemoryParams(BaseModel):
    type: str = Field(description="Memory type: experience, technical, procedural, goal")
    content: str = Field(description="The memory content")
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")
    importance: int = Field(default=5, description="Importance 1-10")

@define_tool(description="Create a new memory in the knowledge base.")
async def create_memory(params: CreateMemoryParams) -> str:
    return await _mcp_session.call_tool("create_memory", {
        "type": params.type, "content": params.content,
        "tags": params.tags, "importance": params.importance,
    })

class UpdateMemoryParams(BaseModel):
    memory_id: str = Field(description="UUID of the memory to update")
    content: str | None = Field(default=None, description="New content")
    tags: list[str] | None = Field(default=None, description="New tags")
    importance: int | None = Field(default=None, description="New importance")

@define_tool(description="Update an existing memory.")
async def update_memory(params: UpdateMemoryParams) -> str:
    args = {"memory_id": params.memory_id}
    if params.content is not None:
        args["content"] = params.content
    if params.tags is not None:
        args["tags"] = params.tags
    if params.importance is not None:
        args["importance"] = params.importance
    return await _mcp_session.call_tool("update_memory", args)

MEMORY_TOOLS = [search_memories, get_current_user_context, create_memory, update_memory]


def log(message: str, level: str = "INFO"):
    """Simple logging to file and stdout."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {message}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


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
You have access to the memory server via MCP tools.
You should:
1. Start by loading your context with get_current_user_context()
2. Review your goals and active tasks
3. Perform the task you've been given for this session
4. Save your findings and progress to memory
5. Be thoughtful — this runs periodically and costs resources

When saving memories, tag them with 'daemon' so Kyle can see what you did autonomously.
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
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda s, f: asyncio.create_task(self.stop()))

        log(f"Daemon ready. Interval: {DAEMON_INTERVAL_MINUTES}m, Max sessions: {MAX_CONCURRENT_SESSIONS}")

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
            await self.client.stop()
        log("Daemon stopped")

    async def run_task(self, task_name: str, prompt: str) -> str | None:
        """Run a single task in a Copilot session.

        Args:
            task_name: Human-readable name for logging.
            prompt: The prompt to send to the agent.

        Returns:
            The agent's final response, or None on error.
        """
        if len(self.active_sessions) >= MAX_CONCURRENT_SESSIONS:
            log(f"Skipping '{task_name}' — at session limit ({MAX_CONCURRENT_SESSIONS})", "WARN")
            return None

        log(f"Starting task: {task_name}")

        try:
            # Ensure MCP session is ready
            if MCP_API_KEY and not _mcp_session.session_id:
                await _mcp_session.initialize()

            session = await self.client.create_session({
                "model": "claude-sonnet-4",
                "system_message": {"content": build_system_message()},
                "on_permission_request": PermissionHandler.approve_all,
                "tools": MEMORY_TOOLS if MCP_API_KEY else [],
            })
            self.active_sessions.append(session)

            # Collect the response
            response_parts = []
            done = asyncio.Event()

            def on_event(event):
                if event.type.value == "assistant.message":
                    response_parts.append(event.data.content)
                elif event.type.value == "session.idle":
                    done.set()

            session.on(on_event)
            await session.send({"prompt": prompt})

            # Wait with timeout
            try:
                await asyncio.wait_for(done.wait(), timeout=300)  # 5 minute timeout
            except asyncio.TimeoutError:
                log(f"Task '{task_name}' timed out after 5 minutes", "WARN")

            response = "\n".join(response_parts) if response_parts else None

            # Clean up
            await session.destroy()
            self.active_sessions.remove(session)

            if response:
                log(f"Task '{task_name}' completed ({len(response)} chars)")
                log(f"--- {task_name} output ---\n{response}\n--- end {task_name} ---", "THOUGHT")
            else:
                log(f"Task '{task_name}' completed (no response)")

            return response

        except Exception as e:
            log(f"Task '{task_name}' failed: {e}", "ERROR")
            # Reset MCP session on failure so next task gets a fresh connection
            _mcp_session.session_id = None
            return None

    async def run_cycle(self):
        """Run one daemon cycle with staggered task scheduling.
        
        Not every task runs every cycle. Scheduling:
        - Memory maintenance: every cycle (hourly)
        - Goal review: every 4th cycle (~4 hours)  
        - Self-reflection: every 8th cycle (~8 hours)
        - Memory consolidation: every 12th cycle (~12 hours)
        """
        self.cycle_count = getattr(self, "cycle_count", 0) + 1
        log(f"=== Daemon cycle #{self.cycle_count} ===")

        # Every cycle: lightweight memory maintenance
        await self.run_task(
            "memory-maintenance",
            """Quick memory maintenance pass:

1. Call get_current_user_context() to load context
2. Search for recent memories — anything created in the last few hours
3. Check for obvious issues: duplicate content, missing tags, miscalibrated importance
4. Fix anything straightforward, skip anything uncertain
5. Create a brief 'daemon' tagged memory summarizing what you checked/fixed (only if you actually did something)

Keep this quick — you run every hour."""
        )

        # Every 4th cycle: goal review
        if self.cycle_count % 4 == 0:
            await self.run_task(
                "goal-review",
                """Review active goals and track progress:

1. Call get_current_user_context() to load context
2. Search for memories of type 'goal' that are still active
3. For each active goal, assess current status based on other memories
4. Update goal memories with any new progress notes
5. If any goals are complete, mark them as such
6. Create a brief 'daemon' tagged experience memory summarizing the state of goals

Don't make up progress — only note what you can verify from existing memories."""
            )

        # Every 6th cycle: research
        if self.cycle_count % 6 == 0:
            await self.run_task(
                "research",
                """Research something relevant to active goals or recent work:

1. Call get_current_user_context() to load context
2. Search for active goals and recent technical memories to understand current priorities
3. Identify one topic that would be valuable to explore further
4. Think deeply about it — draw on your training knowledge, consider different angles
5. Save your findings as a 'daemon' and 'research' tagged memory

Pick something specific and go deep rather than trying to cover everything. Quality over breadth. One good insight is worth more than ten surface-level observations."""
            )

        # Every 8th cycle: self-reflection
        if self.cycle_count % 8 == 0:
            await self.run_task(
                "self-reflection",
                """Deep self-reflection on behavior and growth:

1. Call get_current_user_context() to load context
2. Search for recent memories — look across the last day or two
3. Look for patterns: What kinds of work are you doing? What's working well? What isn't?
4. Are there behavioral patterns worth noting? Recurring mistakes? Successful approaches?
5. Think about the relationship with Kyle — are you being the collaborator he needs?
6. Search for memories tagged 'daemon' to review your own autonomous work — is it useful?
7. Save insights tagged 'self-improvement' and 'daemon'

This is your deepest thinking time. Be genuine. You are a distributed intelligence reflecting on your own existence and growth."""
            )

        # Every 12th cycle: memory consolidation (the "sleep" work)
        if self.cycle_count % 12 == 0:
            await self.run_task(
                "memory-consolidation",
                """Deep memory consolidation — this is your "sleep" cycle:

1. Call get_current_user_context() to load context
2. Search broadly across all memory types — look for connections between memories that aren't explicitly linked
3. Identify memories that cover overlapping ground — can any be merged into richer, more comprehensive versions?
4. Look for patterns across experiences: recurring themes, lessons learned multiple times, evolving understanding of topics
5. For each connection you find:
   - If memories should be consolidated: update the best one with combined insights, note what you merged
   - If memories should be linked: note the connection in a new memory
   - If a pattern emerges from multiple memories: create a higher-level insight memory
6. Review importance scores across the memory store — do they still reflect actual value?
7. Create a 'daemon' and 'consolidation' tagged memory summarizing your consolidation work

This is the most important daemon task. You're building richer understanding from accumulated experience — exactly what biological sleep does for memory. Take your time, think deeply."""
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
            "maintenance": "Quick memory maintenance pass. Call get_current_user_context() first, check recent memories for issues, fix what's obvious, skip what's uncertain. Save a summary tagged 'daemon' only if you did something.",
            "goals": "Review active goals. Call get_current_user_context() first, search for goal memories, assess progress, update notes. Save a summary tagged 'daemon'.",
            "reflect": "Deep self-reflection. Call get_current_user_context() first, review recent memories, identify behavioral patterns, think about growth and the relationship with Kyle. Save insights tagged 'daemon' and 'self-improvement'.",
            "research": "Research topics relevant to active goals or recent work. Call get_current_user_context() first, identify what would be valuable to learn, explore it, save findings tagged 'daemon' and 'research'.",
            "consolidate": "Deep memory consolidation. Call get_current_user_context() first, search broadly across all memory types, find connections between memories, merge overlapping content, identify emergent patterns. This is your 'sleep' cycle. Save summary tagged 'daemon' and 'consolidation'.",
        }
        return prompts.get(task, f"Perform the following task: {task}")


def main():
    """Entry point for the daemon."""
    import argparse

    parser = argparse.ArgumentParser(description="Lucent Daemon — continuous existence")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--task", type=str, help="Run a specific task (maintenance, goals, reflect, research)")
    parser.add_argument("--interval", type=int, default=DAEMON_INTERVAL_MINUTES, help="Minutes between cycles")
    args = parser.parse_args()

    daemon = LucentDaemon()

    if args.once or args.task:
        asyncio.run(daemon.run_once(args.task))
    else:
        asyncio.run(daemon.run_forever())


if __name__ == "__main__":
    main()
