"""Tests for persisted LLM session repository behavior."""

import pytest
import pytest_asyncio

from lucent.db.llm_sessions import LLMSessionRepository
from lucent.db.requests import RequestRepository


@pytest_asyncio.fixture(autouse=True)
async def cleanup_llm_session_rows(db_pool, test_user):
    yield
    org_id = test_user["organization_id"]
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM llm_sessions WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM requests WHERE organization_id = $1", org_id)
        await conn.execute(
            """DELETE FROM memory_audit_log
               WHERE memory_id IN (
                   SELECT id FROM memories
                   WHERE organization_id = $1
                     AND metadata->>'source' = 'llm_session'
               )""",
            org_id,
        )
        await conn.execute(
            """DELETE FROM memory_access_log
               WHERE memory_id IN (
                   SELECT id FROM memories
                   WHERE organization_id = $1
                     AND metadata->>'source' = 'llm_session'
               )""",
            org_id,
        )
        await conn.execute(
            """DELETE FROM memories
               WHERE organization_id = $1
                 AND metadata->>'source' = 'llm_session'""",
            org_id,
        )


@pytest.mark.asyncio
async def test_llm_session_messages_events_and_request_origin(db_pool, test_user):
    session_repo = LLMSessionRepository(db_pool)
    request_repo = RequestRepository(db_pool)

    session = await session_repo.create_session(
        org_id=test_user["organization_id"],
        user_id=test_user["id"],
        kind="chat",
        title="Investigate widget persistence",
        engine="langchain",
        model="test-model",
    )

    user_message = await session_repo.add_message(
        session["id"],
        org_id=test_user["organization_id"],
        role="user",
        content="Please persist this chat.",
    )
    assistant_message = await session_repo.add_message(
        session["id"],
        org_id=test_user["organization_id"],
        role="assistant",
        content="Persisted.",
    )
    event = await session_repo.add_event(
        session["id"],
        org_id=test_user["organization_id"],
        message_id=user_message["id"],
        event_type="tool_call",
        tool_name="create_request",
        tool_input={"title": "Persist chats"},
    )

    req = await request_repo.create_request(
        title="Persist chats",
        org_id=str(test_user["organization_id"]),
        created_by=str(test_user["id"]),
        source="user",
    )
    await session_repo.link_request(
        session["id"],
        req["id"],
        org_id=test_user["organization_id"],
        message_id=user_message["id"],
        event_id=event["id"],
    )

    refreshed_req = await request_repo.get_request(
        str(req["id"]),
        str(test_user["organization_id"]),
    )
    assert str(refreshed_req["origin_session_id"]) == str(session["id"])
    assert str(refreshed_req["origin_message_id"]) == str(user_message["id"])
    assert str(refreshed_req["origin_event_id"]) == str(event["id"])

    detail = await session_repo.get_session_detail(
        session["id"],
        test_user["organization_id"],
        user_id=test_user["id"],
    )
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["id"] == assistant_message["id"]
    assert detail["events"][0]["tool_name"] == "create_request"
    assert detail["requests"][0]["request_title"] == "Persist chats"

    # clean_test_data predates request tracking, so clean request/session rows
    # explicitly to keep teardown focused on the assertions above.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM llm_sessions WHERE organization_id = $1",
            test_user["organization_id"],
        )
        await conn.execute(
            "DELETE FROM requests WHERE organization_id = $1",
            test_user["organization_id"],
        )


@pytest.mark.asyncio
async def test_session_experience_capture_skips_trivial_chat(db_pool, test_user):
    session_repo = LLMSessionRepository(db_pool)

    session = await session_repo.create_session(
        org_id=test_user["organization_id"],
        user_id=test_user["id"],
        kind="chat",
        title="Joke request",
    )
    await session_repo.add_message(
        session["id"],
        org_id=test_user["organization_id"],
        role="user",
        content="Can you crack a joke?",
    )
    await session_repo.add_message(
        session["id"],
        org_id=test_user["organization_id"],
        role="assistant",
        content="Why did the database cross the road? To join the other table.",
    )

    result = await session_repo.maybe_capture_experience(
        session["id"],
        test_user["organization_id"],
        user_id=test_user["id"],
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "insufficient_signal"
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM memories
               WHERE organization_id = $1
                 AND metadata->>'session_id' = $2""",
            test_user["organization_id"],
            str(session["id"]),
        )
    assert count == 0


@pytest.mark.asyncio
async def test_session_experience_capture_creates_and_links_request_memory(
    db_pool, test_user
):
    session_repo = LLMSessionRepository(db_pool)
    request_repo = RequestRepository(db_pool)

    session = await session_repo.create_session(
        org_id=test_user["organization_id"],
        user_id=test_user["id"],
        kind="chat",
        title="Implement output artifacts",
    )
    user_message = await session_repo.add_message(
        session["id"],
        org_id=test_user["organization_id"],
        role="user",
        content="Please implement output artifacts for requests and tasks.",
    )
    await session_repo.add_message(
        session["id"],
        org_id=test_user["organization_id"],
        role="assistant",
        content="Implemented task_outputs, API endpoints, MCP tooling, and UI cards.",
    )
    event = await session_repo.add_event(
        session["id"],
        org_id=test_user["organization_id"],
        message_id=user_message["id"],
        event_type="tool_call",
        tool_name="create_request",
        tool_input={"title": "Implement output artifacts"},
    )
    req = await request_repo.create_request(
        title="Implement output artifacts",
        org_id=str(test_user["organization_id"]),
        created_by=str(test_user["id"]),
        source="user",
    )
    await session_repo.link_request(
        session["id"],
        req["id"],
        org_id=test_user["organization_id"],
        message_id=user_message["id"],
        event_id=event["id"],
    )

    result = await session_repo.maybe_capture_experience(
        session["id"],
        test_user["organization_id"],
        user_id=test_user["id"],
    )

    assert result["status"] == "created"
    memory_id = result["memory_id"]
    async with db_pool.acquire() as conn:
        memory = await conn.fetchrow("SELECT * FROM memories WHERE id = $1", memory_id)
        link_count = await conn.fetchval(
            """SELECT COUNT(*) FROM request_memories
               WHERE request_id = $1 AND memory_id = $2 AND relation = 'context'""",
            req["id"],
            memory_id,
        )
    assert memory is not None
    assert memory["type"] == "experience"
    assert "session-experience" in memory["tags"]
    assert memory["metadata"]["session_id"] == str(session["id"])
    assert str(req["id"]) in memory["metadata"]["request_ids"]
    assert link_count == 1


@pytest.mark.asyncio
async def test_session_experience_capture_updates_existing_memory(db_pool, test_user):
    session_repo = LLMSessionRepository(db_pool)

    session = await session_repo.create_session(
        org_id=test_user["organization_id"],
        user_id=test_user["id"],
        kind="chat",
        title="Multi-turn implementation session",
    )
    await session_repo.add_message(
        session["id"],
        org_id=test_user["organization_id"],
        role="user",
        content="Let's design a session experience capture system with criteria.",
    )
    await session_repo.add_message(
        session["id"],
        org_id=test_user["organization_id"],
        role="assistant",
        content="We can score linked requests, mutating tools, and transcript size.",
    )
    await session_repo.add_event(
        session["id"],
        org_id=test_user["organization_id"],
        event_type="tool_call",
        tool_name="update_memory",
        tool_input={"memory_id": "example"},
    )

    first = await session_repo.maybe_capture_experience(
        session["id"], test_user["organization_id"], user_id=test_user["id"]
    )
    await session_repo.add_message(
        session["id"],
        org_id=test_user["organization_id"],
        role="user",
        content="Add tests so the capture updates instead of duplicating.",
    )
    await session_repo.add_message(
        session["id"],
        org_id=test_user["organization_id"],
        role="assistant",
        content="Added update-path tests for the auto-captured experience memory.",
    )
    second = await session_repo.maybe_capture_experience(
        session["id"], test_user["organization_id"], user_id=test_user["id"]
    )

    assert first["status"] == "created"
    assert second["status"] == "updated"
    assert second["memory_id"] == first["memory_id"]
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM memories
               WHERE organization_id = $1
                 AND type = 'experience'
                 AND metadata->>'session_id' = $2""",
            test_user["organization_id"],
            str(session["id"]),
        )
    assert count == 1


@pytest.mark.asyncio
async def test_session_experience_capture_uses_model_content_override(db_pool, test_user):
    session_repo = LLMSessionRepository(db_pool)

    session = await session_repo.create_session(
        org_id=test_user["organization_id"],
        user_id=test_user["id"],
        kind="chat",
        title="Model summarized session",
    )
    await session_repo.add_message(
        session["id"],
        org_id=test_user["organization_id"],
        role="user",
        content="Let's implement model-written session summaries.",
    )
    await session_repo.add_message(
        session["id"],
        org_id=test_user["organization_id"],
        role="assistant",
        content="I added a configurable summary model call and fallback path.",
    )
    await session_repo.add_event(
        session["id"],
        org_id=test_user["organization_id"],
        event_type="tool_call",
        tool_name="update_memory",
        tool_input={"memory_id": "example"},
    )

    summary = (
        "## Session Summary\n\n"
        "The session replaced metadata-like capture with a narrative summary.\n\n"
        "## What Happened\n\n"
        "- Added model-generated session summaries.\n\n"
        "## Why It Matters\n\n"
        "Future Lucent can recover the actual work context.\n\n"
        "## Follow-up\n\n"
        "- None identified."
    )
    result = await session_repo.maybe_capture_experience(
        session["id"],
        test_user["organization_id"],
        user_id=test_user["id"],
        content_override=summary,
        summary_mode="model",
        summary_model="summary-model",
    )

    assert result["status"] == "created"
    async with db_pool.acquire() as conn:
        memory = await conn.fetchrow(
            "SELECT content, metadata FROM memories WHERE id = $1",
            result["memory_id"],
        )
    assert memory["content"] == summary
    assert memory["metadata"]["summary_mode"] == "model"
    assert memory["metadata"]["summary_model"] == "summary-model"
