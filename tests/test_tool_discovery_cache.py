"""Tests for MCP tool discovery cache repository methods.

Covers:
- save_discovered_tools stores tools JSON and sets timestamp
- get_discovered_tools returns cached tools and timestamp
- get_discovered_tools returns None for nonexistent server
- clear_discovered_tools nulls out cache
- save_discovered_tools produces audit entry
- org_id scoping prevents cross-org access
"""

from uuid import UUID, uuid4

import pytest_asyncio

from lucent.db import AuditRepository, OrganizationRepository, UserRepository
from lucent.db.audit import DEFINITION_UPDATE
from lucent.db.definitions import DefinitionRepository

SENTINEL_ID = UUID("00000000-0000-0000-0000-000000000000")

SAMPLE_TOOLS = [
    {"name": "read_file", "description": "Read a file", "input_schema_summary": "path: string"},
    {"name": "write_file", "description": "Write a file", "input_schema_summary": "path: string, content: string"},
]


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def td_prefix(db_pool):
    """Unique prefix and cleanup for tool-discovery tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_tooldisco_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM mcp_server_configs WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memory_audit_log WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM users WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM organizations WHERE name LIKE $1",
            f"{prefix}%",
        )


@pytest_asyncio.fixture
async def td_org(db_pool, td_prefix):
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{td_prefix}org")


@pytest_asyncio.fixture
async def td_user(db_pool, td_org, td_prefix):
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{td_prefix}user",
        provider="local",
        organization_id=td_org["id"],
        email=f"{td_prefix}user@test.com",
        display_name=f"{td_prefix}User",
        role="admin",
    )


@pytest_asyncio.fixture
def audit_repo(db_pool):
    return AuditRepository(db_pool)


@pytest_asyncio.fixture
def def_repo(db_pool, audit_repo):
    return DefinitionRepository(db_pool, audit_repo=audit_repo)


@pytest_asyncio.fixture
async def td_mcp(def_repo, td_org, td_user):
    """Create a test MCP server."""
    return await def_repo.create_mcp_server(
        name="ToolDiscoMCP",
        description="MCP for tool discovery tests",
        server_type="http",
        url="http://localhost:9999",
        org_id=str(td_org["id"]),
        created_by=str(td_user["id"]),
        owner_user_id=str(td_user["id"]),
    )


# ── Helpers ──────────────────────────────────────────────────────────────


async def _fetch_audit_entries(db_pool, org_id) -> list[dict]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM memory_audit_log "
            "WHERE organization_id = $1 AND memory_id = $2 "
            "ORDER BY created_at DESC",
            str(org_id),
            str(SENTINEL_ID),
        )
    results = []
    for r in rows:
        d = dict(r)
        for key in ("user_id", "organization_id", "memory_id", "id"):
            if isinstance(d.get(key), UUID):
                d[key] = str(d[key])
        results.append(d)
    return results


# ============================================================================
# Tests
# ============================================================================


class TestSaveDiscoveredTools:
    async def test_saves_tools_and_timestamp(self, def_repo, td_org, td_mcp):
        result = await def_repo.save_discovered_tools(
            str(td_mcp["id"]), SAMPLE_TOOLS, str(td_org["id"]),
        )
        assert result is not None
        assert result["discovered_tools"] is not None
        assert result["tools_discovered_at"] is not None

    async def test_returns_none_for_wrong_org(self, def_repo, td_mcp):
        fake_org = str(uuid4())
        result = await def_repo.save_discovered_tools(
            str(td_mcp["id"]), SAMPLE_TOOLS, fake_org,
        )
        assert result is None

    async def test_returns_none_for_nonexistent_server(self, def_repo, td_org):
        result = await def_repo.save_discovered_tools(
            str(uuid4()), SAMPLE_TOOLS, str(td_org["id"]),
        )
        assert result is None

    async def test_produces_audit_entry(self, db_pool, def_repo, td_org, td_mcp):
        await def_repo.save_discovered_tools(
            str(td_mcp["id"]), SAMPLE_TOOLS, str(td_org["id"]),
        )
        entries = await _fetch_audit_entries(db_pool, td_org["id"])
        update_entries = [
            e for e in entries
            if e["action_type"] == DEFINITION_UPDATE
            and "discovered_tools" in (e.get("context", {}).get("updated_fields", []))
        ]
        assert len(update_entries) >= 1
        entry = update_entries[0]
        assert entry["context"]["tool_count"] == 2
        assert entry["context"]["definition_type"] == "mcp_server"


class TestGetDiscoveredTools:
    async def test_returns_cached_tools(self, def_repo, td_org, td_mcp):
        await def_repo.save_discovered_tools(
            str(td_mcp["id"]), SAMPLE_TOOLS, str(td_org["id"]),
        )
        result = await def_repo.get_discovered_tools(
            str(td_mcp["id"]), str(td_org["id"]),
        )
        assert result is not None
        assert result["discovered_tools"] == SAMPLE_TOOLS
        assert result["tools_discovered_at"] is not None

    async def test_returns_none_tools_when_not_cached(self, def_repo, td_org, td_mcp):
        result = await def_repo.get_discovered_tools(
            str(td_mcp["id"]), str(td_org["id"]),
        )
        assert result is not None
        assert result["discovered_tools"] is None
        assert result["tools_discovered_at"] is None

    async def test_returns_none_for_nonexistent_server(self, def_repo, td_org):
        result = await def_repo.get_discovered_tools(
            str(uuid4()), str(td_org["id"]),
        )
        assert result is None

    async def test_org_scoping(self, def_repo, td_org, td_mcp):
        await def_repo.save_discovered_tools(
            str(td_mcp["id"]), SAMPLE_TOOLS, str(td_org["id"]),
        )
        result = await def_repo.get_discovered_tools(
            str(td_mcp["id"]), str(uuid4()),
        )
        assert result is None


class TestClearDiscoveredTools:
    async def test_clears_cache(self, def_repo, td_org, td_mcp):
        await def_repo.save_discovered_tools(
            str(td_mcp["id"]), SAMPLE_TOOLS, str(td_org["id"]),
        )
        cleared = await def_repo.clear_discovered_tools(
            str(td_mcp["id"]), str(td_org["id"]),
        )
        assert cleared is True

        result = await def_repo.get_discovered_tools(
            str(td_mcp["id"]), str(td_org["id"]),
        )
        assert result["discovered_tools"] is None
        assert result["tools_discovered_at"] is None

    async def test_returns_false_for_nonexistent_server(self, def_repo, td_org):
        cleared = await def_repo.clear_discovered_tools(
            str(uuid4()), str(td_org["id"]),
        )
        assert cleared is False

    async def test_returns_false_for_wrong_org(self, def_repo, td_mcp):
        cleared = await def_repo.clear_discovered_tools(
            str(td_mcp["id"]), str(uuid4()),
        )
        assert cleared is False
