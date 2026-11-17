"""Invariants for superseded task attempts.

A corrective continuation replaces the first attempt's text. ``tasks.result``
must read as a single terminal answer (the final attempt only), while the
replaced text remains retrievable and the terminal summary says what happened.

The dispatch tests drive the real ``_dispatch_tracked_tasks`` loop with the
session/API transport faked, so the assertions cover the code path the daemon
actually runs — not just the helpers in isolation.
"""

import json
from uuid import UUID, uuid4

import pytest

from daemon.dispatch.supersession import (
    REASON_MISSING_REQUIRED_TOOLS,
    REASON_STRUCTURED_OUTPUT_REPAIR,
    STRUCTURED_REPAIR_EVENT_TYPE,
    SUPERSEDED_EVENT_TYPE,
    SupersededAttempt,
    apply_repair_notice,
    apply_superseded_header,
    build_repair_notice,
    build_repair_replacement_summary,
    build_terminal_summary,
    find_superseded_event,
    record_superseded_attempt,
    superseded_event_metadata,
)

TASK_ID = "11111111-1111-1111-1111-111111111111"
REQUEST_ID = "22222222-2222-2222-2222-222222222222"
ORG_ID = "33333333-3333-3333-3333-333333333333"
USER_ID = "44444444-4444-4444-4444-444444444444"

HANDOFF_TASK = "Store the summary with send a handoff to the user when done."
PLAIN_TASK = "Summarize the investigation findings in two paragraphs."

PASS_ONE = (
    "I investigated the repository. I could not complete the handoff because "
    "send_handoff is not available in this session, so the summary is inline "
    "here for the record."
)
PASS_TWO = (
    "Called send_handoff with the summary (handoff id h-4242). The work is "
    "complete and the user can retrieve the handoff from the handoffs area."
)


# ── pure invariants ─────────────────────────────────────────────────────


def test_single_pass_result_is_byte_identical_without_supersession():
    text = "  exact bytes\nwith trailing spaces  \n\n"
    assert apply_superseded_header(text, None) == text
    assert apply_superseded_header("", None) == ""
    assert apply_superseded_header(None, None) is None


def test_two_passes_keep_only_the_final_text_plus_a_header():
    superseded = SupersededAttempt(
        text=PASS_ONE,
        attempt=1,
        total_attempts=2,
        reason=REASON_MISSING_REQUIRED_TOOLS,
        missing_tools=("send_handoff",),
        event_id="evt-1",
    )
    result = apply_superseded_header(PASS_TWO, superseded)

    assert result.startswith("[superseded attempt 1 of 2]")
    assert PASS_TWO in result
    assert PASS_ONE not in result
    # Where it is retrievable, machine-readably.
    assert "reason=missing_required_tools" in result
    assert "missing_tools=send_handoff" in result
    assert "task_event:superseded_output:evt-1" in result
    # Only the final attempt follows the header.
    assert result.split("\n\n", 1)[1] == PASS_TWO


def test_superseded_event_metadata_holds_the_full_text():
    superseded = SupersededAttempt(text=PASS_ONE, missing_tools=("send_handoff",))
    metadata = superseded_event_metadata(superseded)

    # The event payload is exactly the documented shape: no lossy excerpt.
    assert metadata == {
        "attempt": 1,
        "reason": {
            "code": REASON_MISSING_REQUIRED_TOOLS,
            "missing_tools": ["send_handoff"],
        },
        "chars": len(PASS_ONE),
        "text": PASS_ONE,
    }
    assert metadata["text"] == PASS_ONE  # equality, not containment


def test_empty_prior_pass_gets_no_header_and_no_leading_separator():
    """Nothing was produced, so nothing was superseded — and no separator."""
    for empty in ("", None, "   \n"):
        superseded = SupersededAttempt(
            text=empty or "",
            missing_tools=("send_handoff",),
            event_id="evt-1",
        )
        result = apply_superseded_header(PASS_TWO, superseded)
        assert result == PASS_TWO
        assert not result.startswith("--- ")
        assert "[superseded attempt" not in result


def test_terminal_summary_source_skips_superseded_prose_and_separator():
    from lucent.db.requests import _terminal_summary_source

    header_form = (
        "[superseded attempt 1 of 2] attempt=1/2; reason=missing_required_tools; "
        "missing_tools=send_handoff; source=task_event:superseded_output:evt-1. "
        "Only the final attempt's text follows.\n\n" + PASS_TWO
    )
    assert _terminal_summary_source(header_form) == PASS_TWO

    legacy_form = f"{PASS_ONE}\n\n--- Completion continuation ---\n\n{PASS_TWO}"
    assert _terminal_summary_source(legacy_form) == PASS_TWO

    assert _terminal_summary_source("plain final text") == "plain final text"
    assert _terminal_summary_source(None) is None


def test_header_never_repeats_the_superseded_claim_as_terminal():
    """Contradiction regression: pass 1 says the tool was unavailable; pass 2
    uses it. A reader of ``result`` must not encounter the false claim."""
    superseded = SupersededAttempt(
        text=PASS_ONE, missing_tools=("send_handoff",), event_id="evt-1"
    )
    result = apply_superseded_header(PASS_TWO, superseded)

    assert "not available" not in result
    assert "could not complete" not in result
    assert "send_handoff" in result  # the header names what was missed
    # The retrievable copy still contains the contradiction, as evidence.
    assert "not available" in superseded_event_metadata(superseded)["text"]


def test_long_superseded_pass_is_preserved_without_truncation():
    long_text = ("A" * 50_100) + " TAIL-MARKER-9f3c"
    assert len(long_text) > 50_000
    superseded = SupersededAttempt(text=long_text, missing_tools=("send_handoff",))

    metadata = superseded_event_metadata(superseded)
    # The event travels over the HTTP API as JSON and is stored as JSONB; a
    # JSON round-trip is the lossless check that matters here.
    round_tripped = json.loads(json.dumps(metadata))
    assert round_tripped["text"] == long_text
    assert round_tripped["text"].endswith("TAIL-MARKER-9f3c")
    assert round_tripped["chars"] == len(long_text)

    result = apply_superseded_header(PASS_TWO, superseded)
    assert PASS_TWO in result  # no exception, final text intact


@pytest.mark.asyncio
async def test_recording_is_idempotent_and_header_application_is_idempotent():
    stored: list[dict] = []

    async def list_events():
        return list(stored)

    async def add_event(event_type, detail, metadata):
        row = {
            "id": f"event-{len(stored) + 1}",
            "event_type": event_type,
            "detail": detail,
            "metadata": metadata,
        }
        stored.append(row)
        return row

    superseded = SupersededAttempt(text=PASS_ONE, missing_tools=("send_handoff",))

    first = await record_superseded_attempt(
        superseded, list_events=list_events, add_event=add_event
    )
    second = await record_superseded_attempt(
        superseded, list_events=list_events, add_event=add_event
    )

    assert first is not None and second is not None
    assert first["id"] == second["id"]
    assert len(stored) == 1, "re-running finalization must not duplicate the event"
    assert stored[0]["event_type"] == SUPERSEDED_EVENT_TYPE
    assert stored[0]["metadata"]["text"] == PASS_ONE

    with_event = superseded.with_event(first["id"])
    once = apply_superseded_header(PASS_TWO, with_event)
    twice = apply_superseded_header(once, with_event)
    assert twice == once
    assert twice.count("[superseded attempt") == 1


def test_find_superseded_event_handles_jsonb_string_metadata():
    events = [
        {"event_type": "other", "metadata": {}},
        {
            "event_type": SUPERSEDED_EVENT_TYPE,
            "metadata": json.dumps({"attempt": 1, "superseded_result": "old"}),
        },
    ]
    found = find_superseded_event(events, 1)
    assert found is not None
    assert find_superseded_event(events, 2) is None


def test_terminal_summary_is_one_line_and_names_both_attempts():
    superseded = SupersededAttempt(text=PASS_ONE, missing_tools=("send_handoff",))
    summary = build_terminal_summary(
        superseded, succeeded=True, satisfied_tools=["send_handoff"]
    )

    assert "\n" not in summary
    assert summary == (
        "Final attempt 2 of 2 succeeded; send_handoff called. "
        "Attempt 1 missed required tool (send_handoff) and is superseded."
    )


# ── structured-output repair displacement (pure invariants) ────────────

REPAIR_CONTRACT = {
    "on_failure": "retry_then_fallback",
    "max_retries": 1,
    "json_schema": {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    },
}
REPAIRED_OUTPUT = '<task_output>\n{"summary": "Repaired summary."}\n</task_output>'


def test_repair_notice_says_the_json_was_not_the_models_answer():
    displaced = SupersededAttempt(
        text=PASS_ONE,
        attempt=1,
        total_attempts=1,
        reason=REASON_STRUCTURED_OUTPUT_REPAIR,
        event_id="evt-repair",
    )
    notice = build_repair_notice(displaced)

    assert notice.startswith("[structured output repaired]")
    assert "not by the model as its answer" in notice
    assert "task_event:superseded_output:evt-repair" in notice
    # Free of the failure phrasings the completion validator treats as blockers.
    for indicator in ("failed to", "unable to", "could not", "missing required tooling"):
        assert indicator not in notice.lower()


def test_repair_notice_is_not_inserted_without_a_replacement():
    assert apply_repair_notice(PASS_TWO, None) == PASS_TWO
    assert apply_repair_notice(PASS_TWO, None, replaced=False) == PASS_TWO
    assert apply_repair_notice(None, None, replaced=False) is None


def test_repair_notice_follows_the_superseded_header_and_is_idempotent():
    superseded = SupersededAttempt(
        text=PASS_ONE, attempt=1, total_attempts=2, missing_tools=("send_handoff",),
        event_id="evt-1",
    )
    displaced = SupersededAttempt(
        text=PASS_TWO,
        attempt=2,
        total_attempts=2,
        reason=REASON_STRUCTURED_OUTPUT_REPAIR,
        event_id="evt-2",
    )
    composed = apply_superseded_header(REPAIRED_OUTPUT, superseded)
    composed = apply_repair_notice(composed, displaced, replaced=True)

    assert composed.startswith("[superseded attempt 1 of 2]")
    assert composed.index("[superseded attempt") < composed.index(
        "[structured output repaired]"
    )
    assert composed.endswith(REPAIRED_OUTPUT)
    assert composed.count("[structured output repaired]") == 1
    # Re-applying is a no-op over the already-composed text.
    assert apply_repair_notice(composed, displaced, replaced=True) == composed


def test_repair_replacement_summary_names_the_terminal_state():
    displaced = SupersededAttempt(
        text=PASS_TWO,
        attempt=2,
        total_attempts=2,
        reason=REASON_STRUCTURED_OUTPUT_REPAIR,
        event_id="evt-2",
    )
    summary = build_repair_replacement_summary(displaced)
    assert summary.startswith("Structured output was repaired")
    assert "task_event:superseded_output:evt-2" in summary
    # Nothing was produced before the repair: say so rather than inventing a source.
    assert "No pre-repair text was present." in build_repair_replacement_summary(None)


# ── dispatch end-to-end (transport faked) ──────────────────────────────


def _task(description: str, output_contract: dict | None = None) -> dict:
    task = {
        "id": UUID(TASK_ID),
        "request_id": UUID(REQUEST_ID),
        "organization_id": UUID(ORG_ID),
        "title": "Analyze the request",
        "description": description,
        "agent_type": "code",
        "requesting_user_id": UUID(USER_ID),
        "claim_version": 1,
    }
    if output_contract is not None:
        task["output_contract"] = output_contract
    return task


async def _harness(
    monkeypatch,
    *,
    description,
    first_text,
    second_text,
    output_contract=None,
    continuation_tools=("send_handoff",),
    repair_text=None,
    validate_result=True,
):
    """Drive ``_dispatch_tracked_tasks`` with faked transport and sessions."""
    import daemon.daemon as daemon_module
    from daemon.daemon import LucentDaemon

    events: list[dict] = []
    calls: dict[str, list] = {
        "completed": [],
        "needs_review": [],
        "sessions": [],
        "failed": [],
    }
    queue = [_task(description, output_contract)]

    async def _noop(*_a, **_k):
        return None

    async def _pending():
        return list(queue)

    async def _claim(task_id, _instance_id):
        return {"id": task_id, "claim_version": 1}

    async def _start(task_id, _instance_id=None):
        return {"id": task_id}

    async def _complete(task_id, result, instance_id=None, **kwargs):
        calls["completed"].append((task_id, result, kwargs))
        # Mirrors RequestRepository.complete_task, which records its terminal
        # event AFTER the row update. Ordering against this is the invariant.
        events.append(
            {
                "id": f"event-{uuid4()}",
                "event_type": "completed",
                "detail": f"Completed ({len(result or '')} chars output)",
                "metadata": {},
            }
        )
        return {"id": task_id}

    async def _fail(task_id, error, instance_id=None, result=None):
        calls["failed"].append((task_id, error, result))
        return {"id": task_id}

    async def _needs_review(
        task_id, error, instance_id=None, result=None, result_summary=None
    ):
        calls["needs_review"].append((result, result_summary))
        # Mirrors RequestRepository.mark_task_needs_review.
        events.append(
            {
                "id": f"event-{uuid4()}",
                "event_type": "needs_review",
                "detail": f"Manual review required: {error[:200]}",
                "metadata": {"rejected_output_chars": len(result or "")},
            }
        )
        return {"id": task_id}

    async def _event(task_id, event_type, detail=None, metadata=None):
        row = {
            "id": f"event-{uuid4()}",
            "event_type": event_type,
            "detail": detail,
            "metadata": metadata or {},
        }
        events.append(row)
        return row

    async def _get_task_events(task_id, limit=200):
        return list(events)

    async def _run_session(session_name, system_message, prompt, **kwargs):
        calls["sessions"].append(session_name)
        daemon._session_terminal_outcomes[session_name] = {
            "completed": True,
            "error": None,
        }
        if session_name.startswith("repair-"):
            daemon._session_tool_trackers[session_name] = []
            return repair_text if repair_text is not None else first_text
        if session_name.endswith("-completion"):
            daemon._session_tool_trackers[session_name] = [
                {"tool": tool} for tool in continuation_tools
            ]
            return second_text
        daemon._session_tool_trackers[session_name] = []
        return first_text

    async def _select_model(**_kw):
        return "chat-a", "selected"

    async def _accessible(_user_id, _org_id):
        return []

    async def _agent(**_kw):
        return {"id": "55555555-5555-5555-5555-555555555555", "name": "code", "content": "agent"}

    async def _list(*_a, **_k):
        return []

    async def _prompt(*_a, **_k):
        return "system prompt"

    async def _mint(**_kw):
        return "hs_task_scoped"

    async def _request(*_a, **_k):
        return None

    async def _request_context(*_a, **_k):
        return "", ""

    daemon = LucentDaemon()
    monkeypatch.setattr(daemon, "_ensure_request_review_tasks", _noop)
    monkeypatch.setattr(daemon, "run_session", _run_session)
    if validate_result:
        monkeypatch.setattr(daemon, "_validate_task_result", lambda *a, **k: (True, ""))
    monkeypatch.setattr(daemon_module, "MCP_CONFIG", {"memory-server": {"type": "http"}})
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_pending_tasks", _pending)
    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", _claim)
    monkeypatch.setattr("daemon.daemon.RequestAPI.update_task_model_settings", _noop)
    monkeypatch.setattr("daemon.daemon.RequestAPI.start_task", _start)
    monkeypatch.setattr("daemon.daemon.RequestAPI.complete_task", _complete)
    monkeypatch.setattr("daemon.daemon.RequestAPI.fail_task", _fail)
    monkeypatch.setattr("daemon.daemon.RequestAPI.mark_task_needs_review", _needs_review)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _event)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_task_events", _get_task_events)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request_context", _request_context)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request", _request)
    monkeypatch.setattr("daemon.daemon.load_accessible_agent", _agent)
    monkeypatch.setattr("daemon.daemon.load_accessible_skills_for_agent", _list)
    monkeypatch.setattr("daemon.daemon.load_accessible_mcp_servers_for_agent", _list)
    monkeypatch.setattr("daemon.daemon.load_accessible_hooks_for_agent", _list)
    monkeypatch.setattr("daemon.daemon.load_accessible_managed_tools_for_agent", _list)
    monkeypatch.setattr("daemon.daemon.build_subagent_prompt", _prompt)
    monkeypatch.setattr("daemon.daemon._mint_scoped_api_key", _mint)
    monkeypatch.setattr("daemon.daemon._select_model_for_user", _select_model)
    monkeypatch.setattr("daemon.daemon._accessible_models_for_user", _accessible)
    return daemon, calls, events


@pytest.mark.asyncio
async def test_dispatch_single_pass_is_unchanged_and_emits_no_superseded_event(
    monkeypatch,
):
    daemon, calls, events = await _harness(
        monkeypatch,
        description=PLAIN_TASK,
        first_text="A complete single-pass answer with enough detail to stand alone.",
        second_text="unused",
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert len(calls["sessions"]) == 1
    _task_id, result, kwargs = calls["completed"][0]
    assert result == "A complete single-pass answer with enough detail to stand alone."
    # No fabricated supersession summary; the contract summary path is intact.
    assert kwargs["result_summary"] is None
    assert not any(e["event_type"] == SUPERSEDED_EVENT_TYPE for e in events)


@pytest.mark.asyncio
async def test_dispatch_two_passes_final_text_plus_header_and_durable_prior_text(
    monkeypatch,
):
    daemon, calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text=PASS_TWO,
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert len(calls["sessions"]) == 2
    _task_id, result, kwargs = calls["completed"][0]

    # A: only the final pass, plus the compact provenance header.
    assert PASS_TWO in result
    assert PASS_ONE not in result
    assert result.startswith("[superseded attempt 1 of 2]")

    # B: the full pre-repair text is retrievable from the durable event.
    superseded_events = [e for e in events if e["event_type"] == SUPERSEDED_EVENT_TYPE]
    assert len(superseded_events) == 1
    metadata = superseded_events[0]["metadata"]
    assert metadata["text"] == PASS_ONE  # equality, not containment
    assert metadata["chars"] == len(PASS_ONE)
    assert metadata["reason"] == {
        "code": REASON_MISSING_REQUIRED_TOOLS,
        "missing_tools": ["send_handoff"],
    }
    assert superseded_events[0]["id"] in result

    # C: one-line terminal state, never null.
    assert kwargs["result_summary"] == (
        "Final attempt 2 of 2 succeeded; send_handoff called. "
        "Attempt 1 missed required tool (send_handoff) and is superseded."
    )

    # The repair event still marks the transition for the activity feed.
    assert [e["event_type"] for e in events].count("tool_requirement_repair") == 1


@pytest.mark.asyncio
async def test_dispatch_contradiction_is_not_terminal(monkeypatch):
    daemon, calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text=PASS_TWO,
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    _task_id, result, _kwargs = calls["completed"][0]
    assert "not available" not in result
    assert "could not complete" not in result
    # The evidence is not lost: it lives in the event, not in `result`.
    superseded = [e for e in events if e["event_type"] == SUPERSEDED_EVENT_TYPE][0]
    assert "not available" in superseded["metadata"]["text"]


@pytest.mark.asyncio
async def test_superseded_event_precedes_the_completed_event(monkeypatch):
    """The durable record must exist before the terminal row is written."""
    daemon, _calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text=PASS_TWO,
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    types = [e["event_type"] for e in events]
    assert types.count(SUPERSEDED_EVENT_TYPE) == 1
    assert SUPERSEDED_EVENT_TYPE in types
    assert "completed" in types
    assert types.index(SUPERSEDED_EVENT_TYPE) < types.index("completed")


@pytest.mark.asyncio
async def test_superseded_event_survives_a_failed_terminal_write(monkeypatch):
    """The prior pass is durable even when the completion write never lands."""
    daemon, calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text=PASS_TWO,
    )

    import daemon.daemon as daemon_module

    async def _drop(task_id, result, instance_id=None, **kwargs):
        return None

    monkeypatch.setattr(daemon_module.RequestAPI, "complete_task", _drop)

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert calls["completed"] == []
    superseded = [e for e in events if e["event_type"] == SUPERSEDED_EVENT_TYPE]
    assert len(superseded) == 1
    assert superseded[0]["metadata"]["text"] == PASS_ONE
    assert any(e["event_type"] == "result_persist_failed" for e in events)


@pytest.mark.asyncio
async def test_needs_review_branch_emits_superseded_event_first(monkeypatch):
    """The needs-review branch supersedes too, with the record already durable."""
    daemon, calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text="I described the handoff but did not call the tool.",
        continuation_tools=(),  # continuation still misses the required call
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert calls["completed"] == []
    assert len(calls["needs_review"]) == 1
    result, summary = calls["needs_review"][0]
    assert result.startswith("[superseded attempt 1 of 2]")
    assert PASS_ONE not in result
    assert summary == (
        "Final attempt 2 of 2 did not satisfy required tool work. "
        "Attempt 1 missed required tool (send_handoff) and is superseded."
    )

    superseded = [e for e in events if e["event_type"] == SUPERSEDED_EVENT_TYPE]
    assert len(superseded) == 1
    assert superseded[0]["metadata"]["text"] == PASS_ONE

    types = [e["event_type"] for e in events]
    assert "needs_review" in types
    assert types.index(SUPERSEDED_EVENT_TYPE) < types.index("needs_review")


@pytest.mark.asyncio
async def test_large_superseded_pass_is_stored_whole_beyond_the_result_cap(
    monkeypatch,
):
    """The event holds the prior pass in full even past result[:50000]."""
    large_first = ("A" * 50_100) + " TAIL-MARKER-9f3c"
    daemon, _calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=large_first,
        second_text=PASS_TWO,
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    superseded = [e for e in events if e["event_type"] == SUPERSEDED_EVENT_TYPE]
    assert len(superseded) == 1
    stored = superseded[0]["metadata"]["text"]
    assert len(stored) == len(large_first)
    assert stored == large_first  # no truncation at 50k
    assert stored.endswith("TAIL-MARKER-9f3c")
    assert superseded[0]["metadata"]["chars"] == len(large_first)


@pytest.mark.asyncio
async def test_completion_transport_caps_final_result_at_50000(monkeypatch):
    """Documented limit: the final `result` is capped at 50k on completion.

    This is the one lossy point the event path deliberately avoids; it is
    asserted here so the limit is explicit rather than assumed.
    """
    from daemon.api.tasks import TaskAPI

    captured: dict = {}

    async def _capture(path, body, operation, **kwargs):
        captured.update(body)
        return {"id": TASK_ID}

    monkeypatch.setattr(TaskAPI, "_post_result", _capture)

    oversized = "Z" * 50_100
    await TaskAPI.complete_task(TASK_ID, oversized)

    assert len(captured["result"]) == 50_000
    assert captured["result"] == "Z" * 50_000


@pytest.mark.asyncio
async def test_dispatch_empty_prior_pass_gets_no_header_or_separator(monkeypatch):
    daemon, calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text="",
        second_text=PASS_TWO,
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert len(calls["sessions"]) == 2
    _task_id, result, kwargs = calls["completed"][0]
    assert result == PASS_TWO
    assert not result.startswith("--- ")
    assert "[superseded attempt" not in result
    assert kwargs["result_summary"] is None
    # Nothing was superseded, so no superseded-attempt event is fabricated.
    assert not any(e["event_type"] == SUPERSEDED_EVENT_TYPE for e in events)


@pytest.mark.asyncio
async def test_dispatch_contract_summary_is_not_overridden_by_terminal_state(
    monkeypatch,
):
    contract = {
        "json_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        }
    }
    contract_final = (
        "Done.\n\n<task_output>"
        '{"summary": "Contract-supplied summary."}'
        "</task_output>"
    )

    daemon, calls, _events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text=contract_final,
        output_contract=contract,
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    _task_id, result, kwargs = calls["completed"][0]
    assert "[superseded attempt" in result  # provenance still recorded
    assert kwargs["result_summary"] == "Contract-supplied summary."
    # The typed payload is untouched by the supersession path.
    assert kwargs["result_structured"] == {"summary": "Contract-supplied summary."}


@pytest.mark.asyncio
async def test_dispatch_repeated_finalization_does_not_duplicate(monkeypatch):
    daemon, calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text=PASS_TWO,
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)
    await daemon._dispatch_tracked_tasks(max_tasks=1)

    superseded_events = [e for e in events if e["event_type"] == SUPERSEDED_EVENT_TYPE]
    assert len(superseded_events) == 1, "second finalization must reuse the event"
    for _task_id, result, _kwargs in calls["completed"]:
        assert result.count("[superseded attempt") == 1
        assert PASS_ONE not in result


# ── structured-output repair displacement (dispatch end-to-end) ────────


@pytest.mark.asyncio
async def test_structured_repair_preserves_both_displaced_passes(monkeypatch):
    """Two passes where the final pass is itself replaced by structured repair.

    `tasks.result` is the repair output and is labelled as such; BOTH displaced
    texts (attempt 1 and the final attempt) stay retrievable from task events.
    """
    daemon, calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text=PASS_TWO,  # no <task_output> block → repair runs
        output_contract=REPAIR_CONTRACT,
        repair_text=REPAIRED_OUTPUT,
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert len(calls["sessions"]) == 3  # attempt 1, continuation, repair
    _task_id, result, kwargs = calls["completed"][0]

    # The stored result is the repair output, and says that is what it is.
    assert result.startswith("[superseded attempt 1 of 2]")
    assert "[structured output repaired]" in result
    assert "not by the model as its answer" in result
    assert result.endswith(REPAIRED_OUTPUT)
    assert PASS_TWO not in result

    repair_records = [
        e
        for e in events
        if e["event_type"] == SUPERSEDED_EVENT_TYPE
        and e["metadata"].get("reason", {}).get("code")
        == REASON_STRUCTURED_OUTPUT_REPAIR
    ]
    assert len(repair_records) == 1
    record = repair_records[0]
    assert record["metadata"] == {
        "attempt": 2,
        "reason": {"code": REASON_STRUCTURED_OUTPUT_REPAIR, "missing_tools": []},
        "chars": len(PASS_TWO),
        "text": PASS_TWO,
    }
    assert record["id"] in result  # the notice points at the durable copy

    # Attempt 1 is still preserved from Step B, untouched.
    assert any(
        e["metadata"].get("text") == PASS_ONE
        and e["metadata"].get("reason", {}).get("code")
        == REASON_MISSING_REQUIRED_TOOLS
        for e in events
        if e["event_type"] == SUPERSEDED_EVENT_TYPE
    )

    # The terminal summary says both what happened and where the text went.
    summary = kwargs["result_summary"]
    assert summary.startswith("Structured output was repaired")
    assert f"task_event:superseded_output:{record['id']}" in summary
    assert "Final attempt 2 of 2 succeeded" in summary
    assert kwargs["validation_status"] == "repair_succeeded"
    assert kwargs["result_structured"] == {"summary": "Repaired summary."}


@pytest.mark.asyncio
async def test_structured_repair_single_pass_preserves_pre_repair_text(monkeypatch):
    daemon, calls, events = await _harness(
        monkeypatch,
        description=PLAIN_TASK,
        first_text=(
            "A narrative answer that never emitted the requested contract block. "
            "It is long enough to pass completion validation and stands alone."
        ),
        second_text="unused",
        output_contract=REPAIR_CONTRACT,
        repair_text=REPAIRED_OUTPUT,
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert len(calls["sessions"]) == 2  # task + repair
    _task_id, result, kwargs = calls["completed"][0]

    assert result.startswith("[structured output repaired]")
    assert result.endswith(REPAIRED_OUTPUT)
    assert "A narrative answer that never emitted" not in result

    records = [e for e in events if e["event_type"] == SUPERSEDED_EVENT_TYPE]
    assert len(records) == 1
    assert records[0]["metadata"]["attempt"] == 1
    assert records[0]["metadata"]["chars"] == len(records[0]["metadata"]["text"])
    assert "A narrative answer that never emitted" in records[0]["metadata"]["text"]
    assert records[0]["metadata"]["reason"]["code"] == REASON_STRUCTURED_OUTPUT_REPAIR
    assert records[0]["id"] in result

    assert kwargs["result_summary"].startswith("Structured output was repaired")
    assert "Repaired summary." in kwargs["result_summary"]


@pytest.mark.asyncio
async def test_repair_preservation_precedes_repair_event_and_completion(monkeypatch):
    daemon, _calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text=PASS_TWO,
        output_contract=REPAIR_CONTRACT,
        repair_text=REPAIRED_OUTPUT,
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    types = [e["event_type"] for e in events]
    repair_record_index = max(
        index
        for index, event in enumerate(events)
        if event["event_type"] == SUPERSEDED_EVENT_TYPE
        and event["metadata"].get("text") == PASS_TWO
    )
    assert repair_record_index < types.index(STRUCTURED_REPAIR_EVENT_TYPE)
    assert types.index(STRUCTURED_REPAIR_EVENT_TYPE) < types.index("completed")


@pytest.mark.asyncio
async def test_valid_contract_output_emits_no_repair_displacement(monkeypatch):
    """The normal path is untouched: no notice, no displacement event, no noise."""
    contract_final = (
        "Done.\n\n<task_output>"
        '{"summary": "Contract-supplied summary."}'
        "</task_output>"
    )
    daemon, calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text=contract_final,
        output_contract=REPAIR_CONTRACT,
        repair_text=REPAIRED_OUTPUT,
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert len(calls["sessions"]) == 2  # repair session never started
    _task_id, result, kwargs = calls["completed"][0]
    assert "[structured output repaired]" not in result
    assert result.endswith(contract_final)
    assert kwargs["result_summary"] == "Contract-supplied summary."

    repair_records = [
        e
        for e in events
        if e["event_type"] == SUPERSEDED_EVENT_TYPE
        and e["metadata"].get("reason", {}).get("code")
        == REASON_STRUCTURED_OUTPUT_REPAIR
    ]
    assert repair_records == []
    assert not any(e["event_type"] == STRUCTURED_REPAIR_EVENT_TYPE for e in events)


@pytest.mark.asyncio
async def test_repair_displacement_is_recorded_once_across_finalizations(monkeypatch):
    daemon, calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text=PASS_TWO,
        output_contract=REPAIR_CONTRACT,
        repair_text=REPAIRED_OUTPUT,
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)
    await daemon._dispatch_tracked_tasks(max_tasks=1)

    repair_records = [
        e
        for e in events
        if e["event_type"] == SUPERSEDED_EVENT_TYPE
        and e["metadata"].get("reason", {}).get("code")
        == REASON_STRUCTURED_OUTPUT_REPAIR
    ]
    assert len(repair_records) == 1, "re-running finalization must reuse the record"
    for _task_id, result, _kwargs in calls["completed"]:
        assert result.count("[structured output repaired]") == 1
        assert PASS_TWO not in result


@pytest.mark.asyncio
async def test_repair_displacement_does_not_reuse_a_stale_text_record():
    stored = [
        {
            "id": "event-old",
            "event_type": SUPERSEDED_EVENT_TYPE,
            "metadata": {
                "attempt": 2,
                "reason": {"code": REASON_STRUCTURED_OUTPUT_REPAIR, "missing_tools": []},
                "chars": 3,
                "text": "old",
            },
        }
    ]

    async def list_events():
        return list(stored)

    async def add_event(event_type, detail, metadata):
        row = {
            "id": f"event-{len(stored) + 1}",
            "event_type": event_type,
            "detail": detail,
            "metadata": metadata,
        }
        stored.append(row)
        return row

    fresh = SupersededAttempt(
        text="a newer displaced answer",
        attempt=2,
        total_attempts=2,
        reason=REASON_STRUCTURED_OUTPUT_REPAIR,
    )
    recorded = await record_superseded_attempt(
        fresh, list_events=list_events, add_event=add_event, match_text=True
    )
    assert recorded is not None
    assert recorded["metadata"]["text"] == fresh.text
    assert len(stored) == 2

    again = await record_superseded_attempt(
        fresh, list_events=list_events, add_event=add_event, match_text=True
    )
    assert again["id"] == recorded["id"]
    assert len(stored) == 2


# ── completion validator vs. the mandated supersedes opening ───────────
#
# A superseded task's final pass must open by naming the earlier attempt and
# what it missed. That sentence is prose about the *replaced* attempt, so
# failure phrasing inside it must not fail the final task — while a final pass
# whose outcome section genuinely reports failure still must.

SUPERSEDES_OPENING = (
    "This response supersedes the prior attempt, which failed to actually call "
    "the `send_handoff` tool."
)
SUPERSEDES_SUCCESS = (
    SUPERSEDES_OPENING
    + "\n\nI have now successfully sent the handoff. The handoff carries the "
    "completed analysis and the user can retrieve it from the handoffs area."
)


@pytest.mark.asyncio
async def test_dispatch_supersedes_opening_does_not_fail_a_successful_final_pass(
    monkeypatch,
):
    daemon, calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text=SUPERSEDES_SUCCESS,
        validate_result=False,  # exercise the real completion validator
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert calls["failed"] == []
    assert calls["needs_review"] == []
    assert len(calls["completed"]) == 1
    _task_id, result, _kwargs = calls["completed"][0]
    # The supersession really happened and the phrase is still recorded
    # verbatim in the stored result; only the validator's reading of it changed.
    assert result.startswith("[superseded attempt 1 of 2]")
    assert "failed to" in result
    assert not any(e["event_type"] == "validation_failed" for e in events)


@pytest.mark.asyncio
async def test_dispatch_genuine_final_failure_still_fails_after_supersession(
    monkeypatch,
):
    final_text = (
        SUPERSEDES_OPENING
        + "\n\nThe handoff is not delivered. The send_handoff call failed to "
        "send the payload, so the task needs another attempt."
    )
    daemon, calls, events = await _harness(
        monkeypatch,
        description=HANDOFF_TASK,
        first_text=PASS_ONE,
        second_text=final_text,
        validate_result=False,  # exercise the real completion validator
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert calls["completed"] == []
    assert len(calls["failed"]) == 1
    _task_id, error, _result = calls["failed"][0]
    assert error == "failure indicator found: 'failed to'"
    assert any(e["event_type"] == "validation_failed" for e in events)


def test_validator_without_supersession_is_unchanged():
    from daemon.daemon import LucentDaemon

    daemon = LucentDaemon()
    texts = (
        SUPERSEDES_SUCCESS,
        "The job failed to run and could not find the input file, so the "
        "migration did not run. Retry it once the paths are corrected.",
        "Status: blocked\n\nNo GitHub MCP tool is available and neither is "
        "exposed to this sub-agent, so the work cannot proceed.",
        "The handoff was sent and the analysis is complete. " * 3,
    )
    for text in texts:
        # Omitting the flag is byte-identical to passing it as False.
        assert daemon._validate_task_result(text) == daemon._validate_task_result(
            text, superseded=False
        )
    # Today's behavior, preserved: no supersession means the supersedes
    # sentence is ordinary prose and its failure phrasing still flags.
    ok, reason = daemon._validate_task_result(SUPERSEDES_SUCCESS)
    assert ok is False
    assert reason == "failure indicator found: 'failed to'"


def test_validator_ignores_only_the_supersedes_opening():
    from daemon.daemon import LucentDaemon

    daemon = LucentDaemon()

    ok, reason = daemon._validate_task_result(SUPERSEDES_SUCCESS, superseded=True)
    assert ok is True, reason

    # The outcome section is still scanned: a final pass that reports failure
    # fails even when its opening legitimately describes the replaced attempt.
    outcome_failure = (
        SUPERSEDES_OPENING
        + "\n\nThe send_handoff call failed to complete, so nothing was delivered."
    )
    ok, reason = daemon._validate_task_result(outcome_failure, superseded=True)
    assert ok is False
    assert reason == "failure indicator found: 'failed to'"


def test_validator_only_exempts_a_leading_statement_that_names_supersession():
    from daemon.daemon import LucentDaemon

    daemon = LucentDaemon()

    # A later failure paragraph is still read even when the opening is a
    # supersedes statement.
    later = SUPERSEDES_SUCCESS + "\n\nFinally, the cleanup failed to remove the temp files."
    ok, _reason = daemon._validate_task_result(later, superseded=True)
    assert ok is False

    # A first paragraph with no supersession cue is not exempt prose.
    no_cue = (
        "Setup could not find the configuration file for this environment.\n\n"
        "The handoff was sent and the analysis is complete and ready for "
        "retrieval by the user."
    )
    ok, reason = daemon._validate_task_result(no_cue, superseded=True)
    assert ok is False
    assert reason == "failure indicator found: 'could not find'"

    # A statement that leaves no outcome text behind is not exempt either.
    only_opening = (
        "This response supersedes the prior attempt, which failed to call "
        "send_handoff and therefore delivered nothing to the user in that "
        "earlier pass."
    )
    ok, reason = daemon._validate_task_result(only_opening, superseded=True)
    assert ok is False
    assert reason == "failure indicator found: 'failed to'"


# ── failure path: result_summary is the terminal state ──────────────────


FAILED_FINAL_TEXT = (
    "The send_handoff call failed to deliver the payload, so the task needs "
    "another attempt before the handoff is recorded."
)


def _superseded_header_form(final_text: str, *, missing: str = "send_handoff") -> str:
    return (
        "[superseded attempt 1 of 2] attempt=1/2; reason=missing_required_tools; "
        f"missing_tools={missing}; source=task_event:superseded_output:evt-1. "
        "Only the final attempt's text follows.\n\n" + final_text
    )


def test_terminal_summary_source_strips_the_repair_notice():
    from lucent.db.requests import _terminal_summary_source

    notice = build_repair_notice(SupersededAttempt(text=PASS_ONE, event_id="evt-2"))
    assert _terminal_summary_source(f"{notice}\n\n{PASS_TWO}") == PASS_TWO
    # Header first, then notice (the order the persistence helpers produce).
    assert (
        _terminal_summary_source(
            _superseded_header_form(f"{notice}\n\n{PASS_TWO}")
        )
        == PASS_TWO
    )


def test_fail_summary_of_a_superseded_result_is_the_terminal_statement():
    from lucent.db.requests import _terminal_summary_value

    header_form = _superseded_header_form(FAILED_FINAL_TEXT)
    summary = _terminal_summary_value(header_form, None)

    expected = build_terminal_summary(
        SupersededAttempt(text=PASS_ONE, missing_tools=("send_handoff",)), succeeded=False
    )
    assert summary == expected
    assert "\n" not in summary
    assert not summary.startswith("[superseded attempt")
    assert FAILED_FINAL_TEXT not in summary
    # The outcome and the superseded attempt are both named.
    assert "Attempt 1 missed required tool (send_handoff)" in summary
    assert "is superseded" in summary


def test_fail_summary_prefers_a_caller_supplied_summary():
    from lucent.db.requests import _terminal_summary_value

    header_form = _superseded_header_form(FAILED_FINAL_TEXT)
    assert _terminal_summary_value(header_form, "Contract supplied") == (
        "Contract supplied"
    )
    # Even with no result at all, an explicit summary is kept.
    assert _terminal_summary_value(None, "Explicit") == "Explicit"


def test_fail_summary_without_supersession_is_unchanged():
    from lucent.db.requests import _terminal_summary_value

    assert _terminal_summary_value("plain failure prose", None) == "plain failure prose"
    assert _terminal_summary_value(None, None) is None
    assert _terminal_summary_value("", None) == ""


@pytest.mark.asyncio
async def test_failed_superseded_task_stores_the_terminal_statement(
    db_pool, delete_test_organizations
):
    from lucent.db import OrganizationRepository, UserRepository
    from lucent.db.requests import RequestRepository

    prefix = f"test_fail_summary_{uuid4().hex[:8]}_"
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}user@test.com",
        display_name=f"{prefix}User",
    )
    repo = RequestRepository(db_pool)
    org_id = str(org["id"])
    user_id = str(user["id"])

    async def _claimed_task(title: str) -> str:
        request = await repo.create_request(
            title=title, org_id=org_id, created_by=user_id
        )
        task = await repo.create_task(
            request_id=str(request["id"]),
            title=title,
            org_id=org_id,
            requesting_user_id=user_id,
        )
        task_id = str(task["id"])
        await repo.claim_task(task_id, "test-inst", org_id=org_id)
        return task_id

    # 1. Superseded failure: summary is the one-line terminal state.
    task_id = await _claimed_task("Superseded failure")
    stored = await repo.fail_task(
        task_id,
        "failure indicator found: 'failed to'",
        org_id=org_id,
        instance_id="test-inst",
        result=_superseded_header_form(FAILED_FINAL_TEXT),
    )
    assert stored is not None
    assert stored["result_summary"] == build_terminal_summary(
        SupersededAttempt(text=PASS_ONE, missing_tools=("send_handoff",)), succeeded=False
    )
    assert not stored["result_summary"].startswith("[superseded attempt")
    assert FAILED_FINAL_TEXT not in stored["result_summary"]

    # 2. A caller-supplied summary wins over the derived one.
    task_id = await _claimed_task("Caller summary wins")
    stored = await repo.fail_task(
        task_id,
        "boom",
        org_id=org_id,
        instance_id="test-inst",
        result=_superseded_header_form(FAILED_FINAL_TEXT),
        result_summary="Caller supplied terminal state",
    )
    assert stored["result_summary"] == "Caller supplied terminal state"

    # 3. No supersession: behavior is unchanged.
    task_id = await _claimed_task("Plain failure")
    stored = await repo.fail_task(
        task_id,
        "boom",
        org_id=org_id,
        instance_id="test-inst",
        result="plain failure prose",
    )
    assert stored["result_summary"] == "plain failure prose"

    # 4. Repaired-then-failed: the repair notice is not part of the summary.
    notice = build_repair_notice(SupersededAttempt(text=PASS_ONE, event_id="evt-2"))
    task_id = await _claimed_task("Repaired failure")
    stored = await repo.fail_task(
        task_id,
        "boom",
        org_id=org_id,
        instance_id="test-inst",
        result=f"{notice}\n\n{FAILED_FINAL_TEXT}",
    )
    assert stored["result_summary"] == FAILED_FINAL_TEXT

    async with db_pool.acquire() as conn:
        await delete_test_organizations(conn, [f"{prefix}%"])
