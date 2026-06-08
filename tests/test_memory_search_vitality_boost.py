"""Tests for phase-2 vitality-boosted search ranking."""

from __future__ import annotations

import json

import pytest

from lucent.db.memory import MemoryRepository


class _CaptureConn:
    def __init__(self) -> None:
        self.search_query: str = ""
        self.search_params: tuple[object, ...] = ()

    async def fetchrow(self, query: str, *params: object) -> dict[str, int]:
        return {"total": 0}

    async def fetch(self, query: str, *params: object) -> list[dict[str, object]]:
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


class _CapturePool:
    def __init__(self, conn: _CaptureConn) -> None:
        self._conn = conn

    def acquire(self, *, timeout: float | None = None) -> _AcquireCM:  # noqa: ARG002
        return _AcquireCM(self._conn)


def test_vitality_boost_math_is_centered_and_bounded() -> None:
    alpha = 0.15
    sim = 0.60

    assert MemoryRepository._vitality_boosted_rank(
        similarity_score=sim, vitality_score=None, alpha=alpha
    ) == sim
    assert MemoryRepository._vitality_boosted_rank(
        similarity_score=sim, vitality_score=1.0, alpha=alpha
    ) == pytest.approx(0.675, rel=1e-6)
    assert MemoryRepository._vitality_boosted_rank(
        similarity_score=sim, vitality_score=0.0, alpha=alpha
    ) == pytest.approx(0.525, rel=1e-6)
    # Out-of-range vitality is clamped before boost.
    assert MemoryRepository._vitality_boosted_rank(
        similarity_score=sim, vitality_score=2.0, alpha=alpha
    ) == pytest.approx(0.675, rel=1e-6)
    assert MemoryRepository._vitality_boosted_rank(
        similarity_score=sim, vitality_score=-1.0, alpha=alpha
    ) == pytest.approx(0.525, rel=1e-6)


async def test_search_default_ranking_sql_unchanged(monkeypatch) -> None:
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ALPHA", raising=False)
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await repo.search(query="ranking regression", limit=3, offset=1)

    assert "ORDER BY sim_score DESC, importance DESC, created_at DESC" in conn.search_query
    assert "final_rank" not in conn.search_query


async def test_search_uses_vitality_boost_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", "true")
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ALPHA", "0.2")
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await repo.search(query="ranking boosted", limit=5, offset=0)

    assert "ORDER BY final_rank DESC, importance DESC, created_at DESC" in conn.search_query
    assert "COALESCE(vitality_score, 0.5) - 0.5" in conn.search_query
    # query, alpha, limit, offset
    assert conn.search_params[-3] == 0.2


async def test_search_override_disables_boost_even_if_env_enabled(monkeypatch) -> None:
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", "true")
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ALPHA", "0.2")
    conn = _CaptureConn()
    repo = MemoryRepository(_CapturePool(conn))

    await repo.search(query="ranking override", vitality_boost=False)

    assert "ORDER BY sim_score DESC, importance DESC, created_at DESC" in conn.search_query
    assert "final_rank" not in conn.search_query


def test_select_canonical_snapshot_prefers_high_vitality_then_tiebreakers() -> None:
    rows = [
        {
            "id": "mem-a",
            "vitality_score": None,  # treated as 0.5
            "importance": 8,
            "created_at": "2026-01-03T00:00:00+00:00",
        },
        {
            "id": "mem-b",
            "vitality_score": 0.8,
            "importance": 5,
            "created_at": "2026-01-04T00:00:00+00:00",
        },
        {
            "id": "mem-c",
            "vitality_score": 0.8,
            "importance": 7,
            "created_at": "2026-01-02T00:00:00+00:00",
        },
        {
            "id": "mem-d",
            "vitality_score": 0.8,
            "importance": 7,
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "id": "mem-e",
            "vitality_score": 0.2,
            "importance": 10,
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    ]

    remaining = rows[:]
    canonical_order: list[str] = []
    while remaining:
        canonical = MemoryRepository.select_canonical(remaining)
        assert canonical is not None
        canonical_id = str(canonical["id"])
        canonical_order.append(canonical_id)
        remaining = [row for row in remaining if str(row["id"]) != canonical_id]

    snapshot = json.dumps(canonical_order, indent=2)
    assert snapshot == (
        '[\n'
        '  "mem-d",\n'
        '  "mem-c",\n'
        '  "mem-b",\n'
        '  "mem-a",\n'
        '  "mem-e"\n'
        "]"
    )
