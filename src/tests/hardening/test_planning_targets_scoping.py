"""Regression tests: planning-targets endpoint caller-identity scoping.

The 2026-09-10 planning-targets leak (second confirmed instance of the
tenant-isolation gap): ``GET /api/requests/planning-targets`` returned
org-wide goals to any caller whose API key was not user-scoped, and
honored a caller-supplied ``?user_id=`` verbatim — so one member's
private goal memories appeared in another member's planner view.

The fix scopes the handler by caller identity, mirroring the router's
own ``_request_visibility_args`` convention: daemon-service callers with
an unscoped key keep the org-wide view (the daemon's unscoped fallback
path legitimately serves the whole org); every other caller — human API
keys, members, admins, owners — sees only their own goals; user-scoped
keys (per-user cognitive fan-out) are still forced to the scoped user.

Leak reproduction: ``planning-targets-leak-repro.md`` (user file
44e4255d-b28a-4641-a5f1-d2041f13a04c, request 47ce1a21).

The scoping tests run against the live Postgres (``DAEMON_DATABASE_URL``)
with a dedicated fixture org and users, cleaned up in teardown. They
never touch production rows. The handler is invoked directly with
constructed ``CurrentUser`` identities — the API-key → identity plumbing
in ``deps.get_current_user`` is unchanged existing code, so the
regression lives at the exact unit where the fix landed.
"""

from __future__ import annotations

import os
import uuid

import pytest

from lucent.api.deps import CurrentUser


# ═════════════════════════════════════════════════════════════════════
# Fixtures — dedicated org, two members + one owner, one goal each
# ═════════════════════════════════════════════════════════════════════


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
    try:
        yield created
    finally:
        await created.close()


@pytest.fixture
async def scoping_env(pool):
    """Org with a member, a second member, and an owner — one goal each."""
    org_id = uuid.uuid4()
    await pool.execute(
        "INSERT INTO organizations (id, name) VALUES ($1, 'planning-targets-test-org')",
        org_id,
    )
    user_a = await pool.fetchrow(
        """INSERT INTO users (external_id, provider, email, display_name,
                               organization_id, role)
           VALUES ('pt-scope-user-a', 'local', 'user-a@pt-scope.test',
                   'PT User A', $1, 'member')
           RETURNING id""",
        org_id,
    )
    user_b = await pool.fetchrow(
        """INSERT INTO users (external_id, provider, email, display_name,
                               organization_id, role)
           VALUES ('pt-scope-user-b', 'local', 'user-b@pt-scope.test',
                   'PT User B', $1, 'member')
           RETURNING id""",
        org_id,
    )
    user_owner = await pool.fetchrow(
        """INSERT INTO users (external_id, provider, email, display_name,
                               organization_id, role)
           VALUES ('pt-scope-user-owner', 'local', 'owner@pt-scope.test',
                   'PT Owner', $1, 'owner')
           RETURNING id""",
        org_id,
    )
    user_a_id, user_b_id, user_owner_id = (
        user_a["id"],
        user_b["id"],
        user_owner["id"],
    )

    # Open-ended goals (no milestones) are the class the endpoint returns
    # with next_milestone_index=None — exactly the rows leaked pre-fix.
    goal_a = await pool.fetchrow(
        """INSERT INTO memories (username, type, content, user_id, organization_id)
           VALUES ('pt-user-a', 'goal', 'PT-SCOPE USER-A PRIVATE GOAL alpha',
                   $1, $2) RETURNING id""",
        user_a_id,
        org_id,
    )
    goal_b = await pool.fetchrow(
        """INSERT INTO memories (username, type, content, user_id, organization_id)
           VALUES ('pt-user-b', 'goal', 'PT-SCOPE USER-B PRIVATE GOAL beta',
                   $1, $2) RETURNING id""",
        user_b_id,
        org_id,
    )
    goal_owner = await pool.fetchrow(
        """INSERT INTO memories (username, type, content, user_id, organization_id)
           VALUES ('pt-owner', 'goal', 'PT-SCOPE OWNER PRIVATE GOAL gamma',
                   $1, $2) RETURNING id""",
        user_owner_id,
        org_id,
    )
    return {
        "org_id": str(org_id),
        "user_a_id": str(user_a_id),
        "user_b_id": str(user_b_id),
        "user_owner_id": str(user_owner_id),
        "goal_a_id": str(goal_a["id"]),
        "goal_b_id": str(goal_b["id"]),
        "goal_owner_id": str(goal_owner["id"]),
    }


@pytest.fixture(autouse=True)
async def cleanup_scoping_env(db_url):
    """Delete the fixture org (cascades users, memories, grants)."""
    created_ids: list[str] = []
    yield created_ids
    import asyncpg

    conn = await asyncpg.connect(dsn=db_url)
    try:
        for org_id in created_ids:
            await conn.execute(
                "DELETE FROM memories WHERE organization_id = $1",
                uuid.UUID(org_id),
            )
            await conn.execute(
                "DELETE FROM users WHERE organization_id = $1",
                uuid.UUID(org_id),
            )
            await conn.execute(
                "DELETE FROM organizations WHERE id = $1",
                uuid.UUID(org_id),
            )
    finally:
        await conn.close()


@pytest.fixture
def record_org(scoping_env, cleanup_scoping_env):
    """Register the fixture org for teardown after the test body runs."""
    cleanup_scoping_env.append(scoping_env["org_id"])
    return scoping_env


def _human(user_id: str, org_id: str, role: str = "member") -> CurrentUser:
    """A human caller authenticated with an unscoped personal API key."""
    return CurrentUser(
        id=uuid.UUID(user_id),
        organization_id=uuid.UUID(org_id),
        role=role,
        email="caller@pt-scope.test",
        display_name="PT Caller",
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )


def _daemon_service(
    user_id: str, org_id: str, *, scoped_to: str | None = None
) -> CurrentUser:
    """The daemon-service caller, optionally with a user-scoped key."""
    return CurrentUser(
        id=uuid.UUID(user_id),
        organization_id=uuid.UUID(org_id),
        role="daemon",
        email="daemon@lucent.local",
        display_name="Lucent Daemon",
        auth_method="api_key",
        api_key_scopes=["read", "write"],
        external_id=f"daemon-service:{org_id}",
        memory_scope="user" if scoped_to else None,
        memory_scope_user_id=uuid.UUID(scoped_to) if scoped_to else None,
    )


async def _call_handler(pool, user: CurrentUser, user_id: str | None = None):
    from lucent.api.routers.requests import list_planning_targets

    return await list_planning_targets(
        user=user,
        pool=pool,
        user_id=user_id,
        limit=50,
    )


# ═════════════════════════════════════════════════════════════════════
# The leak regressions
# ═════════════════════════════════════════════════════════════════════


class TestPlanningTargetsCallerScoping:
    async def test_member_never_sees_other_users_goals(self, pool, record_org):
        """THE LEAK: an unscoped human key must see only its own goals.

        Pre-fix this returned the whole org — user B's private goal
        leaked into user A's planner view.
        """
        env = record_org
        result = await _call_handler(pool, _human(env["user_a_id"], env["org_id"]))
        returned = {t["goal_id"] for t in result["targets"]}
        assert returned == {env["goal_a_id"]}, (
            f"cross-user goal leak: user A saw {returned}, "
            f"expected only its own goal {env['goal_a_id']}"
        )
        assert result["count"] == 1

    async def test_owner_scoped_to_own_goals(self, pool, record_org):
        """Owners get their own goals, not org-wide (list_requests convention)."""
        env = record_org
        result = await _call_handler(
            pool, _human(env["user_owner_id"], env["org_id"], role="owner")
        )
        returned = {t["goal_id"] for t in result["targets"]}
        assert returned == {env["goal_owner_id"]}

    async def test_caller_supplied_user_id_ignored_for_humans(
        self, pool, record_org
    ):
        """?user_id= forgery must not enumerate another user's goals."""
        env = record_org
        result = await _call_handler(
            pool,
            _human(env["user_a_id"], env["org_id"]),
            user_id=env["user_b_id"],
        )
        returned = {t["goal_id"] for t in result["targets"]}
        assert env["goal_b_id"] not in returned
        assert returned == {env["goal_a_id"]}

    async def test_scoped_key_forced_to_scope_user(self, pool, record_org):
        """Per-user fan-out: a user-scoped key sees only the scoped user's
        goals — even when the caller passes a different ?user_id=."""
        env = record_org
        result = await _call_handler(
            pool,
            _daemon_service(
                env["user_owner_id"], env["org_id"], scoped_to=env["user_a_id"]
            ),
            user_id=env["user_b_id"],
        )
        returned = {t["goal_id"] for t in result["targets"]}
        assert returned == {env["goal_a_id"]}

    async def test_daemon_unscoped_key_still_sees_org_wide(self, pool, record_org):
        """The daemon-facing path is unaffected: the unscoped daemon-service
        caller (RequestAPI's API_HEADERS fallback) keeps the org-wide view."""
        env = record_org
        result = await _call_handler(
            pool, _daemon_service(env["user_owner_id"], env["org_id"])
        )
        returned = {t["goal_id"] for t in result["targets"]}
        assert {
            env["goal_a_id"],
            env["goal_b_id"],
            env["goal_owner_id"],
        } <= returned

    async def test_empty_result_for_user_without_goals(self, pool, record_org):
        """A human caller with no goals gets zero targets — not others' goals."""
        env = record_org
        # A caller whose id owns nothing in this org — even a fabricated
        # UUID that exists nowhere — must get zero targets, not org-wide.
        ghost = _human(str(uuid.uuid4()), env["org_id"])
        empty = await _call_handler(pool, ghost)
        assert empty["targets"] == []
        assert empty["count"] == 0
