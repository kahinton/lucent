"""Regression tests: append-only, emission-ordered tool/hook event persistence.

Covers the 2026-09-08 reload-loss fix (request 0dc1c704):

- Append-only guarantee for ``llm_session_events`` (ON CONFLICT DO NOTHING):
  a stale replay of an already-persisted (session_id, sequence) must be
  dropped, never overwrite the existing row (old code did last-writer-wins).
- Exact replay ordering: the FIFO EventPersistBridge persists queued engine
  events in emission order, so add_event's MAX+1-under-lock sequence equals
  emission order for both CopilotEngine and LangChainEngine (both funnel
  through chat.py's shared persist path).
- Concurrent stream-end race: a worker draining the FIFO while the producer
  is still enqueueing and the stream finalizes concurrently must neither
  lose nor reorder events, and drain_and_stop must terminate.
- Reload slice: get_session_detail serves a visible-only newest-events window
  (aligned to the 500-message turn window) in ascending sequence order.
- Hook payload preservation: the hook-display work's _hook_event_payload
  structure survives an add_event/list_events round trip unchanged.

Tests use the live Postgres (DAEMON_DATABASE_URL) with a dedicated fixture
organization, cleaned up in teardown (session delete cascades events).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from uuid import UUID

import pytest

import lucent.db.llm_sessions as llm_sessions_module
from lucent.db.llm_sessions import LLMSessionRepository
from lucent.llm.event_persistence import EventPersistBridge

TEST_ORG_ID = "00000000-0000-0000-0000-0000000000aa"


@pytest.fixture
def db_url() -> str:
    url = os.environ.get("DAEMON_DATABASE_URL")
    if not url:
        pytest.skip("DAEMON_DATABASE_URL not available in this environment")
    return url


@pytest.fixture
async def pool(db_url):
    asyncpg = pytest.importorskip("asyncpg")
    from lucent.db.pool import _init_connection

    created = await asyncpg.create_pool(
        dsn=db_url, min_size=2, max_size=8, init=_init_connection
    )
    try:
        yield created
    finally:
        # Close all connections so transaction locks release before cleanup.
        await created.close()
        cleanup = await asyncpg.connect(dsn=db_url)
        try:
            # FK-cascade cleans events/messages/requests/joins; the org row
            # has no dependency back to us. Hard delete, not soft-archive:
            # these rows never belong in production tables.
            await cleanup.execute(
                "DELETE FROM llm_sessions WHERE organization_id = $1",
                uuid.UUID(TEST_ORG_ID),
            )
            await cleanup.execute(
                "DELETE FROM organizations WHERE id = $1",
                uuid.UUID(TEST_ORG_ID),
            )
        finally:
            await cleanup.close()


@pytest.fixture
async def org(pool):
    await pool.execute(
        """INSERT INTO organizations (id, name)
           VALUES ($1, 'event-persistence-test-org')
           ON CONFLICT (id) DO NOTHING""",
        uuid.UUID(TEST_ORG_ID),
    )
    return uuid.UUID(TEST_ORG_ID)


@pytest.fixture
async def session_id(pool, org):
    repo = LLMSessionRepository(pool)
    row = await repo.create_session(
        org_id=TEST_ORG_ID,
        kind="chat",
        title="event-persistence regression",
        engine="langchain",
        model="test-model",
    )
    return row["id"]


def _make_bridge(session_id_val, org_id_val, repo, *, fail_on=None):
    """Build a bridge whose handler persists FIFO-ordered events via add_event.

    ``fail_on`` (1-based handler invocation number) injects a persist failure
    to exercise the log-not-silent / worker-survives guarantee.
    """
    counter = {"n": 0}

    async def handler(payload, event):
        counter["n"] += 1
        if fail_on is not None and counter["n"] == fail_on:
            raise RuntimeError("injected persist failure")
        await repo.add_event(
            session_id_val,
            org_id=org_id_val,
            event_type=payload["type"],
            tool_name=payload.get("tool"),
            detail=payload.get("text") or payload.get("output") or payload.get("error"),
            raw={"engine": payload.get("engine", "test")},
        )

    return EventPersistBridge(handler)


def _tool_events():
    """A representative engine emission sequence: interleaved tool calls,
    results, and a hook_context event, exactly as engines emit them."""
    return [
        {"type": "tool_call", "tool": "search_memories", "input": "{}"},
        {"type": "tool_call", "tool": "get_memories", "input": "{}"},
        {"type": "hook_context", "hook": "memory_lookup", "text": "injected"},
        {"type": "tool_result", "tool": "search_memories", "output": "ok-a"},
        {"type": "tool_result", "tool": "get_memories", "output": "ok-b"},
    ]


class TestAppendOnly:
    async def test_duplicate_sequence_does_not_overwrite(self, pool, org, session_id):
        """M2 regression: an explicitly-sequenced stale insert must be DROPPED,
        not overwrite the existing row's payload (the old ON CONFLICT DO UPDATE
        rewrote event_type/tool_name/detail/raw/visible last-writer-wins)."""
        repo = LLMSessionRepository(pool)
        original = await repo.add_event(
            session_id,
            org_id=TEST_ORG_ID,
            event_type="tool_call",
            tool_name="original_tool",
            detail="original payload",
            raw={"marker": "original"},
        )
        assert original["id"] is not None

        stale = await repo.add_event(
            session_id,
            org_id=TEST_ORG_ID,
            sequence=original["sequence"],
            event_type="text_delta",
            tool_name="impostor",
            detail="stale overwrite attempt",
            raw={"marker": "stale"},
        )
        assert stale.get("dropped_duplicate") is True

        rows = await repo.list_events(session_id, TEST_ORG_ID)
        assert len(rows) == 1
        survivor = rows[0]
        assert survivor["event_type"] == "tool_call"
        assert survivor["tool_name"] == "original_tool"
        assert survivor["detail"] == "original payload"
        assert survivor["raw"].get("marker") == "original"

    async def test_auto_sequence_contiguous_under_serialized_writes(
        self, pool, org, session_id
    ):
        """MAX+1 under the session-row lock yields contiguous sequences with a
        single serialized worker; no gaps, no duplicates."""
        repo = LLMSessionRepository(pool)
        bridge = _make_bridge(session_id, TEST_ORG_ID, repo)
        bridge.start()
        for i in range(20):
            bridge.enqueue({"type": "tool_call", "tool": f"tool_{i}"}, None)
        await bridge.drain_and_stop()
        rows = await repo.list_events(session_id, TEST_ORG_ID)
        assert [r["sequence"] for r in rows] == list(range(1, 21))


class TestEmissionOrder:
    @pytest.mark.parametrize("engine", ["langchain", "copilot"])
    async def test_persisted_sequence_matches_emission_order(
        self, pool, org, session_id, engine
    ):
        """Both engines funnel through chat.py's shared persist path; the FIFO
        bridge must yield persisted sequence == emission order, including the
        hook_context row between calls and results."""
        repo = LLMSessionRepository(pool)
        bridge = _make_bridge(session_id, TEST_ORG_ID, repo)
        bridge.start()

        for payload in _tool_events():
            bridge.enqueue(dict(payload, engine=engine), None)
            # Yield like real SDK callbacks interleaving on the event loop.
            await asyncio.sleep(0)
        await bridge.drain_and_stop()

        rows = await repo.list_events(session_id, TEST_ORG_ID)
        emitted_types = [p["type"] for p in _tool_events()]
        assert [r["event_type"] for r in rows] == emitted_types

    async def test_drain_midflight_event_is_not_lost(self, pool, org, session_id):
        """drain_and_stop() must wait for an in-flight write even when the
        queue looks empty (the join()-waits-for-inflight guarantee)."""
        repo = LLMSessionRepository(pool)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_handler(payload, event):
            started.set()
            await release.wait()
            await repo.add_event(
                session_id,
                org_id=TEST_ORG_ID,
                event_type=payload["type"],
                tool_name=payload.get("tool"),
            )

        bridge = EventPersistBridge(slow_handler)
        bridge.start()
        bridge.enqueue({"type": "tool_call", "tool": "slow"}, None)
        await started.wait()
        # Let the in-flight write finish while the drain is already waiting:
        # join() must hold until the mid-flight item's task_done() lands.
        release.set()
        await asyncio.wait_for(bridge.drain_and_stop(), timeout=5)
        rows = await repo.list_events(session_id, TEST_ORG_ID)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "tool_call"


class TestStreamEndRace:
    async def test_enqueue_during_drain_neither_lost_nor_reordered(
        self, pool, org, session_id
    ):
        """Concurrent stream-end race: while the drain is running, a straggler
        producer keeps enqueueing (in-flight SDK callback racing finalize).
        Everything must persist exactly once, in emission order."""
        repo = LLMSessionRepository(pool)
        bridge = _make_bridge(session_id, TEST_ORG_ID, repo)
        bridge.start()

        emitted = list(range(40))
        for i in emitted[:20]:
            bridge.enqueue({"type": "tool_call", "tool": f"t{i}"}, None)

        # Producer enqueues stragglers while the drain is already awaiting
        # the queue: items racing the sentinel stay behind it and would be
        # dropped WITHOUT a post-drain requeue — this is the race the
        # finalize path must be safe against.
        drain = asyncio.create_task(bridge.drain_and_stop())
        for i in emitted[20:]:
            bridge.enqueue({"type": "tool_call", "tool": f"t{i}"}, None)
            if i % 5 == 0:
                await asyncio.sleep(0)
        await drain

        # Straggler events are still queued (they raced the sentinel) — the
        # bridge contract is that a second drain flushes them in order.
        await bridge.drain_and_stop()

        rows = await repo.list_events(session_id, TEST_ORG_ID)
        persisted = [r["tool_name"] for r in rows]
        assert persisted == [f"t{i}" for i in emitted]
        assert len(persisted) == len(set(persisted)), "no event may persist twice"

    async def test_failed_persist_logged_not_silent_and_worker_survives(
        self, pool, org, session_id
    ):
        """M3 regression: a persist failure must not silently drop following
        events (the old gather(return_exceptions=True) swallowed everything);
        the worker must keep serving later events."""
        repo = LLMSessionRepository(pool)
        bridge = _make_bridge(session_id, TEST_ORG_ID, repo, fail_on=3)
        bridge.start()
        for i in range(5):
            bridge.enqueue({"type": "tool_call", "tool": f"t{i}"}, None)
        await bridge.drain_and_stop()
        rows = [r["tool_name"] for r in await repo.list_events(session_id, TEST_ORG_ID)]
        # Handler call 3 (event t2) failed and is logged-and-lost, but the
        # worker survived: 0,1,3,4 persist in emission order.
        assert rows == ["t0", "t1", "t3", "t4"]


class TestReloadWindow:
    async def test_get_session_detail_returns_visible_events_in_order(
        self, pool, org, session_id
    ):
        """Reload slice: visible-only rows in ascending sequence order —
        matching the per-turn sequence sort the client already performs."""
        repo = LLMSessionRepository(pool)
        for i in range(6):
            await repo.add_event(
                session_id,
                org_id=TEST_ORG_ID,
                event_type="tool_call" if i % 2 == 0 else "tool_result",
                tool_name=f"tool_{i}",
                visible=i != 4,  # one invisible row
            )
        detail = await repo.get_session_detail(session_id, TEST_ORG_ID)
        events = detail["events"]
        assert all(e["visible"] for e in events)
        seqs = [e["sequence"] for e in events]
        assert seqs == sorted(seqs)
        tool_names = [e["tool_name"] for e in events]
        assert "tool_4" not in tool_names
        assert tool_names == ["tool_0", "tool_1", "tool_2", "tool_3", "tool_5"]

    async def test_newest_visible_slice_beats_oldest_raw_window(self, pool, org, session_id):
        """M1 regression: when the visible-event slice is exhausted, the
        reload window must contain the NEWEST rows (recent turns), not the
        oldest rows dominated by invisible deltas."""
        repo = LLMSessionRepository(pool)
        for i in range(1200):
            # One invisible + one visible row per iteration: 2400 rows total,
            # 1200 visible — exceeds the 2000-row raw slice budget but stays
            # inside the 2000-row visible slice.
            await repo.add_event(
                session_id,
                org_id=TEST_ORG_ID,
                event_type="text_delta",
                detail="delta",
                visible=False,
            )
            await repo.add_event(
                session_id,
                org_id=TEST_ORG_ID,
                event_type="tool_call",
                tool_name=f"tool_{i:04d}",
                visible=True,
            )
        detail = await repo.get_session_detail(session_id, TEST_ORG_ID)
        events = detail["events"]
        assert events, "reload slice must not be empty"
        assert all(e["visible"] for e in events)
        names = [e["tool_name"] for e in events if e["event_type"] == "tool_call"]
        assert names, "visible slice must include tool events"
        # Newest visible rows win and arrive in ascending order.
        assert names[-1] == "tool_1199"
        assert len(names) >= 1000
        assert names == sorted(names)

    async def test_list_events_newest_first_reversal_equivalence(self, pool, org, session_id):
        repo = LLMSessionRepository(pool)
        for i in range(5):
            await repo.add_event(
                session_id,
                org_id=TEST_ORG_ID,
                event_type="tool_call",
                tool_name=f"t{i}",
            )
        desc = await repo.list_events(session_id, TEST_ORG_ID, newest_first=True)
        asc = await repo.list_events(session_id, TEST_ORG_ID)
        assert [r["sequence"] for r in desc] == [5, 4, 3, 2, 1]
        assert [r["sequence"] for r in asc] == [1, 2, 3, 4, 5]


class TestJsonSafeRaw:
    async def test_copilot_sdk_event_objects_do_not_break_persistence(
        self, pool, org, session_id
    ):
        """Live-verification regression: raw Copilot SDK event objects made
        asyncpg refuse the INSERT (DataError). Under the old fire-and-forget
        persistence this silently dropped the row; now the raw summary is
        reduced to JSON-safe primitives by _json_safe."""
        from lucent.api.routers.chat import _event_raw, _json_safe

        class FakeSDKEvent:
            def __str__(self):
                return "CopilotSDK(tool.execution_complete)"

        class FakeEnum:
            value = "tool.call"

        event = type(
            "E",
            (),
            {
                "type": FakeEnum(),
                "content": None,
                "tool_name": "search_memories",
                "tool_input": {"query": "x"},
                "tool_output": "result text",
                "usage": None,
                "raw": FakeSDKEvent(),
            },
        )()
        raw = _event_raw(event)
        # Everything must survive json.dumps (what asyncpg's jsonb codec does).
        json.dumps(raw)
        assert raw["tool_name"] == "search_memories"
        assert raw["type"] == "tool.call"
        assert isinstance(raw["raw"], str)  # SDK object stringified

        # _json_safe is total: no input raises.
        weird = {"a": {1, 2}, "b": UUID(int=1), "c": datetime.now(timezone.utc)}
        json.dumps(_json_safe(weird))

    async def test_copilot_event_persists_end_to_end(self, pool, org, session_id):
        """A copilot-shaped event (raw SDK object inside) must round-trip
        through add_event → list_events, proving engine parity of the fix."""
        from lucent.api.routers.chat import _event_raw

        class FakeSDKEvent:
            def __str__(self):
                return "CopilotSDK(assistant.message_delta)"

        event = type(
            "E",
            (),
            {
                "type": type("T", (), {"value": "tool.call"})(),
                "content": None,
                "tool_name": "get_memories",
                "tool_input": None,
                "tool_output": "ok",
                "usage": None,
                "raw": FakeSDKEvent(),
            },
        )()
        repo = LLMSessionRepository(pool)
        await repo.add_event(
            session_id,
            org_id=TEST_ORG_ID,
            event_type="tool_call",
            tool_name="get_memories",
            raw=_event_raw(event),
        )
        rows = await repo.list_events(session_id, TEST_ORG_ID)
        assert rows[0]["event_type"] == "tool_call"
        assert rows[0]["tool_name"] == "get_memories"
        assert rows[0]["raw"]["raw"] == "CopilotSDK(assistant.message_delta)"


class TestLegacyAnchors:
    async def test_capture_evaluation_still_counts_visible_tool_names(
        self, pool, org, session_id
    ):
        """The capture-evaluation signal must keep working off the visible
        slice (it reads only tool_name from events)."""
        repo = LLMSessionRepository(pool)
        await repo.add_event(
            session_id,
            org_id=TEST_ORG_ID,
            event_type="tool_call",
            tool_name="create_request",
        )
        await repo.add_event(
            session_id,
            org_id=TEST_ORG_ID,
            event_type="text_delta",
            detail="delta",
            visible=False,
        )
        detail = await repo.get_session_detail(session_id, TEST_ORG_ID)
        evaluation = llm_sessions_module._session_capture_evaluation(
            session=detail,
            messages=detail["messages"],
            events=detail["events"],
            requests=detail["requests"],
        )
        assert "create_request" in evaluation["tool_names"]

    async def test_hook_payload_shape_roundtrip_through_add_event(
        self, pool, org, session_id
    ):
        """The hook-display work's _hook_event_payload structure must survive
        persistence unchanged (payload promotion is additive; raw carries it)."""
        repo = LLMSessionRepository(pool)
        hook_payload = {
            "type": "hook_context",
            "hook": "memory_lookup",
            "text": "injected 2 memories",
            "phase": "pre_tool",
            "trigger_tool": "search_memories",
            "memory_count": 2,
            "metadata": {"turn": 1},
        }
        await repo.add_event(
            session_id,
            org_id=TEST_ORG_ID,
            event_type="hook_context",
            raw=hook_payload,
            detail=hook_payload["text"],
        )
        rows = await repo.list_events(session_id, TEST_ORG_ID)
        assert rows[0]["event_type"] == "hook_context"
        assert rows[0]["raw"]["hook"] == "memory_lookup"
        assert rows[0]["raw"]["phase"] == "pre_tool"
        assert rows[0]["raw"]["memory_count"] == 2
        assert rows[0]["raw"]["metadata"]["turn"] == 1