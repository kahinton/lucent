"""Double-encode round-trip test: task_events metadata write/read integrity.

Regression guard for the json.dumps() double-encoding bug: add_task_event
(lucent/db/requests.py) used to json.dumps() the metadata dict before binding
it to the JSONB column, so Postgres stored a JSON *string* whose payload was
the dict — asyncpg decoded it back to str, and every consumer that did
dict(metadata) blew up with ``ValueError: dictionary update sequence element
#0 has length 1; 2 is required``.

The fixed write path binds ``metadata or {}`` directly. This test drives the
real repository method through a recording asyncpg pool stand-in, then feeds
the captured binding through ``_coerce_event_metadata`` — the consumer used
by lucent/tools/requests.py when reading task events back — and asserts the
original object survives the full write -> read round trip.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from lucent.db.requests import RequestRepository
from lucent.tools.requests import _coerce_event_metadata

ORG_ID = "0f9abaa4-7489-47ab-8d6c-7c5be8d69d51"
TASK_ID = str(uuid.uuid4())


class _AcquireCtx:
    """``pool.acquire()`` async-context-manager matching the repos' usage."""

    def __init__(self, pool: "RecordingPool"):
        self._pool = pool

    async def __aenter__(self) -> "RecordingConn":
        return RecordingConn(self._pool)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class RecordingConn:
    """Minimal asyncpg connection recording bound parameters."""

    def __init__(self, pool: "RecordingPool"):
        self._pool = pool

    async def fetchval(self, query: str, *params: Any) -> Any:
        self._pool.recorded.append(
            {"kind": "fetchval", "query": query, "params": list(params)}
        )
        # task-existence probe when org_id is passed
        return 1

    async def execute(self, query: str, *params: Any) -> str:
        # Tenant-scope set_config preamble/scrub issued by scoped_acquire_on.
        self._pool.recorded.append(
            {"kind": "execute", "query": query, "params": list(params)}
        )
        return "INSERT 0 0"

    async def fetchrow(self, query: str, *params: Any) -> dict:
        self._pool.recorded.append(
            {"kind": "fetchrow", "query": query, "params": list(params)}
        )
        # RETURNING * shape: task_events columns. Postgres would decode jsonb
        # back to exactly what the write path bound — dict in, dict out.
        return {
            "task_id": uuid.UUID(TASK_ID),
            "event_type": params[1],
            "detail": params[2],
            "metadata": params[3],
        }


class RecordingPool:
    """asyncpg.Pool stand-in that captures what the write path actually binds."""

    def __init__(self):
        self.recorded: list[dict[str, Any]] = []

    def acquire(self):
        return _AcquireCtx(self)


@pytest.fixture()
def repo() -> RequestRepository:
    return RequestRepository(RecordingPool())


def _captured_metadata_param(repo: RequestRepository) -> Any:
    pool = repo.pool
    inserts = [r for r in pool.recorded if r["kind"] == "fetchrow"]
    assert len(inserts) == 1, f"expected one INSERT, recorded {len(inserts)} statements"
    query = " ".join(inserts[0]["query"].split())
    assert "INSERT INTO task_events" in query, "add_task_event must insert task_events"
    assert "metadata" in query
    return inserts[0]["params"][3]  # $4 = metadata


METADATA_PAYLOADS: list[dict[str, Any]] = [
    {"composed_tool_count": 3, "run_managed_tool": True, "granted_tool_names": ["db_query"]},
    {"total_calls": 2, "tool_counts": {"search_memories": 1, "run_managed_tool": 1},
     "calls": [{"tool": "search_memories", "params": {"query": "q"}}]},
    {"missing_tools": [], "requires_operational_tool": False},
    {"nested": {"deeply": {"encoded": [1, 2, {"x": None}]}}, "flag": False,
     "num": 0, "empty": ""},
    {},  # explicit empty metadata is as legitimate as None
]


@pytest.mark.parametrize("metadata", METADATA_PAYLOADS, ids=str)
def test_metadata_round_trips_through_write_and_read(repo, metadata):
    """Dict written via the fixed code path survives _coerce_event_metadata intact."""
    async def round_trip():
        # Real write path: add_task_event binds metadata to the INSERT.
        written = await repo.add_task_event(
            TASK_ID, "composition_surface", detail="d", metadata=metadata, org_id=ORG_ID
        )
        # Written row == what asyncpg would decode from the stored jsonb.
        assert written["metadata"] is metadata or written["metadata"] == metadata
        # Real read path: the consumer used by tools/requests.py event reads.
        return _coerce_event_metadata(written["metadata"])

    round_tripped = asyncio.run(round_trip())

    # The write path must NOT have json.dumps()d the metadata: the bound $4 is
    # the dict itself, so Postgres stores a jsonb object (not a nested string).
    bound = _captured_metadata_param(repo)
    assert isinstance(bound, dict), (
        "add_task_event bound a non-dict to the jsonb column — the write side "
        "is double-encoding again"
    )
    assert not isinstance(bound, str)
    # Object survives the consumer read exactly.
    assert round_tripped == metadata
    assert isinstance(round_tripped, dict)


def test_none_metadata_binds_empty_dict_and_reads_back():
    """metadata=None stays the ``metadata or {}`` default — no encoding surprises."""
    repo = RequestRepository(RecordingPool())

    async def run():
        return await repo.add_task_event(
            TASK_ID, "agent_dispatched", detail="d", metadata=None, org_id=ORG_ID
        )

    written = asyncio.run(run())
    assert _captured_metadata_param(repo) == {}
    assert _coerce_event_metadata(written["metadata"]) == {}


def test_legacy_double_encoded_string_still_reads_defensively():
    """Historical rows (jsonb holding a JSON string) decode instead of raising."""
    legacy = json.dumps({"total_calls": 1, "tool_counts": {"a": 1}})
    coerced = _coerce_event_metadata(legacy)
    assert coerced == {"total_calls": 1, "tool_counts": {"a": 1}}
    # Pre-fix failure mode: dict() over a string raised ValueError element #0.
    with pytest.raises(ValueError):
        dict(legacy)  # documents the bug this hardening closed
    # Garbage strings degrade to {} rather than raising in the read path.
    assert _coerce_event_metadata("not json at all") == {}