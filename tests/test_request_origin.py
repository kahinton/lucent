"""Origin resolution: which job or conversation actually created a request.

``requests.source`` only distinguishes a person from the machine. These tests
pin the lineage joins that turn "Lucent" into "the scheduled Learning
Extraction workflow", including the daemon case where the creating surface is a
task inside a *parent* request.
"""

from uuid import uuid4

import pytest
import pytest_asyncio

from lucent.db import OrganizationRepository, UserRepository
from lucent.db.requests import RequestRepository
from lucent.services.request_origin import (
    RequestOriginService,
    _created_request_id,
    actor_label,
)


@pytest_asyncio.fixture
async def origin_org(db_pool):
    """Org + owner with cleanup for rows this module writes."""
    prefix = f"test_origin_{str(uuid4())[:8]}_"
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}user@test.com",
        display_name=f"{prefix}User",
    )
    yield {"prefix": prefix, "org": org, "user": user}

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM tool_call_audit_log WHERE organization_id = $1",
            org["id"],
        )
        await conn.execute(
            "DELETE FROM schedule_runs WHERE schedule_id IN "
            "(SELECT id FROM schedules WHERE organization_id = $1)",
            org["id"],
        )
        await conn.execute(
            "DELETE FROM schedules WHERE organization_id = $1", org["id"]
        )
        await conn.execute(
            "DELETE FROM task_events WHERE task_id IN "
            "(SELECT id FROM tasks WHERE organization_id = $1)",
            org["id"],
        )
        await conn.execute(
            "DELETE FROM tasks WHERE organization_id = $1", org["id"]
        )
        await conn.execute(
            "DELETE FROM llm_sessions WHERE organization_id = $1", org["id"]
        )
        await conn.execute(
            "DELETE FROM requests WHERE organization_id = $1", org["id"]
        )
        await conn.execute("DELETE FROM users WHERE id = $1", user["id"])
        await conn.execute("DELETE FROM organizations WHERE id = $1", org["id"])


async def _audit_row(
    db_pool,
    *,
    org_id: str,
    created_request_id: str,
    source: str,
    task_id: str | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
):
    """Insert a create_request audit row the way the MCP bridge writes one."""
    import json

    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO tool_call_audit_log (
                   organization_id, task_id, request_id, session_id, source,
                   tool_name, status, input_preview, output_preview
               ) VALUES (
                   $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, 'create_request',
                   'success', '{}'::jsonb, $6
               )""",
            org_id,
            task_id,
            request_id,
            session_id,
            source,
            json.dumps({"id": created_request_id, "title": "probe", "status": "pending"}),
        )


class TestActorLabel:
    def test_daemon_and_cognitive_read_as_lucent(self):
        # The raw enum is meaningless to a reader: a daemon is not a person.
        assert actor_label("daemon") == "Lucent"
        assert actor_label("cognitive") == "Lucent"
        assert actor_label("schedule") == "Lucent"

    def test_human_and_api_sources_keep_their_identity(self):
        assert actor_label("user") == "You"
        assert actor_label("api") == "An API client"


class TestCreatedRequestId:
    def test_parses_the_success_payload(self):
        payload = '{"id": "1F98F663-15C7-4145-BECB-E75E1C3FE506", "title": "x"}'
        assert _created_request_id(payload) == "1f98f663-15c7-4145-becb-e75e1c3fe506"

    def test_ignores_error_text_and_empty_values(self):
        assert _created_request_id("Error calling tool create_request: boom") is None
        assert _created_request_id("") is None
        assert _created_request_id(None) is None

    def test_does_not_match_an_id_quoted_inside_the_description(self):
        """Only the leading `id` key identifies the created request.

        A payload whose *body* mentions some other request must not resolve to
        that request, or the wrong job would be credited.
        """
        payload = (
            '{"id": "11111111-1111-1111-1111-111111111111", '
            '"description": "follow up on 22222222-2222-2222-2222-222222222222"}'
        )
        assert _created_request_id(payload) == "11111111-1111-1111-1111-111111111111"


class TestWorkflowOrigin:
    async def test_scheduled_request_resolves_to_its_workflow(self, db_pool, origin_org):
        org_id = str(origin_org["org"]["id"])
        repo = RequestRepository(db_pool)
        request = await repo.create_request(
            title="[Scheduled] Morning Briefing",
            org_id=org_id,
            source="schedule",
            created_by=str(origin_org["user"]["id"]),
        )

        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                """INSERT INTO schedules (
                       organization_id, created_by, title, description,
                       schedule_type, cron_expression, prompt, status, enabled,
                       trigger_type
                   ) VALUES ($1::uuid, $2::uuid, 'Morning Briefing', '', 'cron',
                             '0 7 * * *', 'brief me', 'active', TRUE, 'schedule')
                   RETURNING id""",
                org_id,
                origin_org["user"]["id"],
            )
            await conn.execute(
                """INSERT INTO schedule_runs (schedule_id, request_id, status, started_at)
                   VALUES ($1::uuid, $2::uuid, 'completed', NOW())""",
                schedule["id"],
                request["id"],
            )

        origin = await RequestOriginService(db_pool).resolve(request["id"], org_id=org_id)

        assert origin is not None
        assert origin["kind"] == "workflow"
        assert origin["actor"] == "Lucent"
        assert len(origin["chain"]) == 1
        hop = origin["chain"][0]
        assert hop["kind"] == "workflow"
        assert hop["label"] == "Morning Briefing"
        assert hop["href"] == f"/workflows/{schedule['id']}"
        assert origin["url"] == f"/workflows/{schedule['id']}"


class TestDaemonOrigin:
    async def test_daemon_request_resolves_through_its_creating_task(
        self, db_pool, origin_org
    ):
        """A daemon-created request names the job, not a bare "Lucent".

        This is the exact shape found in production: a memory-maintenance
        fan-out task inside a ``[Scheduled]`` parent request creates a new
        request. The person recognises the workflow, not the daemon function.
        """
        org_id = str(origin_org["org"]["id"])
        user_id = str(origin_org["user"]["id"])
        repo = RequestRepository(db_pool)

        parent = await repo.create_request(
            title="[Scheduled] Learning Extraction",
            org_id=org_id,
            source="schedule",
            created_by=user_id,
        )
        parent_task = await repo.create_task(
            request_id=str(parent["id"]),
            title="Learning Extraction",
            org_id=org_id,
            agent_type="reflection",
        )
        child = await repo.create_request(
            title="Fold the amendment into the proposal",
            org_id=org_id,
            source="daemon",
            created_by=user_id,
        )

        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                """INSERT INTO schedules (
                       organization_id, created_by, title, description,
                       schedule_type, cron_expression, prompt, status, enabled,
                       trigger_type
                   ) VALUES ($1::uuid, $2::uuid, 'Learning Extraction', '', 'cron',
                             '0 8 * * *', 'extract', 'active', TRUE, 'schedule')
                   RETURNING id""",
                org_id,
                user_id,
            )
            await conn.execute(
                """INSERT INTO schedule_runs (schedule_id, request_id, status, started_at)
                   VALUES ($1::uuid, $2::uuid, 'completed', NOW())""",
                schedule["id"],
                parent["id"],
            )

        await _audit_row(
            db_pool,
            org_id=org_id,
            created_request_id=str(child["id"]),
            source="daemon.memory_maintenance_fanout",
            task_id=str(parent_task["id"]),
            request_id=str(parent["id"]),
        )

        origin = await RequestOriginService(db_pool).resolve(child["id"], org_id=org_id)

        assert origin is not None
        # The chain must expose BOTH navigable ancestors: the workflow the job
        # belongs to, and the request whose task created this one.
        kinds = [hop["kind"] for hop in origin["chain"]]
        assert kinds == ["workflow", "request", "task"]
        assert origin["chain"][0]["href"] == f"/workflows/{schedule['id']}"
        assert origin["chain"][1]["href"] == f"/activity/{parent['id']}"
        assert origin["chain"][2]["href"] == (
            f"/activity/{parent['id']}#task-{parent_task['id']}"
        )
        assert origin["detail"] == "Created during memory maintenance."

    async def test_unknown_creating_context_falls_back_to_lucent(self, db_pool, origin_org):
        """No audit row and no session → still says Lucent, not nothing.

        Historical requests predate the audit log entirely. The answer must be
        coarse but never blank, because a blank origin reads as "unknown" and
        invites the same "where did this come from?" question.
        """
        org_id = str(origin_org["org"]["id"])
        request = await RequestRepository(db_pool).create_request(
            title="Orphaned background request",
            org_id=org_id,
            source="daemon",
            created_by=str(origin_org["user"]["id"]),
        )

        origin = await RequestOriginService(db_pool).resolve(request["id"], org_id=org_id)

        assert origin is not None
        assert origin["kind"] == "lucent"
        assert origin["actor"] == "Lucent"
        assert origin["label"] == "Lucent"
        assert origin["detail"] == "Created by a Lucent background job."
        assert origin["chain"] == []

    async def test_cognitive_request_names_the_planning_cycle(self, db_pool, origin_org):
        org_id = str(origin_org["org"]["id"])
        request = await RequestRepository(db_pool).create_request(
            title="Planned by the cognition loop",
            org_id=org_id,
            source="cognitive",
            created_by=str(origin_org["user"]["id"]),
        )

        origin = await RequestOriginService(db_pool).resolve(request["id"], org_id=org_id)

        assert origin["kind"] == "lucent"
        assert origin["label"] == "Lucent's planning cycle"


class TestChatOrigin:
    async def test_origin_session_resolves_to_the_conversation(self, db_pool, origin_org):
        org_id = str(origin_org["org"]["id"])
        user_id = str(origin_org["user"]["id"])
        request = await RequestRepository(db_pool).create_request(
            title="Delegated from chat",
            org_id=org_id,
            source="user",
            created_by=user_id,
        )

        async with db_pool.acquire() as conn:
            session = await conn.fetchrow(
                """INSERT INTO llm_sessions (
                       organization_id, user_id, kind, title, model
                   ) VALUES ($1::uuid, $2::uuid, 'chat', 'Refactor the parser', 'm')
                   RETURNING id""",
                org_id,
                user_id,
            )
            await conn.execute(
                "UPDATE requests SET origin_session_id = $2::uuid WHERE id = $1::uuid",
                request["id"],
                session["id"],
            )

        origin = await RequestOriginService(db_pool).resolve(request["id"], org_id=org_id)

        assert origin["kind"] == "chat"
        assert origin["actor"] == "You"
        assert origin["chain"] == [
            {
                "kind": "chat",
                "label": "Refactor the parser",
                "href": f"/chat/{session['id']}",
                "meta": "conversation",
            }
        ]

    async def test_chat_surface_without_a_session_has_no_link_to_offer(
        self, db_pool, origin_org
    ):
        """Older chat requests were linked only in the audit log.

        Without a session row there is nowhere to send the reader, so the
        origin must still read as a conversation but must NOT invent a link.
        """
        org_id = str(origin_org["org"]["id"])
        request = await RequestRepository(db_pool).create_request(
            title="Old chat-delegated request",
            org_id=org_id,
            source="user",
            created_by=str(origin_org["user"]["id"]),
        )
        await _audit_row(
            db_pool,
            org_id=org_id,
            created_request_id=str(request["id"]),
            source="chat.stream_v2",
        )

        origin = await RequestOriginService(db_pool).resolve(request["id"], org_id=org_id)

        assert origin["kind"] == "chat"
        assert origin["chain"] == []
        assert origin["url"] is None

    async def test_audit_session_becomes_a_real_link(self, db_pool, origin_org):
        """A session named only by the audit row still yields a clickable hop.

        `origin_session_id` is unset on older rows, so the audit row's
        `session_id` is the only surviving pointer to the conversation.
        """
        org_id = str(origin_org["org"]["id"])
        user_id = str(origin_org["user"]["id"])
        request = await RequestRepository(db_pool).create_request(
            title="Chat-delegated with audit-only lineage",
            org_id=org_id,
            source="user",
            created_by=user_id,
        )
        async with db_pool.acquire() as conn:
            session = await conn.fetchrow(
                """INSERT INTO llm_sessions (
                       organization_id, user_id, kind, title, model
                   ) VALUES ($1::uuid, $2::uuid, 'chat', 'Audit-linked chat', 'm')
                   RETURNING id""",
                org_id,
                user_id,
            )
        await _audit_row(
            db_pool,
            org_id=org_id,
            created_request_id=str(request["id"]),
            source="chat.stream_v2",
            session_id=str(session["id"]),
        )

        origin = await RequestOriginService(db_pool).resolve(request["id"], org_id=org_id)

        assert origin["kind"] == "chat"
        assert origin["chain"] == [
            {
                "kind": "chat",
                "label": "Audit-linked chat",
                "href": f"/chat/{session['id']}",
                "meta": "conversation",
            }
        ]


class TestBatchResolution:
    async def test_resolve_many_returns_every_id_including_human_ones(
        self, db_pool, origin_org
    ):
        org_id = str(origin_org["org"]["id"])
        user_id = str(origin_org["user"]["id"])
        repo = RequestRepository(db_pool)
        human = await repo.create_request(
            title="A human asked for this", org_id=org_id, created_by=user_id
        )
        automated = await repo.create_request(
            title="A job asked for this", org_id=org_id, source="daemon",
            created_by=user_id,
        )

        origins = await RequestOriginService(db_pool).resolve_many(
            [str(human["id"]), str(automated["id"])], org_id=org_id
        )

        assert origins[str(human["id"])]["kind"] == "you"
        assert origins[str(automated["id"])]["kind"] == "lucent"

    async def test_resolve_many_on_an_empty_batch_is_a_no_op(self, db_pool, origin_org):
        origins = await RequestOriginService(db_pool).resolve_many(
            [], org_id=str(origin_org["org"]["id"])
        )
        assert origins == {}

    async def test_requests_from_another_org_are_not_resolved(self, db_pool, origin_org):
        """Origin lookup must be tenant-scoped, not just id-scoped."""
        org_id = str(origin_org["org"]["id"])
        request = await RequestRepository(db_pool).create_request(
            title="Tenant-scoped probe",
            org_id=org_id,
            created_by=str(origin_org["user"]["id"]),
        )
        other_org = await OrganizationRepository(db_pool).create(
            name=f"{origin_org['prefix']}other"
        )
        try:
            origin = await RequestOriginService(db_pool).resolve(
                request["id"], org_id=str(other_org["id"])
            )
            assert origin is None
        finally:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM organizations WHERE id = $1", other_org["id"]
                )


class TestNoUnexpectedQueries:
    async def test_missing_request_returns_none(self, db_pool, origin_org):
        origin = await RequestOriginService(db_pool).resolve(
            str(uuid4()), org_id=str(origin_org["org"]["id"])
        )
        assert origin is None
