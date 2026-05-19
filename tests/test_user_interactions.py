"""Tests for proactive Lucent handoff user interactions."""

import json
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from mcp.server.fastmcp import FastMCP

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.auth import set_current_user
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    create_session,
)
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.user_interactions import UserInteractionRepository
from lucent.api.routers.schedules import _workflow_interaction_references
from lucent.tools.requests import register_request_tools


@pytest_asyncio.fixture
async def interaction_prefix(db_pool):
    test_id = str(uuid4())[:8]
    prefix = f"test_interactions_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM schedule_runs WHERE schedule_id IN "
            "(SELECT id FROM schedules WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM schedules WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def interaction_user(db_pool, interaction_prefix):
    org = await OrganizationRepository(db_pool).create(name=f"{interaction_prefix}org")
    user = await UserRepository(db_pool).create(
        external_id=f"{interaction_prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{interaction_prefix}user@test.com",
        display_name=f"{interaction_prefix}User",
    )
    return user


@pytest_asyncio.fixture
async def api_client(db_pool, interaction_user):
    app = create_app()
    fake_user = CurrentUser(
        id=interaction_user["id"],
        organization_id=interaction_user["organization_id"],
        role=interaction_user.get("role", "member"),
        email=interaction_user.get("email"),
        display_name=interaction_user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["daemon-tasks"],
    )

    async def override_get_current_user():
        return fake_user

    app.dependency_overrides[get_current_user] = override_get_current_user
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def web_client(db_pool, interaction_user):
    session_token = await create_session(db_pool, interaction_user["id"])
    csrf_token = "test-csrf-inbox-token"
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={
            SESSION_COOKIE_NAME: session_token,
            CSRF_COOKIE_NAME: csrf_token,
        },
    ) as client:
        client._csrf_token = csrf_token  # type: ignore[attr-defined]
        yield client


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    data = {CSRF_FIELD_NAME: client._csrf_token}  # type: ignore[attr-defined]
    if extra:
        data.update(extra)
    return data


async def _call_mcp_tool(mcp: FastMCP, tool_name: str, args: dict | None = None):
    result = await mcp._tool_manager.call_tool(tool_name, args or {})
    return json.loads(result)


def test_workflow_interaction_references_use_activity_request_links():
    workflow_id = str(uuid4())
    run_id = str(uuid4())
    request_id = str(uuid4())

    refs = _workflow_interaction_references(
        action={"references": []},
        sched={"id": workflow_id, "title": "Weekly digest"},
        run={"id": run_id},
        req={"id": request_id, "title": "Weekly digest activity"},
    )

    refs_by_type = {ref["reference_type"]: ref for ref in refs}
    assert refs_by_type["workflow"]["url"] == f"/workflows/{workflow_id}"
    assert refs_by_type["request"]["url"] == f"/activity/{request_id}"
    assert refs_by_type["schedule_run"].get("url") is None
    assert refs_by_type["schedule_run"]["metadata"]["request_id"] == request_id


@pytest.mark.asyncio
async def test_send_handoff_mcp_tool_creates_handoff(db_pool, interaction_user):
    mcp = FastMCP("test-handoffs")
    register_request_tools(mcp)
    set_current_user(
        {
            "id": interaction_user["id"],
            "organization_id": interaction_user["organization_id"],
            "role": "member",
            "display_name": interaction_user.get("display_name"),
            "email": interaction_user.get("email"),
        }
    )
    try:
        created = await _call_mcp_tool(
            mcp,
            "send_handoff",
            {
                "title": "Weather outfit recommendation",
                "body": "It will rain this afternoon; take a light rain jacket.",
                "requires_response": False,
                "dedupe_key": "weather-outfit:test",
            },
        )
    finally:
        set_current_user(None)

    assert created["title"] == "Weather outfit recommendation"
    assert created["interaction_type"] == "handoff"
    assert created["requires_response"] is False
    assert created["url"].startswith("/handoffs/")

    repo = UserInteractionRepository(db_pool)
    detail = await repo.get_interaction(
        created["id"],
        interaction_user["organization_id"],
        user_id=interaction_user["id"],
    )
    assert detail is not None
    assert detail["title"] == "Weather outfit recommendation"
    assert detail["messages"][0]["body"] == (
        "It will rain this afternoon; take a light rain jacket."
    )


@pytest.mark.asyncio
async def test_repository_dedupe_reply_and_resolve(db_pool, interaction_user):
    repo = UserInteractionRepository(db_pool)

    created = await repo.create_interaction(
        org_id=interaction_user["organization_id"],
        user_id=interaction_user["id"],
        created_by=interaction_user["id"],
        title="Clarify Detroit goal next step",
        body="I found two plausible next steps and need your preference.",
        interaction_type="clarification",
        requires_response=True,
        response_prompt="Should Lucent focus on docs or implementation first?",
        references=[
            {
                "reference_type": "url",
                "label": "Planning note",
                "url": "https://example.test/planning",
            }
        ],
        dedupe_key="clarify:detroit:next-step",
    )

    assert created["status"] == "waiting_on_user"
    assert created["requires_response"] is True
    assert created["reference_count"] == 1
    assert len(created["messages"]) == 1
    assert await repo.count_attention_needed(
        org_id=interaction_user["organization_id"],
        user_id=interaction_user["id"],
    ) == 1

    duplicate = await repo.create_interaction(
        org_id=interaction_user["organization_id"],
        user_id=interaction_user["id"],
        created_by=interaction_user["id"],
        title="Clarify Detroit goal next step again",
        body="This should not create a duplicate handoff.",
        interaction_type="clarification",
        requires_response=True,
        dedupe_key="clarify:detroit:next-step",
    )

    assert duplicate["id"] == created["id"]
    assert duplicate["deduplicated"] is True
    assert duplicate["message_count"] == 1

    replied = await repo.add_message(
        interaction_id=created["id"],
        org_id=interaction_user["organization_id"],
        sender_type="user",
        sender_user_id=interaction_user["id"],
        body="Implementation first, docs second.",
    )

    assert replied["status"] == "responded"
    assert replied["first_response_at"] is not None
    assert replied["messages"][-1]["body"] == "Implementation first, docs second."
    assert await repo.count_attention_needed(
        org_id=interaction_user["organization_id"],
        user_id=interaction_user["id"],
    ) == 0

    resolved = await repo.resolve_interaction(
        interaction_id=created["id"],
        org_id=interaction_user["organization_id"],
        user_id=interaction_user["id"],
        note="Daemon consumed the answer.",
    )

    assert resolved is not None
    assert resolved["status"] == "resolved"
    active = await repo.list_interactions(
        org_id=interaction_user["organization_id"],
        user_id=interaction_user["id"],
    )
    assert all(str(item["id"]) != str(created["id"]) for item in active["items"])


@pytest.mark.asyncio
async def test_user_interaction_api_create_list_reply(api_client, interaction_user):
    create_resp = await api_client.post(
        "/api/user-interactions",
        json={
            "title": "Review workflow result",
            "body": "The workflow produced a data summary for you.",
            "interaction_type": "workflow_output",
            "requires_response": True,
            "response_prompt": "Do you want Lucent to turn this into a request?",
            "references": [
                {
                    "reference_type": "url",
                    "label": "Summary artifact",
                    "url": "https://example.test/summary",
                }
            ],
            "dedupe_key": "workflow-output:test",
        },
    )

    assert create_resp.status_code == 200
    created = create_resp.json()
    assert created["status"] == "waiting_on_user"
    assert created["references"][0]["label"] == "Summary artifact"

    list_resp = await api_client.get("/api/user-interactions")
    assert list_resp.status_code == 200
    assert any(item["id"] == created["id"] for item in list_resp.json()["items"])

    reply_resp = await api_client.post(
        f"/api/user-interactions/{created['id']}/reply",
        json={"body": "Yes, make a request from it."},
    )

    assert reply_resp.status_code == 200
    replied = reply_resp.json()
    assert replied["status"] == "responded"
    assert replied["messages"][-1]["sender_type"] == "user"
    assert replied["messages"][-1]["body"] == "Yes, make a request from it."


@pytest.mark.asyncio
async def test_handoffs_web_list_detail_and_reply(web_client, db_pool, interaction_user):
    repo = UserInteractionRepository(db_pool)
    interaction = await repo.create_interaction(
        org_id=interaction_user["organization_id"],
        user_id=interaction_user["id"],
        created_by=interaction_user["id"],
        title="Which source should I use?",
        body="I found two memory candidates and need your call.",
        interaction_type="clarification",
        requires_response=True,
        response_prompt="Pick A or B.",
        references=[
            {
                "reference_type": "url",
                "label": "Legacy self-link",
                "url": f"http://localhost:8767/inbox/{uuid4()}",
            },
            {
                "reference_type": "schedule_run",
                "label": "Workflow run should not show as a duplicate user link",
                "reference_id": str(uuid4()),
                "url": "/workflows/00000000-0000-0000-0000-000000000000",
            },
            {
                "reference_type": "url",
                "label": "Candidate A",
                "url": "https://example.test/a",
            }
        ],
    )

    list_resp = await web_client.get("/handoffs")
    assert list_resp.status_code == 200
    assert "Handoffs" in list_resp.text
    assert "Which source should I use?" in list_resp.text
    assert "Reply needed" in list_resp.text
    assert "Questions, decisions, and updates from Lucent." in list_resp.text
    assert "Waiting for your answer before Lucent continues." in list_resp.text
    assert "Inbox" not in list_resp.text

    legacy_list_resp = await web_client.get("/inbox")
    assert legacy_list_resp.status_code == 200
    assert "Handoffs" in legacy_list_resp.text

    detail_resp = await web_client.get(f"/handoffs/{interaction['id']}")
    assert detail_resp.status_code == 200
    assert "Handoffs" in detail_resp.text
    assert "Pick A or B." in detail_resp.text
    assert "Candidate A" in detail_resp.text
    assert "Legacy self-link" not in detail_resp.text
    assert "Workflow run should not show" not in detail_resp.text
    assert "Continue with Lucent" in detail_resp.text
    assert "Related context" in detail_resp.text
    assert "Reply here, ask a follow-up question" in detail_resp.text
    assert "Question from Lucent" in detail_resp.text
    assert "live session grounded" not in detail_resp.text
    assert "Lucent can see this Inbox message" not in detail_resp.text
    assert "Context Lucent brought" not in detail_resp.text
    assert "Inbox" not in detail_resp.text
    assert "Metadata" not in detail_resp.text
    assert "Dedupe key" not in detail_resp.text
    assert "Details" not in detail_resp.text
    async with db_pool.acquire() as conn:
        session_row = await conn.fetchrow(
            """SELECT id, title, metadata
               FROM llm_sessions
               WHERE organization_id = $1::uuid
                 AND user_id = $2::uuid
                 AND kind = 'embedded_chat'
                 AND metadata->>'interaction_id' = $3""",
            str(interaction_user["organization_id"]),
            str(interaction_user["id"]),
            str(interaction["id"]),
        )
        assert session_row is not None
        seeded_messages = await conn.fetch(
            "SELECT role, content, metadata FROM llm_messages WHERE session_id = $1 ORDER BY sequence",
            session_row["id"],
        )
    assert session_row["title"].startswith("Handoff:")
    assert session_row["metadata"]["interaction_id"] == str(interaction["id"])
    assert [(m["role"], m["content"]) for m in seeded_messages] == [
        ("assistant", "I found two memory candidates and need your call."),
    ]

    reply_resp = await web_client.post(
        f"/handoffs/{interaction['id']}/reply",
        data=_csrf_data(
            web_client,
            {
                "body": "Use source A.",
                "chat_session_id": str(session_row["id"]),
                "inline_chat": "1",
            },
        ),
        follow_redirects=False,
    )
    assert reply_resp.status_code == 303

    updated = await repo.get_interaction(
        interaction["id"],
        interaction_user["organization_id"],
        user_id=interaction_user["id"],
    )
    assert updated["status"] == "responded"
    assert updated["messages"][-1]["body"] == "Use source A."
    assert updated["messages"][-1]["metadata"]["source"] == "inline-chat"
    assert updated["messages"][-1]["metadata"]["llm_session_id"] == str(session_row["id"])

    unread = await repo.create_interaction(
        org_id=interaction_user["organization_id"],
        user_id=interaction_user["id"],
        created_by=interaction_user["id"],
        title="Workflow output ready",
        body="This is an informational handoff.",
        interaction_type="workflow_output",
        requires_response=False,
    )
    unread_detail = await web_client.get(f"/handoffs/{unread['id']}")
    assert unread_detail.status_code == 200
    assert "Workflow output ready" in unread_detail.text
    assert "Lucent messages needing attention" not in unread_detail.text


@pytest.mark.asyncio
async def test_workflow_user_interaction_action_triggers_handoff(
    api_client,
    db_pool,
    interaction_user,
):
    create_resp = await api_client.post(
        "/api/workflows",
        json={
            "title": "Handoff-only workflow",
            "description": "Sends a useful update without daemon task work.",
            "trigger_type": "manual",
            "priority": "high",
            "actions": [
                {
                    "action_type": "user_interaction",
                    "title": "Review {workflow_title}",
                    "description": "Manual run completed and needs a decision.",
                    "interaction_type": "decision",
                    "requires_response": True,
                    "response_prompt": "Should Lucent create follow-up work?",
                    "references": [
                        {
                            "reference_type": "url",
                            "label": "Runbook",
                            "url": "https://example.test/runbook",
                        }
                    ],
                }
            ],
        },
    )
    assert create_resp.status_code == 200
    workflow = create_resp.json()

    trigger_resp = await api_client.post(f"/api/workflows/{workflow['id']}/trigger")
    assert trigger_resp.status_code == 200
    triggered = trigger_resp.json()
    assert triggered["request"] is None
    assert triggered["tasks"] == []
    assert len(triggered["interactions"]) == 1
    interaction_id = triggered["interactions"][0]["id"]

    repo = UserInteractionRepository(db_pool)
    detail = await repo.get_interaction(
        interaction_id,
        interaction_user["organization_id"],
        user_id=interaction_user["id"],
    )
    assert detail["status"] == "waiting_on_user"
    assert detail["source"] == "workflow"
    assert detail["interaction_type"] == "decision"
    refs_by_type = {ref["reference_type"]: ref for ref in detail["references"]}
    assert set(refs_by_type) >= {"workflow", "schedule_run", "url"}
    assert refs_by_type["workflow"]["url"] == f"/workflows/{workflow['id']}"
    assert refs_by_type["schedule_run"]["url"] is None
    assert refs_by_type["schedule_run"]["metadata"]["workflow_id"] == workflow["id"]
    assert refs_by_type["schedule_run"]["metadata"]["request_id"] is None
