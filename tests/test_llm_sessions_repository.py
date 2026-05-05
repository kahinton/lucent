"""Tests for persisted LLM session repository behavior."""

import pytest

from lucent.db.llm_sessions import LLMSessionRepository
from lucent.db.requests import RequestRepository


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
