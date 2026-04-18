"""Regression tests for the consolidation "plan but no execute" failure mode.

Background (2026-04-16):
    The scheduled memory-consolidation task failed across 3+ review cycles.
    Each cycle produced excellent inventory analysis (63 memories surveyed,
    4 merge pairs identified) but executed ZERO write operations.  The output
    validator accepted the lengthy analysis text without checking whether
    any writes actually happened, creating an infinite review loop.

These tests verify the guardrail added in ``daemon/output_validation.py``
(``validate_consolidation_execution``) and its integration through the
daemon's ``_validate_task_result`` pipeline.
"""

import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Import helper — daemon/ lives outside the standard package tree
# ---------------------------------------------------------------------------
_daemon_path = os.path.join(os.path.dirname(__file__), "..", "daemon")
if _daemon_path not in sys.path:
    sys.path.insert(0, _daemon_path)

from output_validation import validate_consolidation_execution  # noqa: E402


# ---------------------------------------------------------------------------
# Realistic output fixtures — modelled on the actual 2026-04-16 failure
# ---------------------------------------------------------------------------

# The exact failure pattern: thorough analysis, zero writes
PLAN_ONLY_OUTPUT = """\
## Memory Consolidation Report

### Inventory
Surveyed 63 technical memories across the codebase.

### Identified Merge Pairs
1. **Pair A** — memories abc123 and def456 overlap on API auth patterns
2. **Pair B** — memories ghi789 and jkl012 duplicate database migration steps
3. **Pair C** — memories mno345 and pqr678 cover identical Docker config
4. **Pair D** — memories stu901 and vwx234 both describe rate-limit architecture

**Summary**: Identified 4 merge pairs and 2 updates planned.
Normalization progress: 16% complete.

### Recommendations
- Merge Pair A into a single "API Authentication Patterns" memory
- Merge Pair B keeping the more recent migration steps
- Consolidate Pair C with Docker Compose v2 conventions
- Merge Pair D preserving rate-limit algorithm details
"""

# Successful consolidation: plan + executed writes
PLAN_AND_EXECUTE_OUTPUT = """\
## Memory Consolidation Report

### Inventory
Surveyed 63 technical memories across the codebase.

### Identified 4 merge pairs and 2 updates planned.

### Executed Operations
- Merged memories abc123 ← def456 (API auth patterns) via update_memory
- Merged memories ghi789 ← jkl012 (database migrations) via update_memory
- Merged memories mno345 ← pqr678 (Docker config) via update_memory
- Merged memories stu901 ← vwx234 (rate-limit architecture) via update_memory
- Soft-deleted 4 superseded memories via delete_memory

Planned operations: 6
Executed write operations: 8

Normalization progress: 84% complete.
"""

# Explicit determination that no work is needed
NO_ACTION_OUTPUT = """\
## Memory Consolidation Report

### Inventory
Surveyed 63 technical memories across the codebase.

### Analysis
No duplicate pairs found.  Tag consistency is good.
All memories have appropriate importance ratings.

Nothing to consolidate.  No action needed.
"""

# Non-consolidation task — should be ignored by the validator entirely
NON_CONSOLIDATION_OUTPUT = "Refactored the auth middleware to use dependency injection."


class TestConsolidationPlanNoExecuteRegression:
    """Regression: consolidation outputs with plans but zero writes must be rejected."""

    # ------------------------------------------------------------------
    # Scenario 1: Plan identified, zero writes → REJECT
    # ------------------------------------------------------------------

    def test_rejects_realistic_plan_only_output(self):
        """The exact failure from 2026-04-16: thorough analysis, zero writes."""
        ok, reason = validate_consolidation_execution(
            result_text=PLAN_ONLY_OUTPUT,
            task_title="Memory consolidation pass",
            task_description="Run scheduled memory consolidation for technical memories",
            tool_counts={"search_memories": 12, "update_memory": 0, "delete_memory": 0},
        )
        assert ok is False, "Should reject plan-only output with zero executed writes"
        assert "0 were executed" in reason

    def test_rejects_plan_only_without_tool_counts(self):
        """When tool_counts is unavailable, fallback text parsing still catches it."""
        ok, reason = validate_consolidation_execution(
            result_text=PLAN_ONLY_OUTPUT,
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts=None,  # MCP tracker unavailable
        )
        assert ok is False, "Should reject even without tool_counts via text parsing"
        assert "0 were executed" in reason

    def test_rejects_plan_only_with_empty_tool_counts(self):
        """Empty tool_counts dict (no write tools recorded) → reject."""
        ok, reason = validate_consolidation_execution(
            result_text=PLAN_ONLY_OUTPUT,
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts={},
        )
        assert ok is False
        assert "0 were executed" in reason

    # ------------------------------------------------------------------
    # Scenario 2: Plan identified AND writes executed → ACCEPT
    # ------------------------------------------------------------------

    def test_accepts_output_with_plan_and_executed_writes(self):
        """Consolidation that plans AND executes writes should pass."""
        ok, reason = validate_consolidation_execution(
            result_text=PLAN_AND_EXECUTE_OUTPUT,
            task_title="Memory consolidation pass",
            task_description="Run scheduled memory consolidation",
            tool_counts={"update_memory": 4, "delete_memory": 4},
        )
        assert ok is True, f"Should accept plan+execute output, got: {reason}"
        assert reason == "ok"

    def test_accepts_partial_execution(self):
        """Even one write operation is enough — the agent made progress."""
        ok, reason = validate_consolidation_execution(
            result_text="Identified 4 merge pairs planned.\nMerged 1 pair successfully.",
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts={"update_memory": 1, "delete_memory": 0},
        )
        assert ok is True, f"Should accept partial execution, got: {reason}"

    # ------------------------------------------------------------------
    # Scenario 3: Rejection message is clear and actionable
    # ------------------------------------------------------------------

    def test_rejection_message_includes_planned_count(self):
        """The rejection reason must tell the agent how many ops were planned."""
        ok, reason = validate_consolidation_execution(
            result_text="Identified 4 merge pairs and 2 updates planned.\nDone.",
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts={"update_memory": 0, "delete_memory": 0},
        )
        assert ok is False
        # Must include the specific count so the retry knows what was expected
        assert "6 operations" in reason, f"Reason should include planned op count: {reason}"
        assert "0 were executed" in reason, f"Reason should state zero executed: {reason}"

    def test_rejection_message_format_is_human_readable(self):
        """The message should be a complete sentence, not a code/enum."""
        ok, reason = validate_consolidation_execution(
            result_text="Identified 3 merges planned. Analysis complete.",
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts={"update_memory": 0, "delete_memory": 0},
        )
        assert ok is False
        # Should read like: "Plan identified 3 operations but 0 were executed"
        assert reason.startswith("Plan identified"), f"Should start with 'Plan identified': {reason}"
        assert "but" in reason, f"Should explain the gap with 'but': {reason}"

    # ------------------------------------------------------------------
    # Edge cases: non-consolidation and explicit no-op
    # ------------------------------------------------------------------

    def test_skips_non_consolidation_tasks(self):
        """Validator must not interfere with non-consolidation tasks."""
        ok, reason = validate_consolidation_execution(
            result_text=NON_CONSOLIDATION_OUTPUT,
            task_title="Refactor auth middleware",
            task_description="Update auth to use DI pattern",
            tool_counts={"update_memory": 0},
        )
        assert ok is True
        assert reason == "not_consolidation_task"

    def test_accepts_explicit_no_action_determination(self):
        """When the agent correctly determines nothing needs consolidation."""
        ok, reason = validate_consolidation_execution(
            result_text=NO_ACTION_OUTPUT,
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts={"update_memory": 0, "delete_memory": 0},
        )
        assert ok is True
        assert reason == "no_action_needed"

    def test_accepts_nothing_to_consolidate_phrasing(self):
        """Alternative phrasing for no-op should also be accepted."""
        ok, reason = validate_consolidation_execution(
            result_text="Surveyed all memories. Nothing to consolidate.",
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts={"update_memory": 0, "delete_memory": 0},
        )
        assert ok is True
        assert reason == "no_action_needed"

    def test_no_planned_ops_detected_passes(self):
        """If the output doesn't mention any planned ops, there's nothing to reject."""
        ok, reason = validate_consolidation_execution(
            result_text="Surveyed 63 memories. All look good. Tags are consistent.",
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts={"update_memory": 0, "delete_memory": 0},
        )
        assert ok is True
        assert reason == "no_planned_writes_detected"


class TestConsolidationEdgeCases:
    """Edge cases and boundary conditions for the consolidation validator."""

    # ------------------------------------------------------------------
    # Key regression: long plan-only output must NOT bypass the guard
    # ------------------------------------------------------------------

    def test_long_plan_only_output_still_rejected(self):
        """The exact production bug: plan text exceeded 1000 chars and passed
        the daemon's length-based shortcut.  The consolidation guard MUST run
        before the length check in _validate_task_result."""
        # Build a realistic long analysis that would pass len >= 1000
        long_plan = PLAN_ONLY_OUTPUT + "\n" + (
            "### Additional Analysis\n"
            "- Memory tag distribution: 12 unique tags across 63 memories\n"
            "- Average importance: 6.2\n"
            "- Oldest memory: 2025-01-15\n"
            "- Most-referenced module: auth middleware (8 memories)\n"
        ) * 5  # Pad well past 1000 chars
        assert len(long_plan) > 1000, "Fixture must exceed the length shortcut threshold"

        ok, reason = validate_consolidation_execution(
            result_text=long_plan,
            task_title="Memory consolidation pass",
            task_description="Run scheduled memory consolidation",
            tool_counts={"search_memories": 15, "update_memory": 0, "delete_memory": 0},
        )
        assert ok is False, "Long plan-only output must still be rejected"
        assert "0 were executed" in reason

    # ------------------------------------------------------------------
    # Write-tool variations
    # ------------------------------------------------------------------

    def test_accepts_delete_only_writes(self):
        """Consolidation with only deletes (no updates) is valid execution."""
        ok, reason = validate_consolidation_execution(
            result_text="Identified 3 merges planned.\nDeleted 3 superseded memories.",
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts={"update_memory": 0, "delete_memory": 3},
        )
        assert ok is True, f"Delete-only execution should be accepted, got: {reason}"
        assert reason == "ok"

    def test_accepts_update_only_writes(self):
        """Consolidation with only updates (no deletes) is valid execution."""
        ok, reason = validate_consolidation_execution(
            result_text="Identified 2 merges planned.\nMerged 2 pairs via update.",
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts={"update_memory": 2, "delete_memory": 0},
        )
        assert ok is True, f"Update-only execution should be accepted, got: {reason}"
        assert reason == "ok"

    # ------------------------------------------------------------------
    # Text-based fallback execution detection (tool_counts=None)
    # ------------------------------------------------------------------

    def test_text_fallback_detects_executed_ops(self):
        """When tool_counts is None, regex fallback should detect executed ops in text."""
        output_with_executed = (
            "Identified 4 merge pairs planned.\n"
            "4 merges executed successfully.\n"
            "Normalization complete."
        )
        ok, reason = validate_consolidation_execution(
            result_text=output_with_executed,
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts=None,  # MCP tracker unavailable
        )
        assert ok is True, f"Text-detected executed ops should be accepted, got: {reason}"
        assert reason == "ok"

    def test_text_fallback_detects_applied_updates(self):
        """Fallback regex should match 'applied' verb."""
        ok, reason = validate_consolidation_execution(
            result_text="Identified 2 updates planned.\n2 updates applied to memories.",
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts=None,
        )
        assert ok is True, f"'Applied' phrasing should be detected, got: {reason}"

    def test_text_fallback_rejects_when_no_executed_language(self):
        """Fallback should reject when text says 'planned' but never 'executed/applied'."""
        ok, reason = validate_consolidation_execution(
            result_text=(
                "Identified 3 merge pairs planned.\n"
                "Recommendation: merge these pairs in the next cycle."
            ),
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts=None,
        )
        assert ok is False, "No executed language should trigger rejection"
        assert "0 were executed" in reason

    # ------------------------------------------------------------------
    # Input edge cases
    # ------------------------------------------------------------------

    def test_none_result_text_passes(self):
        """None result_text should not crash — passes as no_action_needed or no_planned."""
        ok, reason = validate_consolidation_execution(
            result_text=None,
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts=None,
        )
        assert ok is True, "None result_text should pass (no planned ops detected)"

    def test_empty_result_text_passes(self):
        """Empty string result_text should pass (no planned ops to reject)."""
        ok, reason = validate_consolidation_execution(
            result_text="",
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts=None,
        )
        assert ok is True

    # ------------------------------------------------------------------
    # Multi-line planned-ops accumulation
    # ------------------------------------------------------------------

    def test_accumulates_planned_ops_across_lines(self):
        """Planned ops on separate lines should sum, not just use the last one."""
        multi_line_plan = (
            "Identified 3 merge pairs for tag normalization.\n"
            "Also identified 2 updates for importance recalibration.\n"
            "Total planned: 5 operations."
        )
        ok, reason = validate_consolidation_execution(
            result_text=multi_line_plan,
            task_title="Memory consolidation pass",
            task_description="Run memory consolidation",
            tool_counts={"update_memory": 0, "delete_memory": 0},
        )
        assert ok is False
        # Should accumulate: 3 + 2 = 5
        assert "5 operations" in reason, f"Should detect 5 total planned ops: {reason}"

    # ------------------------------------------------------------------
    # Task detection variations
    # ------------------------------------------------------------------

    def test_detects_consolidation_in_title_only(self):
        """'consolidat' in title + 'memory' in description should match."""
        ok, reason = validate_consolidation_execution(
            result_text="Identified 2 merges planned. Done.",
            task_title="Technical consolidation pass",
            task_description="Review and merge duplicate memory records",
            tool_counts={"update_memory": 0, "delete_memory": 0},
        )
        assert ok is False, "Should detect consolidation from title+description combo"

    def test_detects_memory_consolidation_case_insensitive(self):
        """Detection should be case-insensitive."""
        ok, reason = validate_consolidation_execution(
            result_text="Identified 2 merges planned. Analysis complete.",
            task_title="MEMORY CONSOLIDATION PASS",
            task_description="RUN MEMORY CONSOLIDATION",
            tool_counts={"update_memory": 0, "delete_memory": 0},
        )
        assert ok is False, "Should detect regardless of case"

    def test_ignores_task_without_memory_keyword(self):
        """'consolidat' without 'memory' should NOT trigger the guard."""
        ok, reason = validate_consolidation_execution(
            result_text="Identified 2 merges planned. Done.",
            task_title="Data consolidation pass",
            task_description="Consolidate analytics data",
            tool_counts={"update_memory": 0, "delete_memory": 0},
        )
        assert ok is True
        assert reason == "not_consolidation_task"

    def test_ignores_task_without_consolidation_keyword(self):
        """'memory' without 'consolidat' should NOT trigger the guard."""
        ok, reason = validate_consolidation_execution(
            result_text="Identified 2 merges planned. Done.",
            task_title="Memory cleanup",
            task_description="Clean up stale memory entries",
            tool_counts={"update_memory": 0, "delete_memory": 0},
        )
        assert ok is True
        assert reason == "not_consolidation_task"


class TestDaemonValidateTaskResultConsolidationIntegration:
    """Integration: verify _validate_task_result rejects empty consolidation execution."""

    def test_daemon_rejects_plan_only_consolidation(self):
        """The daemon's top-level validation pipeline should reject plan-only output."""
        from daemon.daemon import LucentDaemon

        daemon = LucentDaemon()
        ok, reason = daemon._validate_task_result(
            PLAN_ONLY_OUTPUT,
            task={
                "title": "Memory consolidation pass",
                "description": "Run scheduled memory consolidation for technical memories",
            },
            tool_counts={"search_memories": 12, "update_memory": 0, "delete_memory": 0},
        )
        assert ok is False
        assert "0 were executed" in reason

    def test_daemon_accepts_executed_consolidation(self):
        """The daemon accepts consolidation output when writes were executed."""
        from daemon.daemon import LucentDaemon

        daemon = LucentDaemon()
        ok, reason = daemon._validate_task_result(
            PLAN_AND_EXECUTE_OUTPUT,
            task={
                "title": "Memory consolidation pass",
                "description": "Run scheduled memory consolidation",
            },
            tool_counts={"update_memory": 4, "delete_memory": 4},
        )
        assert ok is True
        assert reason == "ok"

    def test_daemon_accepts_non_consolidation_without_writes(self):
        """Non-consolidation tasks should not be blocked by the consolidation guard."""
        from daemon.daemon import LucentDaemon

        daemon = LucentDaemon()
        ok, reason = daemon._validate_task_result(
            "Completed the code review. Found 2 issues and fixed them. "
            "Updated the test suite to cover the new edge cases. "
            "All 47 tests pass. " * 10,  # ensure >1000 chars
            task={
                "title": "Code review for PR #42",
                "description": "Review and fix issues in auth module",
            },
            tool_counts={"update_memory": 0},
        )
        assert ok is True

    def test_daemon_rejects_long_plan_only_consolidation(self):
        """Critical regression: long plan-only output must NOT bypass via the
        >= 1000 chars shortcut in _validate_task_result.  The consolidation
        guard runs before the length check."""
        long_plan = PLAN_ONLY_OUTPUT + "\n" + ("Extra analysis padding. " * 60)
        assert len(long_plan) > 1000, "Must exceed length shortcut threshold"

        from daemon.daemon import LucentDaemon

        daemon = LucentDaemon()
        ok, reason = daemon._validate_task_result(
            long_plan,
            task={
                "title": "Memory consolidation pass",
                "description": "Run scheduled memory consolidation for technical memories",
            },
            tool_counts={"search_memories": 12, "update_memory": 0, "delete_memory": 0},
        )
        assert ok is False, "Long plan-only output must be rejected by daemon pipeline"
        assert "0 were executed" in reason

    def test_daemon_accepts_no_action_consolidation(self):
        """Daemon should accept consolidation that correctly determines no work needed."""
        from daemon.daemon import LucentDaemon

        daemon = LucentDaemon()
        ok, reason = daemon._validate_task_result(
            NO_ACTION_OUTPUT + "\n" + ("Detailed analysis. " * 50),  # pad to > 1000
            task={
                "title": "Memory consolidation pass",
                "description": "Run scheduled memory consolidation",
            },
            tool_counts={"search_memories": 10, "update_memory": 0, "delete_memory": 0},
        )
        assert ok is True
