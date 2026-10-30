"""Result-bearing writes must survive a transient server outage.

Live failure this covers (request 1a45678f, 2026-10-30): a task finished and
produced 2,114 characters of output at 14:30:48, the server was restarting, every
write-back failed with "All connection attempts failed", and the result was lost
permanently while the task row stayed `running`.

The old transport (`TaskAPI._post`) swallowed every exception, logged a WARN and
returned None, so the caller could not tell "completed" from "server unreachable".
These tests pin the contract that result-bearing writes retry, and that a spent
budget is reported as a failure rather than as success.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from daemon.api import tasks as tasks_api


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for httpx.AsyncClient; returns queued responses in order."""

    def __init__(self, outcomes: list[object]):
        self.outcomes = outcomes
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def patch_client(monkeypatch):
    """Capture the client used by the transport and expose its call count."""

    def _install(outcomes: list[object]):
        holder = _FakeClient(list(outcomes))
        monkeypatch.setattr(
            tasks_api.httpx,
            "AsyncClient",
            lambda *a, **k: holder,
        )
        return holder

    return _install


def test_completion_retries_through_a_connection_outage(monkeypatch, patch_client):
    """A server restart must not cost the task its result."""
    monkeypatch.setattr(tasks_api, "RESULT_WRITE_RETRY_DELAY_SECONDS", 0)
    holder = patch_client(
        [
            httpx.ConnectError("All connection attempts failed"),
            httpx.ConnectError("All connection attempts failed"),
            _FakeResponse(200, {"id": "task-1", "status": "completed"}),
        ]
    )

    result = asyncio.run(
        tasks_api.TaskAPI._post_result(
            "/requests/tasks/task-1/complete",
            {"result": "the real output"},
            "complete_task",
        )
    )

    assert result == {"id": "task-1", "status": "completed"}
    assert holder.calls == 3, "transient connect errors must be retried"


def test_completion_reports_total_failure_instead_of_pretending_success(
    monkeypatch, patch_client
):
    """After the budget is spent the caller must learn the result was not saved."""
    monkeypatch.setattr(tasks_api, "RESULT_WRITE_RETRY_DELAY_SECONDS", 0)
    holder = patch_client([httpx.ConnectError("down")])

    result = asyncio.run(
        tasks_api.TaskAPI._post_result(
            "/requests/tasks/task-1/complete",
            {"result": "x"},
            "complete_task",
            attempts=3,
        )
    )

    assert result is None
    assert holder.calls == 3


def test_client_error_is_not_retried(monkeypatch, patch_client):
    """A 4xx is a permanent rejection; retrying it only delays the report."""
    monkeypatch.setattr(tasks_api, "RESULT_WRITE_RETRY_DELAY_SECONDS", 0)
    holder = patch_client([_FakeResponse(400, text="invalid validation_status")])

    result = asyncio.run(
        tasks_api.TaskAPI._post_result(
            "/requests/tasks/task-1/complete",
            {"result": "x"},
            "complete_task",
        )
    )

    assert result is None
    assert holder.calls == 1, "a rejected request must not be retried"


def test_server_error_is_retried(monkeypatch, patch_client):
    """A 5xx is usually a restart or a saturated pool — worth retrying."""
    monkeypatch.setattr(tasks_api, "RESULT_WRITE_RETRY_DELAY_SECONDS", 0)
    holder = patch_client(
        [_FakeResponse(503, text="starting up"), _FakeResponse(200, {"ok": True})]
    )

    result = asyncio.run(
        tasks_api.TaskAPI._post_result(
            "/requests/tasks/task-1/complete",
            {"result": "x"},
            "complete_task",
        )
    )

    assert result == {"ok": True}
    assert holder.calls == 2


@pytest.mark.parametrize(
    "method,path_fragment",
    [
        ("complete_task", "/complete"),
        ("fail_task", "/fail"),
        ("mark_task_needs_review", "/needs-review"),
    ],
)
def test_all_result_bearing_writes_use_the_retrying_transport(
    monkeypatch, method, path_fragment
):
    """Completion, failure and needs-review all carry output or diagnosis."""
    monkeypatch.setattr(tasks_api, "RESULT_WRITE_RETRY_DELAY_SECONDS", 0)
    seen: list[str] = []

    async def _fake_post_result(path, body, operation, **kwargs):
        seen.append(path)
        return {"ok": True}

    monkeypatch.setattr(tasks_api.TaskAPI, "_post_result", staticmethod(_fake_post_result))

    call = getattr(tasks_api.TaskAPI, method)
    if method == "complete_task":
        asyncio.run(call("t1", "result"))
    elif method == "fail_task":
        asyncio.run(call("t1", "boom", result="partial"))
    else:
        asyncio.run(call("t1", "needs review", result="partial"))

    assert seen and path_fragment in seen[0], (
        f"{method} must not use the fire-and-forget transport"
    )
