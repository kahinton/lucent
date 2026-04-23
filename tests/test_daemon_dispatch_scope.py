"""Regression tests for the daemon dispatch memory-scope decision.

Locks in the security model: every dispatched task runs under a memory key
scoped to the requesting user, except for org-wide system maintenance
schedules (which use 'org_shared_only'). There is no exemption — including
for auto-created post-completion review tasks.
"""

from daemon.daemon import (
    REQUEST_REVIEW_TASK_TITLE,
    _ORG_SHARED_SCHEDULE_TITLES,
    _get_required_memory_scope,
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
