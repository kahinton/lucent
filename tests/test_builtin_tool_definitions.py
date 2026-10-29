"""Built-in managed tools: catalog sync, network policy, and agent binding.

Covers:
- ``sync_built_in_managed_tools`` materializes one active built-in row per
  catalog handler, with the catalog's exact schema and handler config
- a second sync is idempotent (no duplicates, same row count)
- network policy is derived per handler (none / allowlist / bridge)
- an instance-scoped tool that already owns the name wins; that handler is
  skipped instead of aborting the sync
- the ``lucent`` agent receives the tools declared in its AGENT.md frontmatter
- a revoked *declared* grant is re-added on the next sync, because frontmatter
  is the declarative binding (identical to how ``skill_names`` behaves); grants
  that frontmatter does not declare are never removed
"""

import json
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from lucent.db.definitions import DefinitionRepository
from lucent.services.builtin_tool_catalog import BUILTIN_TOOL_CATALOG

GITHUB_DIR = Path(__file__).resolve().parents[1] / ".github"
LUCENT_AGENT_FILE = GITHUB_DIR / "agents" / "definitions" / "lucent" / "AGENT.md"


@pytest_asyncio.fixture
async def builtin_prefix(db_pool):
    """Unique prefix plus cleanup for every row this suite creates."""
    prefix = f"test_btools_{str(uuid4())[:8]}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_managed_tools WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM agent_skills WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM agent_definitions WHERE name LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM skill_definitions WHERE name LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM managed_tool_runs WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM managed_tool_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%"
        )


@pytest_asyncio.fixture
async def builtin_org(db_pool, builtin_prefix):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO organizations (name) VALUES ($1) RETURNING id",
            f"{builtin_prefix}org",
        )
    org_id = str(row["id"])
    try:
        yield org_id
    finally:
        # Definition tables reference organizations restrictively, so they must
        # be cleared explicitly — the org itself is last.
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM agent_managed_tools WHERE agent_id IN (
                    SELECT id FROM agent_definitions WHERE organization_id = $1
                )
                """,
                org_id,
            )
            await conn.execute(
                """
                DELETE FROM agent_skills WHERE agent_id IN (
                    SELECT id FROM agent_definitions WHERE organization_id = $1
                )
                """,
                org_id,
            )
            await conn.execute(
                "DELETE FROM agent_definitions WHERE organization_id = $1", org_id
            )
            await conn.execute(
                "DELETE FROM skill_definitions WHERE organization_id = $1", org_id
            )
            await conn.execute(
                "DELETE FROM managed_tool_runs WHERE organization_id = $1", org_id
            )
            await conn.execute(
                "DELETE FROM managed_tool_definitions WHERE organization_id = $1",
                org_id,
            )
            await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)


@pytest_asyncio.fixture
async def builtin_repo(db_pool):
    return DefinitionRepository(db_pool)


async def _rows(db_pool, org_id):
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM managed_tool_definitions "
            "WHERE organization_id = $1 ORDER BY name",
            org_id,
        )


@pytest.mark.asyncio
async def test_creates_one_active_builtin_row_per_catalog_handler(
    db_pool, builtin_repo, builtin_org
):
    synced = await builtin_repo.sync_built_in_managed_tools(builtin_org)

    assert synced == len(BUILTIN_TOOL_CATALOG)
    rows = await _rows(db_pool, builtin_org)
    assert {row["name"] for row in rows} == set(BUILTIN_TOOL_CATALOG)
    assert all(row["status"] == "active" for row in rows)
    assert all(row["scope"] == "built-in" for row in rows)
    assert all(row["runtime_type"] == "builtin" for row in rows)


@pytest.mark.asyncio
async def test_second_sync_changes_nothing(db_pool, builtin_repo, builtin_org):
    first = await builtin_repo.sync_built_in_managed_tools(builtin_org)
    before = await _rows(db_pool, builtin_org)

    second = await builtin_repo.sync_built_in_managed_tools(builtin_org)
    after = await _rows(db_pool, builtin_org)

    assert first == second == len(BUILTIN_TOOL_CATALOG)
    assert len(before) == len(after) == len(BUILTIN_TOOL_CATALOG)
    assert [row["id"] for row in before] == [row["id"] for row in after]
    names = [row["name"] for row in after]
    assert len(names) == len(set(names))


@pytest.mark.asyncio
async def test_row_shape_matches_catalog(db_pool, builtin_repo, builtin_org):
    await builtin_repo.sync_built_in_managed_tools(builtin_org)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM managed_tool_definitions WHERE organization_id = $1",
            builtin_org,
        )

    for row in rows:
        spec = BUILTIN_TOOL_CATALOG[row["name"]]
        stored_schema = row["input_schema"]
        if isinstance(stored_schema, str):
            stored_schema = json.loads(stored_schema)
        stored_config = row["runtime_config"]
        if isinstance(stored_config, str):
            stored_config = json.loads(stored_config)

        assert stored_schema, f"{row['name']} has an empty input_schema"
        assert stored_schema == spec.input_schema
        assert stored_config == {"handler": spec.name}
        assert row["source_code"] == ""
        assert row["entrypoint"] == spec.name
        assert row["timeout_seconds"] == spec.default_config.get("timeout_seconds", 60)


@pytest.mark.asyncio
async def test_network_policy_is_derived_per_handler(db_pool, builtin_repo, builtin_org):
    await builtin_repo.sync_built_in_managed_tools(builtin_org)

    async with db_pool.acquire() as conn:
        rows = {
            row["name"]: row for row in await conn.fetch(
                "SELECT name, network_policy FROM managed_tool_definitions "
                "WHERE organization_id = $1",
                builtin_org,
            )
        }

    for name, row in rows.items():
        policy = row["network_policy"]
        if isinstance(policy, str):
            policy = json.loads(policy)
        spec = BUILTIN_TOOL_CATALOG[name]
        if spec.network_hosts:
            assert policy["network_mode"] == "allowlist", name
            assert policy["allowed_hosts"] == list(spec.network_hosts), name
        elif spec.uses_network:
            assert policy["network_mode"] == "bridge", name
            assert policy["allowed_hosts"] == [], name
        else:
            assert policy["network_mode"] == "none", name
            assert policy["allowed_hosts"] == [], name

    # Pin the three distinct cases the catalog actually exercises.
    search_policy = rows["web_search"]["network_policy"]
    if isinstance(search_policy, str):
        search_policy = json.loads(search_policy)
    assert search_policy == {
        "network_mode": "allowlist",
        "allowed_hosts": ["html.duckduckgo.com"],
    }
    fetch_policy = rows["web_fetch"]["network_policy"]
    if isinstance(fetch_policy, str):
        fetch_policy = json.loads(fetch_policy)
    assert fetch_policy == {"network_mode": "bridge", "allowed_hosts": []}
    # A handler that touches nothing off-box gets no egress at all.
    offline_policy = rows["pdf_extract_text"]["network_policy"]
    if isinstance(offline_policy, str):
        offline_policy = json.loads(offline_policy)
    assert offline_policy == {"network_mode": "none", "allowed_hosts": []}


@pytest.mark.asyncio
async def test_instance_tool_name_collision_skips_only_that_handler(
    db_pool, builtin_repo, builtin_org
):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO managed_tool_definitions
                (name, description, source_code, entrypoint, runtime_type,
                 status, scope, organization_id)
            VALUES ('web_search', 'user-owned search', 'def handler(a):\n    return a\n',
                    'handler', 'python', 'active', 'instance', $1)
            """,
            builtin_org,
        )

    synced = await builtin_repo.sync_built_in_managed_tools(builtin_org)

    assert synced == len(BUILTIN_TOOL_CATALOG) - 1
    rows = await _rows(db_pool, builtin_org)
    assert len(rows) == len(BUILTIN_TOOL_CATALOG)
    collision = [row for row in rows if row["name"] == "web_search"]
    assert len(collision) == 1
    assert collision[0]["scope"] == "instance"
    assert collision[0]["description"] == "user-owned search"
    assert all(
        row["scope"] == "built-in" for row in rows if row["name"] != "web_search"
    )


@pytest.mark.asyncio
async def test_lucent_agent_gets_declared_builtin_tools(
    db_pool, builtin_repo, builtin_org
):
    await builtin_repo.sync_built_in_managed_tools(builtin_org)
    await builtin_repo.sync_built_in_agents(
        builtin_org, str(GITHUB_DIR / "agents" / "definitions")
    )

    async with db_pool.acquire() as conn:
        agent = await conn.fetchrow(
            "SELECT id FROM agent_definitions "
            "WHERE organization_id = $1 AND name = 'lucent'",
            builtin_org,
        )
        granted = await conn.fetch(
            """
            SELECT t.name, t.scope, amt.config_override, amt.granted_by
            FROM agent_managed_tools amt
            JOIN managed_tool_definitions t ON t.id = amt.tool_id
            WHERE amt.agent_id = $1
            """,
            agent["id"],
        )

    granted_names = {row["name"] for row in granted}
    # The on-disk frontmatter is the declared binding; every name it lists that
    # exists in the catalog must be attached.
    declared = _declared_tool_names(LUCENT_AGENT_FILE)
    assert declared, "lucent/AGENT.md should declare tool_names"
    assert declared <= set(BUILTIN_TOOL_CATALOG)
    assert granted_names == declared
    for row in granted:
        assert row["scope"] == "built-in"
        assert row["config_override"] is None
        assert row["granted_by"] is None


@pytest.mark.asyncio
async def test_resync_readds_revoked_declared_grant_but_keeps_admin_grants(
    db_pool, builtin_repo, builtin_org
):
    """Frontmatter is authoritative for declared tools, exactly like skills.

    Chosen behaviour: a *declared* tool grant that is removed by hand comes back
    on the next sync, because the file is the runtime binding and a restart must
    reproduce it. Grants the frontmatter does not declare are left alone, so a
    tool an admin attached in the UI is never stripped by a restart.
    """
    await builtin_repo.sync_built_in_managed_tools(builtin_org)
    await builtin_repo.sync_built_in_agents(
        builtin_org, str(GITHUB_DIR / "agents" / "definitions")
    )

    async with db_pool.acquire() as conn:
        agent = await conn.fetchrow(
            "SELECT id FROM agent_definitions "
            "WHERE organization_id = $1 AND name = 'lucent'",
            builtin_org,
        )
        agent_id = agent["id"]

        # A tool the frontmatter does not declare, attached by an admin.
        extra_tool = await conn.fetchrow(
            "SELECT id FROM managed_tool_definitions "
            "WHERE organization_id = $1 AND name = 'pdf_extract_text'",
            builtin_org,
        )
        await conn.execute(
            "INSERT INTO agent_managed_tools (agent_id, tool_id, granted_by) "
            "VALUES ($1, $2, NULL) ON CONFLICT DO NOTHING",
            agent_id,
            extra_tool["id"],
        )
        # Deliberately revoke one declared tool.
        await conn.execute(
            """
            DELETE FROM agent_managed_tools
            WHERE agent_id = $1 AND tool_id IN (
                SELECT id FROM managed_tool_definitions
                WHERE organization_id = $2 AND name = 'web_search'
            )
            """,
            agent_id,
            builtin_org,
        )

    await builtin_repo.sync_built_in_managed_tools(builtin_org)
    await builtin_repo.sync_built_in_agents(
        builtin_org, str(GITHUB_DIR / "agents" / "definitions")
    )

    async with db_pool.acquire() as conn:
        names = {
            row["name"]
            for row in await conn.fetch(
                """
                SELECT t.name FROM agent_managed_tools amt
                JOIN managed_tool_definitions t ON t.id = amt.tool_id
                WHERE amt.agent_id = $1
                """,
                agent_id,
            )
        }

    assert "web_search" in names, "declared grants are rebound on every sync"
    assert "pdf_extract_text" in names, "undeclared admin grants must survive"


def _declared_tool_names(agent_file: Path) -> set[str]:
    """Read ``tool_names`` from an AGENT.md file, using the same shape as sync."""
    import re

    raw = agent_file.read_text()
    match = re.match(r"^---\n(.*?)\n---", raw, re.DOTALL)
    assert match, f"{agent_file} has no frontmatter"
    names: list[str] = []
    in_list = False
    for line in match.group(1).splitlines():
        if line.strip() == "tool_names:":
            in_list = True
            continue
        if not in_list:
            continue
        entry = re.match(r"\s+-\s+(.+)", line)
        if entry:
            names.append(entry.group(1).strip())
        elif line.strip():
            in_list = False
    return set(names)


@pytest.mark.asyncio
async def test_removed_handler_is_archived_not_left_active(
    db_pool, builtin_repo, builtin_org
):
    """A handler deleted from the catalog must not survive as a callable tool.

    Removing an entry used to leave its row `active`, granted, and resolvable by
    `run_managed_tool` — which then failed at call time with "no built-in tool
    named ...". The sync has to retire it.
    """
    from lucent.services.builtin_tool_catalog import BUILTIN_TOOL_CATALOG

    await builtin_repo.sync_built_in_managed_tools(builtin_org)

    # Simulate a handler that existed on a previous release and has since been
    # cut by inserting a built-in row for a name the catalog no longer carries.
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO managed_tool_definitions
                (name, description, input_schema, runtime_type, source_code,
                 entrypoint, requirements, runtime_config, env_vars, auth_policy,
                 network_policy, resource_limits, timeout_seconds, status, scope,
                 organization_id)
            VALUES ('retired_handler_probe', 'retired', '{"type":"object"}'::jsonb,
                    'builtin', '', 'retired_handler_probe', '[]'::jsonb,
                    '{"handler":"retired_handler_probe"}'::jsonb, '{}'::jsonb,
                    '{"mode":"agent_grant","require_user_access":true}'::jsonb,
                    '{"network_mode":"none","allowed_hosts":[]}'::jsonb,
                    '{"memory_limit":"512m"}'::jsonb, 60, 'active', 'built-in', $1)
            """,
            builtin_org,
        )

    await builtin_repo.sync_built_in_managed_tools(builtin_org)

    async with db_pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM managed_tool_definitions "
            "WHERE name = 'retired_handler_probe' AND organization_id = $1",
            builtin_org,
        )
    assert status == "archived"

    # Everything still in the catalog stays active and untouched.
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, status FROM managed_tool_definitions "
            "WHERE organization_id = $1 AND scope = 'built-in'",
            builtin_org,
        )
    by_name = {row["name"]: row["status"] for row in rows}
    for name in BUILTIN_TOOL_CATALOG:
        assert by_name.get(name) == "active", name
