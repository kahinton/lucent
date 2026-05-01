"""Regression tests for the daemon dispatch memory-scope decision.

Locks in the security model: every dispatched task runs under a memory key
scoped to the requesting user, except for org-wide system maintenance
schedules (which use 'org_shared_only'). There is no exemption — including
for auto-created post-completion review tasks.
"""

from daemon.daemon import (
    _ORG_SHARED_SCHEDULE_TITLES,
    MEMORY_CONSOLIDATION_PROMPT,
    REQUEST_REVIEW_TASK_TITLE,
    _get_required_memory_scope,
    _task_requires_mcp_tool_usage,
)


class TestRequiredMemoryScope:
    def test_review_task_returns_no_override(self):
        # Review tasks no longer get a daemon-wide bypass. Returning None
        # means the dispatcher falls back to its default of 'user' scope
        # tied to the request's creator.
        assert (
            _get_required_memory_scope(
                REQUEST_REVIEW_TASK_TITLE,
                "Federated-self M3: Fork-and-rejoin experiment design",
            )
            is None
        )

    def test_review_task_on_org_shared_schedule_inherits_org_shared(self):
        # When the parent request IS an org-shared system maintenance task,
        # the review of it must also operate on shared org memories — there's
        # no single owning user whose private memories should be touched.
        assert (
            _get_required_memory_scope(
                REQUEST_REVIEW_TASK_TITLE,
                "[Scheduled] Memory Consolidation",
            )
            == "org_shared_only"
        )

    def test_org_shared_schedule_returns_org_shared_only(self):
        for title in _ORG_SHARED_SCHEDULE_TITLES:
            assert (
                _get_required_memory_scope("Some task", f"[Scheduled] {title}")
                == "org_shared_only"
            )

    def test_per_user_schedule_returns_no_override(self):
        # Per-user schedules are no longer enumerated — they fall through
        # to the dispatcher's default 'user' scope.
        for title in (
            "Experience Compression",
            "Learning Extraction",
            "Memory Vitality Scoring",
        ):
            assert (
                _get_required_memory_scope("Some task", f"[Scheduled] {title}")
                is None
            )

    def test_arbitrary_user_request_returns_no_override(self):
        # A normal user-initiated request gets None → dispatcher uses 'user' scope.
        assert _get_required_memory_scope("Draft a memo", "Investigate widget bug") is None


class TestMemoryConsolidationPrompt:
    def test_prompt_requires_repo_overview_quality_and_blocks_run_logs(self):
        assert "Desired Content Contract" in MEMORY_CONSOLIDATION_PROMPT
        assert "Architecture map" in MEMORY_CONSOLIDATION_PROMPT
        assert "background workers, services" in MEMORY_CONSOLIDATION_PROMPT
        assert "NEVER create a memory maintenance log" in MEMORY_CONSOLIDATION_PROMPT
        assert "heartbeat/state records" in MEMORY_CONSOLIDATION_PROMPT


class TestRequiredToolUsage:
    def test_memory_agent_requires_mcp_tool_usage_for_mutating_work(self):
        assert _task_requires_mcp_tool_usage("memory", "Soft-delete retired memories") is True

    def test_memory_agent_does_not_require_mcp_tool_usage_for_lightweight_work(self):
        assert _task_requires_mcp_tool_usage("memory", "Tag memories") is False

    def test_request_review_does_not_require_mcp_tool_usage(self):
        assert (
            _task_requires_mcp_tool_usage(
                "request-review",
                "Post-completion review",
                "Verify memories were updated with get_memory if needed.",
            )
            is False
        )

    def test_explicit_tool_instruction_requires_mcp_tool_usage(self):
        assert (
            _task_requires_mcp_tool_usage(
                "research",
                "Verify state",
                "You must call an MCP tool before answering.",
            )
            is True
        )

    def test_plain_research_task_does_not_require_mcp_tool_usage(self):
        assert _task_requires_mcp_tool_usage("research", "Summarize sibling results") is False
