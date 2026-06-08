"""Pattern 2 / Fix G1 + G3 regression tests.

Locks in the scoped sub-agent API key TTL math so it can never silently
drift back to the original ``ttl_minutes=60`` value that caused the
``Unauthorized: Invalid or expired credentials`` spike documented in
``docs/runbooks/mcp-auth-and-timeouts.md`` (request faa1ff7a).
"""

from __future__ import annotations

import math

import pytest

from daemon import daemon as daemon_module


@pytest.mark.parametrize(
    "session_seconds,expected",
    [
        # Default session timeout (3600s) → 60 + 15 = 75m (≥ 60m floor).
        (3600, 75),
        # Long sessions get proportional TTL with 15m headroom.
        (7200, 135),
        # Short sessions floor to 60 minutes — never less than the historical
        # value, so the fix can only widen the window, not narrow it.
        (600, 60),
        (0, 60),
    ],
)
def test_scoped_key_ttl_tracks_session_timeout(monkeypatch, session_seconds, expected):
    monkeypatch.setattr(daemon_module, "SESSION_TOTAL_TIMEOUT", session_seconds)
    assert daemon_module._scoped_key_ttl_minutes() == expected


def test_scoped_key_ttl_strictly_exceeds_session_budget_with_headroom(monkeypatch):
    """The whole point of Fix G1 — TTL must outlive any tool call the
    session can emit, with at least 15m of slack."""
    for session_seconds in (1800, 3600, 5400, 7200):
        monkeypatch.setattr(daemon_module, "SESSION_TOTAL_TIMEOUT", session_seconds)
        ttl_minutes = daemon_module._scoped_key_ttl_minutes()
        assert ttl_minutes * 60 >= session_seconds + 15 * 60, (
            f"TTL {ttl_minutes}m does not strictly exceed "
            f"session {session_seconds}s + 15m headroom"
        )


def test_scoped_key_ttl_handles_garbage_session_timeout(monkeypatch):
    """If SESSION_TOTAL_TIMEOUT is somehow corrupted, fall back to a sensible
    TTL (≥ 60m floor, derived from the 3600s default) rather than raising or
    returning a tiny TTL."""

    class _Garbage:
        def __int__(self):
            raise ValueError("nope")

    monkeypatch.setattr(daemon_module, "SESSION_TOTAL_TIMEOUT", _Garbage())
    # Fallback: helper catches the conversion failure, treats it as the
    # default 3600s session budget → ceil(3600/60)+15 = 75m. Never below the
    # 60m floor.
    ttl = daemon_module._scoped_key_ttl_minutes()
    assert ttl >= 60


def test_no_hardcoded_short_ttls_remain_at_call_sites():
    """Guardrail: lock in the textual removal of ``ttl_minutes=60`` and
    ``ttl_minutes=30`` literals at the three dispatch/planning/decomposition
    call sites. The mint function itself still accepts an explicit override
    so tests can pin TTL — that's expected; this guard targets the daemon
    *call sites* only."""
    source = (
        daemon_module.__file__
        and open(daemon_module.__file__, encoding="utf-8").read()
    )
    assert source is not None
    # Exact whitespace-anchored signatures that the call sites had before
    # the fix — if either of these reappears in production code paths it
    # means a regression has slipped in.
    assert "                    ttl_minutes=60,\n" not in source
    assert "                        ttl_minutes=30,\n" not in source
    # And the computed helper must be wired in at least three places
    # (main dispatch, planning, decomposition).
    assert source.count("ttl_minutes=_scoped_key_ttl_minutes()") >= 3


def test_scoped_key_ttl_math_uses_ceiling(monkeypatch):
    """Edge case: non-multiple-of-60 session timeouts round up, never
    down — preventing off-by-one expiry just inside the window."""
    monkeypatch.setattr(daemon_module, "SESSION_TOTAL_TIMEOUT", 3601)
    assert daemon_module._scoped_key_ttl_minutes() == math.ceil(3601 / 60) + 15
