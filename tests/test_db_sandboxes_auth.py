"""Sandbox repositories' clearance-mode usage reads and policy wiring.

Sandbox templates follow the definition pattern (use = clearance, management
= admin/owner org-scoped); sandbox instances are "only yours" (Kyle,
2026-10-01): members see and control only sandboxes created for them or
granted to their group. These tests pin that split without a database.
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from lucent.db.pool import (
    AuthorizedDatabasePool,
    AuthorizedQueryError,
    AuthPrincipal,
    AuthTablePolicy,
)
from lucent.db.sandbox import SANDBOX_AUTH_POLICY, SandboxRepository
from lucent.db.sandbox_template import (
    SANDBOX_TEMPLATE_AUTH_POLICY,
    SandboxTemplateRepository,
    get_authorized_sandboxes_pool,
    get_authorized_templates_pool,
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


def make_authorized_pool(connection, policies):
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    return AuthorizedDatabasePool(FakePool(connection), principal, policies)


MAKE_USER = {"id": str(uuid4()), "organization_id": str(uuid4())}

SANDBOX_POLICIES = (SANDBOX_TEMPLATE_AUTH_POLICY, SANDBOX_AUTH_POLICY)


class Row:
    def __init__(self, mapping):
        self._data = dict(mapping)

    def __getitem__(self, key):
        return self._data[key]

    def keys(self):
        return self._data.keys()

    def __iter__(self):
        return iter(self._data)


async def test_sandbox_policies_grant_nothing_by_default():
    """Both sandbox usage policies have no direct columns: clearances decide."""
    assert SANDBOX_TEMPLATE_AUTH_POLICY.table == "sandbox_templates"
    assert SANDBOX_TEMPLATE_AUTH_POLICY.direct_columns == ()
    assert SANDBOX_AUTH_POLICY.table == "sandboxes"
    assert SANDBOX_AUTH_POLICY.direct_columns == ()


async def test_get_authorized_sandboxes_pool_carries_both_policies():
    fake_pool = FakePool(FakeConnection(rows=[]))
    with patch(
        "lucent.db.pool.get_pool",
        new=AsyncMock(return_value=fake_pool),
    ):
        pool = await get_authorized_sandboxes_pool(fake_pool, MAKE_USER)
    assert str(pool.principal.user_id) == MAKE_USER["id"]
    assert str(pool.principal.organization_id) == MAKE_USER["organization_id"]
    assert SANDBOX_TEMPLATE_AUTH_POLICY in pool.table_policies
    assert SANDBOX_AUTH_POLICY in pool.table_policies


async def test_get_authorized_templates_pool_only_carries_template_policy():
    fake_pool = FakePool(FakeConnection(rows=[]))
    pool = await get_authorized_templates_pool(fake_pool, MAKE_USER)
    assert SANDBOX_TEMPLATE_AUTH_POLICY in pool.table_policies
    assert SANDBOX_AUTH_POLICY not in pool.table_policies


async def test_list_templates_accessible_by_is_clearance_only():
    """Authorized template listing leans on the clearance subquery."""

    def _template(name):
        return Row(
            {
                "id": uuid4(),
                "name": name,
                "description": "test",
                "status": "approved",
                "scope": "built-in",
            }
        )

    connection = FakeConnection(rows=[_template("base-env")], count_total=1)
    pool = make_authorized_pool(connection, SANDBOX_POLICIES)
    repo = SandboxTemplateRepository(pool)
    result = await repo.list_templates_accessible_by(
        MAKE_USER["id"],
        MAKE_USER["organization_id"],
        status="approved",
    )

    count_query, page_query = connection.queries
    # Both queries rewrite the sandbox_templates reference through the
    # clearance probe.
    assert "FROM (SELECT resource.*" in count_query
    assert "FROM (SELECT resource.*" in page_query
    assert "JOIN auth_clearances AS clearance" in count_query
    assert "JOIN auth_clearances AS clearance" in page_query
    # Org predicate stays as defense in depth; status stays as the caller's
    # lifecycle filter.
    assert "st.organization_id = $1" in count_query
    assert "st.organization_id = $1" in page_query
    assert "st.status = $2" in page_query
    # No legacy role/owner bypass survives the rewrite.
    assert "IN ('admin', 'owner')" not in page_query
    assert "owner_user_id = $2" not in count_query
    assert "LIMIT" in page_query
    assert result["total_count"] == 1
    assert result["items"][0]["name"] == "base-env"


async def test_list_templates_accessible_by_fails_closed_without_authorized_pool():
    """A plain pool never falls back to the unguarded ownership predicate."""
    repo = SandboxTemplateRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.list_templates_accessible_by(
            MAKE_USER["id"],
            MAKE_USER["organization_id"],
        )


async def test_list_sandboxes_accessible_by_is_clearance_only():
    """Authorized sandbox listing leans on the clearance subquery only."""
    connection = FakeConnection(
        rows=[Row({"id": uuid4(), "name": "task-sandbox", "status": "ready"})],
        count_total=1,
    )
    pool = make_authorized_pool(connection, SANDBOX_POLICIES)
    repo = SandboxRepository(pool)
    result = await repo.list_sandboxes_accessible_by(
        MAKE_USER["id"],
        MAKE_USER["organization_id"],
    )

    count_query, page_query = connection.queries
    assert "FROM (SELECT resource.*" in count_query
    assert "FROM (SELECT resource.*" in page_query
    assert "JOIN auth_clearances AS clearance" in count_query
    assert "JOIN auth_clearances AS clearance" in page_query
    # Org predicate stays as defense in depth; lifecycle filter parametrized.
    assert "sb.organization_id = $1" in count_query
    assert "sb.organization_id = $1" in page_query
    assert "sb.status !=" in page_query
    assert any("destroyed" in str(p) for p in connection.parameters)
    assert result["total_count"] == 1
    assert result["items"][0]["name"] == "task-sandbox"


async def test_list_sandboxes_accessible_by_fails_closed_without_authorized_pool():
    repo = SandboxRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.list_sandboxes_accessible_by(
            MAKE_USER["id"],
            MAKE_USER["organization_id"],
        )


async def test_get_usable_template_requires_authorized_clearance():
    """By-ID usage lookup rewrites through the clearance probe."""
    connection = FakeConnection(rows=[])
    pool = make_authorized_pool(connection, SANDBOX_POLICIES)
    repo = SandboxTemplateRepository(pool)
    template = await repo.get_usable_template(str(uuid4()))
    assert template is None  # the fake answers nothing; the shape is what we pin
    query = connection.queries[0]
    assert "FROM (SELECT resource.*" in query
    assert "JOIN auth_clearances AS clearance" in query


async def test_get_usable_template_fails_closed_without_authorized_pool():
    repo = SandboxTemplateRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.get_usable_template(str(uuid4()))


async def test_get_usable_sandbox_requires_authorized_clearance():
    connection = FakeConnection(rows=[])
    pool = make_authorized_pool(connection, SANDBOX_POLICIES)
    repo = SandboxRepository(pool)
    sandbox = await repo.get_usable_sandbox(str(uuid4()))
    assert sandbox is None
    query = connection.queries[0]
    assert "FROM (SELECT resource.*" in query
    assert "JOIN auth_clearances AS clearance" in query


async def test_get_usable_sandbox_fails_closed_without_authorized_pool():
    repo = SandboxRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.get_usable_sandbox(str(uuid4()))


async def test_clearance_path_requires_matching_policy():
    """The clearance path only runs when the pool carries the sandbox policies."""
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    pool = AuthorizedDatabasePool(
        FakePool(FakeConnection()),
        principal,
        (AuthTablePolicy("tasks"),),
    )
    with pytest.raises(AuthorizedQueryError, match="only supports configured tables"):
        await SandboxTemplateRepository(pool).get_usable_template(str(uuid4()))
