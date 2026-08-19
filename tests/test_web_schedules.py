"""Integration tests for schedule web routes in web/routes.py.

Tests the HTML-serving endpoints:
- GET  /schedules              (list with filtering)
- GET  /schedules/{id}         (detail page)
- POST /schedules/{id}/toggle  (enable/disable)
- POST /schedules/{id}/delete  (remove schedule)
- POST /schedules/{id}/edit    (update fields)

Uses real DB sessions + CSRF tokens through the full ASGI stack.
"""

import json
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    create_session,
)
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.definitions import DefinitionRepository
from lucent.db.schedules import ScheduleRepository

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for web schedule tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_websched_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        # Clean schedules by org
        await conn.execute(
            "DELETE FROM schedules WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memory_audit_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memory_access_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM memories WHERE username LIKE $1", f"{prefix}%")
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            """DELETE FROM agent_skills
               WHERE agent_id IN (
                   SELECT id FROM agent_definitions
                   WHERE organization_id IN (SELECT id FROM organizations WHERE name LIKE $1)
               )
               OR skill_id IN (
                   SELECT id FROM skill_definitions
                   WHERE organization_id IN (SELECT id FROM organizations WHERE name LIKE $1)
               )""",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM agent_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM skill_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def web_user(db_pool, web_prefix):
    """Create user + org for web tests and return (user, org, session_token)."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{web_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{web_prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{web_prefix}user@test.com",
        display_name=f"{web_prefix}User",
    )
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def client(db_pool, web_user):
    """httpx client with session + CSRF cookies pre-set."""
    _user, _org, session_token = web_user
    csrf_token = "test-csrf-token-abc123"

    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={
            SESSION_COOKIE_NAME: session_token,
            CSRF_COOKIE_NAME: csrf_token,
        },
    ) as c:
        # Stash CSRF token for POST helpers
        c._csrf_token = csrf_token  # type: ignore[attr-defined]
        yield c


@pytest_asyncio.fixture
async def schedule(db_pool, web_user):
    """Create a test schedule and return it."""
    user, org, _token = web_user
    repo = ScheduleRepository(db_pool)
    return await repo.create_schedule(
        title="Web Test Schedule",
        org_id=str(org["id"]),
        schedule_type="interval",
        interval_seconds=3600,
        description="Hourly web test",
        agent_type="code",
        created_by=str(user["id"]),
    )


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    """Build form data dict with CSRF token included."""
    data = {CSRF_FIELD_NAME: client._csrf_token}  # type: ignore[attr-defined]
    if extra:
        data.update(extra)
    return data


# ============================================================================
# GET /schedules — list
# ============================================================================


class TestSchedulesList:
    async def test_list_returns_html(self, client, schedule):
        resp = await client.get("/schedules")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_list_contains_schedule_title(self, client, schedule):
        resp = await client.get("/schedules")
        assert "Web Test Schedule" in resp.text

    async def test_list_filter_by_status(self, client, schedule):
        resp = await client.get("/schedules", params={"status": "active"})
        assert resp.status_code == 200

    async def test_list_filter_by_enabled(self, client, schedule):
        resp = await client.get("/schedules", params={"enabled": "true"})
        assert resp.status_code == 200

    async def test_list_unauthenticated_redirects(self, db_pool):
        """No session cookie → redirect to login."""
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/schedules", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")

    async def test_workflows_alias_contains_schedule_title(self, client, schedule):
        resp = await client.get("/workflows")
        assert resp.status_code == 200
        assert "Workflows" in resp.text
        assert "Web Test Schedule" in resp.text

    async def test_user_cannot_list_or_open_another_users_workflow(
        self, client, schedule, db_pool, web_user, web_prefix
    ):
        _user, org, _token = web_user
        other_user = await UserRepository(db_pool).create(
            external_id=f"{web_prefix}other-user",
            provider="local",
            organization_id=org["id"],
            email=f"{web_prefix}other-user@test.com",
            display_name="Other User",
        )
        other_token = await create_session(db_pool, other_user["id"])
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={SESSION_COOKIE_NAME: other_token},
        ) as other_client:
            listing = await other_client.get("/workflows")
            detail = await other_client.get(f"/workflows/{schedule['id']}")

        assert listing.status_code == 200
        assert "Web Test Schedule" not in listing.text
        assert detail.status_code == 404

    async def test_owner_sees_daemon_workflow_but_not_another_humans_workflow(
        self, client, db_pool, web_user, web_prefix
    ):
        owner, org, _token = web_user
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET role = 'owner' WHERE id = $1", owner["id"])
            daemon = await conn.fetchrow(
                """SELECT * FROM users
                   WHERE organization_id = $1
                     AND (role = 'daemon' OR external_id LIKE 'daemon-service:%')
                   LIMIT 1""",
                org["id"],
            )
        assert daemon is not None
        user_repo = UserRepository(db_pool)
        other_user = await user_repo.create(
            external_id=f"{web_prefix}other-human",
            provider="local",
            organization_id=org["id"],
            email=f"{web_prefix}other-human@test.com",
            display_name="Other Human",
        )
        repo = ScheduleRepository(db_pool)
        daemon_workflow = await repo.create_schedule(
            title="Daemon Workflow",
            org_id=str(org["id"]),
            schedule_type="manual",
            created_by=str(daemon["id"]),
        )
        human_workflow = await repo.create_schedule(
            title="Other Human Workflow",
            org_id=str(org["id"]),
            schedule_type="manual",
            created_by=str(other_user["id"]),
        )

        listing = await client.get("/workflows")
        daemon_detail = await client.get(f"/workflows/{daemon_workflow['id']}")
        human_detail = await client.get(f"/workflows/{human_workflow['id']}")

        assert listing.status_code == 200
        assert "Daemon Workflow" in listing.text
        assert "Other Human Workflow" not in listing.text
        assert "Daemon Workflows" in listing.text
        assert "Your Workflows" not in listing.text
        assert daemon_detail.status_code == 200
        assert human_detail.status_code == 404


class TestWorkflowWizard:
    async def test_new_workflow_wizard_returns_html(self, client):
        resp = await client.get("/workflows/new")
        assert resp.status_code == 200
        assert "Workflow Wizard" in resp.text
        assert "Build workflows by conversation" in resp.text
        assert "Incoming webhook" in resp.text
        assert "Additional sandbox config JSON" in resp.text
        assert '"setup_commands": ["npm ci", "npm test"]' in resp.text
        assert "Describe the structured result the task must return" in resp.text
        assert '/static/chat-message-ui.css' in resp.text
        assert '/static/chat-message-ui.js' in resp.text

    async def test_workflow_assistant_uses_workflow_composer(self, client, db_pool, web_user):
        user, org, _token = web_user
        repo = DefinitionRepository(db_pool)
        workflow_composer = await repo.create_agent(
            name="workflow-composer",
            description="Workflow composer",
            content="# Workflow Composer",
            org_id=str(org["id"]),
            created_by=str(user["id"]),
            status="active",
        )

        resp = await client.get("/workflows/new")

        assert resp.status_code == 200
        assert "Workflow Assistant" in resp.text
        assert "Choose a starting point" in resp.text
        assert "Weekly dependency review" in resp.text
        assert "Webhook" in resp.text
        assert f'data-agent-id="{workflow_composer["id"]}"' in resp.text
        assert "Ask for a draft first" in resp.text
        assert "LucentChatMessageUI.renderMarkdown" in resp.text
        assert "LucentChatMessageUI.appendToolCall" in resp.text
        assert "appendToolNote" not in resp.text

    async def test_wizard_creates_webhook_workflow(self, client, db_pool, web_user):
        _user, org, _token = web_user
        resp = await client.post(
            "/workflows/new",
            data=_csrf_data(
                client,
                {
                    "title": "Webhook wizard test",
                    "description": "Created by the workflow wizard",
                    "trigger_type": "webhook",
                    "webhook_secret": "wizard-secret",
                    "request_title": "{event_summary} webhook",
                    "action_title": "Handle webhook",
                    "action_prompt": "Process the webhook payload and record outputs.",
                    "action_agent_type": "code",
                    "action_priority": "high",
                    "action_stage": "3",
                    "action_sandbox_config_json": json.dumps(
                        {"network_mode": "none", "memory_limit": "4g"}
                    ),
                    "action_repo_url": "https://github.com/example/hooks",
                    "action_timeout_seconds": "1200",
                    "action_reuse_sandbox": "true",
                    "action_commit_approved": "false",
                    "action_output_schema_json": json.dumps(
                        {"type": "object", "required": ["summary"]}
                    ),
                    "action_output_failure": "retry_then_fallback",
                    "action_output_retries": "2",
                    "review_instructions": "Confirm outputs are recorded.",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/workflows/" in resp.headers.get("location", "")

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT title, trigger_type, schedule_type, actions, review_instructions,
                          webhook_secret_hash
                   FROM schedules
                   WHERE organization_id = $1 AND title = 'Webhook wizard test'""",
                org["id"],
            )
        assert row is not None
        assert row["trigger_type"] == "webhook"
        assert row["schedule_type"] == "webhook"
        assert row["webhook_secret_hash"]
        assert "Confirm outputs" in row["review_instructions"]
        action = row["actions"][0]
        assert action["priority"] == "high"
        assert action["sequence_order"] == 2
        assert action["sandbox_config"] == {
            "network_mode": "none",
            "memory_limit": "4g",
            "repo_url": "https://github.com/example/hooks",
            "timeout_seconds": 1200,
            "reuse_within_request": True,
        }
        assert action["output_contract"] == {
            "json_schema": {"type": "object", "required": ["summary"]},
            "on_failure": "retry_then_fallback",
            "max_retries": 2,
        }


# ============================================================================
# GET /schedules/{id} — detail
# ============================================================================


class TestScheduleDetail:
    async def test_run_history_uses_ten_items_per_page(self, client, schedule, monkeypatch):
        observed = {}

        async def list_runs(_self, schedule_id, *, limit, offset):
            observed.update(schedule_id=schedule_id, limit=limit, offset=offset)
            return {"items": [], "total_count": 0}

        monkeypatch.setattr(ScheduleRepository, "list_runs", list_runs)

        resp = await client.get(f"/workflows/{schedule['id']}")

        assert resp.status_code == 200
        assert observed == {
            "schedule_id": str(schedule["id"]),
            "limit": 10,
            "offset": 0,
        }

    async def test_detail_represents_missing_task_stages(
        self, client, schedule, db_pool, web_user
    ):
        _user, org, _token = web_user
        await db_pool.execute(
            "UPDATE schedules SET actions = $1 WHERE id = $2",
            [
                {
                    "action_type": "task",
                    "title": "First",
                    "description": "Run first.",
                    "agent_type": "code",
                    "priority": "medium",
                    "sequence_order": 0,
                },
                {
                    "action_type": "task",
                    "title": "Second",
                    "description": "Run second.",
                    "agent_type": "code",
                    "priority": "medium",
                    "sequence_order": 1,
                },
                {
                    "action_type": "task",
                    "title": "Fourth",
                    "description": "Run fourth.",
                    "agent_type": "code",
                    "priority": "medium",
                    "sequence_order": 3,
                },
            ],
            schedule["id"],
        )

        resp = await client.get(f"/workflows/{schedule['id']}")

        assert resp.status_code == 200
        assert "Stage 3" in resp.text
        assert "No tasks configured" in resp.text

    async def test_detail_returns_html(self, client, schedule):
        resp = await client.get(f"/schedules/{schedule['id']}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_detail_contains_title(self, client, schedule):
        resp = await client.get(f"/schedules/{schedule['id']}")
        assert "Web Test Schedule" in resp.text

    async def test_detail_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.get(f"/schedules/{fake_id}")
        assert resp.status_code == 404

    async def test_workflow_detail_alias_shows_flow(self, client, schedule):
        resp = await client.get(f"/workflows/{schedule['id']}")
        assert resp.status_code == 200
        assert "Workflow flow" in resp.text
        assert "Steps" in resp.text
        assert "Save workflow" in resp.text
        assert "No unsaved changes" in resp.text


# ============================================================================
# POST /schedules/{id}/toggle
# ============================================================================


class TestScheduleToggle:
    async def test_toggle_disables(self, client, schedule):
        """Toggle an enabled schedule → disabled, redirects."""
        resp = await client.post(
            f"/schedules/{schedule['id']}/toggle",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_toggle_changes_state(self, client, schedule, db_pool, web_user):
        """After toggling, enabled state flips."""
        _user, org, _token = web_user
        repo = ScheduleRepository(db_pool)

        before = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert before is not None
        was_enabled = before["enabled"]

        await client.post(
            f"/schedules/{schedule['id']}/toggle",
            data=_csrf_data(client),
            follow_redirects=False,
        )

        after = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert after is not None
        assert after["enabled"] is not was_enabled

    async def test_toggle_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.post(
            f"/schedules/{fake_id}/toggle",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    async def test_toggle_no_csrf_fails(self, client, schedule):
        """Missing CSRF token → 403."""
        resp = await client.post(
            f"/schedules/{schedule['id']}/toggle",
            follow_redirects=False,
        )
        assert resp.status_code == 403


# ============================================================================
# POST /schedules/{id}/delete
# ============================================================================


class TestScheduleDelete:
    async def test_delete_redirects(self, client, schedule):
        resp = await client.post(
            f"/schedules/{schedule['id']}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/schedules" in resp.headers.get("location", "")

    async def test_delete_removes_from_db(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        repo = ScheduleRepository(db_pool)

        await client.post(
            f"/schedules/{schedule['id']}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )

        result = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert result is None

    async def test_delete_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.post(
            f"/schedules/{fake_id}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    async def test_delete_no_csrf_fails(self, client, schedule):
        resp = await client.post(
            f"/schedules/{schedule['id']}/delete",
            follow_redirects=False,
        )
        assert resp.status_code == 403


# ============================================================================
# POST /schedules/{id}/edit
# ============================================================================


class TestScheduleEdit:
    async def test_edit_visual_request_template(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        resp = await client.post(
            f"/workflows/{schedule['id']}/edit",
            data=_csrf_data(
                client,
                {
                    "request_title": "Repeated request title",
                    "request_description": "Context included on every run.",
                    "dependency_policy": "permissive",
                },
            ),
            follow_redirects=False,
        )

        assert resp.status_code == 303
        updated = await ScheduleRepository(db_pool).get_schedule(
            str(schedule["id"]), str(org["id"])
        )
        assert updated is not None
        assert updated["request_template"]["title"] == "Repeated request title"
        assert updated["request_template"]["description"] == "Context included on every run."
        assert updated["request_template"]["dependency_policy"] == "permissive"

    async def test_edit_raw_json_can_override_visual_steps(
        self, client, schedule, db_pool, web_user
    ):
        _user, org, _token = web_user
        raw_actions = [
            {
                "action_type": "user_interaction",
                "title": "Ask for approval",
                "interaction_type": "question",
                "sequence_order": 0,
            }
        ]
        resp = await client.post(
            f"/workflows/{schedule['id']}/edit",
            data=_csrf_data(
                client,
                {
                    "use_raw_json": "on",
                    "actions_json": json.dumps(raw_actions),
                    "action_title": "Ignored visual step",
                    "action_prompt": "This should not replace the raw action.",
                },
            ),
            follow_redirects=False,
        )

        assert resp.status_code == 303
        updated = await ScheduleRepository(db_pool).get_schedule(
            str(schedule["id"]), str(org["id"])
        )
        assert updated is not None
        assert updated["actions"] == raw_actions

    async def test_edit_visual_steps_updates_ordered_actions(
        self, client, schedule, db_pool, web_user
    ):
        _user, org, _token = web_user
        csrf_token = client._csrf_token  # type: ignore[attr-defined]
        resp = await client.post(
            f"/workflows/{schedule['id']}/edit",
            content=urlencode([
                (CSRF_FIELD_NAME, csrf_token),
                ("action_title", "Research changes"),
                ("action_title", "Implement changes"),
                ("action_prompt", "Find the relevant behavior."),
                ("action_prompt", "Apply and verify the change."),
                ("action_agent_type", "research"),
                ("action_agent_type", "code"),
                ("action_model", ""),
                ("action_model", ""),
                ("action_reasoning_effort", ""),
                ("action_reasoning_effort", ""),
                ("action_priority", "low"),
                ("action_priority", "high"),
                ("action_stage", "1"),
                ("action_stage", "1"),
                ("action_sandbox_template_id", ""),
                ("action_sandbox_template_id", ""),
                ("action_sandbox_config_json", "{}"),
                ("action_sandbox_config_json", ""),
                ("action_repo_url", "https://github.com/example/repo"),
                ("action_repo_url", ""),
                ("action_branch", "main"),
                ("action_branch", ""),
                ("action_timeout_seconds", "900"),
                ("action_timeout_seconds", ""),
                ("action_output_mode", "diff"),
                ("action_output_mode", ""),
                ("action_commit_approved", "false"),
                ("action_commit_approved", "false"),
                ("action_reuse_sandbox", "true"),
                ("action_reuse_sandbox", "false"),
                ("action_output_schema_json", json.dumps({"type": "object"})),
                ("action_output_schema_json", ""),
                ("action_output_failure", "fail"),
                ("action_output_failure", "fallback"),
                ("action_output_retries", "0"),
                ("action_output_retries", "1"),
                ("action_existing_json", json.dumps({"output_contract": {"on_failure": "fail"}})),
                ("action_existing_json", "{}"),
            ]),
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

        assert resp.status_code == 303
        updated = await ScheduleRepository(db_pool).get_schedule(
            str(schedule["id"]), str(org["id"])
        )
        assert updated is not None
        actions = updated["actions"]
        assert [action["title"] for action in actions] == [
            "Research changes",
            "Implement changes",
        ]
        assert [action["sequence_order"] for action in actions] == [0, 0]
        assert [action["priority"] for action in actions] == ["low", "high"]
        assert actions[0]["sandbox_config"] == {
            "repo_url": "https://github.com/example/repo",
            "branch": "main",
            "timeout_seconds": 900,
            "output_mode": "diff",
            "reuse_within_request": True,
        }
        assert actions[0]["output_contract"] == {
            "json_schema": {"type": "object"},
            "on_failure": "fail",
            "max_retries": 0,
        }
        assert updated["agent_type"] == "research"

    async def test_edit_title(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        resp = await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client, {"title": "Updated Title"}),
            follow_redirects=False,
        )
        assert resp.status_code == 303

        repo = ScheduleRepository(db_pool)
        updated = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert updated is not None
        assert updated["title"] == "Updated Title"

    async def test_edit_description(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client, {"description": "New description"}),
            follow_redirects=False,
        )
        repo = ScheduleRepository(db_pool)
        updated = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert updated is not None
        assert updated["description"] == "New description"

    async def test_edit_agent_type(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client, {"agent_type": "research"}),
            follow_redirects=False,
        )
        repo = ScheduleRepository(db_pool)
        updated = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert updated is not None
        assert updated["agent_type"] == "research"

    async def test_edit_prompt(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client, {"prompt": "Do weekly review"}),
            follow_redirects=False,
        )
        repo = ScheduleRepository(db_pool)
        updated = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert updated is not None
        assert updated["prompt"] == "Do weekly review"

    async def test_edit_interval_seconds(self, client, schedule, db_pool, web_user):
        _user, org, _token = web_user
        await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client, {"interval_seconds": "7200"}),
            follow_redirects=False,
        )
        repo = ScheduleRepository(db_pool)
        updated = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert updated is not None
        assert updated["interval_seconds"] == 7200

    async def test_edit_invalid_interval_ignored(self, client, schedule, db_pool, web_user):
        """Non-numeric interval_seconds is silently ignored."""
        _user, org, _token = web_user
        await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client, {"interval_seconds": "not-a-number"}),
            follow_redirects=False,
        )
        repo = ScheduleRepository(db_pool)
        updated = await repo.get_schedule(str(schedule["id"]), str(org["id"]))
        assert updated is not None
        assert updated["interval_seconds"] == 3600  # unchanged

    async def test_edit_no_changes(self, client, schedule):
        """Submitting form with no actual changes still redirects OK."""
        resp = await client.post(
            f"/schedules/{schedule['id']}/edit",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_edit_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.post(
            f"/schedules/{fake_id}/edit",
            data=_csrf_data(client, {"title": "Ghost"}),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    async def test_edit_no_csrf_fails(self, client, schedule):
        resp = await client.post(
            f"/schedules/{schedule['id']}/edit",
            follow_redirects=False,
        )
        assert resp.status_code == 403
