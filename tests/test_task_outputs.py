"""Tests for first-class task/request output artifacts."""

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from mcp.server import MCPServer as FastMCP

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.auth import set_current_user
from lucent.db.requests import RequestRepository
from lucent.llm.context import clear_llm_context, set_llm_context
from lucent.tools.requests import register_request_tools


@pytest_asyncio.fixture
async def output_client(test_user):
    app = create_app()
    fake_user = CurrentUser(
        id=test_user["id"],
        organization_id=test_user["organization_id"],
        role=test_user.get("role", "member"),
        email=test_user.get("email"),
        display_name=test_user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override_get_current_user():
        return fake_user

    app.dependency_overrides[get_current_user] = override_get_current_user
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def mcp_outputs(db_pool):
    m = FastMCP("test-task-outputs")
    register_request_tools(m)
    return m


@pytest_asyncio.fixture(autouse=True)
async def cleanup_task_output_requests(db_pool, test_organization):
    yield
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM requests WHERE organization_id = $1",
            test_organization["id"],
        )


async def _call(mcp, tool_name: str, args: dict | None = None):
    import json

    result = await mcp._tool_manager.call_tool(tool_name, args or {}, None)
    return json.loads(result)


async def _create_request_and_task(repo: RequestRepository, test_user, title: str = "Task"):
    req = await repo.create_request(
        title=f"Output test {uuid4()}",
        description="Verify task output artifacts",
        source="user",
        priority="medium",
        created_by=str(test_user["id"]),
        org_id=str(test_user["organization_id"]),
    )
    task = await repo.create_task(
        request_id=str(req["id"]),
        title=title,
        org_id=str(test_user["organization_id"]),
        description="Produce a deliverable",
        requesting_user_id=str(test_user["id"]),
    )
    return req, task


_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=52.52&longitude=13.41&hourly=temperature_2m"
)

# The shape a ``script`` task stores as its result: machine output, including
# the HTTP request line a script printed to stderr.
_SCRIPT_MACHINE_RESULT = (
    "exit_code: 0\n"
    "stdout:\n"
    '{"total": 42}\n'
    "stderr:\n"
    f"GET {_FORECAST_URL} HTTP/1.1\n"
)


async def _insert_script_template(db_pool, org_id: str) -> str:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO sandbox_templates
               (organization_id, name, image, scope, status, created_by)
               VALUES ($1::uuid, 'task-output-script-tpl', 'python:3.12-slim',
                       'built-in', 'approved', NULL)
               ON CONFLICT (organization_id, name)
               DO UPDATE SET image = EXCLUDED.image
               RETURNING id""",
            org_id,
        )
    return str(row["id"])


async def _create_script_task(repo: RequestRepository, db_pool, test_user):
    org_id = str(test_user["organization_id"])
    template_id = await _insert_script_template(db_pool, org_id)
    req = await repo.create_request(
        title=f"Script output test {uuid4()}",
        description="Verify script machine output is not auto-extracted",
        source="user",
        priority="medium",
        created_by=str(test_user["id"]),
        org_id=org_id,
    )
    task = await repo.create_task(
        request_id=str(req["id"]),
        title="Fetch a forecast",
        org_id=org_id,
        description="Run the forecast script",
        requesting_user_id=str(test_user["id"]),
        task_kind="script",
        execution_config={
            "source": "inline",
            "script": "print('{\"total\": 42}')",
        },
        sandbox_template_id=template_id,
    )
    return req, task


class TestTaskOutputRepository:
    @pytest.mark.asyncio
    async def test_create_output_and_load_request_detail(self, db_pool, test_user):
        repo = RequestRepository(db_pool)
        req, task = await _create_request_and_task(repo, test_user)

        output = await repo.create_task_output(
            task_id=str(task["id"]),
            org_id=str(test_user["organization_id"]),
            created_by=str(test_user["id"]),
            output={
                "output_type": "link",
                "title": "Implementation PR",
                "url": "https://github.com/kahinton/lucent/pull/123",
                "is_primary": True,
            },
        )

        assert output["output_type"] == "github_pr"
        assert output["is_primary"] is True

        detail = await repo.get_request_with_tasks(
            str(req["id"]), str(test_user["organization_id"])
        )
        assert detail is not None
        assert detail["outputs"][0]["title"] == "Implementation PR"
        assert detail["tasks"][0]["outputs"][0]["output_type"] == "github_pr"

    @pytest.mark.asyncio
    async def test_complete_task_extracts_structured_outputs(self, db_pool, test_user):
        repo = RequestRepository(db_pool)
        req, task = await _create_request_and_task(repo, test_user)
        claimed = await repo.claim_task(
            str(task["id"]),
            "test-instance",
            org_id=str(test_user["organization_id"]),
        )
        assert claimed is not None

        completed = await repo.complete_task(
            str(task["id"]),
            "Created issue and sent summary email.",
            org_id=str(test_user["organization_id"]),
            instance_id="test-instance",
            result_structured={
                "summary": "Created issue",
                "outputs": [
                    {
                        "title": "Follow-up issue",
                        "url": "https://github.com/kahinton/lucent/issues/456",
                    },
                    {
                        "type": "email",
                        "title": "Summary email",
                        "external_id": "msg-123",
                        "provider": "gmail",
                    },
                ],
            },
            validation_status="valid",
        )

        assert completed is not None
        assert [o["output_type"] for o in completed["outputs"]] == [
            "github_issue",
            "email",
        ]
        detail = await repo.get_request_with_tasks(
            str(req["id"]), str(test_user["organization_id"])
        )
        assert len(detail["outputs"]) == 2

    @pytest.mark.asyncio
    async def test_complete_task_auto_extracts_url_outputs(self, db_pool, test_user):
        repo = RequestRepository(db_pool)
        req, task = await _create_request_and_task(repo, test_user)
        claimed = await repo.claim_task(
            str(task["id"]),
            "test-instance",
            org_id=str(test_user["organization_id"]),
        )
        assert claimed is not None

        completed = await repo.complete_task(
            str(task["id"]),
            (
                "Opened PR: https://github.com/kahinton/lucent/pull/321\n"
                "Published notes: https://docs.google.com/document/d/example"
            ),
            org_id=str(test_user["organization_id"]),
            instance_id="test-instance",
        )

        assert completed is not None
        assert [o["output_type"] for o in completed["outputs"]] == [
            "github_pr",
            "document",
        ]
        assert completed["outputs"][0]["metadata"]["auto_extracted"] is True
        detail = await repo.get_request_with_tasks(
            str(req["id"]), str(test_user["organization_id"])
        )
        assert len(detail["outputs"]) == 2

    @pytest.mark.asyncio
    async def test_complete_task_dedupes_explicit_and_auto_url_outputs(
        self, db_pool, test_user
    ):
        repo = RequestRepository(db_pool)
        _req, task = await _create_request_and_task(repo, test_user)
        claimed = await repo.claim_task(
            str(task["id"]),
            "test-instance",
            org_id=str(test_user["organization_id"]),
        )
        assert claimed is not None

        completed = await repo.complete_task(
            str(task["id"]),
            "Opened PR: https://github.com/kahinton/lucent/pull/654",
            org_id=str(test_user["organization_id"]),
            instance_id="test-instance",
            outputs=[
                {
                    "title": "Canonical PR title",
                    "url": "https://github.com/kahinton/lucent/pull/654",
                    "is_primary": True,
                }
            ],
        )

        assert completed is not None
        assert len(completed["outputs"]) == 1
        assert completed["outputs"][0]["title"] == "Canonical PR title"
        assert completed["outputs"][0]["is_primary"] is True


class TestTaskKindUrlAutoExtraction:
    """URL auto-extraction from ``result`` is an agent-prose affordance only."""

    @pytest.mark.asyncio
    async def test_script_task_machine_output_produces_no_outputs(
        self, db_pool, capable_organization, test_user
    ):
        repo = RequestRepository(db_pool)
        org_id = str(capable_organization["id"])
        req, task = await _create_script_task(repo, db_pool, test_user)
        claimed = await repo.claim_task(str(task["id"]), "test-instance", org_id=org_id)
        assert claimed is not None

        completed = await repo.complete_task(
            str(task["id"]),
            _SCRIPT_MACHINE_RESULT,
            org_id=org_id,
            instance_id="test-instance",
            result_structured={"total": 42},
        )

        assert completed is not None
        assert completed["outputs"] == []
        assert await repo.list_request_outputs(str(req["id"]), org_id) == []

    @pytest.mark.asyncio
    async def test_agent_task_prose_still_extracts_the_same_url(self, db_pool, test_user):
        repo = RequestRepository(db_pool)
        req, task = await _create_request_and_task(repo, test_user)
        org_id = str(test_user["organization_id"])
        claimed = await repo.claim_task(str(task["id"]), "test-instance", org_id=org_id)
        assert claimed is not None

        completed = await repo.complete_task(
            str(task["id"]),
            f"Fetched the forecast from {_FORECAST_URL}",
            org_id=org_id,
            instance_id="test-instance",
        )

        assert completed is not None
        persisted = await repo.list_request_outputs(str(req["id"]), org_id)
        assert len(persisted) == 1
        assert persisted[0]["url"] == _FORECAST_URL

    @pytest.mark.asyncio
    async def test_script_task_keeps_explicitly_recorded_output(
        self, mcp_outputs, db_pool, capable_organization, test_user
    ):
        org_id = str(capable_organization["id"])
        repo = RequestRepository(db_pool)
        req, task = await _create_script_task(repo, db_pool, test_user)

        set_current_user(
            {
                "id": test_user["id"],
                "organization_id": test_user["organization_id"],
                "role": "member",
                "display_name": "Test User",
                "email": "test@test.com",
            }
        )
        try:
            recorded = await _call(
                mcp_outputs,
                "record_task_output",
                {
                    "task_id": str(task["id"]),
                    "title": "Forecast report",
                    "url": "https://docs.google.com/document/d/script-report",
                },
            )
        finally:
            set_current_user(None)

        assert recorded["output_type"] == "document"

        claimed = await repo.claim_task(str(task["id"]), "test-instance", org_id=org_id)
        assert claimed is not None
        completed = await repo.complete_task(
            str(task["id"]),
            _SCRIPT_MACHINE_RESULT,
            org_id=org_id,
            instance_id="test-instance",
            result_structured={"total": 42},
        )

        assert completed is not None
        persisted = await repo.list_request_outputs(str(req["id"]), org_id)
        assert len(persisted) == 1
        assert persisted[0]["title"] == "Forecast report"
        assert persisted[0]["url"] == "https://docs.google.com/document/d/script-report"


class TestTaskOutputApi:
    @pytest.mark.asyncio
    async def test_complete_task_accepts_outputs(self, output_client):
        req_resp = await output_client.post(
            "/api/requests",
            json={"title": "API output request", "source": "user"},
        )
        assert req_resp.status_code == 200
        request_id = req_resp.json()["id"]

        task_resp = await output_client.post(
            f"/api/requests/{request_id}/tasks",
            json={"title": "Create PR"},
        )
        assert task_resp.status_code == 200
        task_id = task_resp.json()["id"]

        claim_resp = await output_client.post(
            f"/api/requests/tasks/{task_id}/claim",
            json={"instance_id": "api-test"},
        )
        assert claim_resp.status_code == 200

        complete_resp = await output_client.post(
            f"/api/requests/tasks/{task_id}/complete",
            json={
                "instance_id": "api-test",
                "result": "Opened a pull request.",
                "outputs": [
                    {
                        "title": "Review PR",
                        "url": "https://github.com/kahinton/lucent/pull/789",
                        "is_primary": True,
                    }
                ],
            },
        )
        assert complete_resp.status_code == 200
        data = complete_resp.json()
        assert data["outputs"][0]["output_type"] == "github_pr"
        assert data["outputs"][0]["title"] == "Review PR"

        detail_resp = await output_client.get(f"/api/requests/{request_id}")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["outputs"][0]["title"] == "Review PR"


class TestTaskOutputMcp:
    @pytest.mark.asyncio
    async def test_record_task_output_tool(self, mcp_outputs, db_pool, test_user):
        set_current_user(
            {
                "id": test_user["id"],
                "organization_id": test_user["organization_id"],
                "role": "member",
                "display_name": "Test User",
                "email": "test@test.com",
            }
        )
        try:
            repo = RequestRepository(db_pool)
            _req, task = await _create_request_and_task(repo, test_user, title="MCP output task")

            result = await _call(
                mcp_outputs,
                "record_task_output",
                {
                    "task_id": str(task["id"]),
                    "title": "Generated doc",
                    "url": "https://docs.google.com/document/d/example",
                    "provider": "google_docs",
                },
            )
        finally:
            set_current_user(None)

        assert result["output_type"] == "document"
        assert result["provider"] == "google_docs"
        assert result["title"] == "Generated doc"


class TestReviewOutputGuidance:
    @pytest.mark.asyncio
    async def test_review_task_prompt_includes_recorded_outputs(self, monkeypatch):
        from daemon.daemon import LucentDaemon

        captured: dict = {}

        async def fake_get_request_memories(_request_id):
            return []

        async def fake_create_task(**kwargs):
            captured.update(kwargs)
            return {"id": "review-task-id", **kwargs}

        monkeypatch.setattr(
            "daemon.daemon.RequestAPI.get_request_memories",
            fake_get_request_memories,
        )
        monkeypatch.setattr("daemon.daemon.RequestAPI.create_task", fake_create_task)

        daemon = LucentDaemon()

        async def fake_find_review_agent_type(_org_id, _requesting_user_id):
            return "request-review", "primary"

        async def fake_resolve_review_requesting_user_id(*, org_id, requester_user_id):
            return requester_user_id

        async def fake_select_model_for_user(**kwargs):
            return "test-review-model", "unit test stub"

        monkeypatch.setattr(daemon, "_find_review_agent_type", fake_find_review_agent_type)
        monkeypatch.setattr("daemon.daemon._select_model_for_user", fake_select_model_for_user)
        monkeypatch.setattr(
            daemon,
            "_resolve_review_requesting_user_id",
            fake_resolve_review_requesting_user_id,
        )

        await daemon._create_request_review_task(
            "11111111-1111-1111-1111-111111111111",
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "title": "Review output artifacts",
                "description": "Make sure deliverables are visible",
                "organization_id": "22222222-2222-2222-2222-222222222222",
                "created_by": "33333333-3333-3333-3333-333333333333",
                "priority": "medium",
                "tasks": [
                    {
                        "id": "44444444-4444-4444-4444-444444444444",
                        "status": "completed",
                        "title": "Create deliverable",
                        "result": "Opened a pull request.",
                        "outputs": [
                            {
                                "output_type": "github_pr",
                                "title": "Implementation PR",
                                "url": "https://github.com/kahinton/lucent/pull/42",
                                "external_id": None,
                            }
                        ],
                    }
                ],
            },
        )

        description = captured["description"]
        assert "recorded outputs" in description
        assert "Implementation PR" in description
        assert "OUTPUT ARTIFACT REVIEW" in description
        assert "record_task_output" in description
        assert "RESEARCH DELIVERY REQUIREMENT" in description
        assert "Create a handoff with `send_handoff`" in description
        assert "Create or update a file" in description
        assert "Memories and review prose do not satisfy this requirement" in description

    @pytest.mark.asyncio
    async def test_review_task_prompt_keeps_large_task_outputs(self, monkeypatch):
        from daemon.daemon import LucentDaemon

        captured: dict = {}
        long_result = "START\n" + ("substantive review context\n" * 500) + "END_SENTINEL"

        async def fake_get_request_memories(_request_id):
            return []

        async def fake_create_task(**kwargs):
            captured.update(kwargs)
            return {"id": "review-task-id", **kwargs}

        monkeypatch.setattr(
            "daemon.daemon.RequestAPI.get_request_memories",
            fake_get_request_memories,
        )
        monkeypatch.setattr("daemon.daemon.RequestAPI.create_task", fake_create_task)

        daemon = LucentDaemon()

        async def fake_find_review_agent_type(_org_id, _requesting_user_id):
            return "request-review", "primary"

        async def fake_resolve_review_requesting_user_id(*, org_id, requester_user_id):
            return requester_user_id

        async def fake_select_model_for_user(**kwargs):
            return "test-review-model", "unit test stub"

        monkeypatch.setattr(daemon, "_find_review_agent_type", fake_find_review_agent_type)
        monkeypatch.setattr("daemon.daemon._select_model_for_user", fake_select_model_for_user)
        monkeypatch.setattr(
            daemon,
            "_resolve_review_requesting_user_id",
            fake_resolve_review_requesting_user_id,
        )

        await daemon._create_request_review_task(
            "11111111-1111-1111-1111-111111111111",
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "title": "Review large outputs",
                "description": "Make sure review sees the actual task body",
                "organization_id": "22222222-2222-2222-2222-222222222222",
                "created_by": "33333333-3333-3333-3333-333333333333",
                "priority": "medium",
                "target_repo": "kahinton/detroit-vertical-harvest",
                "target_paths": ["docs/market-research.md"],
                "tasks": [
                    {
                        "id": "44444444-4444-4444-4444-444444444444",
                        "status": "completed",
                        "title": "Write long deliverable",
                        "result": long_result,
                        "outputs": [],
                    }
                ],
            },
        )

        description = captured["description"]
        assert "target_repo: kahinton/detroit-vertical-harvest" in description
        assert "Durable persistence is mandatory" in description
        assert "START" in description
        assert "END_SENTINEL" in description
        assert "truncated" not in description.lower()

    @pytest.mark.asyncio
    async def test_review_task_for_daemon_request_targets_human_handoff_user(self, monkeypatch):
        from daemon.daemon import LucentDaemon

        captured: dict = {}
        human_user_id = "77777777-7777-7777-7777-777777777777"

        async def fake_get_request_memories(_request_id):
            return []

        async def fake_create_task(**kwargs):
            captured.update(kwargs)
            return {"id": "review-task-id", **kwargs}

        monkeypatch.setattr(
            "daemon.daemon.RequestAPI.get_request_memories",
            fake_get_request_memories,
        )
        monkeypatch.setattr("daemon.daemon.RequestAPI.create_task", fake_create_task)

        daemon = LucentDaemon()

        async def fake_find_review_agent_type(_org_id, _requesting_user_id):
            assert _requesting_user_id == human_user_id
            return "request-review", "primary"

        async def fake_resolve_review_requesting_user_id(*, org_id, requester_user_id):
            assert requester_user_id == "33333333-3333-3333-3333-333333333333"
            return human_user_id

        async def fake_select_model_for_user(**kwargs):
            return "test-review-model", "unit test stub"

        monkeypatch.setattr(daemon, "_find_review_agent_type", fake_find_review_agent_type)
        monkeypatch.setattr("daemon.daemon._select_model_for_user", fake_select_model_for_user)
        monkeypatch.setattr(
            daemon,
            "_resolve_review_requesting_user_id",
            fake_resolve_review_requesting_user_id,
        )

        await daemon._create_request_review_task(
            "11111111-1111-1111-1111-111111111111",
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "title": "Review daemon-owned blocker",
                "description": "A cognitive request hit a setup blocker",
                "organization_id": "22222222-2222-2222-2222-222222222222",
                "created_by": "33333333-3333-3333-3333-333333333333",
                "priority": "medium",
                "tasks": [
                    {
                        "id": "44444444-4444-4444-4444-444444444444",
                        "status": "completed",
                        "title": "Blocked task",
                        "result": "BLOCKED: missing admin configuration.",
                        "outputs": [],
                    }
                ],
            },
        )

        assert captured["requesting_user_id"] == human_user_id
        assert "user_id=" not in captured["description"]
        assert "SETUP/CONFIGURATION BLOCKER HANDOFFS" in captured["description"]


class TestHandoffTaskOutput:
    """send_handoff records the handoff as the task's primary output artifact."""

    @staticmethod
    def _use_user(user, *, task_id=None, request_id=None):
        set_current_user(
            {
                "id": user["id"],
                "organization_id": user["organization_id"],
                "role": "member",
                "display_name": user.get("display_name"),
                "email": user.get("email"),
            }
        )
        set_llm_context(request_id=request_id, task_id=task_id)

    @pytest.mark.asyncio
    async def test_send_handoff_in_task_records_one_primary_output(
        self, mcp_outputs, db_pool, test_user
    ):
        repo = RequestRepository(db_pool)
        req, task = await _create_request_and_task(repo, test_user)
        org_id = str(test_user["organization_id"])

        self._use_user(test_user, task_id=str(task["id"]), request_id=str(req["id"]))
        try:
            created = await _call(
                mcp_outputs,
                "send_handoff",
                {
                    "title": "Delivery complete",
                    "body": "The report is ready for review.",
                },
            )
        finally:
            clear_llm_context()
            set_current_user(None)

        assert "error" not in created
        outputs = await repo.list_request_outputs(str(req["id"]), org_id)
        assert len(outputs) == 1
        output = outputs[0]
        assert output["output_type"] == "handoff"
        assert output["is_primary"] is True
        assert output["title"] == "Delivery complete"
        assert output["url"] == f"/handoffs/{created['id']}"
        assert output["external_id"] == created["id"]
        assert output["metadata"]["source"] == "handoff"
        assert output["metadata"]["interaction_id"] == created["id"]

    @pytest.mark.asyncio
    async def test_two_handoff_calls_with_same_interaction_yield_one_output(
        self, mcp_outputs, db_pool, test_user
    ):
        repo = RequestRepository(db_pool)
        req, task = await _create_request_and_task(repo, test_user)
        org_id = str(test_user["organization_id"])
        args = {
            "title": "Same handoff twice",
            "body": "Both calls represent one handoff.",
            "dedupe_key": "handoff-output:idempotency",
        }

        self._use_user(test_user, task_id=str(task["id"]), request_id=str(req["id"]))
        try:
            first = await _call(mcp_outputs, "send_handoff", args)
            second = await _call(mcp_outputs, "send_handoff", args)
        finally:
            clear_llm_context()
            set_current_user(None)

        # create_interaction collapses the two calls onto one interaction.
        assert first["id"] == second["id"]
        outputs = await repo.list_request_outputs(str(req["id"]), org_id)
        assert len(outputs) == 1
        assert outputs[0]["url"] == f"/handoffs/{first['id']}"

    @pytest.mark.asyncio
    async def test_output_store_failure_does_not_fail_the_handoff(
        self, mcp_outputs, db_pool, test_user, monkeypatch
    ):
        repo = RequestRepository(db_pool)
        req, task = await _create_request_and_task(repo, test_user)
        org_id = str(test_user["organization_id"])

        class _BrokenRepository:
            async def create_task_output(self, **kwargs):
                raise RuntimeError("output store unavailable")

        async def _broken_get_request_repository():
            return _BrokenRepository()

        monkeypatch.setattr(
            "lucent.tools.requests._get_request_repository",
            _broken_get_request_repository,
        )

        self._use_user(test_user, task_id=str(task["id"]), request_id=str(req["id"]))
        try:
            created = await _call(
                mcp_outputs,
                "send_handoff",
                {
                    "title": "Handoff survives output failure",
                    "body": "The handoff is the durable artifact.",
                },
            )
        finally:
            clear_llm_context()
            set_current_user(None)

        assert "error" not in created
        assert created["url"] == f"/handoffs/{created['id']}"

        async with db_pool.acquire() as conn:
            interaction_count = await conn.fetchval(
                "SELECT COUNT(*) FROM user_interactions WHERE id = $1::uuid",
                created["id"],
            )
        assert interaction_count == 1
        # The secondary artifact row was the only thing that failed.
        assert await repo.list_request_outputs(str(req["id"]), org_id) == []

    @pytest.mark.asyncio
    async def test_explicit_record_for_same_handoff_does_not_duplicate(
        self, mcp_outputs, db_pool, test_user
    ):
        repo = RequestRepository(db_pool)
        req, task = await _create_request_and_task(repo, test_user)
        org_id = str(test_user["organization_id"])

        self._use_user(test_user, task_id=str(task["id"]), request_id=str(req["id"]))
        try:
            created = await _call(
                mcp_outputs,
                "send_handoff",
                {"title": "One deliverable", "body": "Recorded twice, once."},
            )
            recorded = await _call(
                mcp_outputs,
                "record_task_output",
                {
                    "task_id": str(task["id"]),
                    "title": "One deliverable",
                    "url": created["url"],
                    "external_id": created["id"],
                    "output_type": "handoff",
                    "is_primary": True,
                },
            )
        finally:
            clear_llm_context()
            set_current_user(None)

        assert recorded["id"]
        outputs = await repo.list_request_outputs(str(req["id"]), org_id)
        assert len(outputs) == 1
        assert outputs[0]["url"] == f"/handoffs/{created['id']}"
