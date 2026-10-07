"""Per-principal group-set cache + authorized-query rewrite cache.

Pins the 5s TTL contract on `db.pool._resolve_group_ids` (mirroring the
legacy AccessControlRepository cache) and the invalidation path through
`AccessControlService.invalidate_user_groups`, plus the deterministic
SQL-rewrite caches in db/pool.py.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from lucent.access_control import AccessControlService
from lucent.db import pool as pool_module
from lucent.db.pool import (
    AuthorizedQueryError,
    AuthTablePolicy,
    _cached_prepare_query,
    _resolve_group_ids,
    invalidate_user_groups,
)


class _FakePool:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls = 0

    async def fetch(self, query: str, *parameters: object) -> list[dict]:
        self.calls += 1
        return self.rows


@pytest.fixture(autouse=True)
def _clear_group_cache():
    pool_module._group_cache.clear()
    yield
    pool_module._group_cache.clear()


def _row(group_id):
    return {"group_id": group_id}


async def test_resolve_group_ids_serves_cache_within_ttl():
    group_id = uuid4()
    fake = _FakePool([_row(group_id)])
    user_id = uuid4()
    org_id = uuid4()

    first = await _resolve_group_ids(fake, user_id, org_id)
    second = await _resolve_group_ids(fake, user_id, org_id)

    assert fake.calls == 1
    assert first == second == (group_id,)


async def test_resolve_group_ids_keys_are_per_user_per_org():
    group_id = uuid4()
    fake = _FakePool([_row(group_id)])
    user_id = uuid4()

    await _resolve_group_ids(fake, user_id, uuid4())
    await _resolve_group_ids(fake, user_id, uuid4())

    assert fake.calls == 2


async def test_resolve_group_ids_misses_after_ttl_expiry(monkeypatch):
    group_id = uuid4()
    fake = _FakePool([_row(group_id)])
    user_id = uuid4()
    org_id = uuid4()

    monkeypatch.setattr(pool_module, "_GROUP_CACHE_TTL", timedelta(0))
    await _resolve_group_ids(fake, user_id, org_id)
    await _resolve_group_ids(fake, user_id, org_id)

    assert fake.calls == 2


async def test_invalidate_user_groups_drops_every_org_entry():
    group_id = uuid4()
    fake = _FakePool([_row(group_id)])
    user_id = uuid4()

    await _resolve_group_ids(fake, user_id, uuid4())
    await _resolve_group_ids(fake, user_id, uuid4())
    invalidate_user_groups(user_id)
    await _resolve_group_ids(fake, user_id, uuid4())

    assert fake.calls == 3


async def test_access_control_service_invalidation_also_drops_pool_cache():
    fake = _FakePool([_row(uuid4())])
    user_id = uuid4()
    org_id = uuid4()

    await _resolve_group_ids(fake, user_id, org_id)
    AccessControlService.invalidate_user_groups(str(user_id))
    await _resolve_group_ids(fake, user_id, org_id)

    assert fake.calls == 2


def test_cached_authorized_table_sql_is_deterministic():
    policy = AuthTablePolicy("memories", direct_columns=())

    first = pool_module._authorized_table_sql(policy)
    second = pool_module._authorized_table_sql(policy)

    assert first is second
    assert "JOIN auth_clearances AS clearance" in first


def test_cached_authorized_table_sql_varies_by_policy():
    owner_only = AuthTablePolicy("memories", auth_id_column="auth_id", direct_columns=())
    other = AuthTablePolicy("models", direct_columns=())

    assert pool_module._authorized_table_sql(owner_only) != pool_module._authorized_table_sql(other)


def test_cached_prepare_query_reuses_rewrite():
    policies = (AuthTablePolicy("memories", direct_columns=()),)
    query = "SELECT * FROM memories"

    first = _cached_prepare_query(policies, query)
    second = _cached_prepare_query(policies, query)

    assert first is second
    assert "SELECT * FROM (" in first


def test_cached_prepare_query_varies_by_policies():
    query = "SELECT * FROM memories"
    direct = (AuthTablePolicy("memories"),)  # legacy direct-column policy
    clearance = (AuthTablePolicy("memories", direct_columns=()),)

    assert _cached_prepare_query(direct, query) != _cached_prepare_query(clearance, query)


def test_authorized_query_errors_are_not_cached():
    policies = (AuthTablePolicy("memories", direct_columns=()),)
    bad_query = "SELECT * FROM auth_clearances"

    with pytest.raises(AuthorizedQueryError, match="Manual auth-table references"):
        _cached_prepare_query(policies, bad_query)
    with pytest.raises(AuthorizedQueryError, match="Manual auth-table references"):
        _cached_prepare_query(policies, bad_query)
