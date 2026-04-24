"""Tests for the M9 Phase-2 ``include_archived`` search parameter.

Mirrors the env-flag-gated style introduced in slice 1's vitality-boost work
(``test_memory_search_vitality_boost.py``):

- The exclusion is gated behind ``LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED``
  (default off) so the emitted SQL is byte-identical to the pre-M9 baseline
  until an operator opts in.
- When the flag is on, ``include_archived=False`` (the default) appends a
  ``lifecycle_stage NOT IN ('archived', 'forgotten')`` WHERE clause; passing
  ``include_archived=True`` opts back in to seeing those rows.
"""

from __future__ import annotations

import pytest

from lucent.db.memory import MemoryRepository


# ---------------------------------------------------------------------------
# Lightweight pool/conn shims so we can inspect the SQL the repository emits
# without standing up a real Postgres.
# ---------------------------------------------------------------------------


class _CaptureConn:
    def __init__(self) -> None:
        self.search_query: str = ""
        self.count_query: str = ""

    async def fetchrow(self, query: str, *params: object) -> dict[str, int]:
        self.count_query = query
        return {"total": 0}

    async def fetch(self, query: str, *params: object) -> list[dict[str, object]]:
        self.search_query = query
        return []


class _AcquireCM:
    def __init__(self, conn: _CaptureConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _CaptureConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _CapturePool:
    def __init__(self, conn: _CaptureConn) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self._conn)


_LIFECYCLE_FRAGMENT = (
    "(lifecycle_stage IS NULL "
    "OR lifecycle_stage NOT IN ('archived', 'forgotten'))"
)


# ---------------------------------------------------------------------------
# SQL-shape tests (no database)
# ---------------------------------------------------------------------------


async def test_search_default_sql_unchanged_when_flag_off(monkeypatch) -> None:
    """Snapshot regression: with the rollout flag off (the default), passing
    no ``include_archived`` argument must emit SQL byte-identical to the
    pre-M9 baseline — i.e. it must NOT contain the lifecycle WHERE addition.
    """
    monkeypatch.delenv("LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED", raising=False)
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await repo.search(query="hello", limit=3, offset=0)

    assert _LIFECYCLE_FRAGMENT not in conn.search_query
    assert "lifecycle_stage" not in conn.count_query
    # Sanity: the legacy ranking + base predicate are still present.
    assert "ORDER BY sim_score DESC, importance DESC, created_at DESC" in conn.search_query
    assert "deleted_at IS NULL" in conn.search_query


async def test_search_full_default_sql_unchanged_when_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED", raising=False)
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await repo.search_full(query="hello", limit=3, offset=0)

    assert _LIFECYCLE_FRAGMENT not in conn.search_query
    assert "lifecycle_stage" not in conn.count_query


async def test_search_default_excludes_archived_when_flag_enabled(monkeypatch) -> None:
    monkeypatch.setenv("LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED", "true")
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await repo.search(query="hello")

    assert _LIFECYCLE_FRAGMENT in conn.search_query
    # The exclusion must also bind on the COUNT path so total_count is honest.
    assert _LIFECYCLE_FRAGMENT in conn.count_query


async def test_search_full_default_excludes_archived_when_flag_enabled(monkeypatch) -> None:
    monkeypatch.setenv("LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED", "true")
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await repo.search_full(query="hello")

    assert _LIFECYCLE_FRAGMENT in conn.search_query
    assert _LIFECYCLE_FRAGMENT in conn.count_query


async def test_include_archived_true_skips_filter_when_flag_enabled(monkeypatch) -> None:
    monkeypatch.setenv("LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED", "true")
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await repo.search(query="hello", include_archived=True)

    assert _LIFECYCLE_FRAGMENT not in conn.search_query
    assert _LIFECYCLE_FRAGMENT not in conn.count_query


async def test_include_archived_false_is_noop_when_flag_off(monkeypatch) -> None:
    """Even when a caller explicitly passes ``include_archived=False`` we
    must NOT add the WHERE clause while the rollout flag is off — otherwise
    behavior would diverge from the documented baseline."""
    monkeypatch.delenv("LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED", raising=False)
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await repo.search(query="hello", include_archived=False)

    assert _LIFECYCLE_FRAGMENT not in conn.search_query
    assert _LIFECYCLE_FRAGMENT not in conn.count_query


# ---------------------------------------------------------------------------
# Integration tests against the real DB (need ``db_pool``).
# These verify the runtime behavior — that archived rows are actually
# filtered out / included as the parameter dictates.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_search_filters_archived_rows(
    db_pool, test_user, monkeypatch, clean_test_data
) -> None:
    monkeypatch.setenv("LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED", "true")
    prefix = clean_test_data
    repo = MemoryRepository(db_pool)

    active = await repo.create(
        username=f"{prefix}u",
        type="experience",
        content=f"{prefix} archived-test ACTIVE row",
        tags=["m9-archived-test"],
        user_id=test_user["id"],
        organization_id=test_user["organization_id"],
    )
    archived = await repo.create(
        username=f"{prefix}u",
        type="experience",
        content=f"{prefix} archived-test ARCHIVED row",
        tags=["m9-archived-test"],
        user_id=test_user["id"],
        organization_id=test_user["organization_id"],
    )
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET lifecycle_stage = 'archived' WHERE id = $1",
            archived["id"],
        )

    default_result = await repo.search(
        query=f"{prefix} archived-test",
        limit=10,
        requesting_user_id=test_user["id"],
        requesting_org_id=test_user["organization_id"],
    )
    ids_default = {m["id"] for m in default_result["memories"]}
    assert active["id"] in ids_default
    assert archived["id"] not in ids_default

    inclusive_result = await repo.search(
        query=f"{prefix} archived-test",
        limit=10,
        requesting_user_id=test_user["id"],
        requesting_org_id=test_user["organization_id"],
        include_archived=True,
    )
    ids_inclusive = {m["id"] for m in inclusive_result["memories"]}
    assert active["id"] in ids_inclusive
    assert archived["id"] in ids_inclusive
    # And the row carries its lifecycle marker for callers to surface.
    archived_row = next(m for m in inclusive_result["memories"] if m["id"] == archived["id"])
    assert archived_row["lifecycle_stage"] == "archived"
