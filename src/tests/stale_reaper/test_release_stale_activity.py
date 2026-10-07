"""Regression tests: activity-based stale-task reaper predicate.

Pins the 2026-09-09 fix (stale-task reaper released actively-working tasks):
`release_stale_tasks` used to release any claimed task whose `claimed_at` was
older than the stale threshold — claim age alone — which killed tasks that
were minutes (sometimes seconds) from their last tool call.

The corrected contract, shared by the reaper and its preflight
`stale_task_reaper_has_work` so the gate and the reaper can never drift:

A claimed/running task is eligible for release ONLY when one of:
  1. its claim lease expired (`claim_expires_at < NOW()`);
  2. the owning daemon instance is dead (status <> 'active' or not seen for
     instance_stale_seconds);
  3. the task is activity-stale: no heartbeat from the owning daemon's
     heartbeat loop (`last_heartbeat_at`, falling back to `claimed_at`), no
     task-attributed tool-audit rows (`tool_call_audit_log.task_id`), and no
     task events (`task_events.task_id`) within the stale threshold.

These tests run the repository against a fake recording pool (no Postgres);
the SQL-shape assertions pin the predicate contract itself.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from lucent.db.requests import RequestRepository
from lucent.db.pool import clear_tenant_scope, set_tenant_scope

ORG_ID = "0f9abaa4-7489-47ab-8d6c-7c5be8d69d51"


@pytest.fixture(autouse=True)
def _daemon_tenant_context():
    """Bind the ambient tenant scope a live daemon task runs under.

    The no-org branch of the reaper/preflight relies on the ambient
    ContextVar scope (role=daemon) exactly as the real daemon loop sets it;
    without it the fail-closed tenant guard refuses the acquire.
    """
    set_tenant_scope(user_id=str(uuid.uuid4()), organization_id=None, role="daemon")
    yield
    clear_tenant_scope()

# The shared activity-staleness predicate, normalized. $1 is the stale
# threshold in seconds in every query variant (org and no-org branches alike).
ACTIVITY_CLAUSE = (
    "COALESCE(T.LAST_HEARTBEAT_AT, T.CLAIMED_AT) < NOW() - MAKE_INTERVAL(SECS := $1) "
    "AND NOT EXISTS ( SELECT 1 FROM TOOL_CALL_AUDIT_LOG A WHERE A.TASK_ID = T.ID "
    "AND A.CREATED_AT >= NOW() - MAKE_INTERVAL(SECS := $1) ) "
    "AND NOT EXISTS ( SELECT 1 FROM TASK_EVENTS TE WHERE TE.TASK_ID = T.ID "
    "AND TE.CREATED_AT >= NOW() - MAKE_INTERVAL(SECS := $1) )"
)

# The removed wrongful trigger: claim-age alone.
OLD_CLAUSE_C = "T.CLAIMED_AT < NOW() - MAKE_INTERVAL(SECS := $1)"


def _norm(sql: str) -> str:
    return " ".join(str(sql).upper().split())


class _RecordingConn:
    """Records executed statements; synthesizes empty result sets."""

    def __init__(self, pool: "_RecordingPool"):
        self.pool = pool

    def _record(self, kind: str, query: str, *params: Any) -> None:
        self.pool.recorded.append(
            {"kind": kind, "query": _norm(query), "params": list(params)}
        )

    async def fetch(self, query: str, *params: Any) -> list[dict]:
        self._record("fetch", query, *params)
        return []

    async def fetchval(self, query: str, *params: Any) -> int:
        self._record("fetchval", query, *params)
        if "SELECT 1 FROM TASKS" in _norm(query):
            return 1  # task-existence probe: the task is real
        return 0

    async def fetchrow(self, query: str, *params: Any) -> dict | None:
        self._record("fetchrow", query, *params)
        if "INSERT INTO TASK_EVENTS" in _norm(query):
            return {"id": str(uuid.uuid4())}
        return None

    async def execute(self, query: str, *params: Any) -> str:
        self._record("execute", query, *params)
        return "INSERT 0 1"


class _AcquireCtx:
    def __init__(self, pool: "_RecordingPool"):
        self.pool = pool

    async def __aenter__(self) -> _RecordingConn:
        return self.pool.conn_cls(self.pool)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _RecordingPool:
    """asyncpg.Pool stand-in that records every statement the repo issues."""

    conn_cls: type[_RecordingConn] = _RecordingConn

    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self)

    def tasks_statements(self) -> list[dict[str, Any]]:
        return [r for r in self.recorded if "FROM TASKS T" in r["query"]]


class _ReleasedRowConn(_RecordingConn):
    """Connection variant whose release statement returns one released row."""

    async def fetch(self, query: str, *params: Any) -> list[dict]:
        self._record("fetch", query, *params)
        if "UPDATE TASKS T" in _norm(query):
            return [{"id": str(uuid.uuid4()), "previous_claimed_by": "daemon-1"}]
        return []


class _ReleasedRowPool(_RecordingPool):
    conn_cls = _ReleasedRowConn


def test_release_org_branch_uses_activity_predicate_not_claim_age():
    pool = _RecordingPool()
    repo = RequestRepository(pool)
    asyncio.run(repo.release_stale_tasks(stale_minutes=30, org_id=ORG_ID))

    stmts = pool.tasks_statements()
    assert len(stmts) == 1, "release_stale_tasks(org_id=...) issues one statement"
    sql = stmts[0]["query"]
    assert OLD_CLAUSE_C not in sql, (
        "claim age alone must never release a task (the wrongful clause C)"
    )
    assert ACTIVITY_CLAUSE in sql, "release must require zero activity within threshold"
    assert "T.CLAIM_EXPIRES_AT < NOW()" in sql, "lease-expiry path preserved"
    assert "DI.STATUS <> 'ACTIVE'" in sql, "dead-owner path preserved"


def test_release_noorg_branch_uses_activity_predicate_not_claim_age():
    pool = _RecordingPool()
    repo = RequestRepository(pool)
    asyncio.run(repo.release_stale_tasks(stale_minutes=30))

    stmts = pool.tasks_statements()
    assert len(stmts) == 1
    sql = stmts[0]["query"]
    assert OLD_CLAUSE_C not in sql
    assert ACTIVITY_CLAUSE in sql
    # No-org branch: stale_seconds=$1, instance_stale_seconds=$2.
    assert stmts[0]["params"][:2] == [1800, 1800], (
        f"unexpected param shape: {stmts[0]['params']}"
    )


def test_preflight_matches_reaper_predicate_both_branches():
    for org_id in (ORG_ID, None):
        pool = _RecordingPool()
        repo = RequestRepository(pool)
        asyncio.run(repo.stale_task_reaper_has_work(stale_minutes=30, org_id=org_id))

        stmts = pool.tasks_statements()
        assert len(stmts) == 1, f"preflight issues one statement (org_id={org_id})"
        sql = stmts[0]["query"]
        assert OLD_CLAUSE_C not in sql
        assert ACTIVITY_CLAUSE in sql, (
            "preflight gate must use the same activity-based predicate as the reaper"
        )
        assert "T.CLAIM_EXPIRES_AT < NOW()" in sql
        assert "DI.STATUS <> 'ACTIVE'" in sql


def test_stale_threshold_is_seconds_scaled_from_env_configurable_minutes():
    """stale_minutes (env-configurable at both callers) converts to seconds."""
    pool = _RecordingPool()
    repo = RequestRepository(pool)
    asyncio.run(repo.release_stale_tasks(stale_minutes=45, org_id=ORG_ID))
    stmts = pool.tasks_statements()
    assert stmts[0]["params"][0] == 45 * 60


def test_reaper_event_recorded_after_release():
    """Released tasks get a 'reaper' task event (existing behavior preserved)."""
    pool = _ReleasedRowPool()
    repo = RequestRepository(pool)
    asyncio.run(repo.release_stale_tasks(stale_minutes=30, org_id=ORG_ID))

    event_inserts = [
        r for r in pool.recorded if "INSERT INTO TASK_EVENTS" in r["query"]
    ]
    assert len(event_inserts) == 1, "each released task gets one 'reaper' event"
    assert event_inserts[0]["params"][1] == "reaper"