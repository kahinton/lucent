"""Tests for request-scoped logging context helpers."""

import asyncio

import pytest

from lucent.log_context import (
    clear_log_context,
    clear_request_id,
    clear_user_id,
    get_request_id,
    get_user_id,
    set_request_id,
    set_user_id,
)


def test_set_get_clear_request_id() -> None:
    clear_log_context()

    set_request_id("req-123")
    assert get_request_id() == "req-123"

    clear_request_id()
    assert get_request_id() is None


def test_set_get_clear_user_id() -> None:
    clear_log_context()

    set_user_id("user-123")
    assert get_user_id() == "user-123"

    clear_user_id()
    assert get_user_id() is None


def test_clear_log_context_clears_both_values() -> None:
    set_request_id("req-123")
    set_user_id("user-123")

    clear_log_context()

    assert get_request_id() is None
    assert get_user_id() is None


@pytest.mark.asyncio
async def test_context_isolation_between_concurrent_tasks() -> None:
    clear_log_context()

    async def worker(request_id: str, user_id: str, delay: float) -> tuple[str | None, str | None]:
        clear_log_context()
        set_request_id(request_id)
        set_user_id(user_id)
        await asyncio.sleep(delay)
        return get_request_id(), get_user_id()

    results = await asyncio.gather(
        worker("req-a", "user-a", 0.02),
        worker("req-b", "user-b", 0.01),
    )

    assert results == [("req-a", "user-a"), ("req-b", "user-b")]
    assert get_request_id() is None
    assert get_user_id() is None
