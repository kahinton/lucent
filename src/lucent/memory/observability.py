"""Phase-2 observability helpers for the memory vitality boost.

Two hooks live here:

* :func:`maybe_log_boost_comparison` — fired from search routes after a
  vitality-boosted ranking is served. When the boost feature flag is on AND
  the per-call sampling roll succeeds, it runs the legacy (non-boosted)
  ranking for the same query and emits a single structured JSON log line
  comparing the two top-N orderings. This lets operators evaluate the boost
  in production without flipping the default.

* :func:`compute_top_n_diff` — pure helper that quantifies the difference
  between two ranked id lists (overlap, set Jaccard, rank-change details).
  Extracted so tests can validate the metrics without standing up a DB.

Both hooks are zero-cost when the boost flag is disabled or when the sample
rate is ``0.0`` (the defaults).
"""

from __future__ import annotations

import json
import logging
import random
from typing import Any, Awaitable, Callable, Iterable, Sequence

from lucent.settings import (
    search_vitality_boost_enabled,
    search_vitality_boost_log_sample_rate,
    search_vitality_boost_log_top_n,
)

logger = logging.getLogger(__name__)


# Type for the legacy-search callable injected by the caller. It must accept
# ``vitality_boost=False`` and return a search-result dict shaped like
# ``MemoryRepository.search``'s output (``{"memories": [...], ...}``).
LegacySearchFn = Callable[..., Awaitable[dict[str, Any]]]


def _ids(memories: Iterable[dict[str, Any]], limit: int) -> list[str]:
    """Return the ids of the first ``limit`` memories as strings."""
    out: list[str] = []
    for mem in memories:
        if len(out) >= limit:
            break
        mid = mem.get("id")
        if mid is None:
            continue
        out.append(str(mid))
    return out


def compute_top_n_diff(
    legacy_ids: Sequence[str],
    boosted_ids: Sequence[str],
    n: int,
) -> dict[str, Any]:
    """Quantify the difference between two ranked top-N id lists.

    Returns a dict with:

    * ``top_n``: the requested ``n``.
    * ``legacy_top``: ids in legacy order, truncated to ``n``.
    * ``boosted_top``: ids in boosted order, truncated to ``n``.
    * ``overlap_count``: number of ids present in both top-N sets.
    * ``jaccard``: Jaccard similarity of the two top-N sets (0.0–1.0).
    * ``identical_order``: True iff the two truncated lists match position-wise.
    * ``rank_changes``: per-id rank delta for ids appearing in both top-N
      sets. Positive ``delta`` means the id moved UP under the boost
      (lower index = higher rank).
    * ``promoted``: ids that appear in ``boosted_top`` but not ``legacy_top``.
    * ``demoted``: ids that appear in ``legacy_top`` but not ``boosted_top``.
    """
    n = max(1, int(n))
    legacy = list(legacy_ids)[:n]
    boosted = list(boosted_ids)[:n]
    legacy_set = set(legacy)
    boosted_set = set(boosted)

    overlap = legacy_set & boosted_set
    union = legacy_set | boosted_set
    jaccard = (len(overlap) / len(union)) if union else 1.0

    legacy_pos = {mid: i for i, mid in enumerate(legacy)}
    boosted_pos = {mid: i for i, mid in enumerate(boosted)}
    rank_changes: list[dict[str, Any]] = []
    for mid in overlap:
        legacy_rank = legacy_pos[mid]
        boosted_rank = boosted_pos[mid]
        if legacy_rank != boosted_rank:
            rank_changes.append(
                {
                    "id": mid,
                    "legacy_rank": legacy_rank,
                    "boosted_rank": boosted_rank,
                    # delta>0: id moved up (toward rank 0) under the boost.
                    "delta": legacy_rank - boosted_rank,
                }
            )
    rank_changes.sort(key=lambda r: (-abs(int(r["delta"])), r["id"]))

    return {
        "top_n": n,
        "legacy_top": legacy,
        "boosted_top": boosted,
        "overlap_count": len(overlap),
        "jaccard": round(jaccard, 6),
        "identical_order": legacy == boosted,
        "rank_changes": rank_changes,
        "promoted": sorted(boosted_set - legacy_set),
        "demoted": sorted(legacy_set - boosted_set),
    }


def should_log_comparison(rng: random.Random | None = None) -> bool:
    """Roll the sample dice. Returns True iff a comparison log should be emitted.

    Always False when the boost flag is disabled (no point comparing legacy to
    legacy). Always False when the sample rate is ``0.0``. Always True when
    the rate is ``>= 1.0``.

    A custom ``random.Random`` may be passed for deterministic tests.
    """
    if not search_vitality_boost_enabled():
        return False
    rate = search_vitality_boost_log_sample_rate()
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    r = rng if rng is not None else random
    return r.random() < rate


async def maybe_log_boost_comparison(
    *,
    legacy_search: LegacySearchFn,
    legacy_search_kwargs: dict[str, Any],
    boosted_result: dict[str, Any],
    query: str | None,
    search_kind: str,
    rng: random.Random | None = None,
) -> dict[str, Any] | None:
    """Conditionally run the legacy ranking and log a structured comparison.

    Parameters
    ----------
    legacy_search:
        Async callable that runs a search. Will be invoked with
        ``**legacy_search_kwargs`` plus an explicit ``vitality_boost=False``
        override so the boost env flag cannot accidentally re-engage.
    legacy_search_kwargs:
        Kwargs to forward to ``legacy_search`` (typically the same kwargs the
        caller used for the user-facing boosted search). The function will
        force ``vitality_boost=False``.
    boosted_result:
        The already-computed boosted search result dict (the one being served
        to the caller). Must contain a ``memories`` list of dicts with ``id``.
    query:
        The free-text query for the search (logged as context).
    search_kind:
        Short tag identifying which endpoint produced this — e.g. ``"search"``
        or ``"search_full"``. Logged so log consumers can disambiguate.
    rng:
        Optional ``random.Random`` for deterministic sampling in tests.

    Returns the diff payload dict that was logged, or ``None`` if no log was
    emitted (sampling miss, flag off, or unrecoverable error).
    """
    if not should_log_comparison(rng=rng):
        return None

    n = search_vitality_boost_log_top_n()

    # Force the legacy code path. Strip any caller-provided override so we
    # always compare against the byte-identical legacy SQL.
    kwargs = dict(legacy_search_kwargs)
    kwargs["vitality_boost"] = False

    try:
        legacy_result = await legacy_search(**kwargs)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "vitality_boost_comparison_failed",
            extra={"event": "vitality_boost_comparison_failed", "error": str(exc)},
        )
        return None

    legacy_ids = _ids(legacy_result.get("memories") or [], n)
    boosted_ids = _ids(boosted_result.get("memories") or [], n)
    diff = compute_top_n_diff(legacy_ids, boosted_ids, n)

    payload = {
        "event": "vitality_boost_comparison",
        "search_kind": search_kind,
        "query": query,
        **diff,
    }
    # Single-line JSON keeps the line greppable and easy to ingest.
    logger.info("vitality_boost_comparison %s", json.dumps(payload, default=str))
    return payload
