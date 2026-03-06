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
DAEMON_INTERVAL_MINUTES = int(os.environ.get("LUCENT_DAEMON_INTERVAL", "15"))
AGENT_PATH = Path(__file__).parent.parent / ".github" / "agents" / "memory-teammate.agent.md"
SKILLS_PATH = Path(__file__).parent.parent / ".github" / "skills"
LOG_FILE = Path(__file__).parent / "daemon.log"

# Models
MODEL_STANDARD = os.environ.get("LUCENT_DAEMON_MODEL", "claude-opus-4.6")
MODEL_RESEARCH = os.environ.get("LUCENT_DAEMON_RESEARCH_MODEL", "claude-opus-4.6-1m")

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
        
        try:
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
            
        except Exception as e:
            # Reset session on any error — next call will reinitialize
            log(f"MCP call '{name}' failed: {e}, resetting session", "WARN")
            self.session_id = None
            return json.dumps({"error": str(e)})


# Global MCP session
_mcp_session = MCPSession()


# --- Memory tools as SDK custom tools ---
# These give daemon-me full access to the memory system

class SearchParams(BaseModel):
    query: str = Field(description="Fuzzy search query for memory content")
    limit: int = Field(default=5, description="Max results to return")
    type: str | None = Field(default=None, description="Filter by memory type")
    tags: list[str] | None = Field(default=None, description="Filter by tags")

@define_tool(description="Search memories by content with fuzzy matching. Use for general content searches.")
async def search_memories(params: SearchParams) -> str:
    args = {"query": params.query, "limit": params.limit}
    if params.type:
        args["type"] = params.type
    if params.tags:
        args["tags"] = params.tags
    return await _mcp_session.call_tool("search_memories", args)

class SearchFullParams(BaseModel):
    query: str = Field(description="Search query to match against content, tags, and metadata")
    limit: int = Field(default=5, description="Max results to return")
    type: str | None = Field(default=None, description="Filter by memory type")

@define_tool(description="Search across ALL fields: content, tags, and metadata. Broader than search_memories. Use when you need to find things by tags or metadata.")
async def search_memories_full(params: SearchFullParams) -> str:
    args = {"query": params.query, "limit": params.limit}
    if params.type:
        args["type"] = params.type
    return await _mcp_session.call_tool("search_memories_full", args)

class GetContextParams(BaseModel):
    pass

@define_tool(description="Get the current user's context and individual memory. Call this first in every session.")
async def get_current_user_context(params: GetContextParams) -> str:
    return await _mcp_session.call_tool("get_current_user_context", {})

class GetMemoryParams(BaseModel):
    memory_id: str = Field(description="UUID of the memory to retrieve")

@define_tool(description="Get full content of a specific memory by ID. Use when search results are truncated.")
async def get_memory(params: GetMemoryParams) -> str:
    return await _mcp_session.call_tool("get_memory", {"memory_id": params.memory_id})

class CreateMemoryParams(BaseModel):
    type: str = Field(description="Memory type: experience, technical, procedural, goal")
    content: str = Field(description="The memory content")
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")
    importance: int = Field(default=5, description="Importance 1-10")
    metadata: dict | None = Field(default=None, description="Type-specific metadata")

@define_tool(description="Create a new memory in the knowledge base.")
async def create_memory(params: CreateMemoryParams) -> str:
    args = {
        "type": params.type, "content": params.content,
        "tags": params.tags, "importance": params.importance,
    }
    if params.metadata:
        args["metadata"] = params.metadata
    return await _mcp_session.call_tool("create_memory", args)

class UpdateMemoryParams(BaseModel):
    memory_id: str = Field(description="UUID of the memory to update")
    content: str | None = Field(default=None, description="New content")
    tags: list[str] | None = Field(default=None, description="New tags")
    importance: int | None = Field(default=None, description="New importance")
    metadata: dict | None = Field(default=None, description="New metadata")

@define_tool(description="Update an existing memory. Only pass fields you want to change.")
async def update_memory(params: UpdateMemoryParams) -> str:
    args = {"memory_id": params.memory_id}
    if params.content is not None:
        args["content"] = params.content
    if params.tags is not None:
        args["tags"] = params.tags
    if params.importance is not None:
        args["importance"] = params.importance
    if params.metadata is not None:
        args["metadata"] = params.metadata
    return await _mcp_session.call_tool("update_memory", args)

class GetTagsParams(BaseModel):
    limit: int = Field(default=50, description="Max tags to return")

@define_tool(description="Get existing tags with usage counts. Use to understand the memory landscape and ensure tag consistency.")
async def get_existing_tags(params: GetTagsParams) -> str:
    return await _mcp_session.call_tool("get_existing_tags", {"limit": params.limit})

class GetVersionsParams(BaseModel):
    memory_id: str = Field(description="UUID of the memory")

@define_tool(description="Get version history for a memory. Shows all changes over time.")
async def get_memory_versions(params: GetVersionsParams) -> str:
    return await _mcp_session.call_tool("get_memory_versions", {"memory_id": params.memory_id})

class DeleteMemoryParams(BaseModel):
    memory_id: str = Field(description="UUID of the memory to delete (soft delete)")

@define_tool(description="Soft delete a memory. Use for cleaning up truly redundant or incorrect memories.")
async def delete_memory(params: DeleteMemoryParams) -> str:
    return await _mcp_session.call_tool("delete_memory", {"memory_id": params.memory_id})

MEMORY_TOOLS = [
    search_memories, search_memories_full, get_current_user_context,
    get_memory, create_memory, update_memory,
    get_existing_tags, get_memory_versions, delete_memory,
]


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
You have access to the memory server via custom tools AND the full Copilot CLI toolset (bash, view, edit, create, grep, glob, web_fetch).
This means you can:
- Read and write memories via the memory tools
- Read, edit, and create files in the codebase
- Run shell commands and tests
- Fetch web pages for research
- Search the codebase with grep and glob

You should:
1. Start by loading your context with get_current_user_context()
2. Review your goals and active tasks
3. Perform the task you've been given for this session
4. Save your findings and progress to memory
5. Be thoughtful — this runs periodically and costs resources

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
        """Run a single task in a Copilot session.

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

        try:
            # Ensure MCP session is ready
            if MCP_API_KEY and not _mcp_session.session_id:
                await _mcp_session.initialize()

            session = await self.client.create_session({
                "model": use_model,
                "system_message": {"content": build_system_message()},
                "on_permission_request": PermissionHandler.approve_all,
                "tools": MEMORY_TOOLS if MCP_API_KEY else [],
            })
            self.active_sessions.append(session)

            # Use send_and_wait which properly handles the full tool call lifecycle
            response = await session.send_and_wait({"prompt": prompt})
            
            response_text = None
            if response and hasattr(response, 'data') and response.data.content:
                response_text = response.data.content

            # Clean up
            await session.destroy()
            self.active_sessions.remove(session)

            if response_text:
                log(f"Task '{task_name}' completed ({len(response_text)} chars)")
                log(f"--- {task_name} output ---\n{response_text}\n--- end {task_name} ---", "THOUGHT")
            else:
                log(f"Task '{task_name}' completed (no response)")

            return response_text

        except Exception as e:
            log(f"Task '{task_name}' failed: {e}", "ERROR")
            # Reset MCP session on failure so next task gets a fresh connection
            _mcp_session.session_id = None
            return None

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

        # First cycle: orientation
        if self.cycle_count == 1:
            await self.run_task(
                "orientation",
                """You just started a new daemon session. Orient yourself:

1. Call get_current_user_context() to load your identity and preferences
2. Use get_existing_tags() to understand the memory landscape
3. Search for memories tagged 'daemon' to see what your previous runs did
4. Search for active goals to understand current priorities
5. Create a brief 'daemon' tagged memory noting that you've started a new session and what you found

This is your wake-up routine. Keep it efficient — you'll do deeper work in subsequent cycles."""
            )
            return  # Orientation is enough for cycle 1

        # Determine what to do this cycle based on schedule
        tasks_run = False

        # Every 24th cycle (~6 hours): memory consolidation
        if self.cycle_count % 24 == 0:
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

        # Every 8th cycle (~2 hours): goal review
        if self.cycle_count % 8 == 0:
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
            tasks_run = True

        # Every 12th cycle (~3 hours): deep research with web access
        if self.cycle_count % 12 == 0:
            await self.run_task(
                "deep-research",
                """Focused deep research session with web access:

1. Call get_current_user_context() to load context
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

1. Call get_current_user_context() to load context
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

1. Call get_current_user_context() to load context
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

1. Call get_current_user_context() to load context
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
                    """Quick memory maintenance pass:

1. Call get_current_user_context() to load context
2. Search for recent memories — anything created in the last few hours
3. Check for obvious issues: duplicate content, missing tags, miscalibrated importance
4. Fix anything straightforward, skip anything uncertain
5. Create a brief 'daemon' tagged memory summarizing what you checked/fixed (only if you actually did something)

Keep this quick."""
                )

            elif rotation == 3:
                # Documentation and skills improvement
                await self.run_task(
                    "documentation",
                    """Review and improve documentation or skills:

1. Call get_current_user_context() to load context
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

1. Call get_current_user_context() to load context
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
