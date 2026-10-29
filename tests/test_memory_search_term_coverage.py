"""Regression tests for long-query term-coverage gating in memory search.

Background
----------
Hybrid retrieval requires a candidate memory to match a minimum share of the
query's distinct terms, originally ``ceil(terms * 0.67)``. That rule scales
badly with query length. A long natural-language query — the shape a model
actually types, e.g. ``"spawn subagent tool chat async result return
standalone request"`` (9 terms) — demanded 7 co-occurring terms in one memory.
Nothing in the corpus qualified, so the tool returned
``total_count: 0`` and the caller concluded the fact did not exist. The
memory that answered the question had 3 of the 9 terms and ranked #1 once it
was allowed through.

The floor is now capped at ``TERM_COVERAGE_FLOOR_CAP`` so long queries match on
their strongest few terms, while off-domain queries still match nothing
(their terms do not occur at all).
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
                "query_terms": [],
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
async def test_long_query_gate_is_capped(
    daemon_tenant_scope, monkeypatch, search_method
) -> None:
    """A multi-term query must not require near-unanimous term overlap.

    The emitted gate is ``LEAST(ceil(terms * 0.67), cap)`` — a bare
    ``ceil(terms * 0.67)`` is what silently zeroed long queries.
    """
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    monkeypatch.delenv("LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED", raising=False)
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await getattr(repo, search_method)(
        query="spawn subagent tool chat async result return standalone request",
        limit=5,
    )

    assert "LEAST(" in conn.search_query
    assert "cardinality(search_input.query_terms) * 0.67" in conn.search_query
    # The cap must be present in BOTH the page query and the COUNT query, or
    # total_count would disagree with the page.
    assert "LEAST(" in conn.count_query
    assert str(MemoryRepository.TERM_COVERAGE_FLOOR_CAP) in conn.search_query


@pytest.mark.parametrize("search_method", ["search", "search_full"])
async def test_gate_preserves_short_query_behavior(
    daemon_tenant_scope, monkeypatch, search_method
) -> None:
    """1- and 2-term queries keep their original (uncapped) requirement."""
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await getattr(repo, search_method)(query="delegated work", limit=5)

    # LEAST(ceil(2*0.67), 3) == 2 == ceil(2*0.67): identical to the old rule.
    assert "LEAST(" in conn.search_query
    assert "0.67" in conn.search_query


def test_coverage_cap_is_bounded() -> None:
    """The cap must stay small — a large cap reintroduces the long-query bug."""
    assert 2 <= MemoryRepository.TERM_COVERAGE_FLOOR_CAP <= 4


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

    assert "GREATEST(1, LEAST(" in conn.search_query
    # The short-query fuzzy fallback must not fire when zero terms survive
    # stop-word removal.
    assert "cardinality(search_input.query_terms) BETWEEN 1 AND 2" in conn.search_query


def test_capped_floor_matches_expectations() -> None:
    """Document the effective requirement per query length."""
    from math import ceil

    cap = MemoryRepository.TERM_COVERAGE_FLOOR_CAP
    effective = {
        terms: min(max(1, ceil(terms * 0.67)), cap) for terms in range(1, 13)
    }
    assert effective[1] == 1
    assert effective[2] == 2
    assert effective[3] == 3
    # Beyond the cap, the requirement stops growing: the long-query fix.
    assert effective[9] == cap
    assert effective[12] == cap


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
        "query_terms": ["spawn", "subag", "tool", "chat", "async", "result"],
    }
    repo = MemoryRepository(_CapturePool(conn))

    result = await getattr(repo, search_method)(query="some long query", limit=5)

    assert result["total_count"] == 0
    quality = result["match_quality"]
    # 42 memories held at least one term but none held 3 of the 6 terms: this
    # is a ranking cut, NOT an absence of the fact.
    assert quality["reason"] == "terms_too_scattered"
    assert quality["terms_required_together"] == 3
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
        "query_terms": ["xylophon", "quantum", "banana", "zeppelin"],
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
        "query_terms": ["banana", "quantum", "xylophon", "zeppelin"],
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
        "query_terms": ["deleg", "work"],
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
