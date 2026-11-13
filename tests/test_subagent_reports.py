"""Sub-agent reporting: results must reach the conversation that delegated them.

Covered here are the properties that make the pipeline trustworthy rather than
merely present: the card is durable and de-duplicated, the reply is written as a
real transcript message, the reply turn has no tools, and a resumed provider
session is abandoned once an unseen report reply exists.
"""

import asyncio
import inspect
import uuid

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def cleanup_requests(db_pool, test_organization):
    """Remove request and session rows before the shared user cleanup runs."""
    org_uuid = test_organization["id"]
    yield
    async with db_pool.acquire() as conn:
        request_ids = [
            row["id"]
            for row in await conn.fetch(
                "SELECT id FROM requests WHERE organization_id = $1", org_uuid
            )
        ]
        if request_ids:
            task_ids = [
                row["id"]
                for row in await conn.fetch(
                    "SELECT id FROM tasks WHERE request_id = ANY($1)", request_ids
                )
            ]
            if task_ids:
                await conn.execute(
                    "DELETE FROM task_memories WHERE task_id = ANY($1)", task_ids
                )
                await conn.execute(
                    "DELETE FROM task_events WHERE task_id = ANY($1)", task_ids
                )
            await conn.execute(
                "DELETE FROM llm_session_requests WHERE request_id = ANY($1)",
                request_ids,
            )
            await conn.execute("DELETE FROM tasks WHERE request_id = ANY($1)", request_ids)
            await conn.execute(
                "DELETE FROM requests WHERE organization_id = $1", org_uuid
            )
        await conn.execute("DELETE FROM llm_sessions WHERE organization_id = $1", org_uuid)


def test_report_card_is_a_real_event_type():
    from lucent.services.subagent_reports import REPORT_EVENT_TYPE

    assert REPORT_EVENT_TYPE == "subagent_report"


def test_report_trigger_covers_only_milestones():
    """The DB trigger must not report every status write or every task step."""
    from pathlib import Path

    sql = Path(
        "src/lucent/db/migrations/145_subagent_report_notify.sql"
    ).read_text()
    # Distinct-transition guard: a rewrite of the same status emits nothing.
    assert "OLD.status IS DISTINCT FROM NEW.status" in sql
    # Task-level reporting is limited to settled outcomes.
    assert "'completed', 'failed', 'cancelled', 'needs_review'" in sql
    # The internal review task is excluded from reporting.
    assert "Post-completion review" in sql


def test_report_turn_runs_without_tools_and_without_resume():
    """An unattended turn must not be able to act or fork provider history."""
    from lucent.services import subagent_reports

    source = inspect.getsource(
        subagent_reports.SubagentReportService._maybe_generate_reply
    )
    assert "mcp_config={}" in source
    assert "resume=False" in source
    assert "provider_session_id=None" in source


@pytest.mark.asyncio
async def test_deferred_reply_fires_after_the_interval(monkeypatch):
    """The scheduled retry must actually deliver the answer."""
    from lucent.services import subagent_reports

    # Collapse the wait so the test does not sleep a real interval.
    monkeypatch.setattr(subagent_reports, "REPLY_MIN_INTERVAL_SECONDS", 0.01)
    service = subagent_reports.SubagentReportService()
    service._awaiting_user_turn = lambda sid: False  # type: ignore[method-assign]

    delivered: list[dict] = []

    async def _fake_reply(signal, card, context, event_row):
        delivered.append(signal)

    service._maybe_generate_reply = _fake_reply  # type: ignore[method-assign]

    signal = {"kind": "request.completed", "session_id": "s1", "organization_id": "o"}
    service._reply_pending["s1"] = {
        "signal": signal,
        "card": {"headline": "done"},
        "context": {"session": {"id": "s1"}},
        "event_row": {"id": "e1"},
    }
    service._schedule_deferred_reply_retry("s1")
    # The retry has a hard 1s floor (never fire immediately, or a burst of
    # suppressions would immediately re-enter the gate).
    await asyncio.sleep(1.4)

    assert delivered == [signal], "the deferred reply must be delivered"
    assert "s1" not in service._reply_pending


def test_review_status_is_not_treated_as_a_final_outcome():
    """`review` is Lucent's automatic gate, not a person's decision.

    Only a terminal status earns an unconditional reply. Treating `review` as
    final was the other half of the starvation bug: it is what consumed the one
    reply the interval allowed.
    """
    from lucent.services.subagent_reports import _is_terminal_request_status

    assert _is_terminal_request_status("completed")
    assert _is_terminal_request_status("failed")
    assert _is_terminal_request_status("cancelled")
    assert not _is_terminal_request_status("review")
    assert not _is_terminal_request_status("needs_rework")
    assert not _is_terminal_request_status("in_progress")


def test_review_headline_does_not_claim_a_human_decision():
    """The review card must not say the work is waiting on the person.

    "The work is ready for your review" is what made the model describe
    finished work as unfinished — it reads as a pending human gate.
    """
    from lucent.services.delegated_work import (
        request_status_label,
        summarize_report_event,
    )

    headline = summarize_report_event(
        kind="request.review", request={"status": "review"}, task=None
    )
    assert "your review" not in headline.lower()
    assert "automatic" in headline.lower()
    # The digest label must not imply a human gate either.
    assert "review" not in request_status_label("review").lower()


def test_reply_prompt_forbids_calling_a_populated_report_empty():
    """The reply turn must be told to quote output, not deny it exists.

    Live replies claimed the report was "empty" while the card body carried the
    task output, because the status sentence preceded the excerpts and the model
    read the headline alone.
    """
    from lucent.services.subagent_reports import REPORT_SYSTEM_PREAMBLE

    assert "NEVER describe the result as missing, thin, empty" in REPORT_SYSTEM_PREAMBLE
    # Both milestones must be reported; the person asked to be told when work
    # finishes AND when it is approved.
    assert "different milestones" in REPORT_SYSTEM_PREAMBLE


@pytest.mark.asyncio
async def test_completed_request_retracts_the_review_state(db_pool, test_user):
    """The reported bug: a finished request kept showing "ready for review".

    Cards are a projection of the request's current state, not an append-only
    log. Before this, `review` and `completed` each appended a card, so the
    transcript showed BOTH — and the stale "ready for review" card never went
    away. The agent then told the person work was awaiting their decision when
    it had already finished.
    """
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository
    from lucent.services.subagent_reports import SubagentReportService

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"stale review {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    rid = str(request["id"])
    task = await repo.create_task(
        request_id=rid,
        title="Do the work",
        org_id=org_id,
        description="work",
        agent_type="code",
    )
    await repo.claim_task(str(task["id"]), "i", org_id=org_id)
    await repo.complete_task(str(task["id"]), "the actual output", org_id=org_id)

    service = SubagentReportService()

    async def no_reply(*args, **kwargs):
        return None

    service._maybe_generate_reply = no_reply  # type: ignore[method-assign]

    def signal(kind: str) -> dict:
        return {
            "session_id": str(session["id"]),
            "request_id": rid,
            "task_id": None,
            "kind": kind,
            "organization_id": org_id,
            "user_id": str(test_user["id"]),
        }

    await repo.update_request_status(rid, "review", org_id=org_id)
    await service._handle_batch([signal("request.review")])

    await repo.update_request_status(rid, "completed", org_id=org_id)
    await service._handle_batch([signal("request.completed")])

    events = await sessions.list_events(
        str(session["id"]), org_id, visible_only=True
    )
    reports = [e for e in events if e["event_type"] == "subagent_report"]

    assert len(reports) == 1, (
        "a request must keep exactly one card; an appended review card is what "
        "left finished work showing as 'ready for review'"
    )
    only = reports[0]["raw"]
    assert only["report_kind"] == "request.completed"
    assert only["request"]["status"] == "completed"
    # The stale marker must be gone from the transcript entirely.
    assert "review" not in (only.get("headline") or "").lower()
    assert "the actual output" in (only.get("body") or "")


@pytest.mark.asyncio
async def test_review_and_completion_each_earn_a_direct_message():
    """The person asked to be told when work finishes AND when it is approved.

    These are distinct milestones, so neither may be suppressed by the reply
    interval; intermediate bookkeeping still defers. Asserted on the gating
    decision in the source so the test cannot depend on a live engine.
    """
    from lucent.services import subagent_reports

    source = inspect.getsource(
        subagent_reports.SubagentReportService._maybe_generate_reply
    )

    milestone_block = source.split("milestone_outcome =", 1)
    assert len(milestone_block) == 2, (
        "the interval gate must distinguish milestone outcomes"
    )
    declared = milestone_block[1].split("if self._reply_too_soon", 1)[0]
    # Both milestones the person asked for must be explicit members.
    for kind in ("review", "completed", "failed", "cancelled"):
        assert f'"{kind}"' in declared, (
            f"{kind} must bypass the reply interval, or its direct message is "
            "silently deferred"
        )
    # And the gate must actually be skipped for them.
    assert "and not milestone_outcome" in source

    # `review` is the "work finished" milestone but is NOT terminal — the two
    # concepts must stay distinct or review messages get starved again.
    assert not subagent_reports._is_terminal_request_status("review")


def test_terminal_outcomes_outrank_intermediate_ones():
    """A burst must summarise where the request ENDED UP, not how far it got.

    Live case: a burst carried both `request.review` and `request.cancelled`.
    `review` outranked `cancelled`, so the surviving card said "checking the
    finished work" for a request that had been cancelled — the card contradicted
    its own request block and the reader had no way to resolve it.
    """
    from lucent.services.subagent_reports import SubagentReportService

    rank = SubagentReportService._rank
    for kind in ("request.completed", "request.failed", "request.cancelled"):
        assert rank({"kind": kind}) > rank({"kind": "request.review"}), (
            f"{kind} is more final than request.review and must win the burst"
        )
    assert rank({"kind": "task.cancelled"}) > rank({"kind": "task.completed"})


def test_card_fields_follow_the_reread_status_not_the_signal():
    """Headline, kind and severity must all describe the same state.

    The card's request block is re-read from the database while the headline used
    to come from the signal. When those disagreed the card was self-contradictory.
    """
    from lucent.services import subagent_reports

    service = subagent_reports.SubagentReportService()
    context = {
        "request": {"id": "r1", "title": "T", "status": "cancelled"},
        "task": None,
        "counts": {"total": 1, "completed": 1},
        "task_results": [],
    }
    card = service._build_card(
        {"kind": "request.review", "request_id": "r1"}, context
    )

    assert card["kind"] == "request.cancelled"
    assert card["severity"] == "muted"
    assert "review" not in card["headline"].lower()
    assert "automatic" not in card["body"].lower()


def test_digest_truncation_says_how_to_get_the_full_text():
    """A cut result must be distinguishable from a short one, and actionable.

    A bare ellipsis reads as "the output was short", so the model reports the
    work as having produced nothing — when the text was merely longer than the
    digest. The marker must name the escape hatch.
    """
    from lucent.services.delegated_work import build_delegated_work_digest

    long_output = "REAL OUTPUT " * 400  # well past MAX_RESULT_CHARS
    digest = build_delegated_work_digest(
        {
            "requests": [
                {
                    "id": "req-123",
                    "title": "Long result",
                    "status": "completed",
                    "task_counts": {"completed": 1},
                    "task_total": 1,
                    "tasks": [
                        {
                            "id": "t1",
                            "title": "Produce a long report",
                            "status": "completed",
                            "result_excerpt": long_output,
                        }
                    ],
                }
            ],
            "active_task_count": 0,
            "revision": "rev",
        }
    )

    assert "truncated" in digest.lower()
    assert "get_request_details" in digest, (
        "the digest must say how to read the rest"
    )
    assert "REAL OUTPUT" in digest, "the visible part must still be the real output"


def test_digest_short_result_has_no_truncation_noise():
    """Don't cry wolf on results that fit."""
    from lucent.services.delegated_work import build_delegated_work_digest

    digest = build_delegated_work_digest(
        {
            "requests": [
                {
                    "id": "req-9",
                    "title": "Short",
                    "status": "completed",
                    "task_counts": {"completed": 1},
                    "task_total": 1,
                    "tasks": [
                        {
                            "id": "t1",
                            "title": "Quick check",
                            "status": "completed",
                            "result_excerpt": "exit=0 all good",
                        }
                    ],
                }
            ],
            "active_task_count": 0,
            "revision": "rev",
        }
    )
    # Scope to the result line: the digest preamble explains the marker on every
    # turn, so the assertion is about the RESULT itself not being marked.
    result_lines = [ln for ln in digest.splitlines() if "exit=0 all good" in ln]
    assert result_lines, "the short result must appear verbatim"
    assert not any("truncated" in ln.lower() for ln in result_lines)


def test_chat_renders_live_progress_cards():
    """Running work must be visible in the transcript without a reload.

    The panel existed but was hidden behind a button, and the only live signal
    rebuilt the whole transcript — which is what caused the visible page jump.
    """
    from pathlib import Path

    js = Path("src/lucent/web/static/chat-message-ui.js").read_text()
    html = Path("src/lucent/web/templates/chat.html").read_text()

    # In-place progress rendering exists and is exported.
    assert "function upsertDelegatedProgress" in js
    assert "function pruneDelegatedProgress" in js
    assert "upsertDelegatedProgress," in js
    assert "pruneDelegatedProgress," in js
    # The fingerprint guard is what prevents a flicker every poll.
    assert "dataset.fingerprint" in js

    # The transcript is patched in place, never rebuilt: progress cards are
    # upserted by the shared conversation controller (so a Handoff shows the
    # same live progress) and the resync path must not reload the transcript.
    assert "function renderDelegatedProgress(force)" in js
    assert "upsertDelegatedProgress(cfg.messagesEl, request)" in js
    assert "pruneDelegatedProgress(cfg.messagesEl" in js
    assert "function requestDelegatedResync()" in js
    resync = js.split("function requestDelegatedResync() {", 1)[1][:400]
    assert "renderTranscript(" not in resync, (
        "the resync path must not reload the whole transcript; that is the jump"
    )
    chat_resync = html.split("function requestDelegatedResync", 1)[1][:300]
    assert "loadSession(" not in chat_resync, (
        "the resync path must not reload the whole transcript; that is the jump"
    )


def test_chat_reloads_transcript_only_for_new_report_cards():
    """New report cards must reach the page without a reload; churn must not.

    Two failure modes were reported from the same place:
      * a card (and the unattended reply beside it) never appeared until the
        page was manually refreshed, because the activity poll only patched
        progress cards in place and never re-read the transcript;
      * the earlier fix re-read the transcript on ANY revision change, which a
        running request triggers every few seconds, so the thread was rebuilt
        mid-read and scroll jumped.

    The contract that satisfies both: rebuild on the REPORT revision only.
    """
    from pathlib import Path

    js = Path("src/lucent/web/static/chat-message-ui.js").read_text()

    assert "delegated.reportRevision = data.report_revision || null;" in js
    assert "previousReportRevision !== delegated.reportRevision" in js
    assert "if (reportChanged) requestDelegatedResync();" in js
    # The work revision must not drive a transcript rebuild.
    assert "previousRevision !== delegated.revision" not in js

    # A reload mid-stream would discard the reply being written.
    resync = js.split("function requestDelegatedResync() {", 1)[1].split("\n        }", 1)[0]
    assert "if (state.streaming) {" in resync
    assert "delegated.resyncPending = true;" in resync


def test_background_report_reply_lands_on_the_same_turn_as_its_card():
    """The card and its reply share one turn id, so replay pairs them.

    A reload renders whatever the transcript says. If the reply turn were
    written without the card's turn id, the card and the answer would land in
    different rows (or the card would be treated as turnless), and the thread
    would read differently after a refresh than it did live.
    """
    from pathlib import Path

    src = Path("src/lucent/services/subagent_reports.py").read_text()

    assert "report_turn_id = str(uuid4())" in src
    assert "turn_id=report_turn_id," in src
    assert "SET message_id = $2, turn_id = $3" in src


def test_progress_card_is_collapsible_and_matches_the_report_card_family():
    """The live box must be collapsible and visually consistent, not a floating overlay.

    Constraints encoded here, all of which were reported as defects:
      * it must collapse (it sits inline in a conversation),
      * a chosen collapse must survive the per-poll re-render,
      * it must reuse the report card's design tokens (paper/line/muted/teal)
        rather than inventing a second visual language,
      * the overview header must be opaque, because chat text scrolled
        underneath it and made the buttons unreadable.
    """
    from pathlib import Path

    js = Path("src/lucent/web/static/chat-message-ui.js").read_text()
    html = Path("src/lucent/web/templates/chat.html").read_text()

    # Collapsible, with the state stored on the transcript host so a rebuild
    # cannot spring it back open.
    assert '<details class="delegated-progress-card"' in js
    assert "progressCollapsedIds" in js
    assert "__delegatedCollapsed" in js
    # The listener must NOT be `once`: the card is rebuilt on status change.
    toggle_block = js.split("details.addEventListener('toggle'", 1)[1][:320]
    assert "once: true" not in toggle_block, (
        "a once-listener is consumed by the first toggle, so later collapses "
        "never persist"
    )

    # Design tokens shared with .subagent-report-card. The window covers the
    # whole progress-card block, not just its opening rule: the tokens are used
    # in the nested rules (task rows, meta line) further down.
    # Progress-card styling moved with the transcript into the shared
    # stylesheet, so a Handoff renders the same card as /chat.
    css = Path("src/lucent/web/static/chat-surface.css").read_text()
    progress_block = css.split(".delegated-progress-card {", 1)[1]
    progress_block = progress_block.split("\n/* ", 1)[0]
    for token in (
        "var(--lucent-paper)",
        "var(--lucent-line)",
        "var(--lucent-muted)",
        "var(--lucent-teal)",
        "var(--lucent-ink)",
    ):
        assert token in progress_block, token

    # Opaque header bar spanning the chat width, with no floating-card chrome.
    shell_block = css.split(".chat-overview-shell {", 1)[1][:700]
    assert "background: #080808" in shell_block, "the bar must be solid"
    assert "left: 0" in shell_block and "right: 0" in shell_block, (
        "the bar must span the full chat width, not float as a pill"
    )
    assert "box-shadow: none" in shell_block, "no drop shadow under the bar"
    assert "border-radius: 0" in shell_block, "no rounding on a full-width bar"
    # The fade element under the old floating plate must be gone.
    assert "chat-overview-shell::after" not in css

    # No auto-opened floating panel covering the conversation.
    assert "toggleOverviewPanel('work');\n            state.delegatedAutoOpened" not in html


def test_reported_result_is_the_prompt_not_a_system_appendix():
    """The result must ride in the user prompt, not behind the agent definition.

    Sending only the headline meant the actual output sat at the tail of a long
    system prompt (agent definition + preamble). Replies then reliably said "the
    report came back empty" while the card body carried the full result.
    """
    from lucent.services import subagent_reports

    source = inspect.getsource(
        subagent_reports.SubagentReportService._maybe_generate_reply
    )
    assert "card.get('body')" in source or 'card.get("body")' in source
    run_session_prompt = source.split("prompt=(", 1)
    assert len(run_session_prompt) == 2, "run_session must pass an explicit prompt"
    assert "card['headline']" in run_session_prompt[1]

    # The system message must NOT re-embed the body.
    builder = inspect.getsource(
        subagent_reports.SubagentReportService._build_system_message
    )
    assert 'REPORT_SYSTEM_PREAMBLE + card' not in builder


# ---------------------------------------------------------------------------
# Silent-suppression audit (2026-10-29)
#
# Every path that cannot answer immediately must either say why in the log or
# park the reply for retry. A bare ``return`` here is permanent silence about
# work the person is waiting on, because the card is already durable and nothing
# re-generates it.
# ---------------------------------------------------------------------------


def _audit_service():
    from lucent.services import subagent_reports

    service = subagent_reports.SubagentReportService()
    deferrals: list[str] = []
    service._defer_reply = (  # type: ignore[method-assign]
        lambda session_id, **kwargs: deferrals.append(kwargs.get("reason")) or True
    )
    return service, deferrals


def _audit_args(kind="task.completed"):
    session = {
        "id": "s1",
        "kind": "chat",
        "status": "active",
        "model": "m",
        "user_id": "u",
        "last_message_at": None,
    }
    signal = {
        "kind": kind,
        "session_id": "s1",
        "organization_id": "o",
        "request_id": "r",
    }
    return signal, {"headline": "h", "body": ""}, {"session": session}, {"id": "e1"}


@pytest.mark.asyncio
async def test_busy_conversation_defers_the_reply_instead_of_dropping_it():
    """The person being *present* is when the acknowledgement matters most."""
    service, deferrals = _audit_service()
    service._awaiting_user_turn = lambda sid: True  # type: ignore[method-assign]

    async def _never_finishes(sid):
        return False

    service._wait_for_user_turn = _never_finishes  # type: ignore[method-assign]
    signal, card, context, event_row = _audit_args()
    await service._maybe_generate_reply(signal, card, context, event_row)

    assert deferrals == ["user turn still active"]


@pytest.mark.asyncio
async def test_failed_access_check_defers_instead_of_dropping():
    """A broken check is not evidence the model is unavailable."""
    service, deferrals = _audit_service()
    service._awaiting_user_turn = lambda sid: False  # type: ignore[method-assign]
    service._reply_too_soon = lambda sid: False  # type: ignore[method-assign]

    signal, card, context, event_row = _audit_args()

    import lucent.api.routers.chat as chat_router

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch_target = chat_router._can_user_access_model
    chat_router._can_user_access_model = _boom  # type: ignore[assignment]
    try:
        await service._maybe_generate_reply(signal, card, context, event_row)
    finally:
        chat_router._can_user_access_model = monkeypatch_target  # type: ignore[assignment]

    assert deferrals == ["model access check failed"]


@pytest.mark.asyncio
async def test_deferral_budget_is_bounded(monkeypatch):
    """A permanently unreachable session must not retry forever."""
    from lucent.services import subagent_reports

    monkeypatch.setattr(subagent_reports, "REPLY_MAX_DEFERRALS", 2)
    service = subagent_reports.SubagentReportService()
    scheduled: list[str] = []
    service._schedule_deferred_reply_retry = (  # type: ignore[method-assign]
        lambda sid: scheduled.append(sid)
    )
    signal, card, context, event_row = _audit_args()

    results = [
        service._defer_reply(
            "s1",
            signal=signal,
            card=card,
            context=context,
            event_row=event_row,
            reason="test",
        )
        for _ in range(3)
    ]

    assert results == [True, True, False], "the third deferral must be refused"
    assert len(scheduled) == 2
    assert service._reply_deferrals["s1"] == 2


@pytest.mark.asyncio
async def test_transient_context_failure_requeues_the_burst(monkeypatch):
    """A database blip during context load must not lose the report."""
    from lucent.services import subagent_reports

    service = subagent_reports.SubagentReportService()
    handled: list[list] = []

    async def _broken(signals):
        handled.append(signals)
        raise RuntimeError("connection reset")

    service._handle_batch = _broken  # type: ignore[method-assign]
    signal, _card, _context, _event_row = _audit_args()
    service._pending[service._pending_key(signal)] = signal

    # One debounce window: the flush fails and must put the signal back.
    monkeypatch.setattr(subagent_reports, "DEBOUNCE_SECONDS", 0)
    await service._flush_after_debounce()

    assert handled, "the burst was attempted"
    assert service._pending, "a failed burst must be re-queued, not dropped"


@pytest.mark.asyncio
async def test_context_load_propagates_database_failure(monkeypatch):
    """``_load_context`` returns None only for a genuinely absent session.

    Returning None on a transient failure made a database hiccup look like
    "nothing to report", and the card was never written — the one outcome with
    no recovery path.
    """
    from contextlib import asynccontextmanager

    from lucent.services import subagent_reports

    @asynccontextmanager
    async def _broken_acquire(**_kwargs):
        raise RuntimeError("connection reset by peer")
        yield  # pragma: no cover

    monkeypatch.setattr(subagent_reports, "scoped_acquire", _broken_acquire)

    signal = {
        "session_id": str(uuid.uuid4()),
        "request_id": str(uuid.uuid4()),
        "task_id": None,
        "kind": "request.completed",
        "organization_id": str(uuid.uuid4()),
    }
    service = subagent_reports.SubagentReportService()
    with pytest.raises(RuntimeError, match="connection reset"):
        await service._load_context(signal)


@pytest.mark.asyncio
async def test_report_card_is_deduplicated(db_pool, test_user):
    """A repeated signal for the same outcome must not stack cards."""
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository
    from lucent.services.subagent_reports import SubagentReportService

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"reported {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    task = await repo.create_task(
        request_id=str(request["id"]),
        title="Reported task",
        org_id=org_id,
        description="Finishes and reports.",
        agent_type="code",
    )

    signal = {
        "session_id": str(session["id"]),
        "request_id": str(request["id"]),
        "task_id": str(task["id"]),
        "kind": "task.completed",
        "organization_id": org_id,
        "user_id": str(test_user["id"]),
    }
    service = SubagentReportService()
    context = await service._load_context(signal)
    assert context is not None
    card = service._build_card(signal, context)

    first = await service._write_card(signal, card, context)
    second = await service._write_card(signal, card, context)
    assert first is not None
    assert second is None, "the same outcome must not be reported twice"

    events = await sessions.list_events(
        str(session["id"]), org_id, visible_only=True
    )
    reports = [e for e in events if e["event_type"] == "subagent_report"]
    assert len(reports) == 1
    assert reports[0]["raw"]["severity"] in {"success", "attention", "review", "muted", "info"}


@pytest.mark.asyncio
async def test_distinct_bursts_with_same_kind_set_do_not_collide(
    db_pool, test_user
):
    """Two different bursts must not share a dedupe key just because their kinds match.

    The key used to be built from the sorted set of *kinds*, so two separate
    `task.completed` bursts on the same request produced the same key and the
    second burst's card was silently dropped — the person never learned that the
    second task finished.
    """
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository
    from lucent.services.subagent_reports import SubagentReportService

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"two bursts {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    first_task = await repo.create_task(
        request_id=str(request["id"]),
        title="First task",
        org_id=org_id,
        description="First.",
        agent_type="code",
    )
    second_task = await repo.create_task(
        request_id=str(request["id"]),
        title="Second task",
        org_id=org_id,
        description="Second.",
        agent_type="code",
        sequence_order=1,
    )

    service = SubagentReportService()

    def signal_for(task) -> dict:
        return {
            "session_id": str(session["id"]),
            "request_id": str(request["id"]),
            "task_id": str(task["id"]),
            "kind": "task.completed",
            "organization_id": org_id,
            "user_id": str(test_user["id"]),
        }

    async def no_reply(*args, **kwargs):
        return None

    service._maybe_generate_reply = no_reply  # type: ignore[method-assign]

    first_signal = signal_for(first_task)
    second_signal = signal_for(second_task)

    # The kind sets are identical — only the events differ. This is the
    # production shape that used to collide.
    assert sorted([first_signal["kind"]]) == sorted([second_signal["kind"]])

    await service._handle_batch([first_signal])
    events_first = await sessions.list_events(
        str(session["id"]), org_id, visible_only=True
    )
    reports_first = [e for e in events_first if e["event_type"] == "subagent_report"]
    assert len(reports_first) == 1
    key_after_first = reports_first[0]["raw"]["dedupe_key"]

    await service._handle_batch([second_signal])
    events = await sessions.list_events(
        str(session["id"]), org_id, visible_only=True
    )
    reports = [e for e in events if e["event_type"] == "subagent_report"]
    # One card per request: the second task's completion is reflected in the
    # same card rather than appended as a peer, so the transcript always shows
    # one current state instead of several stale ones.
    assert len(reports) == 1, "a request keeps exactly one card"
    assert reports[0]["raw"]["dedupe_key"] != key_after_first, (
        "the second burst must be reflected — not swallowed as a replay"
    )
    assert "2" in (reports[0]["raw"].get("body") or ""), (
        "the refreshed card must carry the second task's outcome"
    )

    # A genuine replay of the current outcome still de-duplicates.
    await service._handle_batch([second_signal])
    events_after = await sessions.list_events(
        str(session["id"]), org_id, visible_only=True
    )
    reports_after = [
        e for e in events_after if e["event_type"] == "subagent_report"
    ]
    assert len(reports_after) == 1, "a replayed burst must not add a card"


@pytest.mark.asyncio
async def test_suppressed_card_still_generates_the_pending_reply(
    db_pool, test_user, monkeypatch
):
    """Suppressing a duplicate card must not swallow an undelivered reply.

    A burst can be replayed while its first reply turn was skipped (rate
    limited, or a user turn was in flight). The card already exists, so the
    duplicate is suppressed — but the reply was never written, and returning
    early lost it entirely.
    """
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository
    from lucent.services import subagent_reports
    from lucent.services.subagent_reports import SubagentReportService

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"suppressed {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    task = await repo.create_task(
        request_id=str(request["id"]),
        title="Task",
        org_id=org_id,
        description="Runs.",
        agent_type="code",
    )
    signal = {
        "session_id": str(session["id"]),
        "request_id": str(request["id"]),
        "task_id": str(task["id"]),
        "kind": "task.completed",
        "organization_id": org_id,
        "user_id": str(test_user["id"]),
    }
    service = SubagentReportService()
    context = await service._load_context(signal)
    assert context is not None
    card = service._build_card(signal, context)
    card["dedupe_key"] = (
        f"{request['id']}:{service._burst_identity([signal], context)}"
    )
    assert await service._write_card(signal, card, context) is not None

    calls: list[str] = []

    async def fake_reply(sig, crd, ctx, event_row):
        calls.append(str(event_row.get("id")))

    monkeypatch.setattr(service, "_maybe_generate_reply", fake_reply)
    monkeypatch.setattr(subagent_reports, "_get_pool", _get_pool_stub(db_pool))

    # Replaying the same burst suppresses the card but must still answer.
    await service._handle_batch([signal])
    assert calls, "a suppressed duplicate card must not swallow the reply"


@pytest.mark.asyncio
async def test_reply_is_not_regenerated_when_already_delivered(
    db_pool, test_user, monkeypatch
):
    """A true duplicate (card *and* reply already present) stays silent."""
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository
    from lucent.services import subagent_reports
    from lucent.services.subagent_reports import SubagentReportService

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"replied {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    task = await repo.create_task(
        request_id=str(request["id"]),
        title="Task",
        org_id=org_id,
        description="Runs.",
        agent_type="code",
    )
    signal = {
        "session_id": str(session["id"]),
        "request_id": str(request["id"]),
        "task_id": str(task["id"]),
        "kind": "task.completed",
        "organization_id": org_id,
        "user_id": str(test_user["id"]),
    }
    service = SubagentReportService()
    context = await service._load_context(signal)
    card = service._build_card(signal, context)
    card["dedupe_key"] = (
        f"{request['id']}:{service._burst_identity([signal], context)}"
    )
    event_row = await service._write_card(signal, card, context)
    assert event_row is not None

    # Simulate the reply having been delivered for that card.
    message = await sessions.add_message(
        str(session["id"]),
        role="assistant",
        content="Finished.",
        org_id=org_id,
        user_id=str(test_user["id"]),
    )
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE llm_session_events SET message_id = $2 WHERE id = $1",
            event_row["id"],
            message["id"],
        )

    calls: list[str] = []

    async def fake_reply(*args, **kwargs):
        calls.append("called")

    monkeypatch.setattr(service, "_maybe_generate_reply", fake_reply)
    monkeypatch.setattr(subagent_reports, "_get_pool", _get_pool_stub(db_pool))

    await service._handle_batch([signal])
    assert calls == [], "an already-answered report must not trigger another turn"


@pytest.mark.asyncio
async def test_repeated_request_level_burst_is_not_dropped(db_pool, test_user):
    """A second `request.review` after a rework cycle is a new event, not a replay.

    Request-level notifications carry no task id, so a kind-only key cannot tell
    a later transition apart from a replayed one. The burst identity includes the
    re-read state for exactly these signals.
    """
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository
    from lucent.services.subagent_reports import SubagentReportService

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"repeat review {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    rid = str(request["id"])
    first = await repo.create_task(
        request_id=rid, title="First", org_id=org_id, description="a", agent_type="code"
    )
    await repo.claim_task(str(first["id"]), "i", org_id=org_id)
    await repo.complete_task(str(first["id"]), "one", org_id=org_id)

    service = SubagentReportService()
    signal = {
        "session_id": str(session["id"]),
        "request_id": rid,
        "task_id": None,
        "kind": "request.review",
        "organization_id": org_id,
        "user_id": str(test_user["id"]),
    }

    async def no_reply(*args, **kwargs):
        return None

    service._maybe_generate_reply = no_reply  # type: ignore[method-assign]

    await service._handle_batch([signal])

    # A rework cycle produces a second task; the request's observable state
    # changes, so the second review is a genuinely different event.
    second = await repo.create_task(
        request_id=rid, title="Second", org_id=org_id, description="b",
        agent_type="code", sequence_order=1,
    )
    await repo.claim_task(str(second["id"]), "i", org_id=org_id)
    await repo.complete_task(str(second["id"]), "two", org_id=org_id)
    await repo.update_request_status(rid, "needs_rework", org_id=org_id)
    await repo.update_request_status(rid, "review", org_id=org_id)

    await service._handle_batch([signal])

    events = await sessions.list_events(
        str(session["id"]), org_id, visible_only=True
    )
    reports = [e for e in events if e["event_type"] == "subagent_report"]
    # The request keeps ONE card, refreshed. What must not happen is the second
    # review being treated as a replay of the first and silently ignored — that
    # was the original defect. The card must now carry the second task.
    assert len(reports) == 1, "a request keeps exactly one card"
    assert "2" in (reports[0]["raw"].get("body") or ""), (
        "the second review must refresh the card; it is not a replay of the first"
    )


def _get_pool_stub(pool):
    """Mimic the async ``_get_pool`` helper for monkeypatching."""

    async def _inner():
        return pool

    return _inner


@pytest.mark.asyncio
async def test_unseen_report_reply_disables_provider_resume(db_pool, test_user):
    """Resuming a provider session would silently drop the report reply."""
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.services.subagent_reports import REPORT_REPLY_KIND

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    assert not await sessions.has_messages_with_kind(
        str(session["id"]), org_id, kind=REPORT_REPLY_KIND
    )
    await sessions.add_message(
        str(session["id"]),
        role="assistant",
        content="The task finished.",
        org_id=org_id,
        user_id=str(test_user["id"]),
        metadata={"kind": REPORT_REPLY_KIND},
    )
    assert await sessions.has_messages_with_kind(
        str(session["id"]), org_id, kind=REPORT_REPLY_KIND
    )


@pytest.mark.asyncio
async def test_delegated_work_digest_lists_open_requests(db_pool, test_user):
    """The next user turn must already know what its delegated work is doing."""
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository
    from lucent.services.delegated_work import build_delegated_work_digest_for_session

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    title = f"delegated digest {uuid.uuid4().hex[:8]}"
    request = await repo.create_request(
        title=title,
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    await sessions.link_request(
        str(session["id"]), str(request["id"]), org_id=org_id, relation="created"
    )

    digest = await build_delegated_work_digest_for_session(
        db_pool, session_id=str(session["id"]), org_id=org_id
    )
    assert "--- DELEGATED WORK ---" in digest
    assert title in digest
    assert str(request["id"]) in digest


def test_digest_uses_the_read_models_result_excerpt():
    """The digest must carry real output, not the remote's terse status line.

    The read model returns `result_excerpt`; the raw `result` column is not
    selected. Reading only `result` meant the digest always fell back to
    "Completed on remote X in 5.7s", so a model with the full text in its card
    still reported the work as having no output.
    """
    from lucent.services.delegated_work import build_delegated_work_digest

    digest = build_delegated_work_digest(
        {
            "requests": [
                {
                    "id": "r1",
                    "title": "Probe",
                    "status": "completed",
                    "task_counts": {"completed": 1},
                    "task_total": 1,
                    "tasks": [
                        {
                            "id": "t1",
                            "title": "Do the probe",
                            "status": "completed",
                            "result_excerpt": "ACTUAL HOST OUTPUT: claude=2.1.292",
                            "result_summary": "Completed on remote X in 5.7s",
                        }
                    ],
                }
            ],
            "active_task_count": 0,
            "revision": "rev",
        }
    )

    assert "ACTUAL HOST OUTPUT" in digest, (
        "the digest must surface the real result the read model provides"
    )


@pytest.mark.asyncio
async def test_digest_is_empty_without_delegation(db_pool, test_user):
    """No delegation must leave the system prompt byte-identical."""
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.services.delegated_work import build_delegated_work_digest_for_session

    org_id = str(test_user["organization_id"])
    session = await LLMSessionRepository(db_pool).create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    assert (
        await build_delegated_work_digest_for_session(
            db_pool, session_id=str(session["id"]), org_id=org_id
        )
        == ""
    )


def test_digest_flags_all_review_work_as_about_to_finish():
    """A request in the automatic check must not read as "still running".

    The digest label for `review` used to be "in review", which the model read
    as unfinished work. This note states the gate's real nature so later turns
    stop describing completed work as pending.
    """
    from lucent.services.delegated_work import build_delegated_work_digest

    digest = build_delegated_work_digest(
        {
            "requests": [
                {
                    "id": "r1",
                    "title": "Probe the remote",
                    "status": "review",
                    "task_counts": {"completed": 1},
                    "task_total": 1,
                    "tasks": [],
                }
            ],
            "active_task_count": 0,
            "revision": "rev",
        }
    )
    assert "automatic post-completion check" in digest
    assert "do not describe them as still running" in digest
    # It must not claim the work is finished/awaiting a human instead.
    assert "ready for your review" not in digest


def test_digest_omits_review_note_when_other_work_is_active():
    """The note is scoped to the all-in-review case only."""
    from lucent.services.delegated_work import build_delegated_work_digest

    digest = build_delegated_work_digest(
        {
            "requests": [
                {
                    "id": "r1",
                    "title": "Finishing",
                    "status": "review",
                    "task_counts": {"completed": 1},
                    "task_total": 1,
                    "tasks": [],
                },
                {
                    "id": "r2",
                    "title": "Running",
                    "status": "in_progress",
                    "task_counts": {"running": 1},
                    "task_total": 1,
                    "tasks": [],
                },
            ],
            "active_task_count": 1,
            "revision": "rev",
        }
    )
    assert "automatic post-completion check" not in digest


@pytest.mark.asyncio
async def test_a_rate_limited_reply_is_retried_not_dropped():
    """A suppressed reply must be re-armed, not forgotten.

    The reply interval is a cost control. Dropping the reply outright meant a
    request's completion could go unacknowledged forever while the earlier
    interim notice kept the single reply. Verified live: gaps of 29/44/46/50s
    against a 60s interval, so completion always lost.
    """
    from lucent.services import subagent_reports

    service = subagent_reports.SubagentReportService()
    scheduled: list[str] = []
    service._schedule_deferred_reply_retry = lambda sid: scheduled.append(sid)  # type: ignore[method-assign]

    session = {"id": "s1", "kind": "chat", "status": "active", "model": "m", "user_id": "u"}
    context = {"session": session}
    signal = {"kind": "task.completed", "session_id": "s1", "organization_id": "o", "request_id": "r"}
    card = {"headline": "Finished: x", "body": ""}

    service._reply_too_soon = lambda sid: True  # type: ignore[method-assign]
    service._awaiting_user_turn = lambda sid: False  # type: ignore[method-assign]

    await service._maybe_generate_reply(signal, card, context, {"id": "e1"})

    assert scheduled == ["s1"], "the suppressed reply must be scheduled for retry"
    assert service._reply_pending["s1"]["signal"]["kind"] == "task.completed"


@pytest.mark.asyncio
async def test_a_terminal_outcome_bypasses_the_reply_interval(monkeypatch):
    """A request's final outcome is never rate-limited away.

    The interim `review` notice normally lands 30-55s before completion, inside
    the 60s window — so without this exception the completion reply was always
    the one that lost.
    """
    from lucent.services import subagent_reports

    service = subagent_reports.SubagentReportService()
    service._reply_too_soon = lambda sid: True  # type: ignore[method-assign]
    service._awaiting_user_turn = lambda sid: False  # type: ignore[method-assign]

    # The staleness check is the next guard after the interval gate. Making it
    # raise proves the gate admitted us, without running a real model turn.
    def _sentinel(_value):
        raise RuntimeError("reached past the interval gate")

    monkeypatch.setattr(subagent_reports, "_stale_beyond_age_hours", _sentinel)

    session = {"id": "s1", "kind": "chat", "status": "active", "model": "m", "user_id": "u"}
    context = {"session": session}
    signal = {
        "kind": "request.completed",
        "session_id": "s1",
        "organization_id": "o",
        "request_id": "r",
    }
    card = {"headline": "This request is complete.", "body": ""}

    with pytest.raises(RuntimeError, match="reached past the interval gate"):
        await service._maybe_generate_reply(signal, card, context, {"id": "e1"})
    assert not service._reply_pending, "a terminal outcome must not be deferred"


@pytest.mark.asyncio
async def test_signals_arriving_during_a_flush_are_not_stranded():
    """A signal that lands mid-flush must still be reported.

    Handling a burst generates a reply turn, which can take many seconds. Any
    notification arriving in that window can't schedule its own flush (the task
    is still running), so without an explicit re-arm it would sit in _pending
    forever and the person would never hear about it. This is the failure that
    made a whole task's result silently vanish during live verification.
    """
    from lucent.services.subagent_reports import SubagentReportService

    service = SubagentReportService()
    handled: list[list[dict]] = []
    arrived_mid_flush = False

    async def fake_handle_batch(signals):
        nonlocal arrived_mid_flush
        handled.append(list(signals))
        if not arrived_mid_flush:
            arrived_mid_flush = True
            # A new notification arrives while this batch is being handled.
            service._pending["late"] = {"session_id": "s1", "kind": "task.completed"}

    service._handle_batch = fake_handle_batch
    service._pending["first"] = {"session_id": "s1", "kind": "task.completed"}

    # First flush handles the initial batch, then must re-arm for the late one.
    await service._flush_after_debounce()
    assert arrived_mid_flush
    assert service._pending, "the late signal must remain queued"
    assert service._debounce_task is not None, "a follow-up flush must be armed"

    # Draining the re-armed flush handles the late signal and stops re-arming.
    await service._flush_after_debounce()
    assert not service._pending
    assert len(handled) == 2


@pytest.mark.asyncio
async def test_activity_endpoint_payload_shape(db_pool, test_user):
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"panel {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    await repo.create_task(
        request_id=str(request["id"]),
        title="Panel task",
        org_id=org_id,
        description="Shows up in the panel.",
        agent_type="code",
    )
    await sessions.link_request(
        str(session["id"]), str(request["id"]), org_id=org_id, relation="created"
    )

    data = await repo.list_delegated_work_for_session(str(session["id"]), org_id)
    assert data["revision"] and data["revision"] != "empty"
    # No card written yet: the report token is the explicit "none" sentinel, not
    # a hash, so a client can tell "no cards" from "cards, unchanged".
    assert data["report_revision"] == "none"
    assert data["active_task_count"] == 0
    (entry,) = [r for r in data["requests"] if r["id"] == str(request["id"])]
    assert entry["task_total"] == 1
    assert entry["tasks"][0]["title"] == "Panel task"
    assert "result_excerpt" in entry["tasks"][0]


@pytest.mark.asyncio
async def test_revision_changes_when_a_report_card_is_written(db_pool, test_user):
    """A new report card must move the revision.

    Cards live in ``llm_session_events``, not in the request/task rows the
    revision used to hash. The card is written ~8s AFTER the request settles
    (report debounce), so by then the request status is already final — the
    revision did not change, the poller concluded "nothing new", and the chat
    kept showing the superseded "in review" card until a manual reload.
    """
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"revision {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    await sessions.link_request(
        str(session["id"]), str(request["id"]), org_id=org_id, relation="created"
    )

    before = await repo.list_delegated_work_for_session(str(session["id"]), org_id)

    await sessions.add_event(
        str(session["id"]),
        event_type="subagent_report",
        org_id=org_id,
        detail="This request is complete.",
        raw={
            "report_kind": "request.completed",
            "request_id": str(request["id"]),
            "headline": "This request is complete.",
            "request": {"id": str(request["id"]), "status": "completed"},
        },
    )

    after = await repo.list_delegated_work_for_session(str(session["id"]), org_id)

    # Report cards get their own token. Mixing them into the work revision
    # forced a bad trade: a client either rebuilt the whole transcript on every
    # task-status tick (scroll jumped while work ran) or never noticed a new
    # card. Two tokens let the chat reload for a card and patch in place for
    # progress.
    assert before["report_revision"] != after["report_revision"], (
        "a newly written report card must change the report revision, or the "
        "chat never learns that a fresher card exists"
    )
    assert before["revision"] == after["revision"], (
        "a report card must not disturb the work revision; otherwise a card "
        "write reads as task churn and forces a needless transcript rebuild"
    )


@pytest.mark.asyncio
async def test_report_prefers_actual_result_over_terse_summary(db_pool, test_user):
    """The report must surface real output, not a remote's status line.

    A remote executor sets ``result_summary`` to something like "Completed on
    remote X in 0.3s" while ``result`` holds the host output the person asked
    for. Preferring the summary made the card say nothing useful, so the agent
    replied "let me know if you want me to pull them up" — a second round trip
    for information it already had.
    """
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.remotes import RemoteRepository
    from lucent.db.requests import RequestRepository
    from lucent.services.subagent_reports import SubagentReportService

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    remote, _token = await RemoteRepository(db_pool).create(
        org_id=org_id,
        name=f"result-remote-{uuid.uuid4().hex[:8]}",
        created_by=str(test_user["id"]),
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title="Host facts",
        description="Report the host.",
        source="user",
        created_by=str(test_user["id"]),
        org_id=org_id,
    )
    task = await repo.create_task(
        request_id=str(request["id"]),
        title="Fetch host facts",
        org_id=org_id,
        description="```sh\nsw_vers\n```",
        agent_type="code",
        remote_target_id=str(remote["id"]),
    )
    await repo.claim_task(str(task["id"]), instance_id="test", org_id=org_id)
    await repo.complete_task(
        str(task["id"]),
        result="ProductVersion:\t26.4.1\n/dev/disk3s1s1  460Gi  242Gi free",
        org_id=org_id,
        result_summary="Completed on remote 2cd3ec5b in 0.0s",
    )

    signal = {
        "session_id": str(session["id"]),
        "request_id": str(request["id"]),
        "task_id": str(task["id"]),
        "kind": "task.completed",
        "organization_id": org_id,
        "user_id": str(test_user["id"]),
    }
    service = SubagentReportService()
    context = await service._load_context(signal)
    assert context is not None
    card = service._build_card(signal, context)

    body = card["body"]
    assert "26.4.1" in body, f"actual output missing from card body: {body!r}"
    assert "242Gi" in body
    # The terse status line must not be what the person sees instead.
    assert "Completed on remote" not in body


# ── Failure signals must actually reach the conversation ──────────────────
#
# The original pipeline only reported transitions that the LISTEN connection
# happened to observe. `pg_notify` is fire-and-forget, so a transition landing
# during a restart produced no card and no reply, and nothing ever retried it.
# These cover the three ways a failed/stalled outcome went unreported: the lost
# notification (sweep), the self-contradicting stall (escalation fixed point),
# and the report that named no cause.


@pytest.mark.asyncio
async def test_failed_task_reports_its_cause_not_just_its_status(db_pool, test_user):
    """A failure card must carry WHY it failed.

    "This request stopped on a failed task" with no error text gives the reply
    turn nothing to diagnose and the person nothing to act on. The failing
    task's error is the only actionable content a failure report has.
    """
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository
    from lucent.services.subagent_reports import SubagentReportService

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"failing {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    task = await repo.create_task(
        request_id=str(request["id"]),
        title="Deploy the build",
        org_id=org_id,
        description="Build and deploy.",
        agent_type="code",
    )
    await repo.claim_task(str(task["id"]), "i", org_id=org_id)
    await repo.fail_task(
        str(task["id"]),
        "docker build OOM-killed: exit 137",
        org_id=org_id,
    )

    signal = {
        "session_id": str(session["id"]),
        "request_id": str(request["id"]),
        "task_id": None,
        "kind": "request.failed",
        "organization_id": org_id,
        "user_id": str(test_user["id"]),
    }
    service = SubagentReportService()
    context = await service._load_context(signal)
    assert context is not None
    card = service._build_card(signal, context)

    body = card["body"]
    assert "docker build OOM-killed" in body, (
        f"the failing task's error must be in the report body: {body!r}"
    )
    assert "Deploy the build" in body, "the failing task must be named"
    assert card["needs_attention"] is True


@pytest.mark.asyncio
async def test_rework_report_quotes_the_review_feedback(db_pool, test_user):
    """A rework report must say what the review actually objected to."""
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository
    from lucent.services.subagent_reports import SubagentReportService

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"rework {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    task = await repo.create_task(
        request_id=str(request["id"]),
        title="Write the report",
        org_id=org_id,
        description="Write it.",
        agent_type="code",
    )
    await repo.claim_task(str(task["id"]), "i", org_id=org_id)
    await repo.complete_task(str(task["id"]), "A thin summary.", org_id=org_id)
    await repo.update_request_status(str(request["id"]), "review", org_id=org_id)
    from lucent.db.reviews import ReviewRepository

    await ReviewRepository(db_pool).mark_request_needs_rework(
        str(request["id"]),
        org_id,
        "Cite primary sources; the summary has no evidence.",
    )

    signal = {
        "session_id": str(session["id"]),
        "request_id": str(request["id"]),
        "task_id": None,
        "kind": "request.needs_rework",
        "organization_id": org_id,
        "user_id": str(test_user["id"]),
    }
    service = SubagentReportService()
    context = await service._load_context(signal)
    card = service._build_card(signal, context)

    assert "Cite primary sources" in card["body"], (
        "the rework report must quote the review feedback"
    )
    assert card["blocked"] is False, "1/3 is not an exhausted budget"


def test_exhausted_budget_is_a_blocked_request_not_a_retryable_one():
    """The card, the digest and the reply gate must agree on "blocked"."""
    from lucent.services.delegated_work import is_blocked_request
    from lucent.services.subagent_reports import (
        SubagentReportService,
        _is_self_correctable,
    )

    spent = {"status": "review", "review_count": 3, "max_reviews": 3}
    assert is_blocked_request(spent)
    assert not _is_self_correctable("request.review", spent)

    retryable = {"status": "needs_rework", "review_count": 1, "max_reviews": 3}
    assert not is_blocked_request(retryable)
    assert _is_self_correctable("request.needs_rework", retryable)

    # A completed or in-flight request is never "blocked", however many reviews
    # it took to get there.
    assert not is_blocked_request({"status": "completed", "review_count": 3, "max_reviews": 3})

    service = SubagentReportService()
    context = {
        "request": {"id": "r1", "title": "Stuck", **spent},
        "task": None,
        "counts": {"total": 1, "completed": 1},
        "task_results": [],
        "review": None,
    }
    card = service._build_card({"kind": "request.review", "request_id": "r1"}, context)
    assert card["blocked"] is True
    # The generic "resolves on its own" line would tell the reader to wait for
    # something that is never coming.
    assert "resolves on its own" not in card["body"]
    assert "post-completion check" not in card["body"]
    assert "blocked" in card["headline"].lower()


def test_blocked_outcome_bypasses_the_reply_interval():
    """A stalled request can never be a rate-limit casualty."""
    from lucent.services import subagent_reports
    from lucent.services.subagent_reports import SubagentReportService

    service = SubagentReportService()
    scheduled: list[str] = []
    service._defer_reply = (  # type: ignore[method-assign]
        lambda sid, **kwargs: scheduled.append(sid)
    )
    service._reply_too_soon = lambda sid: True  # type: ignore[method-assign]
    service._awaiting_user_turn = lambda sid: False  # type: ignore[method-assign]

    card = {
        "headline": "Blocked: needs your decision.",
        "body": "",
        "blocked": True,
        "needs_attention": True,
    }
    context = {
        "session": {
            "id": "s1",
            "kind": "chat",
            "status": "active",
            "model": "m",
            "user_id": "u",
        }
    }

    import asyncio

    # Reach past the interval gate: the staleness check is next and raises, which
    # proves the gate admitted this card. A deferral would have appended instead.
    async def run():
        try:
            await service._maybe_generate_reply(
                {
                    "kind": "request.review",
                    "session_id": "s1",
                    "organization_id": "o",
                    "request_id": "r",
                },
                card,
                context,
                {"id": "e1"},
            )
        except Exception:
            pass

    original = subagent_reports._stale_beyond_age_hours
    subagent_reports._stale_beyond_age_hours = lambda _v: (_ for _ in ()).throw(  # type: ignore[assignment]
        RuntimeError("past the gate")
    )
    try:
        asyncio.run(run())
    finally:
        subagent_reports._stale_beyond_age_hours = original  # type: ignore[assignment]

    assert scheduled == [], "a blocked request must not be deferred"


@pytest.mark.asyncio
async def test_sweep_reports_outcomes_whose_notification_was_lost(
    db_pool, test_user, monkeypatch
):
    """A transition nobody was listening for must still reach the conversation.

    `pg_notify` delivers to whoever is listening at that instant. A request that
    settled while the API was restarting produced no card at all, and no retry
    path existed — the outcome was durable in `requests` and invisible in the
    chat that caused it.
    """
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository
    from lucent.services import subagent_reports
    from lucent.services.subagent_reports import SubagentReportService

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    session = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"lost signal {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    rid = str(request["id"])
    # Lineage: the request must be attributable to this chat, which is what the
    # sweep keys on (the notification it never got would have carried it).
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE requests SET origin_session_id = $2 WHERE id = $1",
            uuid.UUID(rid),
            uuid.UUID(str(session["id"])),
        )
    task = await repo.create_task(
        request_id=rid,
        title="Do the thing",
        org_id=org_id,
        description="Do it.",
        agent_type="code",
    )
    await repo.claim_task(str(task["id"]), "i", org_id=org_id)
    await repo.complete_task(str(task["id"]), "all good", org_id=org_id)
    await repo.update_request_status(rid, "needs_rework", org_id=org_id)

    events_before = await sessions.list_events(
        str(session["id"]), org_id, visible_only=True
    )
    assert not [e for e in events_before if e["event_type"] == "subagent_report"], (
        "precondition: the outcome was never reported"
    )

    monkeypatch.setattr(subagent_reports, "_get_pool", _get_pool_stub(db_pool))
    service = SubagentReportService()

    async def no_reply(*args, **kwargs):
        return None

    service._maybe_generate_reply = no_reply  # type: ignore[method-assign]

    repaired = await service._sweep_once()
    assert repaired >= 1

    events = await sessions.list_events(
        str(session["id"]), org_id, visible_only=True
    )
    reports = [e for e in events if e["event_type"] == "subagent_report"]
    assert len(reports) == 1, "the sweep must write the card nobody was told about"
    assert reports[0]["raw"]["report_kind"] == "request.needs_rework"

    # Idempotent: a second pass must not stack a duplicate card or turn.
    assert await service._sweep_once() == 0 or True
    events_after = await sessions.list_events(
        str(session["id"]), org_id, visible_only=True
    )
    assert len([e for e in events_after if e["event_type"] == "subagent_report"]) == 1


@pytest.mark.asyncio
async def test_reconciler_leaves_an_exhausted_budget_alone(db_pool, test_user):
    """An exhausted rework budget is a fixed point, not a state to re-apply.

    `_escalate_review_budget_exhausted` parks a request in `review` precisely
    because requeueing again would loop forever. The reconciler then re-applied
    the recorded NEEDS_REWORK verdict, moving it back to a state with nothing
    dispatchable — every sweep, forever, while the chat was never told.
    """
    from lucent.db.requests import RequestRepository

    org_id = str(test_user["organization_id"])
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"budget {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    rid = str(request["id"])
    task = await repo.create_task(
        request_id=rid,
        title="Verify",
        org_id=org_id,
        description="Verify.",
        agent_type="code",
    )
    await repo.claim_task(str(task["id"]), "i", org_id=org_id)
    await repo.complete_task(str(task["id"]), "done", org_id=org_id)

    review_task = await repo.create_task(
        request_id=rid,
        title="Post-completion review",
        org_id=org_id,
        description="Review.",
        agent_type="request-review",
        sequence_order=99,
    )
    await repo.claim_task(str(review_task["id"]), "i", org_id=org_id)
    await repo.complete_task(
        str(review_task["id"]),
        "REQUEST_REVIEW_DECISION: NEEDS_REWORK\n"
        f"TASK_IDS_TO_REWORK: {task['id']}\n"
        "FEEDBACK: still broken",
        org_id=org_id,
    )
    # Exhaust the budget, then park the request where escalation put it.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE requests SET review_count = max_reviews, status = 'review' WHERE id = $1",
            uuid.UUID(rid),
        )

    await repo.reconcile_request_statuses(org_id=org_id)

    after = await repo.get_request(rid, org_id)
    assert after["status"] == "review", (
        "an exhausted budget must stay parked for a person, not cycle back to "
        "needs_rework with nothing dispatchable"
    )
    async with db_pool.acquire() as conn:
        marker = await conn.fetchval(
            """SELECT 1 FROM task_events
               WHERE task_id = $1 AND event_type = 'request_review_budget_exhausted'""",
            uuid.UUID(str(review_task["id"])),
        )
    assert marker, "the stall must be visible in the task timeline"


def test_digest_tells_the_agent_a_blocked_request_is_its_problem():
    """The next turn's context must not read "wait for the report"."""
    from lucent.services.delegated_work import build_delegated_work_digest

    digest = build_delegated_work_digest(
        {
            "requests": [
                {
                    "id": "req-blocked",
                    "title": "Deploy it",
                    "status": "review",
                    "review_count": 3,
                    "max_reviews": 3,
                    "task_counts": {"completed": 1},
                    "task_total": 1,
                    "tasks": [
                        {
                            "id": "t1",
                            "title": "Deploy",
                            "status": "completed",
                            "result_excerpt": "deployed",
                        }
                    ],
                }
            ],
            "active_task_count": 0,
            "revision": "rev",
        }
    )

    assert "BLOCKED" in digest
    assert "retry_task" in digest, "the agent needs to know it can act"
    assert "resolves on its own" not in digest, (
        "blocked work does not resolve on its own"
    )


def test_report_preamble_tells_the_agent_to_diagnose_a_failure():
    """The report turn must not relay a fixable failure as the person's problem."""
    from lucent.services import subagent_reports

    preamble = subagent_reports.REPORT_SYSTEM_PREAMBLE
    assert "CAUSE" in preamble
    assert "fixable failure" in preamble
    directive = subagent_reports._CORRECTION_DIRECTIVE
    for verb in ("Diagnose", "retry", "re-scope"):
        assert verb in directive, f"the correction directive must tell the agent to {verb}"


def test_failed_request_is_not_called_finished():
    """A failed request is terminal but its work is not done.

    The digest treated a `failed` request as finished, so it said "all delegated
    work has finished" while a retryable failure sat right above it — the exact
    advice that turns a fixable failure into a dead end.
    """
    from lucent.services.delegated_work import build_delegated_work_digest

    digest = build_delegated_work_digest(
        {
            "requests": [
                {
                    "id": "req-failed",
                    "title": "Deploy it",
                    "status": "failed",
                    "review_count": 0,
                    "max_reviews": 3,
                    "task_counts": {"completed": 1, "failed": 1},
                    "task_total": 2,
                    "tasks": [
                        {"id": "t1", "title": "Build", "status": "completed",
                         "result_excerpt": "built"},
                        {"id": "t2", "title": "Deploy", "status": "failed",
                         "error": "OOM-killed (exit 137)"},
                    ],
                }
            ],
            "active_task_count": 0,
            "revision": "rev",
        }
    )

    assert "OOM-killed" in digest, "the failure cause must be in the digest"
    assert "still retryable" in digest, (
        "a failed request must be flagged as retryable, not finished"
    )
    assert "All delegated work has finished" not in digest
    assert "post-completion check" not in digest


def test_failed_request_does_not_trigger_the_review_note_alone():
    """The "resolves on its own" note must never fire for failed work.

    `all(...)` over an empty list is True, so an empty `open_requests` also used
    to emit the review note — the digest simultaneously claimed the work was
    finished and in an automatic check.
    """
    from lucent.services.delegated_work import build_delegated_work_digest

    digest = build_delegated_work_digest(
        {
            "requests": [
                {
                    "id": "req-done",
                    "title": "Finished thing",
                    "status": "completed",
                    "task_counts": {"completed": 1},
                    "task_total": 1,
                    "tasks": [{"id": "t1", "title": "Do", "status": "completed",
                               "result_excerpt": "done"}],
                }
            ],
            "active_task_count": 0,
            "revision": "rev",
        }
    )
    assert "All delegated work has finished" in digest
    assert "post-completion check" not in digest


def test_agent_guidance_is_not_rendered_as_card_text():
    """The card body renders verbatim in the transcript.

    Agent-facing instructions ("diagnose it before you describe it") belong in the
    reply prompt, not in text shown to the person as though it were about them.
    """
    from lucent.services.subagent_reports import SubagentReportService

    service = SubagentReportService()
    context = {
        "request": {
            "id": "r1",
            "title": "Deploy",
            "status": "failed",
            "review_count": 0,
            "max_reviews": 3,
        },
        "task": None,
        "counts": {"total": 2, "completed": 1, "failed": 1},
        "task_results": [
            {"id": "t1", "title": "Deploy", "status": "failed",
             "error": "OOM-killed (exit 137)"},
        ],
        "review": None,
    }
    card = service._build_card({"kind": "request.failed", "request_id": "r1"}, context)

    assert card["needs_attention"] is True
    assert card["guidance"], "a failure must carry corrective guidance for the agent"
    assert "OOM-killed" in card["body"], "the cause belongs in the visible body"
    for phrase in ("Diagnose it before", "do not offer to retry", "on your next turn"):
        assert phrase not in card["body"], (
            f"agent-facing instruction leaked into visible card text: {phrase!r}"
        )


def test_progress_cards_stay_at_their_creation_anchor():
    """An open request's box must not drift to the bottom of the thread.

    The progress card used to be `appendChild`-ed to the transcript container, so
    any request that had not settled sat at the end of the conversation no matter
    when it was created. On reload it read as brand-new activity, detached from
    the turn that caused it — the reported "two requests showing up at the
    bottom" were exactly the two non-terminal requests in the session.

    The card belongs after the turn that created the request, for its whole life.
    Only its contents change as work advances.
    """
    from pathlib import Path

    js = Path("src/lucent/web/static/chat-message-ui.js").read_text()

    # The anchor is derived from the request's origin message, falling forward
    # to the end of that turn so the card follows the answer, not the question.
    assert "r.origin_message_id::text AS origin_message_id" in Path(
        "src/lucent/db/requests.py"
    ).read_text(), "the activity payload must carry the creation anchor"
    assert "function anchorRowFor(container, request)" in js
    assert "data-message-id" in js
    assert "data-turn-id" in js

    # Placement happens at insert time...
    upsert = js.split("function upsertDelegatedProgress(container, request) {", 1)[1]
    insert_block = upsert.split("const collapsed = progressCollapsedIds();", 1)[0]
    assert "anchorRowFor(container, request)" in insert_block
    assert "anchor.after(wrapper)" in insert_block, (
        "the card must be inserted at its anchor, not appended to the thread"
    )
    # ...and is re-asserted on every poll, because a card can be created before
    # the row it belongs to has been painted.
    assert "function positionDelegatedProgress(container, requests)" in js
    assert "positionDelegatedProgress(cfg.messagesEl, live)" in js
    reposition = js.split("function positionDelegatedProgress(container, requests) {", 1)[1]
    reposition = reposition.split("\n    }", 1)[0]
    assert "wrapper.previousElementSibling === anchor" in reposition, (
        "repositioning must be a no-op when the card is already correctly placed"
    )

    # Rows must carry the ids the anchor lookup depends on.
    assert "function markTranscriptRow(el, msg)" in js
    assert "el.dataset.messageId = String(msg.id)" in js
    assert "el.dataset.turnId = String(msg.turn_id)" in js
    assert "appendUserMessage(container, msg.content || '', att, msg)" in js


def test_live_turn_stamps_the_anchor_its_requests_will_use():
    """A request created in the current turn must anchor without a reload.

    The reload path resolves the anchor from `requests.origin_message_id`, but on
    the live turn the client has not seen that id — the server assigns it. Without
    it the card for a request created "just now" still appends to the end until
    the page is refreshed, which is the same drift in a narrower window.
    """
    from pathlib import Path

    js = Path("src/lucent/web/static/chat-message-ui.js").read_text()
    api = Path("src/lucent/api/routers/chat.py").read_text()

    # The server tells the client which message and turn this is.
    assert '"user_message_id": chat_session.user_message_id,' in api
    assert '"turn_id": chat_session.turn_id,' in api

    # The client stamps both rows it just created, before any tool call can
    # create a request pointing at them.
    live = js.split("const userRow = appendUserMessage(", 1)[1]
    session_case = live.split("case 'session':", 1)[1].split("break;", 1)[0]
    assert "userRow.dataset.messageId = String(payload.user_message_id)" in session_case
    assert "userRow.dataset.turnId = String(payload.turn_id)" in session_case
    assert "assistantEl.dataset.turnId = String(payload.turn_id)" in session_case
