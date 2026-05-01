"""Global Lucent runtime settings and feature flags."""

from __future__ import annotations

import os


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def shadow_forget_enabled() -> bool:
    """Whether shadow forgetting sidecar reads/writes are enabled."""
    return _env_bool("LUCENT_SHADOW_FORGET_ENABLED", default=False)


def search_vitality_boost_enabled() -> bool:
    """Whether search ranking includes the vitality boost term."""
    return _env_bool("LUCENT_SEARCH_VITALITY_BOOST_ENABLED", default=False)


def search_exclude_archived_enabled() -> bool:
    """Whether search excludes ``archived``/``forgotten`` lifecycle stages by default.

    Phase-2 M9 rollout flag, mirroring ``LUCENT_SEARCH_VITALITY_BOOST_ENABLED``.

    - Off (default): the search SQL is byte-identical to the pre-M9 baseline —
      ``include_archived`` is accepted on the API surface but has no effect on
      the emitted query, so production behavior is unchanged until an operator
      opts in.
    - On: queries with ``include_archived=False`` (the default) get a
      ``lifecycle_stage NOT IN ('archived', 'forgotten')`` WHERE-clause
      addition. ``include_archived=True`` callers continue to see all rows.
    """
    return _env_bool("LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED", default=False)


def search_vitality_boost_alpha() -> float:
    """Weight for vitality contribution in ranked memory search."""
    return _env_float("LUCENT_SEARCH_VITALITY_BOOST_ALPHA", default=0.15)


def search_vitality_boost_log_sample_rate() -> float:
    """Sampling rate (0.0–1.0) for emitting legacy-vs-boosted top-N comparison logs.

    Phase-2 observability hook: when the vitality boost flag is enabled, a
    fraction of search calls additionally run the legacy ranking and emit a
    structured JSON log line comparing the top-N results. Defaults to ``0.0``
    (disabled) so production is unaffected unless an operator opts in.
    """
    rate = _env_float("LUCENT_SEARCH_VITALITY_BOOST_LOG_SAMPLE_RATE", default=0.0)
    if rate < 0.0:
        return 0.0
    if rate > 1.0:
        return 1.0
    return rate


def search_vitality_boost_log_top_n() -> int:
    """How many ranked results to compare in the boost-vs-legacy log line."""
    raw = os.environ.get("LUCENT_SEARCH_VITALITY_BOOST_LOG_TOP_N")
    if raw is None:
        return 10
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 10
    return max(1, min(50, value))
