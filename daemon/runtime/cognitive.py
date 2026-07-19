"""Top-level cognitive-cycle orchestration."""

from __future__ import annotations

import contextlib


class CognitiveCycleMixin:
    """Runs executive planning cycles while leaving lifecycle concerns outside."""

    def _build_cognitive_schedule_prompt(self) -> str:
        from daemon.runtime.module_proxy import runtime

        cognitive = (
            runtime.COGNITIVE_PROMPT_PATH.read_text()
            if runtime.COGNITIVE_PROMPT_PATH.exists()
            else ""
        )
        return (
            f"{cognitive}\n\n"
            "Begin your cognitive cycle. Load state with list_active_work() and "
            "list_pending_requests(). Search for active goal memories with "
            "search_memories(type='goal', limit=20) and create requests for any "
            "unaddressed active goals. Call list_available_models() before "
            "assigning models to tasks. Perceive, reason, decide. Use request/task "
            "tools to create work items. Output a brief summary of your decisions."
        )

    async def run_cognitive_cycle(self):
        """Run one executive planning cycle without dispatching tasks."""
        from daemon.runtime.module_proxy import runtime

        await self._reload_runtime_settings()
        self.cycle_count += 1
        runtime.log(
            f"=== Cognitive cycle #{self.cycle_count} "
            f"(instance: {self.instance_id}) ==="
        )
        if self._tracer:
            self._cognitive_cycles_total.add(1)
        span = (
            self._tracer.start_as_current_span(
                "daemon.cognitive_cycle",
                attributes={
                    "daemon.cycle_count": self.cycle_count,
                    "daemon.instance_id": self.instance_id,
                },
            )
            if self._tracer
            else contextlib.nullcontext()
        )
        with span:
            if not await runtime._verify_and_provision_key(self.instance_id):
                runtime.log(
                    "Cannot proceed without valid API key — skipping cycle", "ERROR"
                )
                return
            if self.cycle_count == 1:
                await self._check_environment_adaptation()
            prompt = await runtime.build_cognitive_prompt()
            result = await self.run_session(
                f"cognitive-{self.cycle_count}",
                prompt,
                (
                    "Begin your cognitive cycle. Load state, perceive, reason, decide. "
                    "Use memory tools to create tasks and update state. Output a brief "
                    "summary of your decisions."
                ),
            )
            if result:
                runtime.log(
                    f"Cognitive cycle #{self.cycle_count} produced output", "INFO"
                )
