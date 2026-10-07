"""Schedule repository clearance-mode usage reads and policy wiring.

Schedules are "only yours" (plan decision, 2026-10-01): the creator can see
and control their schedules via the owner clearance; admins/owners keep the
org-wide management view on the legacy path; `schedule_runs` stay
parent-gated (reads only through their parent schedule). These tests pin
that split without a database.
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
from lucent.db.schedules import (
    SCHEDULE_AUTH_POLICY,
    ScheduleRepository,
    get_authorized_schedules_pool,
)


class FakeConnection:
    def __init__(self, *, rows=(), count_total=0):
        self.rows = rows
        self.count_total = count_total
        self.queries = []
        self.parameters = []

    async def execute(self, query, *parameters):
        # set_config context binding — kept out of the query log.
        pass

    async def fetchrow(self, query, *parameters):
        self.queries.append(query)
        self.parameters.append(parameters)
        if "count(*)" in query.lower():
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

SCHEDULE_POLICIES = (SCHEDULE_AUTH_POLICY,)


def _schedule(title="nightly-maintenance") -> dict:
    return {
        "id": uuid4(),
        "title": title,
        "status": "active",
        "enabled": True,
        "trigger_type": "schedule",
        "schedule_type": "interval",
        "next_run_at": None,
    }


class Row:
    def __init__(self, mapping):
        self._data = dict(mapping)

    def __getitem__(self, key):
        return self._data[key]

    def keys(self):
        return self._data.keys()

    def __iter__(self):
        return iter(self._data)


async def test_schedule_policy_grants_nothing_by_default():
    """The schedule usage policy has no direct columns: clearances decide."""
    assert SCHEDULE_AUTH_POLICY.table == "schedules"
    assert SCHEDULE_AUTH_POLICY.direct_columns == ()


async def test_get_authorized_schedules_pool_carries_schedule_policy_only():
    """schedule_runs have no policy — reads go through the parent schedule."""
    fake_pool = FakePool(FakeConnection(rows=[]))
    with patch(
        "lucent.db.pool.get_pool",
        new=AsyncMock(return_value=fake_pool),
    ):
        pool = await get_authorized_schedules_pool(fake_pool, MAKE_USER)
    assert str(pool.principal.user_id) == MAKE_USER["id"]
    assert str(pool.principal.organization_id) == MAKE_USER["organization_id"]
    assert pool.table_policies == SCHEDULE_POLICIES


def _schedule(title="nightly-maintenance"):
    return Row(
        {
            "id": uuid4(),
            "title": title,
            "status": "active",
            "enabled": True,
            "trigger_type": "schedule",
            "schedule_type": "interval",
            "next_run_at": None,
        }
    )


async def test_list_schedules_accessible_by_is_clearance_only():
    """Authorized schedule listing leans on the clearance subquery."""
    connection = FakeConnection(rows=[Row(_schedule())], count_total=1)
    pool = make_authorized_pool(connection, SCHEDULE_POLICIES)
    repo = ScheduleRepository(pool)
    result = await repo.list_schedules_accessible_by(
        MAKE_USER["id"],
        MAKE_USER["organization_id"],
        status="active",
    )

    count_query, page_query = connection.queries
    # Both queries rewrite the schedules reference through the clearance probe.
    assert "FROM (SELECT resource.*" in count_query
    assert "FROM (SELECT resource.*" in page_query
    assert "JOIN auth_clearances AS clearance" in count_query
    assert "JOIN auth_clearances AS clearance" in page_query
    # Org predicate stays as defense in depth; filters are the caller's.
    assert "sc.organization_id = $1" in count_query
    assert "sc.organization_id = $1" in page_query
    assert "sc.status = $2" in page_query
    # No legacy role/creator bypass survives the rewrite.
    assert "IN ('admin', 'owner')" not in page_query
    assert "created_by = $2" not in count_query
    assert "LIMIT" in page_query
    assert result["total_count"] == 1
    assert result["items"][0]["title"] == "nightly-maintenance"


async def test_list_schedules_accessible_by_fails_closed_without_authorized_pool():
    """A plain pool never falls back to the unguarded creator predicate."""
    repo = ScheduleRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.list_schedules_accessible_by(
            MAKE_USER["id"],
            MAKE_USER["organization_id"],
        )


async def test_get_usable_schedule_requires_authorized_clearance():
    """By-ID usage lookup rewrites through the clearance probe."""
    connection = FakeConnection(rows=[])
    pool = make_authorized_pool(connection, SCHEDULE_POLICIES)
    repo = ScheduleRepository(pool)
    schedule = await repo.get_usable_schedule(str(uuid4()))
    assert schedule is None  # the fake answers nothing; the shape is what we pin
    query = connection.queries[0]
    assert "FROM (SELECT resource.*" in query
    assert "JOIN auth_clearances AS clearance" in query


async def test_get_usable_schedule_fails_closed_without_authorized_pool():
    repo = ScheduleRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.get_usable_schedule(str(uuid4()))


async def test_get_summary_accessible_by_requires_authorized_clearance():
    """Summary counts over cleared schedules rewrite through the clearance
    probe too — and a plain pool fails closed."""
    repo = ScheduleRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.get_summary_accessible_by(MAKE_USER["organization_id"])

    connection = FakeConnection(rows=[], count_total=3)
    pool = make_authorized_pool(connection, SCHEDULE_POLICIES)
    repo = ScheduleRepository(pool)
    summary = await repo.get_summary_accessible_by(MAKE_USER["organization_id"])
    query = connection.queries[0]
    assert "FROM (SELECT resource.*" in query
    assert "JOIN auth_clearances AS clearance" in query
    assert summary["total"] == 3


async def test_clearance_path_requires_matching_policy():
    """The clearance path only runs when the pool carries the schedule policy."""
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    pool = AuthorizedDatabasePool(
        FakePool(FakeConnection()),
        principal,
        (AuthTablePolicy("tasks"),),
    )
    with pytest.raises(AuthorizedQueryError, match="only supports configured tables"):
        await ScheduleRepository(pool).get_usable_schedule(str(uuid4()))
