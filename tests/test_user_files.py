"""Tests for user-owned file storage and request artifact integration."""

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from mcp.server import MCPServer as FastMCP

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.auth import set_current_user
from lucent.db import UserRepository
from lucent.db.llm_sessions import LLMSessionRepository
from lucent.db.requests import RequestRepository
from lucent.llm.context import clear_llm_context, set_llm_context
from lucent.storage.providers import FileStorageRegistry, LocalFileStorageProvider
from lucent.storage.service import UserFileService
from lucent.tools.requests import register_request_tools


def _service(db_pool, tmp_path) -> UserFileService:
    registry = FileStorageRegistry([LocalFileStorageProvider(tmp_path)])
    return UserFileService(db_pool, registry)


@pytest_asyncio.fixture(autouse=True)
async def cleanup_user_file_requests(db_pool, test_organization):
    yield
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM requests WHERE organization_id = $1",
            test_organization["id"],
        )


async def _call(mcp: FastMCP, tool_name: str, args: dict | None = None):
    import json

    result = await mcp._tool_manager.call_tool(tool_name, args or {}, None)
    return json.loads(result)


@pytest.mark.asyncio
async def test_user_file_content_is_private_to_its_owner(
    db_pool, test_user, test_organization, clean_test_data, tmp_path
):
    other_user = await UserRepository(db_pool).create(
        external_id=f"{clean_test_data}file-other-user",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{clean_test_data}file-other@test.com",
        display_name="Other file user",
    )
    service = _service(db_pool, tmp_path)

    created = await service.create(
        org_id=str(test_organization["id"]),
        user_id=str(test_user["id"]),
        created_by=str(test_user["id"]),
        filename="research notes.md",
        content=b"# Private notes\n",
    )

    owned = await service.read_owned(
        str(created["id"]), str(test_organization["id"]), str(test_user["id"])
    )
    assert owned is not None
    assert owned[1] == b"# Private notes\n"
    assert created["storage_key"] not in created["content_url"]

    denied = await service.read_owned(
        str(created["id"]), str(test_organization["id"]), str(other_user["id"])
    )
    assert denied is None
    other_files = await service.repository.list_owned(
        str(test_organization["id"]), str(other_user["id"])
    )
    assert other_files["items"] == []


@pytest.mark.asyncio
async def test_task_file_becomes_request_output(db_pool, test_user, tmp_path):
    request_repo = RequestRepository(db_pool)
    request = await request_repo.create_request(
        title=f"File output {uuid4()}",
        description="Create a durable report",
        source="user",
        created_by=str(test_user["id"]),
        org_id=str(test_user["organization_id"]),
    )
    task = await request_repo.create_task(
        request_id=str(request["id"]),
        title="Write report",
        org_id=str(test_user["organization_id"]),
        requesting_user_id=str(test_user["id"]),
    )

    created = await _service(db_pool, tmp_path).create(
        org_id=str(test_user["organization_id"]),
        user_id=str(test_user["id"]),
        created_by=str(test_user["id"]),
        task_id=str(task["id"]),
        filename="report.md",
        display_name="Final report",
        content=b"# Final report\n",
        is_primary=True,
    )

    detail = await request_repo.get_request_with_tasks(
        str(request["id"]), str(test_user["organization_id"])
    )
    assert detail is not None
    assert detail["outputs"][0]["output_type"] == "file"
    assert detail["outputs"][0]["url"] == created["url"]
    assert detail["outputs"][0]["metadata"]["user_file_id"] == str(created["id"])
    revisions = await _service(db_pool, tmp_path).repository.list_revisions_owned(
        str(created["id"]),
        str(test_user["organization_id"]),
        str(test_user["id"]),
    )
    assert revisions[0]["task_id"] == task["id"]
    assert revisions[0]["context_url"] == f"/activity/{request['id']}#task-{task['id']}"


@pytest.mark.asyncio
async def test_file_revisions_keep_content_and_chat_provenance(
    db_pool, test_user, test_organization, clean_test_data, tmp_path
):
    sessions = LLMSessionRepository(db_pool)
    origin = await sessions.create_session(
        org_id=str(test_user["organization_id"]),
        user_id=str(test_user["id"]),
        kind="chat",
        title="Draft the report",
    )
    editor = await sessions.create_session(
        org_id=str(test_user["organization_id"]),
        user_id=str(test_user["id"]),
        kind="chat",
        title="Revise the report",
    )
    service = _service(db_pool, tmp_path)
    created = await service.create(
        org_id=str(test_user["organization_id"]),
        user_id=str(test_user["id"]),
        created_by=str(test_user["id"]),
        session_id=str(origin["id"]),
        filename="tracked.md",
        content=b"# First draft\n",
    )
    updated = await service.update(
        file_id=str(created["id"]),
        org_id=str(test_user["organization_id"]),
        user_id=str(test_user["id"]),
        edited_by=str(test_user["id"]),
        session_id=str(editor["id"]),
        content=b"# Revised draft\n",
        change_summary="Clarified the conclusion",
    )

    revisions = await service.repository.list_revisions_owned(
        str(created["id"]),
        str(test_user["organization_id"]),
        str(test_user["id"]),
    )
    assert updated["current_revision"] == 2
    assert [revision["revision_number"] for revision in revisions] == [2, 1]
    assert revisions[0]["session_title"] == "Revise the report"
    assert revisions[0]["context_url"] == f"/chat/{editor['id']}"
    assert revisions[1]["session_title"] == "Draft the report"
    assert revisions[1]["context_url"] == f"/chat/{origin['id']}"

    first = await service.read_revision_owned(
        str(created["id"]), 1, str(test_user["organization_id"]), str(test_user["id"])
    )
    second = await service.read_revision_owned(
        str(created["id"]), 2, str(test_user["organization_id"]), str(test_user["id"])
    )
    assert first and first[1] == b"# First draft\n"
    assert second and second[1] == b"# Revised draft\n"

    other_user = await UserRepository(db_pool).create(
        external_id=f"{clean_test_data}revision-other-user",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{clean_test_data}revision-other@test.com",
        display_name="Other revision user",
    )
    assert await service.read_revision_owned(
        str(created["id"]),
        1,
        str(test_organization["id"]),
        str(other_user["id"]),
    ) is None


@pytest.mark.asyncio
async def test_unseen_file_count_tracks_only_the_owner_latest_revision(
    db_pool, test_user, test_organization, clean_test_data, tmp_path
):
    service = _service(db_pool, tmp_path)
    other_user = await UserRepository(db_pool).create(
        external_id=f"{clean_test_data}file-view-other-user",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{clean_test_data}file-view-other@test.com",
        display_name="Other file viewer",
    )
    created = await service.create(
        org_id=str(test_organization["id"]),
        user_id=str(test_user["id"]),
        created_by=str(test_user["id"]),
        filename="unseen.md",
        content=b"# First version\n",
    )

    repository = service.repository
    assert await repository.count_unseen_current_revisions(
        str(test_organization["id"]), str(test_user["id"])
    ) == 1
    assert await repository.count_unseen_current_revisions(
        str(test_organization["id"]), str(other_user["id"])
    ) == 0
    assert await repository.mark_current_revision_viewed_owned(
        str(created["id"]), str(test_organization["id"]), str(other_user["id"])
    ) is False
    assert await repository.mark_current_revision_viewed_owned(
        str(created["id"]), str(test_organization["id"]), str(test_user["id"])
    ) is True
    assert await repository.count_unseen_current_revisions(
        str(test_organization["id"]), str(test_user["id"])
    ) == 0

    await service.update(
        file_id=str(created["id"]),
        org_id=str(test_organization["id"]),
        user_id=str(test_user["id"]),
        edited_by=str(test_user["id"]),
        content=b"# Second version\n",
    )

    listed = await repository.list_owned(
        str(test_organization["id"]), str(test_user["id"])
    )
    assert listed["unseen_count"] == 1
    assert listed["items"][0]["has_unseen_revision"] is True


@pytest.mark.asyncio
async def test_local_provider_rejects_parent_traversal(tmp_path):
    provider = LocalFileStorageProvider(tmp_path)

    with pytest.raises(ValueError, match="Invalid storage key"):
        await provider.put("../outside", b"blocked")


@pytest.mark.asyncio
async def test_file_api_returns_404_to_another_user(
    db_pool,
    test_user,
    test_organization,
    clean_test_data,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LUCENT_FILE_STORAGE_PATH", str(tmp_path))
    other_user = await UserRepository(db_pool).create(
        external_id=f"{clean_test_data}api-file-other-user",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{clean_test_data}api-file-other@test.com",
        display_name="Other API file user",
    )
    app = create_app()

    def current(user):
        return CurrentUser(
            id=user["id"],
            organization_id=user["organization_id"],
            role="member",
            email=user.get("email"),
            display_name=user.get("display_name"),
            auth_method="api_key",
        )

    app.dependency_overrides[get_current_user] = lambda: current(test_user)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/files",
            json={"filename": "api.md", "content": "# API file\n"},
        )
        assert response.status_code == 200
        file_id = response.json()["id"]

        content = await client.get(f"/api/files/{file_id}/content")
        assert content.status_code == 200
        assert content.text == "# API file\n"
        assert content.headers["content-security-policy"] == "sandbox; default-src 'none'"

        update = await client.patch(
            f"/api/files/{file_id}",
            json={
                "content": "# Updated API file\n",
                "change_summary": "Updated through the API",
            },
        )
        assert update.status_code == 200
        assert update.json()["current_revision"] == 2
        history = await client.get(f"/api/files/{file_id}/revisions")
        assert history.status_code == 200
        assert [item["revision_number"] for item in history.json()["items"]] == [2, 1]
        original = await client.get(f"/api/files/{file_id}/revisions/1/content")
        assert original.status_code == 200
        assert original.content == b"# API file\n"

        app.dependency_overrides[get_current_user] = lambda: current(other_user)
        assert (await client.get(f"/api/files/{file_id}")).status_code == 404
        assert (await client.get(f"/api/files/{file_id}/content")).status_code == 404
        assert (await client.get(f"/api/files/{file_id}/revisions")).status_code == 404
        assert (
            await client.get(f"/api/files/{file_id}/revisions/1/content")
        ).status_code == 404
        listed = await client.get("/api/files")
        assert listed.status_code == 200
        assert listed.json()["items"] == []

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_file_mcp_tools_store_edit_and_recall(
    db_pool, test_user, tmp_path, monkeypatch
):
    monkeypatch.setenv("LUCENT_FILE_STORAGE_PATH", str(tmp_path))
    mcp = FastMCP("test-user-files")
    register_request_tools(mcp)
    session = await LLMSessionRepository(db_pool).create_session(
        org_id=str(test_user["organization_id"]),
        user_id=str(test_user["id"]),
        kind="chat",
        title="MCP file conversation",
    )
    set_current_user(
        {
            "id": test_user["id"],
            "organization_id": test_user["organization_id"],
            "role": "member",
        }
    )
    set_llm_context(session_id=str(session["id"]))
    try:
        created = await _call(
            mcp,
            "store_user_file",
            {"filename": "mcp.md", "content": "# MCP file\n"},
        )
        edited = await _call(
            mcp,
            "edit_user_file",
            {
                "file_id": created["id"],
                "content": "# Revised MCP file\n",
                "change_summary": "Expanded the report",
            },
        )
        listed = await _call(mcp, "list_user_files")
        recalled = await _call(mcp, "read_user_file", {"file_id": created["id"]})
    finally:
        clear_llm_context()
        set_current_user(None)

    assert listed["total_count"] == 1
    assert listed["items"][0]["id"] == created["id"]
    assert edited["current_revision"] == 2
    assert recalled["content"] == "# Revised MCP file\n"
    assert len(recalled["file"]["revisions"]) == 2
    assert recalled["file"]["revisions"][0]["session_id"] == str(session["id"])
