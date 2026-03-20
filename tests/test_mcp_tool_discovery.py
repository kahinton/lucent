"""Comprehensive tests for MCP tool discovery features."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.auth_providers import CSRF_COOKIE_NAME, CSRF_FIELD_NAME, SESSION_COOKIE_NAME, create_session
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.definitions import DefinitionRepository
from lucent.services import mcp_discovery
from lucent.services.mcp_discovery import MCPDiscoveryError


@pytest_asyncio.fixture
async def mcpd_prefix(db_pool):
    test_id = str(uuid4())[:8]
    prefix = f"test_mcpd_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_mcp_servers WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM agent_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM mcp_server_configs WHERE organization_id IN "
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
async def mcpd_org_user(db_pool, mcpd_prefix):
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{mcpd_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{mcpd_prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{mcpd_prefix}user@test.com",
        display_name=f"{mcpd_prefix}User",
        role="admin",
    )
    return org, user


@pytest_asyncio.fixture
async def mcpd_other_org_user(db_pool, mcpd_prefix):
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{mcpd_prefix}other_org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{mcpd_prefix}other_user",
        provider="local",
        organization_id=org["id"],
        email=f"{mcpd_prefix}other@test.com",
        display_name=f"{mcpd_prefix}Other",
        role="admin",
    )
    return org, user


@pytest_asyncio.fixture
async def mcpd_repo(db_pool):
    return DefinitionRepository(db_pool)


@pytest_asyncio.fixture
async def mcpd_http_server(mcpd_repo, mcpd_org_user):
    org, user = mcpd_org_user
    return await mcpd_repo.create_mcp_server(
        name=f"{uuid4()}-http",
        description="HTTP server",
        server_type="http",
        url="http://localhost:8766/mcp",
        org_id=str(org["id"]),
        created_by=str(user["id"]),
        owner_user_id=str(user["id"]),
    )


@pytest_asyncio.fixture
async def mcpd_stdio_server(mcpd_repo, mcpd_org_user):
    org, user = mcpd_org_user
    return await mcpd_repo.create_mcp_server(
        name=f"{uuid4()}-stdio",
        description="stdio server",
        server_type="stdio",
        url=None,
        command="dummy-mcp",
        args=["--stdio"],
        org_id=str(org["id"]),
        created_by=str(user["id"]),
        owner_user_id=str(user["id"]),
    )


@pytest_asyncio.fixture
async def api_client(mcpd_org_user):
    _org, user = mcpd_org_user
    app = create_app()

    fake_user = CurrentUser(
        id=user["id"],
        organization_id=user["organization_id"],
        role=user.get("role", "admin"),
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override_get_current_user():
        return fake_user

    app.dependency_overrides[get_current_user] = override_get_current_user
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def api_client_other_org(mcpd_other_org_user):
    _org, user = mcpd_other_org_user
    app = create_app()

    fake_user = CurrentUser(
        id=user["id"],
        organization_id=user["organization_id"],
        role=user.get("role", "admin"),
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override_get_current_user():
        return fake_user

    app.dependency_overrides[get_current_user] = override_get_current_user
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def api_no_auth_client():
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def web_client(db_pool, mcpd_org_user):
    user, _ = mcpd_org_user[1], mcpd_org_user[0]
    session_token = await create_session(db_pool, user["id"])
    csrf_token = "test-csrf-mcpd"

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
    data = {CSRF_FIELD_NAME: getattr(client, "_csrf_token", "")}
    if extra:
        data.update(extra)
    return data


SAMPLE_TOOLS = [
    {"name": "search_memories", "description": "Search", "input_schema": {"type": "object"}},
    {"name": "create_memory", "description": "Create", "input_schema": {"type": "object"}},
]


class TestDatabaseLayer:
    async def test_save_discovered_tools_stores_json(self, mcpd_repo, mcpd_http_server, mcpd_org_user):
        org, _ = mcpd_org_user
        saved = await mcpd_repo.save_discovered_tools(
            str(mcpd_http_server["id"]), SAMPLE_TOOLS, str(org["id"])
        )
        assert saved is not None
        cached = await mcpd_repo.get_discovered_tools(str(mcpd_http_server["id"]), str(org["id"]))
        assert cached["discovered_tools"] == SAMPLE_TOOLS

    async def test_get_discovered_tools_returns_timestamp(
        self, mcpd_repo, mcpd_http_server, mcpd_org_user
    ):
        org, _ = mcpd_org_user
        await mcpd_repo.save_discovered_tools(str(mcpd_http_server["id"]), SAMPLE_TOOLS, str(org["id"]))
        cached = await mcpd_repo.get_discovered_tools(str(mcpd_http_server["id"]), str(org["id"]))
        assert cached is not None
        assert isinstance(cached["tools_discovered_at"], datetime)

    async def test_clear_discovered_tools_removes_cache(self, mcpd_repo, mcpd_http_server, mcpd_org_user):
        org, _ = mcpd_org_user
        await mcpd_repo.save_discovered_tools(str(mcpd_http_server["id"]), SAMPLE_TOOLS, str(org["id"]))
        assert await mcpd_repo.clear_discovered_tools(str(mcpd_http_server["id"]), str(org["id"])) is True
        cached = await mcpd_repo.get_discovered_tools(str(mcpd_http_server["id"]), str(org["id"]))
        assert cached["discovered_tools"] is None
        assert cached["tools_discovered_at"] is None

    async def test_org_scoping_is_enforced(self, mcpd_repo, mcpd_http_server, mcpd_org_user, mcpd_other_org_user):
        org, _ = mcpd_org_user
        other_org, _ = mcpd_other_org_user
        await mcpd_repo.save_discovered_tools(str(mcpd_http_server["id"]), SAMPLE_TOOLS, str(org["id"]))
        assert await mcpd_repo.get_discovered_tools(str(mcpd_http_server["id"]), str(other_org["id"])) is None

    async def test_migration_columns_exist_with_null_defaults(self, db_pool, mcpd_http_server):
        async with db_pool.acquire() as conn:
            cols = await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'mcp_server_configs'
                  AND column_name IN ('discovered_tools', 'tools_discovered_at')
                """
            )
            row = await conn.fetchrow(
                """
                SELECT discovered_tools, tools_discovered_at
                FROM mcp_server_configs
                WHERE id = $1
                """,
                mcpd_http_server["id"],
            )
        names = {r["column_name"] for r in cols}
        assert names == {"discovered_tools", "tools_discovered_at"}
        assert row["discovered_tools"] is None
        assert row["tools_discovered_at"] is None


class TestDiscoveryService:
    async def test_http_server_discovery(self, monkeypatch, mcpd_repo, mcpd_http_server, mcpd_org_user, db_pool):
        org, _ = mcpd_org_user
        server = await mcpd_repo.get_mcp_server(str(mcpd_http_server["id"]), str(org["id"]))

        class FakeBridge:
            def __init__(self, *args, **kwargs):
                self._client = types.SimpleNamespace(timeout=None)

            async def discover_tools(self):
                return [
                    {
                        "function": {
                            "name": "search_memories",
                            "description": "Search memories",
                            "parameters": {"type": "object"},
                        }
                    }
                ]

            async def close(self):
                return None

        monkeypatch.setattr(mcp_discovery, "MCPToolBridge", FakeBridge)
        tools = await mcp_discovery.discover_mcp_tools(server, db_pool)
        assert tools[0]["name"] == "search_memories"

    async def test_stdio_server_discovery_mocked_subprocess(
        self, monkeypatch, mcpd_repo, mcpd_stdio_server, mcpd_org_user
    ):
        org, _ = mcpd_org_user
        server = await mcpd_repo.get_mcp_server(str(mcpd_stdio_server["id"]), str(org["id"]))

        fake_mcp = types.ModuleType("mcp")

        class FakeStdioServerParameters:
            def __init__(self, command, args, env):
                self.command = command
                self.args = args
                self.env = env

        class FakeClientSession:
            def __init__(self, *_):
                self.initialized = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def initialize(self):
                self.initialized = True

            async def list_tools(self):
                return types.SimpleNamespace(
                    tools=[
                        types.SimpleNamespace(
                            name="list_pending_tasks",
                            description="List tasks",
                            inputSchema={"type": "object"},
                        )
                    ]
                )

        fake_mcp.StdioServerParameters = FakeStdioServerParameters
        fake_mcp.ClientSession = FakeClientSession

        fake_mcp_client = types.ModuleType("mcp.client")
        fake_stdio = types.ModuleType("mcp.client.stdio")

        class FakeStdioContext:
            async def __aenter__(self):
                return object(), object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        def fake_stdio_client(_params):
            return FakeStdioContext()

        fake_stdio.stdio_client = fake_stdio_client

        monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
        monkeypatch.setitem(sys.modules, "mcp.client", fake_mcp_client)
        monkeypatch.setitem(sys.modules, "mcp.client.stdio", fake_stdio)

        tools = await mcp_discovery._discover_stdio(server)
        assert tools == [
            {
                "name": "list_pending_tasks",
                "description": "List tasks",
                "input_schema": {"type": "object"},
            }
        ]

    async def test_cache_hit_returns_fresh_cached_tools(
        self, monkeypatch, mcpd_repo, mcpd_http_server, mcpd_org_user, db_pool
    ):
        org, _ = mcpd_org_user
        await mcpd_repo.save_discovered_tools(str(mcpd_http_server["id"]), SAMPLE_TOOLS, str(org["id"]))

        async def should_not_discover(*_args, **_kwargs):
            raise AssertionError("discover should not be called on cache hit")

        monkeypatch.setattr(mcp_discovery, "discover_mcp_tools", should_not_discover)
        tools, from_cache = await mcp_discovery.get_tools_cached(
            str(mcpd_http_server["id"]), str(org["id"]), db_pool, max_age_seconds=60
        )
        assert from_cache is True
        assert tools == SAMPLE_TOOLS

    async def test_cache_miss_calls_discovery_when_expired(
        self, monkeypatch, mcpd_repo, mcpd_http_server, mcpd_org_user, db_pool
    ):
        org, _ = mcpd_org_user
        await mcpd_repo.save_discovered_tools(str(mcpd_http_server["id"]), SAMPLE_TOOLS, str(org["id"]))
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE mcp_server_configs SET tools_discovered_at = $1 WHERE id = $2",
                datetime.now(timezone.utc) - timedelta(minutes=5),
                mcpd_http_server["id"],
            )

        async def fake_discover(server_config, _db_pool):
            assert str(server_config["id"]) == str(mcpd_http_server["id"])
            return [{"name": "fresh", "description": "new", "input_schema": {}}]

        monkeypatch.setattr(mcp_discovery, "discover_mcp_tools", fake_discover)
        tools, from_cache = await mcp_discovery.get_tools_cached(
            str(mcpd_http_server["id"]), str(org["id"]), db_pool, max_age_seconds=60
        )
        assert from_cache is False
        assert tools[0]["name"] == "fresh"

    async def test_connection_failure_returns_discovery_error(
        self, monkeypatch, mcpd_repo, mcpd_http_server, mcpd_org_user, db_pool
    ):
        org, _ = mcpd_org_user
        server = await mcpd_repo.get_mcp_server(str(mcpd_http_server["id"]), str(org["id"]))

        class FailingBridge:
            def __init__(self, *args, **kwargs):
                self._client = types.SimpleNamespace(timeout=None)

            async def discover_tools(self):
                raise RuntimeError("connection refused")

            async def close(self):
                return None

        monkeypatch.setattr(mcp_discovery, "MCPToolBridge", FailingBridge)
        with pytest.raises(MCPDiscoveryError, match="connection refused"):
            await mcp_discovery.discover_mcp_tools(server, db_pool)

    async def test_timeout_handling_for_stdio(self, monkeypatch, mcpd_repo, mcpd_stdio_server, mcpd_org_user):
        org, _ = mcpd_org_user
        server = await mcpd_repo.get_mcp_server(str(mcpd_stdio_server["id"]), str(org["id"]))

        fake_mcp = types.ModuleType("mcp")
        fake_mcp.StdioServerParameters = lambda *args, **kwargs: types.SimpleNamespace()

        class FakeClientSession:
            def __init__(self, *_):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def initialize(self):
                return None

            async def list_tools(self):
                return types.SimpleNamespace(tools=[])

        fake_mcp.ClientSession = FakeClientSession

        fake_mcp_client = types.ModuleType("mcp.client")
        fake_stdio = types.ModuleType("mcp.client.stdio")

        class FakeStdioContext:
            async def __aenter__(self):
                return object(), object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        fake_stdio.stdio_client = lambda _params: FakeStdioContext()

        class TimeoutContext:
            async def __aenter__(self):
                raise TimeoutError()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
        monkeypatch.setitem(sys.modules, "mcp.client", fake_mcp_client)
        monkeypatch.setitem(sys.modules, "mcp.client.stdio", fake_stdio)
        monkeypatch.setattr("asyncio.timeout", lambda _seconds: TimeoutContext())

        with pytest.raises(MCPDiscoveryError, match="Connection timed out"):
            await mcp_discovery._discover_stdio(server)


class TestRestApiEndpoint:
    async def test_get_tools_returns_tools(self, api_client, mcpd_http_server, monkeypatch):
        async def fake_get_tools_cached(_server_id, _org_id, _pool, max_age_seconds=60):
            assert max_age_seconds == 60
            return SAMPLE_TOOLS, True

        async def fake_cached(self, _server_id, _org_id):
            return {"discovered_tools": SAMPLE_TOOLS, "tools_discovered_at": datetime.now(timezone.utc)}

        monkeypatch.setattr("lucent.api.routers.definitions.get_tools_cached", fake_get_tools_cached)
        monkeypatch.setattr(
            "lucent.api.routers.definitions.DefinitionRepository.get_discovered_tools",
            fake_cached,
        )

        resp = await api_client.get(f"/api/definitions/mcp-servers/{mcpd_http_server['id']}/tools")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tools"] == SAMPLE_TOOLS
        assert body["error"] is None

    async def test_auth_required_without_session(self, api_no_auth_client, mcpd_http_server):
        resp = await api_no_auth_client.get(f"/api/definitions/mcp-servers/{mcpd_http_server['id']}/tools")
        assert resp.status_code == 401

    async def test_org_scoped_cannot_access_other_org_server(self, api_client_other_org, mcpd_http_server):
        resp = await api_client_other_org.get(
            f"/api/definitions/mcp-servers/{mcpd_http_server['id']}/tools"
        )
        assert resp.status_code == 404

    async def test_error_response_format(self, api_client, mcpd_http_server, monkeypatch):
        async def fail_cached(*_args, **_kwargs):
            raise MCPDiscoveryError("boom")

        monkeypatch.setattr("lucent.api.routers.definitions.get_tools_cached", fail_cached)

        resp = await api_client.get(f"/api/definitions/mcp-servers/{mcpd_http_server['id']}/tools")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tools"] == []
        assert body["error"] == "Connection failed: boom"

    async def test_refresh_parameter_bypasses_cache(self, api_client, mcpd_http_server, monkeypatch):
        called = {"discover": False}

        async def fake_discover(server, _pool):
            called["discover"] = True
            assert str(server["id"]) == str(mcpd_http_server["id"])
            return [{"name": "fresh_tool", "description": "", "input_schema": {}}]

        async def fake_cached(self, _server_id, _org_id):
            return {
                "discovered_tools": [{"name": "fresh_tool", "description": "", "input_schema": {}}],
                "tools_discovered_at": datetime.now(timezone.utc),
            }

        monkeypatch.setattr("lucent.api.routers.definitions.discover_mcp_tools", fake_discover)
        monkeypatch.setattr(
            "lucent.api.routers.definitions.DefinitionRepository.get_discovered_tools",
            fake_cached,
        )

        resp = await api_client.get(f"/api/definitions/mcp-servers/{mcpd_http_server['id']}/tools?refresh=true")
        assert resp.status_code == 200
        body = resp.json()
        assert body["from_cache"] is False
        assert called["discover"] is True


class TestWebRoutesAndUx:
    async def test_discover_tools_ajax_returns_json(self, web_client, mcpd_http_server, monkeypatch):
        async def fake_get_tools_cached(_server_id, _org_id, _pool, max_age_seconds=60):
            assert max_age_seconds == 60
            return SAMPLE_TOOLS, False

        async def fake_cached(self, _server_id, _org_id):
            return {"discovered_tools": SAMPLE_TOOLS, "tools_discovered_at": datetime.now(timezone.utc)}

        monkeypatch.setattr("lucent.services.mcp_discovery.get_tools_cached", fake_get_tools_cached)
        monkeypatch.setattr(
            "lucent.db.definitions.DefinitionRepository.get_discovered_tools",
            fake_cached,
        )

        resp = await web_client.get(f"/definitions/mcp-servers/{mcpd_http_server['id']}/discover-tools")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tools"] == SAMPLE_TOOLS
        assert body["error"] is None

    async def test_mcp_server_detail_page_has_discover_button(self, web_client, mcpd_http_server):
        resp = await web_client.get(f"/definitions/mcp-servers/{mcpd_http_server['id']}")
        assert resp.status_code == 200
        assert "Discover Tools" in resp.text

    async def test_agent_detail_page_renders_discovered_tools(
        self, web_client, mcpd_repo, mcpd_org_user, mcpd_http_server
    ):
        org, user = mcpd_org_user
        agent = await mcpd_repo.create_agent(
            name=f"{uuid4()}-agent",
            description="agent",
            content="# agent",
            org_id=str(org["id"]),
            created_by=str(user["id"]),
            owner_user_id=str(user["id"]),
        )
        await mcpd_repo.approve_mcp_server(str(mcpd_http_server["id"]), str(org["id"]), str(user["id"]))
        await mcpd_repo.grant_mcp_server(str(agent["id"]), str(mcpd_http_server["id"]))
        await mcpd_repo.save_discovered_tools(
            str(mcpd_http_server["id"]),
            [{"name": "tool_discovered", "description": "desc", "input_schema": {}}],
            str(org["id"]),
        )
        assigned = await mcpd_repo.get_agent_mcp_servers(str(agent["id"]))
        assert assigned
        assert assigned[0]["discovered_tools"] is not None

        resp = await web_client.get(f"/definitions/agents/{agent['id']}")
        assert resp.status_code == 200
        assert f"tools-checklist-{mcpd_http_server['id']}" in resp.text

    async def test_tool_grant_form_submission_saves_selected_tools(
        self, web_client, db_pool, mcpd_repo, mcpd_org_user, mcpd_http_server
    ):
        org, user = mcpd_org_user
        agent = await mcpd_repo.create_agent(
            name=f"{uuid4()}-agent-tools",
            description="agent",
            content="# agent",
            org_id=str(org["id"]),
            created_by=str(user["id"]),
            owner_user_id=str(user["id"]),
        )
        await mcpd_repo.grant_mcp_server(str(agent["id"]), str(mcpd_http_server["id"]))

        resp = await web_client.post(
            f"/definitions/agents/{agent['id']}/mcp-tools/{mcpd_http_server['id']}",
            data=_csrf_data(web_client, {"allowed_tools": "search_memories, create_memory"}),
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT allowed_tools FROM agent_mcp_servers WHERE agent_id = $1 AND mcp_server_id = $2",
                agent["id"],
                mcpd_http_server["id"],
            )
        assert row is not None
        assert json.loads(row["allowed_tools"]) == ["search_memories", "create_memory"]

    async def test_empty_selection_saves_null_all_tools(
        self, web_client, db_pool, mcpd_repo, mcpd_org_user, mcpd_http_server
    ):
        org, user = mcpd_org_user
        agent = await mcpd_repo.create_agent(
            name=f"{uuid4()}-agent-empty",
            description="agent",
            content="# agent",
            org_id=str(org["id"]),
            created_by=str(user["id"]),
            owner_user_id=str(user["id"]),
        )
        await mcpd_repo.grant_mcp_server(str(agent["id"]), str(mcpd_http_server["id"]))

        resp = await web_client.post(
            f"/definitions/agents/{agent['id']}/mcp-tools/{mcpd_http_server['id']}",
            data=_csrf_data(web_client, {"allowed_tools": ""}),
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT allowed_tools FROM agent_mcp_servers WHERE agent_id = $1 AND mcp_server_id = $2",
                agent["id"],
                mcpd_http_server["id"],
            )
        assert row is not None
        assert row["allowed_tools"] is None

    async def test_custom_tool_names_are_saved(
        self, web_client, db_pool, mcpd_repo, mcpd_org_user, mcpd_http_server
    ):
        org, user = mcpd_org_user
        agent = await mcpd_repo.create_agent(
            name=f"{uuid4()}-agent-custom",
            description="agent",
            content="# agent",
            org_id=str(org["id"]),
            created_by=str(user["id"]),
            owner_user_id=str(user["id"]),
        )
        await mcpd_repo.grant_mcp_server(str(agent["id"]), str(mcpd_http_server["id"]))

        resp = await web_client.post(
            f"/definitions/agents/{agent['id']}/mcp-tools/{mcpd_http_server['id']}",
            data=_csrf_data(web_client, {"allowed_tools": "search_memories, custom.tool"}),
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT allowed_tools FROM agent_mcp_servers WHERE agent_id = $1 AND mcp_server_id = $2",
                agent["id"],
                mcpd_http_server["id"],
            )
        assert row is not None
        assert json.loads(row["allowed_tools"]) == ["search_memories", "custom.tool"]
