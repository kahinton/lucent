"""Regression tests: chat catch-up (intro-summary) user scoping.

The 2026-09-10 catch-up leak: ``gather_work_context``'s goal-milestone rollup
selected goal memories by organization_id alone, so one member's PRIVATE goal
memories grounded another member's catch-up summary. Since the clearance
migration (129) the goals query runs on the memories authorized pool —
clearance-driven and default-deny (own rows via the owner clearance +
org/user/group read grants) — the same tenancy boundary every other memory
read enforces.

Fix design + leak reproduction: ``intro-catchup-leak-map-rca.md`` (repo root).

The identity-propagation test is unit-level (no DB); the SQL-composition test
pins the rewrite shape with fakes. Boundary semantics (who can actually read
whom) are covered by tests/test_db_memories_auth.py and the live suites.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch


# ═════════════════════════════════════════════════════════════════════
# Identity propagation into the background LLM call's audit_context
# ═════════════════════════════════════════════════════════════════════


class _FakeEngine:
    name = "fake"

    def __init__(self, result: str = "Working summary.\n- Do a thing"):
        self.result = result
        self.calls: list[dict] = []

    async def run_session(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def test_intro_summary_audit_context_carries_user_id():
    """The background asyncio task runs the LLM call attributed to the
    requesting user — identity propagates through the closure (audit trail;
    the execution identity was never wrong, the grounding read was)."""
    from lucent.api.routers.chat import (
        INTRO_SUMMARY_SYSTEM_MESSAGE,
        _generate_intro_summary,
    )

    user = {"id": "11111111-1111-1111-1111-111111111111",
            "organization_id": "22222222-2222-2222-2222-222222222222"}
    engine = _FakeEngine()

    async def run():
        async def fake_gather(u):
            return {
                "is_quiet": False,
                "fingerprint": "fp1",
                "context_text": "data",
                "snapshot": {},
            }

        with patch(
            "lucent.services.chat_intro.gather_work_context",
            side_effect=fake_gather,
        ), \
             patch("lucent.api.routers.chat._resolve_chat_model", return_value="m1"), \
             patch(
                 "lucent.api.routers.chat.chat_intro_summary_model_id",
                 lambda **kw: None,
             ), \
             patch("lucent.model_registry.validate_model", return_value=None), \
             patch("lucent.llm.get_engine_for_model", return_value=engine):
            await _generate_intro_summary(user, None)

    asyncio.run(run())

    assert engine.calls, "engine should have been called once"
    audit_context = engine.calls[0]["audit_context"]
    assert audit_context["user_id"] == user["id"]
    assert audit_context["organization_id"] == user["organization_id"]
    assert audit_context["source"] == "chat.intro_summary"
    assert engine.calls[0]["system_message"] == INTRO_SUMMARY_SYSTEM_MESSAGE


# ═════════════════════════════════════════════════════════════════════
# Goal rollup SQL composition — clearance-driven, org predicate in SQL
# ═════════════════════════════════════════════════════════════════════


class _FakeConnection:
    def __init__(self):
        self.queries: list[str] = []

    async def execute(self, query, *parameters):
        return "OK"

    async def fetchrow(self, query, *parameters):
        self.queries.append(query)
        return None

    async def fetch(self, query, *parameters):
        self.queries.append(query)
        return []

    async def fetchval(self, query, *parameters):
        self.queries.append(query)
        return None


class _FakePool:
    def __init__(self, connection):
        self._connection = connection

    def acquire(self):
        outer = self

        class Context:
            async def __aenter__(self):
                return outer._connection

            async def __aexit__(self, *args):
                return False

        return Context()

    async def fetch(self, query, *parameters):
        return []


def test_goal_rollup_sql_is_clearance_composed():
    """The goals query enforces tenancy via the authorized pool, not Python.

    The rewritten SQL must lean on the clearance probe (default-deny: own
    rows + explicit read grants) and keep the org predicate as defense in
    depth — no bare organization_id-only arm (the leak).
    """
    from lucent.db.memory import MemoryRepository

    connection = _FakeConnection()
    repo = MemoryRepository(_FakePool(connection))

    asyncio.run(
        repo.list_recent_goal_milestones(
            user_id="11111111-1111-1111-1111-111111111111",
            organization_id="22222222-2222-2222-2222-222222222222",
            limit=8,
        )
    )

    sql = connection.queries[-1]  # the rewritten goals read
    # The rollup runs on the memories authorized pool (clearance probe).
    assert "JOIN auth_clearances" in sql
    assert "user_memory_access_condition" not in sql
    # Org predicate stays in SQL as defense in depth ($1; the requesting
    # identity lives only in the pool principal binding).
    assert "organization_id = $1::uuid" in sql
    assert "type = 'goal'" in sql