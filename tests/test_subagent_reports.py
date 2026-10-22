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
