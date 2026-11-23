"""Regression tests for long-query term-coverage gating in memory search.

Background
----------
Hybrid retrieval admits a row only when it contains every term of the query's
*anchored core* — its first ``TERM_COVERAGE_CORE_TERMS`` content terms in the
order the caller typed them. This replaced an earlier rule that required
``ceil(terms * 0.67)`` co-occurring terms and then capped the requirement at 3
(``TERM_COVERAGE_FLOOR_CAP``). The cap fixed long queries returning nothing,
but it broke selectivity: past four terms the required count stayed at 3 while
the term set grew, so appending a word could only ADD candidates. On the live
corpus the 3-term query "memory search relevance" admitted 7 rows while the
4-term "memory search relevance scoring" admitted 49 — the reported inversion.

A fraction alone cannot fix that: every fraction below 1 has a flat step
(``ceil(3*0.67) == ceil(4*0.67) == 3``), and a flat step is exactly where
widening happens. Requiring the whole anchored core is provably nested instead:
``admitted(query + extra terms) ⊆ admitted(query)`` for every appended term.
Long queries still match, on their first four content terms; extra words only
sharpen ranking. Off-domain queries still match nothing.
"""

from __future__ import annotations

import pytest

from lucent.db.memory import MemoryRepository

# ---------------------------------------------------------------------------
# Lightweight pool/conn shims (same shape as the include_archived tests)
# ---------------------------------------------------------------------------


class _CaptureConn:
    def __init__(self) -> None:
        self.search_query: str = ""
        self.count_query: str = ""
        self.diagnostic_query: str = ""
        self.diagnostic_row: dict[str, object] | None = None
        self.count_total: int = 0

    async def execute(self, query: str, *params: object) -> str:
        return "SET"

    async def fetchrow(self, query: str, *params: object) -> dict[str, object]:
        if "filtered_total" in query:
            self.diagnostic_query = query
            return self.diagnostic_row or {
                "filtered_total": 0,
                "matching_any": 0,
                "best_overlap": 0,
                "best_core_overlap": 0,
                "query_terms": [],
                "core_terms": [],
            }
        self.count_query = query
        return {"total": self.count_total}

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


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("search_method", ["search", "search_full"])
async def test_long_query_gate_requires_anchored_core(
    daemon_tenant_scope, monkeypatch, search_method
) -> None:
    """A long query admits on ALL of its first N content terms, not a fraction.

    The anchored core is what makes selectivity monotone: extra words can only
    grow the core, never shrink the requirement. The old
    ``LEAST(ceil(terms * 0.67), cap)`` rule is gone from both the page query
    and the COUNT query (they must agree or ``total_count`` would lie).
    """
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    monkeypatch.delenv("LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED", raising=False)
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await getattr(repo, search_method)(
        query="spawn subagent tool chat async result return standalone request",
        limit=5,
    )

    for query in (conn.search_query, conn.count_query):
        assert "ts_debug(" in query
        assert "AS core_terms" in query
        assert "unnest(search_input.core_terms)" in query
        assert "cardinality(search_input.core_terms)" in query
        # The widening rule is gone, cap included.
        assert "cardinality(search_input.query_terms) * 0.67" not in query
        assert "TERM_COVERAGE_FLOOR_CAP" not in query
        # The anchored core is bounded, and it is required in full.
        assert f"LIMIT {MemoryRepository.TERM_COVERAGE_CORE_TERMS}" in query


@pytest.mark.parametrize("search_method", ["search", "search_full"])
async def test_gate_preserves_short_query_behavior(
    daemon_tenant_scope, monkeypatch, search_method
) -> None:
    """1- and 2-term queries keep their original (uncapped) requirement."""
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await getattr(repo, search_method)(query="delegated work", limit=5)

    # A 2-term query's anchored core is both of its terms: still exactly the
    # old ceil(2*0.67) == 2 requirement, with no fraction left behind.
    assert "GREATEST(1, cardinality(search_input.core_terms))" in conn.search_query
    assert "0.67" not in conn.search_query


def test_anchored_core_is_bounded() -> None:
    """The core must stay small: every query re-requires it in full.

    Too large re-creates the old "long query returns nothing" failure; too
    small throws away the terms that make a query specific. The vector damping
    knobs must be a real ceiling (positive step, ceiling above the base floor).
    """
    assert 2 <= MemoryRepository.TERM_COVERAGE_CORE_TERMS <= 6
    assert MemoryRepository.VECTOR_MIN_SIMILARITY_STEP > 0
    assert (
        MemoryRepository.VECTOR_MIN_SIMILARITY_MAX
        > MemoryRepository.VECTOR_MIN_SIMILARITY
    )


@pytest.mark.parametrize("search_method", ["search", "search_full"])
async def test_stopword_query_cannot_match_everything(
    daemon_tenant_scope, monkeypatch, search_method
) -> None:
    """A stop word-only query has no searchable terms and must match nothing.

    Without the ``GREATEST(1, ...)`` floor and the ``BETWEEN 1 AND 2`` fuzzy
    guard, ``ceil(0 * 0.67) == 0`` made the gate vacuous (``0 >= 0``) and the
    fuzzy branch matched 88 unrelated rows — so a query with no content words
    returned a large arbitrary slice of the corpus.
    """
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await getattr(repo, search_method)(query="the a of and", limit=5)

    assert "GREATEST(1, cardinality(search_input.core_terms))" in conn.search_query
    # The short-query fuzzy fallback must not fire when zero terms survive
    # stop-word removal.
    assert "cardinality(search_input.query_terms) BETWEEN 1 AND 2" in conn.search_query


def test_vector_floor_never_relaxes_with_query_length() -> None:
    """Damping is monotone non-decreasing in word count: longer never looser."""
    base = MemoryRepository.VECTOR_MIN_SIMILARITY
    step = MemoryRepository.VECTOR_MIN_SIMILARITY_STEP
    ceiling = MemoryRepository.VECTOR_MIN_SIMILARITY_MAX

    floors = [
        min(ceiling, base + step * max(0, words - 2)) for words in range(1, 21)
    ]
    assert floors[0] == base and floors[1] == base
    assert all(later >= earlier for earlier, later in zip(floors, floors[1:]))
    assert floors[-1] == ceiling


# ---------------------------------------------------------------------------
# Empty-result diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("search_method", ["search", "search_full"])
async def test_empty_search_reports_diagnosis(
    daemon_tenant_scope, monkeypatch, search_method
) -> None:
    """An empty text search must explain itself, not just return zero."""
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    conn = _CaptureConn()
    conn.diagnostic_row = {
        "filtered_total": 100,
        "matching_any": 42,
        "best_overlap": 2,
        "best_core_overlap": 2,
        "query_terms": ["spawn", "subag", "tool", "chat", "async", "result"],
        "core_terms": ["spawn", "subag", "tool", "chat"],
    }
    repo = MemoryRepository(_CapturePool(conn))

    result = await getattr(repo, search_method)(query="some long query", limit=5)

    assert result["total_count"] == 0
    quality = result["match_quality"]
    # 42 memories held at least one term but none held all 4 core terms: this
    # is a ranking cut, NOT an absence of the fact.
    assert quality["reason"] == "terms_too_scattered"
    assert quality["terms_required_together"] == 4
    assert quality["memories_matching_any_term"] == 42
    assert quality["best_term_overlap"] == 2
    assert quality["hint"]


async def test_empty_search_reports_genuine_no_match(
    daemon_tenant_scope, monkeypatch
) -> None:
    """No term present at all is a genuine no-match; say so plainly."""
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    conn = _CaptureConn()
    conn.diagnostic_row = {
        "filtered_total": 100,
        "matching_any": 0,
        "best_overlap": 0,
        "best_core_overlap": 0,
        "query_terms": ["xylophon", "quantum", "banana", "zeppelin"],
        "core_terms": ["xylophon", "quantum", "banana", "zeppelin"],
    }
    repo = MemoryRepository(_CapturePool(conn))

    result = await repo.search(query="xylophone quantum banana zeppelin")

    quality = result["match_quality"]
    assert quality["reason"] == "no_memory_contains_any_query_term"
    assert "genuine no-match" in quality["hint"]


async def test_empty_search_reports_off_corpus_vocabulary(
    daemon_tenant_scope, monkeypatch
) -> None:
    """A couple of incidental single-term hits are still a genuine no-match.

    Live case: "xylophone quantum banana zeppelin" matched 2 memories on one
    term each. Reporting that as a ranking cut would push the caller to retry
    a query whose wording simply is not in the corpus.
    """
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    conn = _CaptureConn()
    conn.diagnostic_row = {
        "filtered_total": 373,
        "matching_any": 2,
        "best_overlap": 1,
        "best_core_overlap": 1,
        "query_terms": ["banana", "quantum", "xylophon", "zeppelin"],
        "core_terms": ["banana", "quantum", "xylophon", "zeppelin"],
    }
    repo = MemoryRepository(_CapturePool(conn))

    result = await repo.search(query="xylophone quantum banana zeppelin")

    quality = result["match_quality"]
    assert quality["reason"] == "query_vocabulary_not_in_corpus"
    assert "genuine no-match" in quality["hint"]


async def test_empty_search_reports_filter_elimination(
    daemon_tenant_scope, monkeypatch
) -> None:
    """Filters can exclude everything before text is ever evaluated."""
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    conn = _CaptureConn()
    conn.diagnostic_row = {
        "filtered_total": 0,
        "matching_any": 0,
        "best_overlap": 0,
        "best_core_overlap": 0,
        "query_terms": ["deleg", "work"],
        "core_terms": ["deleg", "work"],
    }
    repo = MemoryRepository(_CapturePool(conn))

    result = await repo.search(query="delegated work", type="goal")

    quality = result["match_quality"]
    assert quality["reason"] == "no_memories_match_filters"
    assert "filter" in quality["hint"]


async def test_diagnostics_omitted_when_results_found(
    daemon_tenant_scope, monkeypatch
) -> None:
    """Non-empty searches must not pay for the diagnostic query."""
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    conn = _CaptureConn()
    conn.count_total = 3
    repo = MemoryRepository(_CapturePool(conn))

    result = await repo.search(query="delegated work")

    assert result["total_count"] == 3
    assert "match_quality" not in result
    assert conn.diagnostic_query == ""


async def test_diagnostics_failure_never_breaks_search(
    daemon_tenant_scope, monkeypatch
) -> None:
    """A diagnostics error must not fail a search that already succeeded."""

    class _ExplodingConn(_CaptureConn):
        async def fetchrow(self, query: str, *params: object):
            if "filtered_total" in query:
                raise RuntimeError("diagnostics unavailable")
            return await super().fetchrow(query, *params)

    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    repo = MemoryRepository(_CapturePool(_ExplodingConn()))

    result = await repo.search(query="delegated work")

    assert result["total_count"] == 0
    assert result["match_quality"] == {}


async def test_filter_only_search_skips_diagnostics(
    daemon_tenant_scope, monkeypatch
) -> None:
    """No text query means nothing to diagnose."""
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    result = await repo.search(type="goal")

    assert "match_quality" not in result
