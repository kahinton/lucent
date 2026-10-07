"""DefinitionRepository clearance-mode usage reads and policy wiring.

Definitions (agents/skills/MCP servers/hooks/managed tools) are org-level
resources; *use* is clearance-driven and default-denied while management
stays on the owner/admin org-scoped path. These tests pin that split
without a database.
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from lucent.db.definitions import (
    AGENT_AUTH_POLICY,
    DEFINITIONS_AUTH_POLICIES,
    DefinitionRepository,
    get_authorized_definitions_pool,
)
from lucent.db.pool import (
    AuthorizedDatabasePool,
    AuthorizedQueryError,
    AuthPrincipal,
    AuthTablePolicy,
)


class FakeConnection:
    def __init__(self, *, rows=(), count_total=0):
        self.rows = rows
        self.count_total = count_total
        self.queries = []
        self.parameters = []
        self.config_calls = []

    async def execute(self, query, *parameters):
        # set_config context binding — kept out of the query log.
        self.config_calls.append((query, parameters))

    async def fetchrow(self, query, *parameters):
        self.queries.append(query)
        self.parameters.append(parameters)
        if "COUNT(*)" in query:
            return {"total": self.count_total}
        return self.rows[0] if self.rows else None

    async def fetch(self, query, *parameters):
        self.queries.append(query)
        self.parameters.append(parameters)
        return self.rows


class FakePool:
    def __init__(self, connection):
        self._connection = connection

    def acquire(self):
        class Context:
            async def __aenter__(self):
                return self.outer._connection

            async def __aexit__(self, *args):
                return False

        Context.outer = self
        return Context()

    async def fetch(self, query, *parameters):
        return []


def make_authorized_pool(connection):
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    return AuthorizedDatabasePool(FakePool(connection), principal, DEFINITIONS_AUTH_POLICIES)


MAKE_USER = {"id": str(uuid4()), "organization_id": str(uuid4())}


class Row:
    def __init__(self, mapping):
        self._data = dict(mapping)

    def __getitem__(self, key):
        return self._data[key]

    def keys(self):
        return self._data.keys()

    def __iter__(self):
        return iter(self._data)


async def test_definitions_policies_grant_nothing_by_default():
    """Every definition usage policy has no direct columns: clearances decide."""
    assert AGENT_AUTH_POLICY.table == "agent_definitions"
    assert AGENT_AUTH_POLICY.direct_columns == ()
    assert len(DEFINITIONS_AUTH_POLICIES) == 5
    for policy in DEFINITIONS_AUTH_POLICIES:
        assert policy.direct_columns == ()


async def test_get_authorized_definitions_pool_carries_all_policies():
    fake_pool = FakePool(FakeConnection(rows=[]))
    with patch(
        "lucent.db.pool.get_pool",
        new=AsyncMock(return_value=fake_pool),
    ):
        pool = await get_authorized_definitions_pool(fake_pool, MAKE_USER)
    assert str(pool.principal.user_id) == MAKE_USER["id"]
    assert str(pool.principal.organization_id) == MAKE_USER["organization_id"]
    assert pool.table_policies == DEFINITIONS_AUTH_POLICIES


async def test_list_agents_accessible_by_is_clearance_only():
    """Authorized listing leans on the clearance subquery, not roles."""

    def _agent(name):
        return Row(
            {
                "id": uuid4(),
                "name": name,
                "description": "test",
                "content": "# agent",
                "status": "active",
                "scope": "instance",
            }
        )

    connection = FakeConnection(rows=[_agent("helper")], count_total=1)
    pool = make_authorized_pool(connection)
    repo = DefinitionRepository(pool)
    result = await repo.list_agents_accessible_by(
        MAKE_USER["id"],
        MAKE_USER["organization_id"],
        requester_role="owner",  # a role grants nothing on the usage path
    )

    count_query, page_query = connection.queries
    # Both queries rewrite the agent_definitions reference through the
    # clearance probe.
    assert "FROM (SELECT resource.*" in count_query
    assert "FROM (SELECT resource.*" in page_query
    assert "JOIN auth_clearances AS clearance" in count_query
    assert "JOIN auth_clearances AS clearance" in page_query
    # Org predicate stays as defense in depth; status stays as the caller's
    # lifecycle filter.
    assert "a.organization_id = $1" in count_query
    assert "a.organization_id = $1" in page_query
    assert "a.status = $2" in page_query
    # No legacy role/owner bypass survives the rewrite.
    assert "IN ('admin', 'owner')" not in page_query
    assert "owner_user_id = $2" not in count_query
    assert "LIMIT" in page_query
    assert result["total_count"] == 1
    assert result["items"][0]["name"] == "helper"


async def test_list_skills_accessible_by_search_hits_page_query():
    connection = FakeConnection(rows=[], count_total=0)
    pool = make_authorized_pool(connection)
    repo = DefinitionRepository(pool)
    await repo.list_skills_accessible_by(
        MAKE_USER["id"],
        MAKE_USER["organization_id"],
        search="triage",
    )
    page_query = connection.queries[1]
    assert "s.name ILIKE" in page_query
    assert "s.description ILIKE" in page_query
    assert "s.content ILIKE" in page_query
    # Search term is parameterized, not interpolated.
    assert any("%triage%" in str(p) for p in connection.parameters)


async def test_list_agents_accessible_by_fails_closed_without_authorized_pool():
    """A plain pool never falls back to the unguarded ownership predicate."""
    repo = DefinitionRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.list_agents_accessible_by(
            MAKE_USER["id"],
            MAKE_USER["organization_id"],
        )


async def test_get_usable_agent_requires_authorized_clearance():
    """By-ID usage lookup rewrites through the clearance probe."""
    connection = FakeConnection(rows=[])
    pool = make_authorized_pool(connection)
    repo = DefinitionRepository(pool)
    agent = await repo.get_usable_agent(str(uuid4()))
    assert agent is None  # the fake answers nothing; the shape is what we pin
    query = connection.queries[0]
    assert "FROM (SELECT resource.*" in query
    assert "JOIN auth_clearances AS clearance" in query


async def test_get_usable_skill_by_name_requires_authorized_clearance():
    connection = FakeConnection(rows=[])
    pool = make_authorized_pool(connection)
    repo = DefinitionRepository(pool)
    skill = await repo.get_usable_skill_by_name("triage", MAKE_USER["organization_id"])
    assert skill is None
    query = connection.queries[0]
    assert "FROM (SELECT resource.*" in query
    assert "organization_id = $1 AND name = $2" in query


async def test_get_usable_agent_fails_closed_without_authorized_pool():
    repo = DefinitionRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.get_usable_agent(str(uuid4()))


async def test_get_usable_agent_requires_authorized_connection_policy():
    """The clearance path only runs when the pool carries the definitions policies."""
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    pool = AuthorizedDatabasePool(
        FakePool(FakeConnection()),
        principal,
        (AuthTablePolicy("tasks"),),
    )
    repo = DefinitionRepository(pool)
    with pytest.raises(AuthorizedQueryError, match="only supports configured tables"):
        await repo.get_usable_agent(str(uuid4()))
