"""Tests for first-class task/request output artifacts."""

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from mcp.server.fastmcp import FastMCP

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.auth import set_current_user
from lucent.db.requests import RequestRepository
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

    result = await mcp._tool_manager.call_tool(tool_name, args or {})
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

        monkeypatch.setattr(daemon, "_find_review_agent_type", fake_find_review_agent_type)

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
