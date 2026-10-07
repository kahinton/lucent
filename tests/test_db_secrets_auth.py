"""SecretRepository clearance-mode listing and policy wiring.

Secrets migrate to auth-ID clearances in stages: listing goes through the
authorized pool (ownership via migration 123, group shares via migration
124's read clearances, plus grants); provider-scoped reads and writes stay
on the scoped pool. These tests pin that split without a database.
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
from lucent.db.secrets import (
    SECRETS_AUTH_POLICY,
    SecretRepository,
    get_authorized_secrets_pool,
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
        return {"total": self.count_total}

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
    return AuthorizedDatabasePool(FakePool(connection), principal, (SECRETS_AUTH_POLICY,))


MAKE_USER = {"id": str(uuid4()), "organization_id": str(uuid4())}


async def test_secrets_policy_is_clearance_only():
    """Secrets never had a principal GUC for owner_user_id — grant-only policy."""
    assert SECRETS_AUTH_POLICY.table == "secrets"
    assert SECRETS_AUTH_POLICY.direct_columns == ()


def test_secret_access_checks_have_no_admin_override():
    """Org admin/owner carry no blanket read/modify on secrets."""
    from lucent.db.access_control import _TABLES_WITHOUT_ADMIN_OVERRIDE

    assert "secrets" in _TABLES_WITHOUT_ADMIN_OVERRIDE


async def test_get_authorized_secrets_pool_carries_policy():
    fake_pool = FakePool(
        FakeConnection(
            rows=[],
            # principal lookup rows are unused here; the fetch path below
            # answers group resolution with nothing.
        ),
    )
    with patch(
        "lucent.db.pool.get_pool",
        new=AsyncMock(return_value=fake_pool),
    ):
        pool = await get_authorized_secrets_pool(fake_pool, MAKE_USER)
    assert str(pool.principal.user_id) == MAKE_USER["id"]
    assert str(pool.principal.organization_id) == MAKE_USER["organization_id"]
    assert pool.table_policies == (SECRETS_AUTH_POLICY,)


async def test_list_scoped_clearance_mode_drops_group_predicates():
    """Authorized listing leans on the clearance subquery, not owner ORs."""

    class Row:
        def __init__(self, mapping):
            self._data = dict(mapping)

        def __getitem__(self, key):
            return self._data[key]

        def keys(self):
            return self._data.keys()

        def __iter__(self):
            return iter(self._data)

    connection = FakeConnection(
        rows=[Row({"key": "api.example", "owner_user_id": MAKE_USER["id"]})],
        count_total=1,
    )
    pool = make_authorized_pool(connection)
    repo = SecretRepository(pool)
    total, items = await repo.list_scoped(
        organization_id=MAKE_USER["organization_id"],
        limit=25,
        offset=0,
    )

    count_query, page_query = connection.queries
    # Both queries rewrite the secrets reference through the clearance probe.
    assert "FROM (SELECT resource.*" in count_query
    assert "FROM (SELECT resource.*" in page_query
    # No owner/group/admin OR predicates survive in the clearance path.
    assert "owner_user_id = $2" not in count_query
    assert "IN ('admin', 'owner')" not in page_query
    # Org predicate stays as defense in depth.
    assert "s.organization_id = $1" in count_query
    assert "s.organization_id = $1" in page_query
    assert "JOIN auth_clearances AS clearance" in count_query
    assert "JOIN auth_clearances AS clearance" in page_query
    # Pagination applies to the rewritten reference.
    assert "LIMIT $2 OFFSET $3" in page_query
    assert total == 1
    assert items[0]["key"] == "api.example"


async def test_list_scoped_fails_closed_without_authorized_pool():
    """A plain pool never falls back to the org-predicated legacy query.

    The legacy branch (including the admin see-all) is retired: only
    auth_clearances decide what a caller lists, for admins included.
    """
    repo = SecretRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.list_scoped(
            organization_id=str(uuid4()),
            limit=25,
            offset=0,
        )


async def test_list_scoped_requires_authorized_connection_policy():
    """The clearance path only runs when the pool carries the secrets policy."""
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    pool = AuthorizedDatabasePool(
        FakePool(FakeConnection()),
        principal,
        (AuthTablePolicy("tasks"),),
    )
    repo = SecretRepository(pool)
    with pytest.raises(AuthorizedQueryError, match="only supports configured tables"):
        await repo.list_scoped(
            organization_id=str(uuid4()),
            limit=25,
            offset=0,
        )
