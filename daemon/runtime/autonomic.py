"""Autonomic learning and experience-compression capabilities."""

from __future__ import annotations

from datetime import datetime, timezone


class AutonomicMixin:
    """Runs the daemon's legacy/manual autonomic maintenance operations."""

    async def run_autonomic(self):
        """Retained compatibility entrypoint for retired consolidation work."""
        from daemon.runtime.module_proxy import runtime

        runtime.log(
            "Technical memory consolidation is retired — duplicate file-scoped "
            "technical memories are rejected at create/update time."
        )

    async def run_learning_extraction(self):
        """Process recent results and feedback into reusable lessons."""
        from daemon.runtime.module_proxy import runtime

        runtime.log("Running autonomic: learning extraction")
        try:
            system_message = await runtime.build_subagent_prompt(
                "reflection",
                "Learning extraction pass — process recent daemon-results and feedback into reusable lessons.",
                "This is an autonomic background task. Follow the learning-extraction skill instructions.",
            )
        except runtime.AgentNotFoundError:
            runtime.log("No approved 'reflection' agent — skipping learning extraction", "WARN")
            return

        await self.run_session(
            "autonomic-learning",
            system_message,
            (
                "Run the learning extraction pipeline from the learning-extraction skill. "
                "Core principle: INTEGRATE, don't accumulate. Lessons get folded into "
                "existing memories, not stored as standalone 'Lesson:' entries. "
                "If the lesson reveals missing capability or bad behavior, create a "
                "human-reviewed activation artifact: a proposed agent/skill/hook or a "
                "follow-up request for grants, definition updates, or built-in/source-code "
                "changes.\n\n"
                "1. Search for memories tagged 'daemon-result' or 'rejection-lesson' or "
                "'feedback-rejected' or 'validated' that do NOT have the "
                "'lesson-extracted' tag. Cap at 10.\n"
                "2. For each non-routine experience, find the existing memory or skill "
                "that this lesson is ABOUT.\n"
                "3. Update that existing memory with the new knowledge. If no related "
                "memory exists, create ONE well-scoped technical or experience memory. "
                "Reusable workflows belong in skills.\n"
                "4. Tag processed source memories with 'lesson-extracted'.\n"
                "5. Delete source experience memories that are now redundant.\n"
                "6. Review repeated tool failures with analyze_tool_failure_patterns. "
                "For confirmed patterns, queue the smallest concrete improvement for human review.\n\n"
                "STRICT RULES:\n"
                "- NEVER create standalone 'Lesson:' or 'Learning Extraction Run' memories.\n"
                "- NEVER create a new memory if an existing one covers the same scope — update it.\n"
                "- The total memory count must go DOWN or stay the same, never up.\n"
                "- Prefer update_memory and delete_memory. Only use create_memory for genuine gaps.\n"
                "- Skip runtime heartbeat or telemetry records.\n"
                "- NEVER treat capability requests as documentation-only work.\n"
                "- NEVER grant yourself access to runtime powers."
            ),
        )

    async def run_experience_compression(self):
        """Compress granular experience memories into daily digests."""
        from daemon.runtime.module_proxy import runtime

        runtime.log("Running autonomic: daily experience compression")
        try:
            system_message = await runtime.build_subagent_prompt(
                "memory",
                "Daily experience compression — merge granular experience memories into daily digests.",
                "This is an autonomic background task. Compress old experience memories into concise daily summaries.",
            )
        except runtime.AgentNotFoundError:
            runtime.log("No approved 'memory' agent — skipping experience compression", "WARN")
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prompt = runtime.EXPERIENCE_COMPRESSION_PROMPT.replace(
            "Skip memories from today - only compress older ones.",
            f"Skip memories from today ({today}) — only compress older ones.",
        )
        await self.run_session(
            "autonomic-compression",
            system_message,
            prompt,
        )
