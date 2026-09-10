"""Regression tests: chat catch-up (intro-summary) user scoping.

The 2026-09-10 catch-up leak: ``gather_work_context``'s goal-milestone rollup
selected goal memories by organization_id alone, so one member's PRIVATE goal
memories grounded another member's catch-up summary. The fix composes the
goals query with ``MemoryRepository.user_memory_access_condition`` (own +
org-granted + daemon-owner-visible) at the query layer — the same tenancy
boundary every other memory read enforces.

Fix design + leak reproduction: ``intro-catchup-leak-map-rca.md`` (repo root).

The scoping tests run against the live Postgres (``DAEMON_DATABASE_URL``) with
a dedicated fixture org and users, cleaned up in teardown. They never touch
production rows. The identity-propagation test for the LLM audit_context is
unit-level (no DB): it drives the real generator through a fake engine and
asserts the requesting user's identity reached the engine's audit metadata.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import patch

import pytest


# ═════════════════════════════════════════════════════════════════════
# Pure composition guard — the access condition must be inside the SQL
# ═════════════════════════════════════════════════════════════════════


def test_goal_rollup_sql_composes_access_condition():
    """The goals query enforces tenancy in SQL, not in Python."""
    from lucent.db.memory import MemoryRepository
    from lucent.services.chat_intro import _goal_rollup_sql

    condition = MemoryRepository.user_memory_access_condition("$2", "$1")
    sql = _goal_rollup_sql(condition)
    assert condition in sql
    # The condition builders hard-code the bare table name — an alias would
    # make the refs unresolvable at execution time.
    assert "FROM memories m\n" not in sql
    assert "FROM memories\n" in sql
    # Params follow the repo convention: $1 = user_id, $2 = organization_id.
    assert "$2::uuid" in sql
    # The boundary is part of the WHERE clause, not a post-filter.
    assert "memories.user_id" in sql


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
# Query-layer tenancy — live Postgres, fixture org, teardown cleanup
# ═════════════════════════════════════════════════════════════════════

TEST_ORG_ID = "00000000-0000-0000-0000-0000000000bb"


@pytest.fixture
def db_url() -> str:
    url = os.environ.get("DAEMON_DATABASE_URL")
    if not url:
        pytest.skip("DAEMON_DATABASE_URL not available in this environment")
    return url


@pytest.fixture
async def pool(db_url):
    asyncpg = pytest.importorskip("asyncpg")
    from lucent.db.pool import _init_connection

    created = await asyncpg.create_pool(
        dsn=db_url, min_size=2, max_size=8, init=_init_connection
    )

    async def fake_get_pool():
        return created

    # gather_work_context resolves the app pool via `from lucent.db import
    # get_pool` at call time — point it at the fixture pool.
    import lucent.db

    original = lucent.db.get_pool
    lucent.db.get_pool = fake_get_pool
    try:
        yield created
    finally:
        lucent.db.get_pool = original
        await created.close()
        cleanup = await asyncpg.connect(dsn=db_url)
        try:
            # Fixture rows only. Grants cascade from their memory; users and
            # the org have no dependency back to us.
            await cleanup.execute(
                "DELETE FROM memory_access_grants WHERE organization_id = $1",
                uuid.UUID(TEST_ORG_ID),
            )
            await cleanup.execute(
                "DELETE FROM memories WHERE organization_id = $1",
                uuid.UUID(TEST_ORG_ID),
            )
            await cleanup.execute(
                "DELETE FROM users WHERE organization_id = $1",
                uuid.UUID(TEST_ORG_ID),
            )
            await cleanup.execute(
                "DELETE FROM organizations WHERE id = $1",
                uuid.UUID(TEST_ORG_ID),
            )
        finally:
            await cleanup.close()


@pytest.fixture
async def scoping_env(pool):
    """Org with two member users and one private goal per user.

    Returns the user dicts exactly as the chat surface passes them to
    ``gather_work_context``.
    """
    org_id = uuid.UUID(TEST_ORG_ID)
    await pool.execute(
        "INSERT INTO organizations (id, name) VALUES ($1, 'intro-scoping-test-org')",
        org_id,
    )
    user_a = await pool.fetchrow(
        """INSERT INTO users (external_id, provider, email, display_name,
                               organization_id, role)
           VALUES ('intro-scope-user-a', 'local', 'user-a@intro-scope.test',
                   'User A', $1, 'member')
           RETURNING id""",
        org_id,
    )
    user_b = await pool.fetchrow(
        """INSERT INTO users (external_id, provider, email, display_name,
                               organization_id, role)
           VALUES ('intro-scope-user-b', 'local', 'user-b@intro-scope.test',
                   'User B', $1, 'member')
           RETURNING id""",
        org_id,
    )
    user_a_id = user_a["id"]
    user_b_id = user_b["id"]

    goal_a = await pool.fetchrow(
        """INSERT INTO memories (username, type, content, user_id, organization_id)
           VALUES ('user-a', 'goal', 'USER-A PRIVATE GOAL alpha',
                   $1, $2) RETURNING id""",
        user_a_id,
        org_id,
    )
    goal_b = await pool.fetchrow(
        """INSERT INTO memories (username, type, content, user_id, organization_id)
           VALUES ('user-b', 'goal', 'USER-B PRIVATE GOAL beta',
                   $1, $2) RETURNING id""",
        user_b_id,
        org_id,
    )
    return {
        "org_id": str(org_id),
        "user_a": {"id": str(user_a_id), "organization_id": str(org_id),
                   "role": "member"},
        "user_b": {"id": str(user_b_id), "organization_id": str(org_id),
                   "role": "member"},
        "goal_a_id": str(goal_a["id"]),
        "goal_b_id": str(goal_b["id"]),
    }


class TestGoalScopingQueryLayer:
    async def test_cross_user_goal_never_gounds_another_members_summary(
        self, pool, scoping_env
    ):
        """The leak regression: user A's catch-up must not contain user B's
        private goal — enforced by the goals SQL itself."""
        from lucent.services.chat_intro import gather_work_context

        env = scoping_env
        for requester in (env["user_a"], env["user_b"]):
            context = await gather_work_context(requester)
            other_title = (
                "USER-B PRIVATE GOAL beta"
                if requester is env["user_a"]
                else "USER-A PRIVATE GOAL alpha"
            )
            assert other_title not in context["context_text"]
            other_id = env["goal_b_id"] if requester is env["user_a"] else env["goal_a_id"]
            assert other_id not in context["snapshot"]["goals"]
            own_id = env["goal_a_id"] if requester is env["user_a"] else env["goal_b_id"]
            own_ids = [g["id"] for g in context["snapshot"]["goals"]]
            assert own_id in own_ids

    async def test_org_granted_goal_is_visible_to_grantee(self, pool, scoping_env):
        """Shared memory is granted access, not org-visible by default — the
        grant mechanism, not a bare user_id filter, is the boundary."""
        from lucent.db.memory import MemoryRepository
        from lucent.services.chat_intro import gather_work_context

        env = scoping_env
        org_id = uuid.UUID(TEST_ORG_ID)
        await pool.execute(
            """INSERT INTO memory_access_grants
                   (memory_id, organization_id, grantee_type, grantee_user_id)
               VALUES ($1, $2, 'user', $3)""",
            uuid.UUID(env["goal_b_id"]),
            org_id,
            uuid.UUID(env["user_a"]["id"]),
        )
        # Sanity: the canonical condition admits the granted goal for user A.
        condition = MemoryRepository.user_memory_access_condition("$2", "$1")
        row = await pool.fetchrow(
            f"""SELECT memories.id FROM memories
                WHERE memories.id = $3::uuid
                  AND {condition}""",
            uuid.UUID(env["user_a"]["id"]),
            org_id,
            uuid.UUID(env["goal_b_id"]),
        )
        assert row is not None

        context = await gather_work_context(env["user_a"])
        granted_ids = [g["id"] for g in context["snapshot"]["goals"]]
        assert env["goal_b_id"] in granted_ids

    async def test_grant_independence_condition_only_admits_grantee(
        self, pool, scoping_env
    ):
        """A grant to user A must not widen user B's boundary: a third
        user's goal granted to user A is invisible to user B, and A's
        ownership of it is the only channel."""
        from lucent.db.memory import MemoryRepository
        from lucent.services.chat_intro import _goal_rollup_sql

        env = scoping_env
        org_id = uuid.UUID(TEST_ORG_ID)
        user_c = await pool.fetchrow(
            """INSERT INTO users (external_id, provider, email, display_name,
                                   organization_id, role)
               VALUES ('intro-scope-user-c', 'local', 'user-c@intro-scope.test',
                       'User C', $1, 'member')
               RETURNING id""",
            org_id,
        )
        goal_c = await pool.fetchrow(
            """INSERT INTO memories (username, type, content, user_id, organization_id)
               VALUES ('user-c', 'goal', 'USER-C PRIVATE GOAL gamma',
                       $1, $2) RETURNING id""",
            user_c["id"],
            org_id,
        )
        await pool.execute(
            """INSERT INTO memory_access_grants
                   (memory_id, organization_id, grantee_type, grantee_user_id)
               VALUES ($1, $2, 'user', $3)""",
            goal_c["id"],
            org_id,
            uuid.UUID(env["user_a"]["id"]),
        )
        condition = MemoryRepository.user_memory_access_condition("$2", "$1")
        rows = await pool.fetch(
            _goal_rollup_sql(condition),
            uuid.UUID(env["user_b"]["id"]),
            org_id,
            16,
        )
        b_ids = {str(r["id"]) for r in rows}
        # B sees only its own goal — C's goal granted to A does not leak,
        # and the grant on C's goal does not widen B's boundary either.
        assert b_ids == {env["goal_b_id"]}

    async def test_owner_daemon_authored_goal_asymmetry(self, pool, scoping_env):
        """An org member cannot read a daemon-authored goal via the rollup,
        while the org owner can — matching user_memory_access_condition."""
        from lucent.services.chat_intro import gather_work_context

        env = scoping_env
        org_id = uuid.UUID(TEST_ORG_ID)
        daemon_user = await pool.fetchrow(
            """INSERT INTO users (external_id, provider, email, display_name,
                                   organization_id, role)
               VALUES ('daemon-service:intro-scope', 'local',
                       'daemon@intro-scope.test', 'Daemon', $1, 'daemon')
               RETURNING id""",
            org_id,
        )
        goal_d = await pool.fetchrow(
            """INSERT INTO memories (username, type, content, user_id,
                                     organization_id, tags)
               VALUES ('Lucent Daemon', 'goal', 'DAEMON GOAL delta',
                       $1, $2, $3) RETURNING id""",
            daemon_user["id"],
            org_id,
            ["daemon"],
        )
        context_member = await gather_work_context(env["user_a"])
        assert "DAEMON GOAL delta" not in context_member["context_text"]
        member_ids = [g["id"] for g in context_member["snapshot"]["goals"]]
        assert str(goal_d["id"]) not in member_ids

        owner_row = await pool.fetchrow(
            """UPDATE users SET role = 'owner'
               WHERE id = $1::uuid RETURNING role""",
            uuid.UUID(env["user_a"]["id"]),
        )
        owner = dict(env["user_a"])
        owner["role"] = owner_row["role"]
        context_owner = await gather_work_context(owner)
        owner_ids = [g["id"] for g in context_owner["snapshot"]["goals"]]
        assert str(goal_d["id"]) in owner_ids
        # The owner still cannot see the other member's private goal.
        assert env["goal_b_id"] not in owner_ids

    async def test_deleted_goal_never_appears(self, pool, scoping_env):
        from lucent.services.chat_intro import gather_work_context

        env = scoping_env
        await pool.execute(
            "UPDATE memories SET deleted_at = NOW() WHERE id = $1::uuid",
            uuid.UUID(env["goal_a_id"]),
        )
        context = await gather_work_context(env["user_a"])
        ids = [g["id"] for g in context["snapshot"]["goals"]]
        assert env["goal_a_id"] not in ids