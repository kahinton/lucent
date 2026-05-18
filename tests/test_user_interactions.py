"""Tests for proactive Lucent Inbox user interactions."""

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    create_session,
)
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.user_interactions import UserInteractionRepository


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
        body="This should not create a duplicate Inbox item.",
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
async def test_inbox_web_list_detail_and_reply(web_client, db_pool, interaction_user):
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
                "label": "Candidate A",
                "url": "https://example.test/a",
            }
        ],
    )

    list_resp = await web_client.get("/inbox")
    assert list_resp.status_code == 200
    assert "Which source should I use?" in list_resp.text
    assert "Reply needed" in list_resp.text

    detail_resp = await web_client.get(f"/inbox/{interaction['id']}")
    assert detail_resp.status_code == 200
    assert "Pick A or B." in detail_resp.text
    assert "Candidate A" in detail_resp.text
    assert "Reply to Lucent" in detail_resp.text

    reply_resp = await web_client.post(
        f"/inbox/{interaction['id']}/reply",
        data=_csrf_data(web_client, {"body": "Use source A."}),
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

    unread = await repo.create_interaction(
        org_id=interaction_user["organization_id"],
        user_id=interaction_user["id"],
        created_by=interaction_user["id"],
        title="Workflow output ready",
        body="This is an informational handoff.",
        interaction_type="workflow_output",
        requires_response=False,
    )
    unread_detail = await web_client.get(f"/inbox/{unread['id']}")
    assert unread_detail.status_code == 200
    assert "Workflow output ready" in unread_detail.text
    assert "Lucent messages needing attention" not in unread_detail.text


@pytest.mark.asyncio
async def test_workflow_user_interaction_action_triggers_inbox_item(
    api_client,
    db_pool,
    interaction_user,
):
    create_resp = await api_client.post(
        "/api/workflows",
        json={
            "title": "Inbox-only workflow",
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
    assert {ref["reference_type"] for ref in detail["references"]} >= {
        "workflow",
        "schedule_run",
        "url",
    }
