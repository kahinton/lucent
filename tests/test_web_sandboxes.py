"""Integration tests for sandbox web routes in web/routes.py.

Tests:
- GET  /sandboxes                                (list page)
- POST /sandboxes/templates/create               (create template)
- GET  /sandboxes/templates/{id}/edit             (edit page)
- POST /sandboxes/templates/{id}/edit             (update template)
- POST /sandboxes/templates/{id}/delete           (delete template)
- POST /sandboxes/launch                          (launch sandbox)
- POST /sandboxes/{id}/stop                       (stop sandbox)
- POST /sandboxes/{id}/destroy                    (destroy sandbox)
"""

from uuid import uuid4
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    create_session,
    set_user_password,
)
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.sandbox_template import SandboxTemplateRepository

TEST_PASSWORD = "TestPass1"


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    test_id = str(uuid4())[:8]
    prefix = f"test_websbox_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM sandbox_templates WHERE created_by IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
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
            "DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%"
        )


@pytest_asyncio.fixture
async def web_user(db_pool, web_prefix):
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{web_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{web_prefix}user",
        provider="basic",
        organization_id=org["id"],
        email=f"{web_prefix}user@test.com",
        display_name=f"{web_prefix}User",
    )
    await set_user_password(db_pool, user["id"], TEST_PASSWORD)
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def client(db_pool, web_user):
    _user, _org, session_token = web_user
    csrf_token = "test-csrf-token-set123"
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: session_token, CSRF_COOKIE_NAME: csrf_token},
    ) as c:
        c._csrf_token = csrf_token
        yield c


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    data = {CSRF_FIELD_NAME: client._csrf_token}
    if extra:
        data.update(extra)
    return data


# ---------------------------------------------------------------------------
# GET /sandboxes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandboxes_page_returns_200(client):
    resp = await client.get("/sandboxes", follow_redirects=True)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_sandboxes_instances_tab_returns_200(client):
    mock_manager = AsyncMock()
    mock_manager.list_all.return_value = []
    with patch("lucent.sandbox.manager.get_sandbox_manager", return_value=mock_manager):
        resp = await client.get("/sandboxes?tab=instances", follow_redirects=True)
    assert resp.status_code == 200
    mock_manager.list_all.assert_called_once()


@pytest.mark.asyncio
async def test_sandboxes_instances_tab_active_filter(client):
    mock_manager = AsyncMock()
    mock_manager.list_active.return_value = []
    with patch("lucent.sandbox.manager.get_sandbox_manager", return_value=mock_manager):
        resp = await client.get(
            "/sandboxes?tab=instances&show=active", follow_redirects=True
        )
    assert resp.status_code == 200
    mock_manager.list_active.assert_called_once()


@pytest.mark.asyncio
async def test_sandboxes_unauthenticated_redirects(db_pool):
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/sandboxes", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/login" in resp.headers.get("location", "")


# ---------------------------------------------------------------------------
# POST /sandboxes/templates/create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_template(client):
    resp = await client.post(
        "/sandboxes/templates/create",
        data=_csrf_data(client, {"name": "integration-test-template", "image": "python:3.12-slim"}),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/sandboxes" in resp.headers["location"]


@pytest.mark.asyncio
async def test_create_template_with_all_fields(client):
    resp = await client.post(
        "/sandboxes/templates/create",
        data=_csrf_data(
            client,
            {
                "name": "full-field-template",
                "image": "node:20-slim",
                "description": "All fields test",
                "repo_url": "https://github.com/test/repo",
                "branch": "develop",
                "setup_commands": "npm install\nnpm run build",
                "env_vars": "NODE_ENV=test\nCI=true",
                "memory_limit": "4g",
                "cpu_limit": "4.0",
                "disk_limit": "20g",
                "network_mode": "bridge",
                "timeout_seconds": "3600",
            },
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_create_template_without_csrf_fails(client):
    resp = await client.post(
        "/sandboxes/templates/create",
        data={"name": "no-csrf-template"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /sandboxes/templates/{id}/edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_template_page_returns_200(client, db_pool, web_user, web_prefix):
    user, org, _token = web_user
    repo = SandboxTemplateRepository(db_pool)
    tpl = await repo.create(
        name=f"{web_prefix}template",
        organization_id=str(org["id"]),
        description="test template",
        image="python:3.12-slim",
        created_by=str(user["id"]),
    )
    resp = await client.get(
        f"/sandboxes/templates/{tpl['id']}/edit", follow_redirects=True
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_edit_nonexistent_template_returns_404(client):
    fake_id = str(uuid4())
    resp = await client.get(
        f"/sandboxes/templates/{fake_id}/edit", follow_redirects=True
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /sandboxes/templates/{id}/edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_template(client, db_pool, web_user, web_prefix):
    user, org, _token = web_user
    repo = SandboxTemplateRepository(db_pool)
    tpl = await repo.create(
        name=f"{web_prefix}upd_template",
        organization_id=str(org["id"]),
        description="original",
        image="python:3.12-slim",
        created_by=str(user["id"]),
    )
    resp = await client.post(
        f"/sandboxes/templates/{tpl['id']}/edit",
        data=_csrf_data(client, {"name": f"{web_prefix}upd_template", "description": "updated"}),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/sandboxes" in resp.headers["location"]

    # Verify the update persisted
    updated = await repo.get(str(tpl["id"]))
    assert updated["description"] == "updated"


@pytest.mark.asyncio
async def test_update_template_without_csrf_fails(client, db_pool, web_user, web_prefix):
    user, org, _token = web_user
    repo = SandboxTemplateRepository(db_pool)
    tpl = await repo.create(
        name=f"{web_prefix}csrf_upd_tpl",
        organization_id=str(org["id"]),
        description="original",
        image="python:3.12-slim",
        created_by=str(user["id"]),
    )
    resp = await client.post(
        f"/sandboxes/templates/{tpl['id']}/edit",
        data={"name": "hacked"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /sandboxes/templates/{id}/delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_template(client, db_pool, web_user, web_prefix):
    user, org, _token = web_user
    repo = SandboxTemplateRepository(db_pool)
    tpl = await repo.create(
        name=f"{web_prefix}del_template",
        organization_id=str(org["id"]),
        description="to delete",
        image="python:3.12-slim",
        created_by=str(user["id"]),
    )
    resp = await client.post(
        f"/sandboxes/templates/{tpl['id']}/delete",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/sandboxes" in resp.headers["location"]

    # Verify the template was actually deleted
    deleted = await repo.get(str(tpl["id"]))
    assert deleted is None


@pytest.mark.asyncio
async def test_delete_template_without_csrf_fails(client, db_pool, web_user, web_prefix):
    user, org, _token = web_user
    repo = SandboxTemplateRepository(db_pool)
    tpl = await repo.create(
        name=f"{web_prefix}csrf_del_tpl",
        organization_id=str(org["id"]),
        description="should not delete",
        image="python:3.12-slim",
        created_by=str(user["id"]),
    )
    resp = await client.post(
        f"/sandboxes/templates/{tpl['id']}/delete",
        data={},
        follow_redirects=False,
    )
    assert resp.status_code == 403

    # Verify template still exists
    still_exists = await repo.get(str(tpl["id"]))
    assert still_exists is not None


# ---------------------------------------------------------------------------
# POST /sandboxes/launch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_sandbox(client, db_pool, web_user, web_prefix):
    user, org, _token = web_user
    repo = SandboxTemplateRepository(db_pool)
    tpl = await repo.create(
        name=f"{web_prefix}launch_template",
        organization_id=str(org["id"]),
        description="for launch",
        image="python:3.12-slim",
        created_by=str(user["id"]),
    )
    mock_manager = AsyncMock()
    mock_manager.create.return_value = {"id": str(uuid4()), "status": "running"}
    with patch("lucent.sandbox.manager.get_sandbox_manager", return_value=mock_manager):
        resp = await client.post(
            "/sandboxes/launch",
            data=_csrf_data(client, {"template_id": str(tpl["id"])}),
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "/sandboxes" in resp.headers["location"]
    mock_manager.create.assert_called_once()


@pytest.mark.asyncio
async def test_launch_sandbox_with_custom_name(client, db_pool, web_user, web_prefix):
    user, org, _token = web_user
    repo = SandboxTemplateRepository(db_pool)
    tpl = await repo.create(
        name=f"{web_prefix}named_launch",
        organization_id=str(org["id"]),
        description="for named launch",
        image="python:3.12-slim",
        created_by=str(user["id"]),
    )
    mock_manager = AsyncMock()
    mock_manager.create.return_value = {"id": str(uuid4()), "status": "running"}
    with patch("lucent.sandbox.manager.get_sandbox_manager", return_value=mock_manager):
        resp = await client.post(
            "/sandboxes/launch",
            data=_csrf_data(
                client,
                {"template_id": str(tpl["id"]), "name": "my-custom-sandbox"},
            ),
            follow_redirects=False,
        )
    assert resp.status_code == 303
    # Verify the custom name was passed to the config
    config_arg = mock_manager.create.call_args[0][0]
    assert config_arg.name == "my-custom-sandbox"


@pytest.mark.asyncio
async def test_launch_sandbox_nonexistent_template_returns_404(client):
    fake_id = str(uuid4())
    mock_manager = AsyncMock()
    with patch("lucent.sandbox.manager.get_sandbox_manager", return_value=mock_manager):
        resp = await client.post(
            "/sandboxes/launch",
            data=_csrf_data(client, {"template_id": fake_id}),
            follow_redirects=False,
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_launch_sandbox_without_csrf_fails(client, db_pool, web_user, web_prefix):
    user, org, _token = web_user
    repo = SandboxTemplateRepository(db_pool)
    tpl = await repo.create(
        name=f"{web_prefix}csrf_launch_tpl",
        organization_id=str(org["id"]),
        description="csrf test",
        image="python:3.12-slim",
        created_by=str(user["id"]),
    )
    resp = await client.post(
        "/sandboxes/launch",
        data={"template_id": str(tpl["id"])},
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /sandboxes/{id}/stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_sandbox(client):
    sandbox_id = str(uuid4())
    mock_manager = AsyncMock()
    with patch("lucent.sandbox.manager.get_sandbox_manager", return_value=mock_manager):
        resp = await client.post(
            f"/sandboxes/{sandbox_id}/stop",
            data=_csrf_data(client),
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "/sandboxes" in resp.headers["location"]
    mock_manager.stop.assert_called_once_with(sandbox_id)


@pytest.mark.asyncio
async def test_stop_sandbox_without_csrf_fails(client):
    sandbox_id = str(uuid4())
    mock_manager = AsyncMock()
    with patch("lucent.sandbox.manager.get_sandbox_manager", return_value=mock_manager):
        resp = await client.post(
            f"/sandboxes/{sandbox_id}/stop",
            data={},
            follow_redirects=False,
        )
    assert resp.status_code == 403
    mock_manager.stop.assert_not_called()


# ---------------------------------------------------------------------------
# POST /sandboxes/{id}/destroy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destroy_sandbox(client):
    sandbox_id = str(uuid4())
    mock_manager = AsyncMock()
    with patch("lucent.sandbox.manager.get_sandbox_manager", return_value=mock_manager):
        resp = await client.post(
            f"/sandboxes/{sandbox_id}/destroy",
            data=_csrf_data(client),
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "/sandboxes" in resp.headers["location"]
    mock_manager.destroy.assert_called_once_with(sandbox_id)


@pytest.mark.asyncio
async def test_destroy_sandbox_without_csrf_fails(client):
    sandbox_id = str(uuid4())
    mock_manager = AsyncMock()
    with patch("lucent.sandbox.manager.get_sandbox_manager", return_value=mock_manager):
        resp = await client.post(
            f"/sandboxes/{sandbox_id}/destroy",
            data={},
            follow_redirects=False,
        )
    assert resp.status_code == 403
    mock_manager.destroy.assert_not_called()


# ---------------------------------------------------------------------------
# Unauthenticated access to mutation endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_post_endpoints_redirect(db_pool):
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    fake_id = str(uuid4())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        endpoints = [
            ("/sandboxes/templates/create", {"name": "test", "image": "python:3.12"}),
            (f"/sandboxes/templates/{fake_id}/edit", {"name": "test"}),
            (f"/sandboxes/templates/{fake_id}/delete", {}),
            ("/sandboxes/launch", {"template_id": fake_id}),
            (f"/sandboxes/{fake_id}/stop", {}),
            (f"/sandboxes/{fake_id}/destroy", {}),
        ]
        for path, data in endpoints:
            resp = await c.post(path, data=data, follow_redirects=False)
            assert resp.status_code in (302, 303, 401), (
                f"POST {path} should require auth, got {resp.status_code}"
            )
