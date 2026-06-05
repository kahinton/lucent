"""Structured logging context test for the daemon_messages router.

Validates that ``send_daemon_message`` emits its INFO log line through the
namespaced module logger (``lucent.api.routers.daemon_messages``) and that
the structured ``extra`` fields (memory_id, in_reply_to, organization_id)
plus contextvar-backed request_id/user_id round-trip into the JSON record.

Analogous to ``test_dispatch_log_line_includes_task_request_and_user_context``
for the daemon dispatcher, but scoped to a slice-2 converted module.
"""

from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lucent.api.models import DaemonMessageCreate
from lucent.api.routers import daemon_messages as dm_module
from lucent.log_context import clear_log_context, set_request_id, set_user_id
from lucent.logging import JSONFormatter


@pytest.mark.asyncio
async def test_send_daemon_message_log_line_includes_structured_context(monkeypatch):
    # Wire a JSON-formatted capture handler onto the module's namespaced logger.
    target_logger = logging.getLogger("lucent.api.routers.daemon_messages")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())

    prev_handlers = list(target_logger.handlers)
    prev_level = target_logger.level
    prev_propagate = target_logger.propagate

    target_logger.handlers = [handler]
    target_logger.setLevel(logging.INFO)
    target_logger.propagate = False

    request_id = "req-test-daemon-msg-001"
    user_id = uuid4()
    org_id = uuid4()
    created_memory_id = uuid4()
    reply_to = uuid4()

    class _StubRepo:
        def __init__(self, _pool):
            pass

        async def create(self, **kwargs):
            return {
                "id": created_memory_id,
                "content": kwargs["content"],
                "created_at": "2026-04-25T00:00:00+00:00",
                "updated_at": "2026-04-25T00:00:00+00:00",
                "tags": kwargs["tags"],
                "metadata": kwargs["metadata"],
            }

    async def _stub_get_pool():
        return object()

    monkeypatch.setattr(dm_module, "MemoryRepository", _StubRepo)
    monkeypatch.setattr(dm_module, "get_pool", _stub_get_pool)

    user = SimpleNamespace(
        id=user_id,
        organization_id=org_id,
        display_name="Daemon Tester",
        email="daemon@example.test",
    )

    set_request_id(request_id)
    set_user_id(str(user_id))

    try:
        await dm_module.send_daemon_message(
            data=DaemonMessageCreate(content="hello world", in_reply_to=reply_to),
            user=user,  # type: ignore[arg-type]
        )

        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        assert lines, "expected at least one JSON log line from daemon_messages router"
        record = next(
            (line for line in lines if "Daemon message sent" in str(line.get("message", ""))),
            None,
        )
        assert record is not None, f"no dispatch line found in: {lines}"

        # Namespaced logger
        assert record["logger"] == "lucent.api.routers.daemon_messages"

        # Contextvar-propagated fields
        assert record["request_id"] == request_id
        assert record["user_id"] == str(user_id)

        # Structured extras emitted via extra={}
        assert record["memory_id"] == str(created_memory_id)
        assert record["in_reply_to"] == str(reply_to)
        assert record["organization_id"] == str(org_id)
    finally:
        target_logger.handlers = prev_handlers
        target_logger.setLevel(prev_level)
        target_logger.propagate = prev_propagate
        clear_log_context()
