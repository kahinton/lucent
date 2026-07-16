"""Live configuration refresh and first-run environment adaptation."""

from __future__ import annotations

import time


class RuntimeConfigurationMixin:
    """Applies runtime settings and adapts Lucent to an unassessed environment."""

    async def _check_environment_adaptation(self):
        from daemon.runtime.module_proxy import runtime

        memories = await runtime.MemoryAPI.search(
            "environment", tags=["environment"], limit=1
        )
        if memories:
            runtime.log("Environment profile found — skipping adaptation")
            return
        runtime.log("No environment profile found — running adaptation pipeline")
        try:
            system_message = await runtime.build_subagent_prompt(
                "assessment",
                "Perform a full environment assessment. Discover tools, domain, "
                "collaborators, and goals. Produce structured output for the "
                "adaptation pipeline.",
            )
        except runtime.AgentNotFoundError:
            runtime.log(
                "No approved 'assessment' agent definition — skipping adaptation. "
                "Create and approve one at /definitions to enable environment assessment.",
                "WARN",
            )
            return
        output = await self.run_session(
            "adaptation-assessment",
            system_message,
            "Run a complete environment assessment. At the end of your response, "
            "include the structured <assessment_result> JSON block as described "
            "in your instructions. This is critical — the adaptation pipeline "
            "depends on it.",
        )
        if not output:
            runtime.log("Assessment agent produced no output", "WARN")
            return
        assessment = runtime.parse_assessment_output(output)
        if assessment is None:
            runtime.log(
                "Could not parse structured assessment output — the assessment agent "
                "may not have included <assessment_result> tags",
                "WARN",
            )
            return
        summary = await runtime.AdaptationPipeline(assessment).run(
            memory_api=runtime.MemoryAPI,
            api_base=runtime.API_BASE,
            api_headers=runtime.API_HEADERS,
        )
        runtime.log(
            f"Adaptation complete: {len(summary.get('agents_proposed', []))} agents, "
            f"{len(summary.get('skills_proposed', []))} skills proposed for domain "
            f"'{summary.get('domain', 'unknown')}' — awaiting human approval"
        )

    async def _reload_runtime_settings(
        self, *, min_interval_seconds: float = 15.0
    ) -> None:
        from daemon.runtime.module_proxy import runtime

        now = time.monotonic()
        if now - self._settings_reloaded_at < min_interval_seconds:
            return
        async with self._settings_reload_lock:
            now = time.monotonic()
            if now - self._settings_reloaded_at < min_interval_seconds:
                return
            try:
                from lucent.db import get_pool
                from lucent.settings import load_runtime_settings_from_db

                await load_runtime_settings_from_db(await get_pool())
                runtime._refresh_config_from_runtime_settings()
                self.roles = self._parse_roles(runtime.DAEMON_ROLES_STR)
                self._settings_reloaded_at = now
            except Exception as error:
                runtime.log(f"Runtime settings reload failed: {error}", "WARN")
