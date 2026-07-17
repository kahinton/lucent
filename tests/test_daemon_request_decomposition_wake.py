"""Event-driven request decomposition tests."""

import asyncio
import json

import pytest

from daemon.runtime.loops import RuntimeLoopsMixin


class DecompositionHarness(RuntimeLoopsMixin):
    def __init__(self):
        self._request_ready = asyncio.Event()
        self._decomposition_ready = asyncio.Event()
        self._decomposition_request_ids: set[str] = set()
        self.calls: list[dict] = []

    async def _backfill_pending_decomposition(self, **kwargs):
        self.calls.append(kwargs)
        return 1


def test_request_notification_wakes_cognition_and_decomposition():
    daemon = DecompositionHarness()
    request_id = "8c8c8b95-e414-47eb-9938-6f765d6ea5c6"

    daemon._on_request_ready(
        None,
        1,
        "request_ready",
        json.dumps({"request_id": request_id, "source": "api"}),
    )

    assert daemon._request_ready.is_set()
    assert daemon._decomposition_ready.is_set()
    assert daemon._decomposition_request_ids == {request_id}


@pytest.mark.asyncio
async def test_notified_requests_are_decomposed_immediately():
    daemon = DecompositionHarness()
    daemon._decomposition_request_ids.update({"request-b", "request-a"})

    attempted = await daemon._run_decomposition_pass("org-1")

    assert attempted == 2
    assert daemon.calls == [
        {"org_id": "org-1", "min_age_seconds": 0, "request_id": "request-a"},
        {"org_id": "org-1", "min_age_seconds": 0, "request_id": "request-b"},
    ]
    assert daemon._decomposition_request_ids == set()


@pytest.mark.asyncio
async def test_decomposition_poll_fallback_keeps_five_minute_guard():
    daemon = DecompositionHarness()

    attempted = await daemon._run_decomposition_pass("org-1")

    assert attempted == 1
    assert daemon.calls == [{"org_id": "org-1", "min_age_seconds": 300}]
