"""API endpoint tests for /api/export."""

from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import MemoryRepository, OrganizationRepository, UserRepository


@pytest_asyncio.fixture
async def export_prefix(db_pool):
    test_id = str(uuid4())[:8]
    prefix = f"test_exportapi_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
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
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def export_user(db_pool, export_prefix):
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{export_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{export_prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{export_prefix}user@test.com",
        display_name=f"{export_prefix}User",
    )
    return user


@pytest_asyncio.fixture
async def export_client(export_user):
    app = create_app()
    fake_user = CurrentUser(
        id=export_user["id"],
        organization_id=export_user["organization_id"],
        role=export_user.get("role", "member"),
        email=export_user.get("email"),
        display_name=export_user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake_user

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class TestExportAPI:
    async def test_export_filters_repo_tagged_memories_when_acl_denied(
        self, export_client, export_user, export_prefix, db_pool, monkeypatch
    ):
        repo = MemoryRepository(db_pool)
        await repo.create(
            username=f"{export_prefix}user",
            type="technical",
            content=f"{export_prefix}Private repo export memory",
            tags=["acl-export-api"],
            metadata={"repo": "org/private-repo"},
            user_id=export_user["id"],
            organization_id=export_user["organization_id"],
        )
        await repo.create(
            username=f"{export_prefix}user",
            type="experience",
            content=f"{export_prefix}Visible export memory",
            tags=["acl-export-api"],
            user_id=export_user["id"],
            organization_id=export_user["organization_id"],
        )

        async def _deny_access(self, user_id, repo_full_name):  # pragma: no cover - signature shim
            return False

        monkeypatch.setattr(
            "lucent.integrations.github_repo_access_service.GitHubRepoAccessService.check_access",
            _deny_access,
        )

        resp = await export_client.get("/api/memories/export", params={"tags": ["acl-export-api"]})
        assert resp.status_code == 200

        data = resp.json()
        contents = {m["content"] for m in data["memories"]}
        assert f"{export_prefix}Private repo export memory" not in contents
        assert f"{export_prefix}Visible export memory" in contents
