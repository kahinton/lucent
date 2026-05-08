"""Regression tests for the daemon dispatch memory-scope decision.

Locks in the security model: every dispatched task runs under a memory key
scoped to the requesting user, except for org-wide system maintenance
schedules (which use 'org_shared_only'). There is no exemption — including
for auto-created post-completion review tasks.
"""

from daemon.daemon import (
    _ORG_SHARED_SCHEDULE_TITLES,
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

    def test_review_task_on_retired_memory_consolidation_schedule_has_no_override(self):
        # Technical memory consolidation is retired; there is no org-wide
        # shared-memory override for its historical schedule title.
        assert (
            _get_required_memory_scope(
                REQUEST_REVIEW_TASK_TITLE,
                "[Scheduled] Memory Consolidation",
            )
            is None
        )

    def test_no_org_shared_schedule_overrides_exist(self):
        assert _ORG_SHARED_SCHEDULE_TITLES == frozenset()

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
