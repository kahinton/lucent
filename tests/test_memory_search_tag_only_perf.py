"""Regression tests for Pattern 2 — tag-only ``search_memories`` timeouts.

These tests pin the two fixes shipped together with migration 091:

1. ``MemoryRepository.search`` must emit an explicit ``::text[]`` cast on
   the ``tags @> $N`` predicate so the planner consistently chooses a
   GIN index plan rather than treating ``$N`` as an unknown-type value.
2. When the caller filters by ``tags`` and provides no fuzzy ``query``,
   the emitted SQL must wrap the WHERE in a ``WITH ... AS MATERIALIZED``
   CTE so Postgres evaluates the tag filter first instead of using a
   partial btree index from the ACL clauses and inline-filtering on tags.

A live integration test (skipped unless ``LUCENT_TEST_DATABASE_URL`` is
set) seeds >1000 memories and asserts that
``search(tags=["validated"], limit=10)`` completes well under a sane
wall-clock budget — the same shape of call that produced the recurring
MCP -32001 timeouts in production between 2026-05-30 and 2026-06-11.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import pytest
import pytest_asyncio

from lucent.db.memory import MemoryRepository


# ---------------------------------------------------------------------------
# SQL-shape regression (no Postgres required)
# ---------------------------------------------------------------------------


class _CaptureConn:
    def __init__(self) -> None:
        self.search_query: str = ""
        self.search_params: tuple[Any, ...] = ()
        self.count_query: str = ""
        self.count_params: tuple[Any, ...] = ()

    async def fetchrow(self, query: str, *params: Any) -> dict[str, int]:
        self.count_query = query
        self.count_params = params
        return {"total": 0}

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.search_query = query
        self.search_params = params
        return []


class _AcquireCM:
    def __init__(self, conn: _CaptureConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _CaptureConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _CaptureConn) -> None:
        self._conn = conn

    def acquire(self, timeout: float | None = None) -> _AcquireCM:  # noqa: ARG002
        return _AcquireCM(self._conn)


@pytest.mark.asyncio
async def test_tag_predicate_has_explicit_text_array_cast() -> None:
    """``tags @> $N`` must be emitted with an explicit ``::text[]`` cast.

    Without the cast asyncpg passes the list as an unknown-type parameter
    and the Postgres planner sometimes refuses to use the GIN tag index,
    falling back to a btree+filter scan that produces the MCP timeouts.
    """
    conn = _CaptureConn()
    repo = MemoryRepository(_FakePool(conn))

    await repo.search(tags=["validated"], limit=10)

    assert "tags @>" in conn.search_query
    assert "::text[]" in conn.search_query, (
        "tag filter lost its explicit ::text[] cast — planner may stop "
        "using the GIN index. See migration 091 and Pattern 2 fix."
    )
    # The count query rides on the same WHERE clause and must keep the cast.
    assert "::text[]" in conn.count_query


@pytest.mark.asyncio
async def test_tag_only_search_uses_materialized_cte() -> None:
    """Tag-only searches (no fuzzy text query) must wrap the WHERE in a
    ``WITH ... AS MATERIALIZED`` CTE so the tag filter is evaluated before
    the ORDER BY/LIMIT pass.
    """
    conn = _CaptureConn()
    repo = MemoryRepository(_FakePool(conn))

    await repo.search(tags=["validated"], limit=10)

    sql = " ".join(conn.search_query.split())
    assert "WITH tag_filtered AS MATERIALIZED" in sql, (
        "tag-only fast path lost its MATERIALIZED CTE — planner can now "
        "pick a partial btree ACL index and inline-filter on tags, which "
        "is the regression that caused the MCP search_memories timeouts."
    )
    # The materialized subquery must restrict on tags, and the outer
    # query must still apply the ordering+limit the caller expects.
    assert "tags @> $" in sql
    assert "ORDER BY importance DESC, created_at DESC" in sql
    assert "LIMIT $" in sql


@pytest.mark.asyncio
async def test_query_path_unchanged_when_fuzzy_query_provided() -> None:
    """The MATERIALIZED CTE fast path is gated on ``query is None``. When
    a fuzzy ``query`` is also supplied, the existing similarity-ranked
    plan must remain in use (no CTE, similarity expression intact).
    """
    conn = _CaptureConn()
    repo = MemoryRepository(_FakePool(conn))

    await repo.search(query="alpha", tags=["validated"], limit=10)

    sql = " ".join(conn.search_query.split())
    assert "WITH tag_filtered AS MATERIALIZED" not in sql
    assert "similarity(content" in sql
    assert "tags @> $" in sql and "::text[]" in sql


# ---------------------------------------------------------------------------
# Live wall-clock regression (gated on LUCENT_TEST_DATABASE_URL)
# ---------------------------------------------------------------------------

_LIVE_DB_URL = os.environ.get(
    "LUCENT_TEST_DATABASE_URL",
    "postgresql://lucent:change-me-insecure-dev-password@localhost:5433/lucent",
)
_REQUIRE_LIVE = os.environ.get("LUCENT_REQUIRE_LIVE_DB_TESTS") == "1"


@pytest_asyncio.fixture()
async def _seeded_repo() -> Any:
    """Yield a ``MemoryRepository`` backed by a Postgres pool that has at
    least 1000 ``validated``-tagged memories plus noise rows.

    Cleans up its own data on teardown so the fixture is idempotent.
    """
    asyncpg = pytest.importorskip("asyncpg")
    try:
        pool = await asyncpg.create_pool(_LIVE_DB_URL, min_size=1, max_size=4)
    except Exception as exc:  # pragma: no cover — environment-dependent
        if _REQUIRE_LIVE:
            raise
        pytest.skip(f"live DB unavailable: {exc}")

    marker = f"tagperf-{uuid.uuid4().hex[:8]}"
    try:
        async with pool.acquire() as conn:
            # Seed 1200 validated rows + 1500 noise rows for the tag-only
            # path to have a non-trivial planner choice.
            await conn.execute(
                """
                INSERT INTO memories (username, type, content, tags, importance)
                SELECT $1, 'experience', 'seed ' || g::text,
                       ARRAY['validated', $1]::text[],
                       (g % 10) + 1
                FROM generate_series(1, 1200) g
                """,
                marker,
            )
            await conn.execute(
                """
                INSERT INTO memories (username, type, content, tags, importance)
                SELECT $1, 'experience', 'noise ' || g::text,
                       ARRAY['other', $1]::text[],
                       (g % 10) + 1
                FROM generate_series(1, 1500) g
                """,
                marker,
            )
            await conn.execute("ANALYZE memories")

        repo = MemoryRepository(pool)
        repo._tagperf_marker = marker  # type: ignore[attr-defined]
        yield repo
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM memories WHERE username = $1", marker
            )
        await pool.close()


@pytest.mark.asyncio
async def test_tag_only_search_completes_under_budget(_seeded_repo) -> None:
    """The failing-input shape ``{"limit": 10, "tags": ["validated"]}``
    must return well under the MCP timeout budget. We assert <500ms wall
    clock against a seeded dataset of >2500 memories — comfortably below
    the ~10s MCP request budget that was being blown in production.
    """
    repo = _seeded_repo

    # Warm any planner/parser caches the first call may pay for.
    await repo.search(tags=["validated"], limit=10)

    start = time.perf_counter()
    result = await repo.search(tags=["validated"], limit=10)
    elapsed = time.perf_counter() - start

    assert len(result["memories"]) == 10
    assert elapsed < 0.5, (
        f"tag-only search exceeded 500ms budget: {elapsed*1000:.1f}ms "
        "— the MATERIALIZED CTE fast path or the partial GIN index "
        "(migration 091) may have regressed."
    )
