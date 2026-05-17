"""Tests for first-class workflow behavior layered on schedules."""

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.db.schedules import ScheduleRepository, webhook_secret_hash


@pytest_asyncio.fixture
async def workflow_cleanup(db_pool, test_organization):
    yield
    org_id = test_organization["id"]
    async with db_pool.acquire() as conn:
        req_ids = [
            row["id"]
            for row in await conn.fetch(
                "SELECT id FROM requests WHERE organization_id = $1",
                org_id,
            )
        ]
        if req_ids:
            task_ids = [
                row["id"]
                for row in await conn.fetch(
                    "SELECT id FROM tasks WHERE request_id = ANY($1)",
                    req_ids,
                )
            ]
            if task_ids:
                await conn.execute("DELETE FROM task_memories WHERE task_id = ANY($1)", task_ids)
                await conn.execute("DELETE FROM task_events WHERE task_id = ANY($1)", task_ids)
                await conn.execute("DELETE FROM task_outputs WHERE task_id = ANY($1)", task_ids)
            await conn.execute("DELETE FROM tasks WHERE request_id = ANY($1)", req_ids)
            await conn.execute(
                "UPDATE schedule_runs SET request_id = NULL WHERE request_id = ANY($1)",
                req_ids,
            )
            await conn.execute("DELETE FROM requests WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM schedules WHERE organization_id = $1", org_id)
        await conn.execute(
            "DELETE FROM agent_definitions WHERE organization_id = $1 AND name = 'code'",
            org_id,
        )


async def _insert_code_agent(db_pool, org_id: str, user_id: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_definitions (
                name, description, content, status, scope,
                organization_id, created_by, owner_user_id
            )
            VALUES (
                'code', 'test code agent', 'code agent content', 'active', 'instance',
                $1::uuid, $2::uuid, $2::uuid
            )
            ON CONFLICT (name, organization_id) DO UPDATE
            SET status = 'active', updated_at = now()
            """,
            org_id,
            user_id,
        )


@pytest.mark.asyncio
async def test_webhook_workflow_triggers_multi_action_request(
    db_pool,
    test_organization,
    test_user,
    workflow_cleanup,
):
    org_id = str(test_organization["id"])
    user_id = str(test_user["id"])
    await _insert_code_agent(db_pool, org_id, user_id)

    repo = ScheduleRepository(db_pool)
    secret = f"secret-{uuid4()}"
    workflow = await repo.create_schedule(
        title="Webhook triage",
        org_id=org_id,
        schedule_type="webhook",
        trigger_type="webhook",
        description="Handle inbound webhook events",
        created_by=user_id,
        webhook_secret_hash=webhook_secret_hash(secret),
        request_template={
            "title_prefix": "[Webhook]",
            "title": "{event_summary} for {workflow_title}",
            "description": "Process the incoming event: {event_summary}",
        },
        actions=[
            {
                "action_type": "task",
                "title": "Classify event",
                "description": "Classify the webhook and decide what follow-up is needed.",
                "agent_type": "code",
                "sequence_order": 0,
            },
            {
                "action_type": "task",
                "title": "Record follow-up",
                "description": "Create or record the follow-up artifact as a task output.",
                "agent_type": "code",
                "sequence_order": 1,
            },
        ],
        review_instructions="Confirm both task outputs are recorded before approval.",
    )

    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            f"/api/workflows/{workflow['id']}/webhook",
            headers={"X-Lucent-Workflow-Token": "wrong"},
            json={"action": "opened", "number": 123},
        )
        assert denied.status_code == 401

        accepted = await client.post(
            f"/api/workflows/{workflow['id']}/webhook",
            headers={
                "X-Lucent-Workflow-Token": secret,
                "X-Event-Type": "issue.opened",
            },
            json={"action": "opened", "number": 123},
        )

    assert accepted.status_code == 200
    payload = accepted.json()
    assert payload["request"]["title"] == "[Webhook] issue.opened for Webhook triage"
    assert len(payload["tasks"]) == 2

    async with db_pool.acquire() as conn:
        request = await conn.fetchrow(
            "SELECT title, description FROM requests WHERE organization_id = $1::uuid",
            org_id,
        )
        tasks = await conn.fetch(
            "SELECT title, sequence_order FROM tasks WHERE request_id = $1 ORDER BY sequence_order",
            payload["request"]["id"],
        )
        schedule = await conn.fetchrow(
            """SELECT status, run_count, next_run_at, webhook_last_received_at
               FROM schedules WHERE id = $1""",
            workflow["id"],
        )
        run_request_id = await conn.fetchval(
            """SELECT request_id FROM schedule_runs
               WHERE schedule_id = $1
               ORDER BY started_at DESC LIMIT 1""",
            workflow["id"],
        )

    assert request["title"] == "[Webhook] issue.opened for Webhook triage"
    assert "Confirm both task outputs" in request["description"]
    assert "issue.opened" in request["description"]
    assert [(t["title"], t["sequence_order"]) for t in tasks] == [
        ("Classify event", 0),
        ("Record follow-up", 1),
    ]
    assert schedule["status"] == "active"
    assert schedule["run_count"] == 1
    assert schedule["next_run_at"] is None
    assert schedule["webhook_last_received_at"] is not None
    assert str(run_request_id) == payload["request"]["id"]


@pytest.mark.asyncio
async def test_webhook_workflows_are_not_due_schedules(
    db_pool,
    test_organization,
    test_user,
    workflow_cleanup,
):
    repo = ScheduleRepository(db_pool)
    workflow = await repo.create_schedule(
        title="Webhook only",
        org_id=str(test_organization["id"]),
        schedule_type="webhook",
        trigger_type="webhook",
        created_by=str(test_user["id"]),
        webhook_secret_hash=webhook_secret_hash("secret"),
    )

    due = await repo.get_due_schedules(str(test_organization["id"]))
    assert str(workflow["id"]) not in {str(item["id"]) for item in due}
    assert workflow["next_run_at"] is None
    assert workflow["status"] == "active"
