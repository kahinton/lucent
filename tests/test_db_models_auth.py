"""ModelRepository clearance-mode usage reads and policy wiring.

Models are org-level resources; *use* is clearance-driven and default-denied
while management stays on the admin-gated org-scoped path. These tests pin
that split without a database.
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from lucent.db.models import (
    MODELS_AUTH_POLICY,
    ModelRepository,
    get_authorized_models_pool,
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
    return AuthorizedDatabasePool(FakePool(connection), principal, (MODELS_AUTH_POLICY,))


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


async def test_models_policy_grants_nothing_by_default():
    """Usage policy has no direct columns: only clearances decide."""
    assert MODELS_AUTH_POLICY.table == "models"
    assert MODELS_AUTH_POLICY.direct_columns == ()


async def test_get_authorized_models_pool_carries_policy():
    fake_pool = FakePool(FakeConnection(rows=[]))
    with patch(
        "lucent.db.pool.get_pool",
        new=AsyncMock(return_value=fake_pool),
    ):
        pool = await get_authorized_models_pool(fake_pool, MAKE_USER)
    assert str(pool.principal.user_id) == MAKE_USER["id"]
    assert str(pool.principal.organization_id) == MAKE_USER["organization_id"]
    assert pool.table_policies == (MODELS_AUTH_POLICY,)


async def test_list_models_accessible_by_is_clearance_only():
    """Authorized listing leans on the clearance subquery, not roles."""

    connection = FakeConnection(
        rows=[
            Row(
                {
                    "id": "anthropic/claude-sonnet-5",
                    "provider": "anthropic",
                    "name": "Claude Sonnet 5",
                    "is_enabled": True,
                }
            )
        ],
        count_total=1,
    )
    pool = make_authorized_pool(connection)
    repo = ModelRepository(pool)
    result = await repo.list_models_accessible_by(
        MAKE_USER["id"],
        MAKE_USER["organization_id"],
        requester_role="owner",  # a role grants nothing on the usage path
    )

    count_query, page_query = connection.queries
    # Both queries rewrite the models reference through the clearance probe.
    assert "FROM (SELECT resource.*" in count_query
    assert "FROM (SELECT resource.*" in page_query
    assert "JOIN auth_clearances AS clearance" in count_query
    assert "JOIN auth_clearances AS clearance" in page_query
    # Org predicate stays as defense in depth; the enabled toggle (the
    # "approved for use" gate) stays in the caller's SQL.
    assert "m.organization_id = $1" in count_query
    assert "m.organization_id = $1" in page_query
    assert "m.is_enabled = true" in count_query
    assert "m.is_enabled = true" in page_query
    # No legacy role/owner bypass survives the rewrite.
    assert "IN ('admin', 'owner')" not in page_query
    assert "owner_user_id = $2" not in count_query
    assert "LIMIT $2 OFFSET $3" in page_query
    assert result["total_count"] == 1
    assert result["items"][0]["id"] == "anthropic/claude-sonnet-5"


async def test_list_models_accessible_by_can_include_disabled():
    """enabled_only=False stays caller-scoped, independent of clearances."""
    connection = FakeConnection(rows=[], count_total=0)
    pool = make_authorized_pool(connection)
    repo = ModelRepository(pool)
    await repo.list_models_accessible_by(
        MAKE_USER["id"],
        MAKE_USER["organization_id"],
        enabled_only=False,
    )
    page_query = connection.queries[1]
    assert "m.organization_id = $1" in page_query
    assert "m.is_enabled = true" not in page_query


async def test_list_models_accessible_by_fails_closed_without_authorized_pool():
    """A plain pool never falls back to an unguarded registry listing."""
    repo = ModelRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.list_models_accessible_by(
            MAKE_USER["id"],
            MAKE_USER["organization_id"],
        )


async def test_get_usable_model_requires_authorized_clearance():
    """By-ID usage lookup rewrites through the clearance probe."""
    connection = FakeConnection(rows=[])
    pool = make_authorized_pool(connection)
    repo = ModelRepository(pool)
    model = await repo.get_usable_model("anthropic/claude-sonnet-5")
    assert model is None  # the fake answers nothing; the shape is what we pin
    query = connection.queries[0]
    assert "FROM (SELECT resource.*" in query
    assert "JOIN auth_clearances AS clearance" in query


async def test_get_usable_model_fails_closed_without_authorized_pool():
    repo = ModelRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.get_usable_model("anthropic/claude-sonnet-5")


async def test_get_usable_model_requires_authorized_connection_policy():
    """The clearance path only runs when the pool carries the models policy."""
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    pool = AuthorizedDatabasePool(
        FakePool(FakeConnection()),
        principal,
        (AuthTablePolicy("tasks"),),
    )
    repo = ModelRepository(pool)
    with pytest.raises(AuthorizedQueryError, match="only supports configured tables"):
        await repo.get_usable_model("anthropic/claude-sonnet-5")
