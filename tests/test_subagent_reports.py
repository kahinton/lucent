"""Sub-agent reporting: results must reach the conversation that delegated them.

Covered here are the properties that make the pipeline trustworthy rather than
merely present: the card is durable and de-duplicated, the reply is written as a
real transcript message, the reply turn has no tools, and a resumed provider
session is abandoned once an unseen report reply exists.
"""

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
    await service._handle_batch([second_signal])

    events = await sessions.list_events(
        str(session["id"]), org_id, visible_only=True
    )
    reports = [e for e in events if e["event_type"] == "subagent_report"]
    assert len(reports) == 2, (
        "two distinct bursts with the same kind set must both reach the conversation"
    )
    assert len({e["raw"]["dedupe_key"] for e in reports}) == 2

    # A genuine replay of the first burst still de-duplicates.
    await service._handle_batch([first_signal])
    events_after = await sessions.list_events(
        str(session["id"]), org_id, visible_only=True
    )
    reports_after = [
        e for e in events_after if e["event_type"] == "subagent_report"
    ]
    assert len(reports_after) == 2, "a replayed burst must not add a second card"


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
    assert len(reports) == 2, (
        "the second review must be reported; it is not a replay of the first"
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
    assert data["active_task_count"] == 0
    (entry,) = [r for r in data["requests"] if r["id"] == str(request["id"])]
    assert entry["task_total"] == 1
    assert entry["tasks"][0]["title"] == "Panel task"
    assert "result_excerpt" in entry["tasks"][0]


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
