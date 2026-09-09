"""Targeted tests for the LLM-generated chat intro work summary.

Covers the pure logic layers introduced by the feature:
- lucent.services.chat_intro: grounding/quiet/fingerprint rendering
- lucent.api.routers.chat._sanitize_intro_summary: output clipping while
  preserving the bullet-line structure the system prompt asks for
- chat_intro_summary_payload: cache/fingerprint refresh and fallback policy
  (driven through fake engine/settings modules so no real LLM or DB pool is
  touched)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


# ═════════════════════════════════════════════════════════════════════
# lucent.services.chat_intro — pure rendering helpers
# ═════════════════════════════════════════════════════════════════════


def test_clean_collapses_whitespace_and_clips():
    from lucent.services.chat_intro import _clean

    assert _clean("  a\n\nb  ") == "a b"
    assert _clean("x" * 300) == "x" * 119 + "…"


def test_request_line_includes_status_and_tasks():
    from lucent.services.chat_intro import _request_line

    line = _request_line(
        {
            "id": "r-1",
            "title": "Ship the thing",
            "status": "in_progress",
            "approval_status": "auto_approved",
            "task_count": 4,
            "tasks_completed": 2,
            "tasks_running": 1,
        }
    )
    assert "Ship the thing" in line
    assert "status=in_progress" in line
    assert "2/4 tasks done" in line
    assert "1 running" in line


def test_goal_line_includes_milestones():
    from lucent.services.chat_intro import _goal_line

    line = _goal_line(
        {
            "id": "g-1",
            "title": "Launch",
            "milestone_total": 3,
            "milestone_completed": 1,
            "current_milestone_label": "M2: polish",
        }
    )
    assert "Launch" in line
    assert "milestones 1/3 completed" in line
    assert "current: M2: polish" in line


def test_fingerprint_is_stable_and_sensitive_to_changes():
    from lucent.services.chat_intro import compute_fingerprint

    snapshot = {
        "active_requests": [
            {"id": "r-1", "status": "in_progress", "approval_status": "auto_approved",
             "task_count": 2, "tasks_completed": 1, "tasks_running": 0, "tasks_failed": 0}
        ],
        "recently_completed": [],
        "goals": [],
        "pending_approval_count": 0,
    }
    assert compute_fingerprint(snapshot) == compute_fingerprint(dict(snapshot))

    changed = {**snapshot, "pending_approval_count": 1}
    assert compute_fingerprint(snapshot) != compute_fingerprint(changed)

    task_done = {
        **snapshot,
        "active_requests": [{**snapshot["active_requests"][0], "tasks_completed": 2}],
    }
    assert compute_fingerprint(snapshot) != compute_fingerprint(task_done)


# ═════════════════════════════════════════════════════════════════════
# _sanitize_intro_summary — bullet-line preservation + clipping
# ═════════════════════════════════════════════════════════════════════


def test_sanitize_preserves_bullets_and_collapses_wrapping():
    from lucent.api.routers.chat import _sanitize_intro_summary

    # Newlines before bullets are preserved; wrapped prose collapses.
    raw = "Two requests in flight.\n\n- Next: review\n- Then: merge"
    out = _sanitize_intro_summary(raw)
    assert out == "Two requests in flight.\n- Next: review\n- Then: merge"
    assert _sanitize_intro_summary("Line one wraps\nonto line two.") == \
        "Line one wraps onto line two."


def test_sanitize_clips_to_budget():
    from lucent.api.routers.chat import _INTRO_SUMMARY_MAX_CHARS, _sanitize_intro_summary

    out = _sanitize_intro_summary("w" * 5000)
    assert len(out) == _INTRO_SUMMARY_MAX_CHARS
    assert out.endswith("…")


def test_sanitize_handles_empty():
    from lucent.api.routers.chat import _sanitize_intro_summary

    assert _sanitize_intro_summary("") == ""
    assert _sanitize_intro_summary(None) == ""


# ═════════════════════════════════════════════════════════════════════
# chat_intro_summary_payload — cache/fallback policy (no LLM, no DB)
# ═════════════════════════════════════════════════════════════════════


class _FakeEngine:
    name = "fake"

    def __init__(self, result: str = "Working summary.\n- Do a thing"):
        self.result = result
        self.calls: list[dict] = []

    async def run_session(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _context(fingerprint: str, quiet: bool = False):
    return {"is_quiet": quiet, "fingerprint": fingerprint, "context_text": "data", "snapshot": {}}


@pytest.fixture()
def router_module():
    import lucent.api.routers.chat as module

    module._intro_summary_cache.clear()
    module._intro_summary_tasks.clear()
    yield module
    module._intro_summary_cache.clear()
    module._intro_summary_tasks.clear()


class TestPayloadPolicy:
    def test_disabled_returns_none(self, router_module):
        user = {"id": "u1", "organization_id": "org1"}
        with patch.object(router_module, "chat_intro_summary_enabled", return_value=False):
            result = asyncio.run(router_module.chat_intro_summary_payload(user, None))
        assert result == {"summary": None, "reason": "disabled"}

    def test_quiet_clears_cache_and_returns_none(self, router_module):
        user = {"id": "u1", "organization_id": "org1"}
        key = router_module._cache_key(user)
        router_module._intro_summary_cache[key] = router_module._IntroSummaryCacheEntry(
            "old", "m", "fp0"
        )

        async def fake_gather(user):
            return _context("fp1", quiet=True)

        with patch.object(router_module, "chat_intro_summary_enabled", return_value=True), \
             patch(
                 "lucent.services.chat_intro.gather_work_context",
                 side_effect=fake_gather,
             ):
            result = asyncio.run(router_module.chat_intro_summary_payload(user, None))
        assert result["summary"] is None
        assert key not in router_module._intro_summary_cache

    def test_cached_with_same_fingerprint_served_while_regen_in_flight(self, router_module):
        import asyncio as _asyncio

        async def run():
            user = {"id": "u1", "organization_id": "org1"}
            key = router_module._cache_key(user)
            entry = router_module._IntroSummaryCacheEntry("good summary", "m", "fp1")
            entry.generated_at = datetime.now(timezone.utc)
            router_module._intro_summary_cache[key] = entry

            async def never():
                await _asyncio.sleep(30)

            task = _asyncio.ensure_future(never())
            router_module._intro_summary_tasks[key] = task
            try:
                async def fake_gather(user):
                    return _context("fp1")

                with patch.object(router_module, "chat_intro_summary_enabled", return_value=True), \
                     patch(
                         "lucent.services.chat_intro.gather_work_context",
                         side_effect=fake_gather,
                     ):
                    result = await router_module.chat_intro_summary_payload(user, None)
                assert result["summary"] == "good summary"
                assert result["regenerating"] is True
                assert result["fingerprint"] == "fp1"
            finally:
                task.cancel()
                try:
                    await task
                except _asyncio.CancelledError:
                    pass

        asyncio.run(run())

    def test_fingerprint_change_starts_background_task(self, router_module):
        user = {"id": "u1", "organization_id": "org1"}
        key = router_module._cache_key(user)

        async def fake_gather(user):
            return _context("fp-changed")

        async def run():
            with patch.object(router_module, "chat_intro_summary_enabled", return_value=True), \
                 patch(
                     "lucent.services.chat_intro.gather_work_context",
                     side_effect=fake_gather,
                 ), \
                 patch.object(router_module, "_resolve_chat_model", return_value="m1"), \
                 patch.object(
                     router_module, "chat_intro_summary_model_id", lambda **kw: None
                 ):
                # First call: nothing cached -> regenerating, task started.
                result = await router_module.chat_intro_summary_payload(user, None)
                assert result["summary"] is None
                assert result["reason"] == "generating"
                started = router_module._intro_summary_tasks[key]
                # Generation completes against the patched engine.
                engine = _FakeEngine()
                with patch(
                    "lucent.model_registry.validate_model", return_value=None
                ), patch("lucent.llm.get_engine_for_model", return_value=engine):
                    await asyncio.wait_for(started, timeout=5)
                assert key in router_module._intro_summary_cache
                assert engine.calls, "engine should have been called once"
                # Second call: cached hit, no regeneration needed.
                result2 = await router_module.chat_intro_summary_payload(user, None)
                assert result2["summary"] == "Working summary.\n- Do a thing"
                assert result2["regenerating"] is False

        asyncio.run(run())

    def test_generation_failure_leaves_silent_fallback(self, router_module):
        user = {"id": "u1", "organization_id": "org1"}
        key = router_module._cache_key(user)

        async def fake_gather(user):
            return _context("fp1")

        async def run():
            with patch.object(router_module, "chat_intro_summary_enabled", return_value=True), \
                 patch(
                     "lucent.services.chat_intro.gather_work_context",
                     side_effect=fake_gather,
                 ), \
                 patch.object(router_module, "_resolve_chat_model", return_value="m1"), \
                 patch.object(
                     router_module, "chat_intro_summary_model_id", lambda **kw: None
                 ):
                result = await router_module.chat_intro_summary_payload(user, None)
                assert result["summary"] is None
                assert result["reason"] == "generating"
                started = router_module._intro_summary_tasks[key]
                with patch(
                    "lucent.model_registry.validate_model",
                    return_value="model disabled",
                ):
                    await asyncio.wait_for(started, timeout=5)
                # Failure: no cache entry. The next request starts a fresh
                # attempt (retry-on-next-load policy), so it reports
                # "generating" again rather than "unavailable".
                assert key not in router_module._intro_summary_cache
                result2 = await router_module.chat_intro_summary_payload(user, None)
                assert result2["summary"] is None
                assert result2["reason"] == "generating"
                assert router_module._intro_summary_tasks[key].done() is False

        asyncio.run(run())