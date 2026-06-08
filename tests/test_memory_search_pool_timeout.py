"""Pattern 1 (search_memories tool_error) — pool-timeout error path.

These tests exercise the bounded ``pool.acquire(timeout=...)`` added to
``MemoryDB.search`` / ``search_full`` and the typed
``PoolAcquireTimeoutError`` it raises. The goal is to prove that pool
exhaustion surfaces as a distinct, retryable failure class instead of an
opaque ``tool_error``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from lucent.db.memory import MemoryRepository, PoolAcquireTimeoutError
from lucent.db.tool_audit import classify_tool_result


def _starved_pool() -> MagicMock:
    """Build a fake asyncpg pool whose ``acquire(timeout=...)`` always times
    out, mimicking a fully-checked-out connection pool."""

    pool = MagicMock()

    def acquire(timeout=None):  # noqa: ARG001 — signature must accept timeout
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    pool.acquire = MagicMock(side_effect=acquire)
    return pool


@pytest.mark.asyncio
async def test_search_raises_pool_acquire_timeout(monkeypatch):
    """``MemoryDB.search`` must raise ``PoolAcquireTimeoutError`` when the
    pool can't hand out a connection within the bounded acquire window."""

    # Tighten the bound so we don't actually wait — env var is read at call
    # time inside ``_search_pool_acquire_timeout``.
    monkeypatch.setenv("LUCENT_SEARCH_POOL_ACQUIRE_TIMEOUT", "0.1")

    repo = MemoryRepository(_starved_pool())

    with pytest.raises(PoolAcquireTimeoutError) as exc_info:
        await repo.search(query="anything")

    assert exc_info.value.op == "search"
    assert exc_info.value.timeout == pytest.approx(0.1)
    assert "DB pool acquire timeout" in str(exc_info.value)


@pytest.mark.asyncio
async def test_search_full_raises_pool_acquire_timeout(monkeypatch):
    monkeypatch.setenv("LUCENT_SEARCH_POOL_ACQUIRE_TIMEOUT", "0.1")

    repo = MemoryRepository(_starved_pool())

    with pytest.raises(PoolAcquireTimeoutError) as exc_info:
        await repo.search_full(query="anything")

    assert exc_info.value.op == "search_full"


@pytest.mark.asyncio
async def test_search_passes_explicit_acquire_timeout(monkeypatch):
    """The acquire() call must include the bounded ``timeout`` kwarg."""

    monkeypatch.setenv("LUCENT_SEARCH_POOL_ACQUIRE_TIMEOUT", "3.5")

    captured: dict = {}

    pool = MagicMock()

    def acquire(timeout=None):
        captured["timeout"] = timeout
        cm = MagicMock()
        # Short-circuit so the test doesn't need a real connection.
        cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    pool.acquire = MagicMock(side_effect=acquire)
    repo = MemoryRepository(pool)

    with pytest.raises(PoolAcquireTimeoutError):
        await repo.search(query="x")

    assert captured["timeout"] == pytest.approx(3.5)


def test_classifier_labels_db_pool_timeout_distinctly():
    """``tool_error`` ≠ ``db_pool_acquire_timeout`` — without this distinction
    ``analyze_tool_failure_patterns`` cannot tell the patterns apart."""

    text = (
        "Error calling tool search_memories: DBPoolAcquireTimeout: "
        "DB pool acquire timeout after 5.0s during search"
    )
    status, failure_class, message = classify_tool_result(text)
    assert status == "failed"
    assert failure_class == "db_pool_acquire_timeout"
    assert "DBPoolAcquireTimeout" in (message or "")


def test_classifier_labels_mcp_timeout_distinctly():
    text = (
        "Error calling tool search_memories: MCPTimeoutError MCP tool "
        "search_memories timed out after 120s (2 attempt(s))"
    )
    status, failure_class, _ = classify_tool_result(text)
    assert status == "failed"
    assert failure_class == "mcp_timeout"


def test_classifier_labels_invalid_input_distinctly():
    """Malformed-query / parse errors must not be lumped with tool_error."""
    text = 'Error calling tool search_memories: {"error": "Invalid input: bad uuid"}'
    # _error_response wraps the message in JSON, so the canonical surface form
    # the model sees starts with `{"error": "Invalid input: ..."}`. We accept
    # either shape — the classifier keys on lowered substring presence.
    status, failure_class, _ = classify_tool_result(text)
    assert status == "failed"
    assert failure_class == "invalid_input"
