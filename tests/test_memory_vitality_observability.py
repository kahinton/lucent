"""Tests for the Phase-2 vitality observability hooks."""

from __future__ import annotations

import json
import logging
import random

import pytest

from lucent.memory import observability

# ---------- compute_top_n_diff -----------------------------------------------


def test_compute_top_n_diff_identical_lists() -> None:
    diff = observability.compute_top_n_diff(["a", "b", "c"], ["a", "b", "c"], n=10)
    assert diff["overlap_count"] == 3
    assert diff["jaccard"] == 1.0
    assert diff["identical_order"] is True
    assert diff["rank_changes"] == []
    assert diff["promoted"] == []
    assert diff["demoted"] == []
    assert diff["top_n"] == 10


def test_compute_top_n_diff_disjoint() -> None:
    diff = observability.compute_top_n_diff(["a", "b"], ["c", "d"], n=5)
    assert diff["overlap_count"] == 0
    assert diff["jaccard"] == 0.0
    assert diff["identical_order"] is False
    assert diff["promoted"] == ["c", "d"]
    assert diff["demoted"] == ["a", "b"]


def test_compute_top_n_diff_partial_overlap_with_rank_changes() -> None:
    legacy = ["a", "b", "c", "d"]
    boosted = ["c", "a", "e", "b"]
    diff = observability.compute_top_n_diff(legacy, boosted, n=4)

    # overlap = {a, b, c}; union = {a, b, c, d, e} -> 3/5
    assert diff["overlap_count"] == 3
    assert diff["jaccard"] == pytest.approx(0.6)
    assert diff["identical_order"] is False
    assert diff["promoted"] == ["e"]
    assert diff["demoted"] == ["d"]

    # 'c' moved from rank 2 -> 0 (delta +2, the largest absolute change),
    # 'a' moved from 0 -> 1 (delta -1), 'b' from 1 -> 3 (delta -2).
    # Rank changes are sorted by descending |delta|.
    by_id = {row["id"]: row for row in diff["rank_changes"]}
    assert by_id["c"]["delta"] == 2
    assert by_id["a"]["delta"] == -1
    assert by_id["b"]["delta"] == -2
    assert diff["rank_changes"][0]["id"] in {"c", "b"}  # both have |delta|=2
    assert abs(diff["rank_changes"][0]["delta"]) == 2


def test_compute_top_n_diff_truncates_to_n() -> None:
    diff = observability.compute_top_n_diff(
        ["a", "b", "c", "d"], ["a", "b", "c", "d"], n=2
    )
    assert diff["legacy_top"] == ["a", "b"]
    assert diff["boosted_top"] == ["a", "b"]
    assert diff["overlap_count"] == 2


# ---------- should_log_comparison sampling -----------------------------------


def test_should_log_comparison_off_when_flag_disabled(monkeypatch) -> None:
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_LOG_SAMPLE_RATE", "1.0")
    assert observability.should_log_comparison() is False


def test_should_log_comparison_off_when_rate_zero(monkeypatch) -> None:
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", "true")
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_LOG_SAMPLE_RATE", "0.0")
    assert observability.should_log_comparison() is False


def test_should_log_comparison_always_when_rate_one(monkeypatch) -> None:
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", "true")
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_LOG_SAMPLE_RATE", "1.0")
    # Even with an "unlucky" RNG that always returns ~1.0, rate>=1 short-circuits.
    rng = random.Random(0)
    assert observability.should_log_comparison(rng=rng) is True


def test_should_log_comparison_respects_rng(monkeypatch) -> None:
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", "true")
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_LOG_SAMPLE_RATE", "0.5")

    class _StubRng:
        def __init__(self, value: float) -> None:
            self._value = value

        def random(self) -> float:
            return self._value

    assert observability.should_log_comparison(rng=_StubRng(0.49)) is True
    assert observability.should_log_comparison(rng=_StubRng(0.51)) is False


def test_sample_rate_clamped_to_unit_interval(monkeypatch) -> None:
    from lucent.settings import search_vitality_boost_log_sample_rate

    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_LOG_SAMPLE_RATE", "5.0")
    assert search_vitality_boost_log_sample_rate() == 1.0
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_LOG_SAMPLE_RATE", "-1.0")
    assert search_vitality_boost_log_sample_rate() == 0.0
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_LOG_SAMPLE_RATE", "garbage")
    assert search_vitality_boost_log_sample_rate() == 0.0


# ---------- maybe_log_boost_comparison ---------------------------------------


class _StubRng:
    """Deterministic RNG that always returns a fixed float in [0, 1)."""

    def __init__(self, value: float) -> None:
        self._value = value

    def random(self) -> float:
        return self._value


@pytest.mark.asyncio
async def test_maybe_log_skips_when_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", raising=False)

    called = {"n": 0}

    async def legacy_search(**kwargs: object) -> dict[str, object]:
        called["n"] += 1
        return {"memories": []}

    out = await observability.maybe_log_boost_comparison(
        legacy_search=legacy_search,
        legacy_search_kwargs={"query": "x"},
        boosted_result={"memories": [{"id": "1"}]},
        query="x",
        search_kind="search",
        rng=_StubRng(0.0),
    )
    assert out is None
    assert called["n"] == 0  # never even invoked the legacy search


@pytest.mark.asyncio
async def test_maybe_log_emits_structured_payload(monkeypatch, caplog) -> None:
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", "true")
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_LOG_SAMPLE_RATE", "1.0")
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_LOG_TOP_N", "3")

    captured_kwargs: dict[str, object] = {}

    async def legacy_search(**kwargs: object) -> dict[str, object]:
        captured_kwargs.update(kwargs)
        return {"memories": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}

    boosted_result = {
        "memories": [{"id": "c"}, {"id": "a"}, {"id": "d"}, {"id": "b"}]
    }

    # Some app tests configure the top-level lucent logger with propagate=False.
    # Re-enable propagation here so caplog reliably sees this module's record
    # even when the full suite runs before this test.
    monkeypatch.setattr(logging.getLogger("lucent"), "propagate", True)
    caplog.set_level(logging.INFO, logger="lucent.memory.observability")

    out = await observability.maybe_log_boost_comparison(
        legacy_search=legacy_search,
        legacy_search_kwargs={"query": "ranking", "vitality_boost": True},
        boosted_result=boosted_result,
        query="ranking",
        search_kind="search",
        rng=_StubRng(0.0),
    )

    # Forced to legacy ranking even though caller passed vitality_boost=True.
    assert captured_kwargs.get("vitality_boost") is False
    assert captured_kwargs.get("query") == "ranking"

    assert out is not None
    assert out["event"] == "vitality_boost_comparison"
    assert out["search_kind"] == "search"
    assert out["query"] == "ranking"
    assert out["top_n"] == 3
    assert out["legacy_top"] == ["a", "b", "c"]
    assert out["boosted_top"] == ["c", "a", "d"]  # truncated to top_n=3
    assert out["overlap_count"] == 2
    assert out["identical_order"] is False
    assert "d" in out["promoted"]
    assert "b" in out["demoted"]

    # Log was emitted as a single line containing parseable JSON payload.
    matching = [r for r in caplog.records if "vitality_boost_comparison" in r.message]
    assert matching, "expected at least one comparison log record"
    line = matching[-1].message
    json_blob = line.split(" ", 1)[1]
    parsed = json.loads(json_blob)
    assert parsed["event"] == "vitality_boost_comparison"
    assert parsed["legacy_top"] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_maybe_log_swallows_legacy_search_errors(monkeypatch) -> None:
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", "true")
    monkeypatch.setenv("LUCENT_SEARCH_VITALITY_BOOST_LOG_SAMPLE_RATE", "1.0")

    async def legacy_search(**kwargs: object) -> dict[str, object]:
        raise RuntimeError("boom")

    # Must not propagate — observability must never break the request path.
    out = await observability.maybe_log_boost_comparison(
        legacy_search=legacy_search,
        legacy_search_kwargs={"query": "x"},
        boosted_result={"memories": [{"id": "1"}]},
        query="x",
        search_kind="search",
        rng=_StubRng(0.0),
    )
    assert out is None
