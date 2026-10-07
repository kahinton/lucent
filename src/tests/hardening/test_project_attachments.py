"""Project-attachment regression tests (Projects round 2, migration 112).

Covers the A-scope attachment feature end to end at the two layers where the
scoping lives:

1. Repository — (org, user)-scoped attach/detach/list for memories and
   handoffs (user_interactions), mirroring the projects v1 session/file
   contract: the target project must be owned by the caller, member rows
   must belong to the caller's scope, foreign ids are silently not-moved
   (moved-count reports the truth), unscoped calls raise TypeError loudly,
   and fail-closed context threading (``get_context_for_session``) returns
   attached-memory ids scoped to the chat's own project.

2. Hook rank boost — the message-memory-lookup hook reorders candidates so
   project-attached memories fill the cap first, without widening the
   candidate set: the search query/limit are unchanged, threshold/dedup/
   budget rules are untouched, and the injected set stays limited to what
   the (already org+user-scoped) search returned. Boost is driven entirely
   by the session caller's threaded ``_project_context`` — hooks never touch
   the DB, and an absent/malformed key means plain "no boost".

Adversarial coverage follows Kyle's standing data-layer isolation directive:
every tenant-isolation claim in the repository is exercised adversarially
(cross-user, cross-org, unscoped-call, foreign-project-id).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lucent.db.projects import ProjectNotFoundError, ProjectRepository
from lucent.llm.hooks import HookManager


# ═════════════════════════════════════════════════════════════════════
# 1. Repository scoping — scratch-Postgres lineage 001..112
# ═════════════════════════════════════════════════════════════════════


@pytest.fixture
def db_url() -> str:
    import os

    url = os.environ.get("DAEMON_DATABASE_URL")
    if not url:
        pytest.skip("DAEMON_DATABASE_URL not available in this environment")
    # The scratch lineage lives on the local Postgres cluster (17/main,
    # port 5432) — start it with `pg_ctlcluster 17 main start` when the
    # environment booted without it. Skip, never error, when unreachable.
    import asyncpg

    async def _reachable() -> bool:
        try:
            conn = await asyncpg.connect(
                "postgresql://root@/postgres?host=/var/run/postgresql", timeout=3
            )
            await conn.close()
            return True
        except Exception:
            return False

    import asyncio

    try:
        if not asyncio.run(_reachable()):
            pytest.skip("local scratch Postgres cluster not reachable")
    except RuntimeError:
        pass
    return url


@pytest.fixture
async def pool(db_url):
    asyncpg = pytest.importorskip("asyncpg")
    from lucent.db.pool import init_db, _run_migrations

    # Isolated scratch database on the local Postgres cluster (the
    # daemon DSN's role cannot CREATE DATABASE), provisioned by the
    # application's own migration chain, dropped in teardown. Zero
    # live-DB writes; the scratch cluster is harness-only infrastructure.
    admin_dsn = "postgresql://root@/postgres?host=/var/run/postgresql"
    scratch_name = f"proj_attach_test_{abs(hash(db_url)) % 10_000_000}"
    scratch_dsn = f"postgresql://root@/{scratch_name}?host=/var/run/postgresql"
    admin = await asyncpg.connect(dsn=admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')
        await admin.execute(f'CREATE DATABASE "{scratch_name}"')
    finally:
        await admin.close()
    created = await asyncpg.create_pool(dsn=scratch_dsn, min_size=1, max_size=4)
    await init_db(scratch_dsn, run_migrations=False)
    await _run_migrations(created)
    try:
        yield created
    finally:
        await created.close()
        cleanup = await asyncpg.connect(dsn=admin_dsn)
        try:
            await cleanup.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                scratch_name,
            )
            await cleanup.execute(f'DROP DATABASE "{scratch_name}"')
        finally:
            await cleanup.close()


@pytest.fixture
async def env(pool):
    """Org with two members (A/B), a project for A, and a foreign org+user."""
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'proj-attach-test') RETURNING id"
        )
        user_a = await conn.fetchrow(
            """INSERT INTO users (external_id, provider, email, display_name,
                                   organization_id, role)
               VALUES ('proj-attach-a', 'local', 'a@proj-attach.test',
                       'Attach User A', $1, 'member') RETURNING id""",
            org_id,
        )
        user_b = await conn.fetchrow(
            """INSERT INTO users (external_id, provider, email, display_name,
                                   organization_id, role)
               VALUES ('proj-attach-b', 'local', 'b@proj-attach.test',
                       'Attach User B', $1, 'member') RETURNING id""",
            org_id,
        )
        foreign_org = await conn.fetchval(
            "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'proj-attach-foreign') RETURNING id"
        )
        foreign_user = await conn.fetchrow(
            """INSERT INTO users (external_id, provider, email, display_name,
                                   organization_id, role)
               VALUES ('proj-attach-x', 'local', 'x@proj-attach.test',
                       'Attach User X', $1, 'member') RETURNING id""",
            foreign_org,
        )
        project = await conn.fetchrow(
            """INSERT INTO projects (organization_id, user_id, name)
               VALUES ($1, $2, 'Apollo') RETURNING *""",
            org_id,
            user_a["id"],
        )
    return {
        "org_id": org_id,
        "user_a": user_a["id"],
        "user_b": user_b["id"],
        "foreign_org": foreign_org,
        "foreign_user": foreign_user["id"],
        "project": dict(project),
    }


def _repo(pool) -> ProjectRepository:
    return ProjectRepository(pool)


async def _seed_memory(
    pool, org_id, user_id, content="signal", type="goal", importance=7, project_id=None
):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO memories (username, type, content, tags, importance,
                                      user_id, organization_id, project_id)
               VALUES ('Attach User A', $2, $1, '{}', $3, $4, $5, $6) RETURNING id""",
            content,
            type,
            importance,
            user_id,
            org_id,
            project_id,
        )


async def _seed_interaction(pool, org_id, user_id, title="handoff", project_id=None):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO user_interactions (organization_id, user_id, source,
                                   interaction_type, status, priority, title, body,
                                   project_id)
               VALUES ($1, $2, 'daemon', 'handoff', 'open', 'medium', $3, 'body text', $4)
               RETURNING id""",
            org_id,
            user_id,
            title,
            project_id,
        )


# ── attach / detach (memories) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attach_memory_roundtrip(pool, env):
    repo = _repo(pool)
    memory_id = await _seed_memory(pool, env["org_id"], env["user_a"])
    assert memory_id
    moved = await repo.set_memories_project_bulk(
        [memory_id], env["project"]["id"],
        org_id=env["org_id"], user_id=env["user_a"],
    )
    assert moved == 1
    attached = await repo.list_memories_in_project(
        env["project"]["id"], org_id=env["org_id"], user_id=env["user_a"]
    )
    assert [str(m["id"]) for m in attached["items"]] == [str(memory_id)]
    assert attached["total_count"] == 1
    detached = await repo.set_memories_project_bulk(
        [memory_id], None, org_id=env["org_id"], user_id=env["user_a"]
    )
    assert detached == 1
    assert (await repo.list_memories_in_project(
        env["project"]["id"], org_id=env["org_id"], user_id=env["user_a"]
    ))["items"] == []


@pytest.mark.asyncio
async def test_attach_memory_cross_user_blocked(pool, env):
    """User B cannot attach user A's memory to B's view — even with A's id."""
    repo = _repo(pool)
    memory_id = await _seed_memory(pool, env["org_id"], env["user_a"])
    moved = await repo.set_memories_project_bulk(
        [memory_id], env["project"]["id"],
        org_id=env["org_id"], user_id=env["user_b"],
    )
    assert moved == 0
    row = await pool.fetchval(
        "SELECT project_id FROM memories WHERE id = $1", memory_id
    )
    assert row is None


@pytest.mark.asyncio
async def test_attach_memory_cross_org_blocked(pool, env):
    """A foreign-org caller cannot attach any memory to a project it can't see.

    Both scoping layers must hold: the project ownership check rejects the
    foreign pair, and even an attach request naming the caller's own memory
    cannot move rows whose org differs from the caller's.
    """
    repo = _repo(pool)
    memory_id = await _seed_memory(pool, env["org_id"], env["user_a"])
    # Bulk moves follow the silent not-moved convention (moved-count reports
    # the truth): a foreign project id is invisible to the caller, so the
    # count is 0 and nothing changes.
    moved = await repo.set_memories_project_bulk(
        [memory_id], env["project"]["id"],
        org_id=env["foreign_org"], user_id=env["foreign_user"],
    )
    assert moved == 0
    row = await pool.fetchval(
        "SELECT project_id FROM memories WHERE id = $1", memory_id
    )
    assert row is None
    # Foreign-org user attaching THEIR OWN memory: the EXISTS project check
    # (same org as the memory) fails because the project lives in another org.
    foreign_memory = await _seed_memory(pool, env["foreign_org"], env["foreign_user"])
    moved = await repo.set_memories_project_bulk(
        [foreign_memory], env["project"]["id"],
        org_id=env["foreign_org"], user_id=env["foreign_user"],
    )
    assert moved == 0


@pytest.mark.asyncio
async def test_unscoped_attach_raises(pool, env):
    repo = _repo(pool)
    memory_id = await _seed_memory(pool, env["org_id"], env["user_a"])
    with pytest.raises(TypeError):
        await repo.set_memories_project_bulk(
            [memory_id], env["project"]["id"], org_id=env["org_id"], user_id=None
        )
    with pytest.raises(TypeError):
        await repo.set_memories_project_bulk(
            [memory_id], env["project"]["id"], org_id=None, user_id=env["user_a"]
        )


@pytest.mark.asyncio
async def test_attach_memory_foreign_project_id_raises_or_noops(pool, env):
    """An unknown/foreign project id never silently attaches rows."""
    repo = _repo(pool)
    memory_id = await _seed_memory(pool, env["org_id"], env["user_a"])
    moved = await repo.set_memories_project_bulk(
        [memory_id], env["foreign_user"],  # a user id, not a project id
        org_id=env["org_id"], user_id=env["user_a"],
    )
    assert moved == 0
    row = await pool.fetchval(
        "SELECT project_id FROM memories WHERE id = $1", memory_id
    )
    assert row is None


# ── unfiled listing (memories) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unfiled_memories_exclude_archived_and_attached(pool, env):
    repo = _repo(pool)
    live_id = await _seed_memory(pool, env["org_id"], env["user_a"], content="live")
    attached_id = await _seed_memory(pool, env["org_id"], env["user_a"], content="attached")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET deleted_at = NOW() WHERE id = $1", attached_id
        )
    other_id = await _seed_memory(pool, env["org_id"], env["user_b"], content="other user")
    unfiled = await repo.list_unfiled_memories(
        org_id=env["org_id"], user_id=env["user_a"]
    )
    ids = {str(m["id"]) for m in unfiled["items"]}
    assert str(live_id) in ids
    assert str(attached_id) not in ids
    assert str(other_id) not in ids  # scoped to the caller
    # Attach the live one; it must disappear from the picker list.
    await repo.set_memories_project_bulk(
        [live_id], env["project"]["id"], org_id=env["org_id"], user_id=env["user_a"]
    )
    unfiled_after = await repo.list_unfiled_memories(
        org_id=env["org_id"], user_id=env["user_a"]
    )
    assert str(live_id) not in {str(m["id"]) for m in unfiled_after["items"]}


@pytest.mark.asyncio
async def test_list_memories_in_project_requires_owned_project(pool, env):
    repo = _repo(pool)
    memory_id = await _seed_memory(pool, env["org_id"], env["user_a"])
    await repo.set_memories_project_bulk(
        [memory_id], env["project"]["id"], org_id=env["org_id"], user_id=env["user_a"]
    )
    with pytest.raises(ProjectNotFoundError):
        await repo.list_memories_in_project(
            env["project"]["id"], org_id=env["foreign_org"], user_id=env["foreign_user"]
        )


# ── handoffs (user_interactions) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_attach_personal_handoff_roundtrip(pool, env):
    repo = _repo(pool)
    interaction_id = await _seed_interaction(pool, env["org_id"], env["user_a"])
    moved = await repo.set_interactions_project_bulk(
        [interaction_id], env["project"]["id"],
        org_id=env["org_id"], user_id=env["user_a"],
    )
    assert moved == 1
    attached = await repo.list_interactions_in_project(
        env["project"]["id"], org_id=env["org_id"], user_id=env["user_a"]
    )
    assert [str(i["id"]) for i in attached["items"]] == [str(interaction_id)]
    # A different member of the same org cannot even list the project
    # (fail-closed 404-equivalent at the repo layer), so B never sees A's
    # personal handoff through this path.
    with pytest.raises(ProjectNotFoundError):
        await repo.list_interactions_in_project(
            env["project"]["id"], org_id=env["org_id"], user_id=env["user_b"]
        )


@pytest.mark.asyncio
async def test_attach_handoff_cross_user_blocked(pool, env):
    repo = _repo(pool)
    interaction_id = await _seed_interaction(pool, env["org_id"], env["user_a"])
    moved = await repo.set_interactions_project_bulk(
        [interaction_id], env["project"]["id"],
        org_id=env["org_id"], user_id=env["user_b"],
    )
    assert moved == 0
    row = await pool.fetchval(
        "SELECT project_id FROM user_interactions WHERE id = $1", interaction_id
    )
    assert row is None


@pytest.mark.asyncio
async def test_unfiled_handoffs_pick_open_statuses_only(pool, env):
    repo = _repo(pool)
    open_id = await _seed_interaction(pool, env["org_id"], env["user_a"], title="open one")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_interactions SET status = 'dismissed' WHERE id = $1", open_id
        )
    unfiled = await repo.list_unfiled_interactions(
        org_id=env["org_id"], user_id=env["user_a"]
    )
    assert str(open_id) not in {str(i["id"]) for i in unfiled["items"]}


# ── project context threading (injection data source) ──────────────────────


@pytest.mark.asyncio
async def test_get_context_for_session_carries_scoped_memory_ids(pool, env):
    repo = _repo(pool)
    session_id = await pool.fetchval(
        """INSERT INTO llm_sessions (organization_id, user_id, kind, status, title, project_id)
           VALUES ($1, $2, 'chat', 'active', 'ctx chat', $3) RETURNING id""",
        env["org_id"], env["user_a"], env["project"]["id"],
    )
    attached_id = await _seed_memory(
        pool, env["org_id"], env["user_a"], project_id=env["project"]["id"]
    )
    foreign_id = await _seed_memory(pool, env["org_id"], env["user_b"])
    context = await repo.get_context_for_session(
        session_id, org_id=env["org_id"], user_id=env["user_a"]
    )
    assert context is not None
    assert str(attached_id) in context["memory_ids"]
    assert str(foreign_id) not in context["memory_ids"]
    # Cross-user read of the same session sees nothing at all.
    assert await repo.get_context_for_session(
        session_id, org_id=env["org_id"], user_id=env["user_b"]
    ) is None


# ═════════════════════════════════════════════════════════════════════
# 2. Message-memory-lookup rank boost (injection touch)
# ═════════════════════════════════════════════════════════════════════


class FakeBridge:
    """Records call_tool invocations and replays canned search results."""

    def __init__(self, results: list[dict[str, Any]]):
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, payload: dict[str, Any]) -> str:
        self.calls.append((name, payload))
        if name == "get_memories":
            memories = [
                {"id": memory_id, "content": content}
                for memory_id, content in self.full_contents.items()
                if memory_id in payload.get("memory_ids", [])
            ] if hasattr(self, "full_contents") else []
            return json.dumps({"memories": memories})
        return json.dumps({"memories": self.results})


def _memory(mid: str, score: float, content: str = "x") -> dict[str, Any]:
    return {"id": mid, "content": content, "tags": [], "similarity_score": score}


def _hook_metadata(execution) -> dict[str, Any]:
    return execution.metadata


@pytest.mark.asyncio
async def test_boost_moves_project_memory_into_capped_injection():
    """A project-attached memory below the cap displaces a lower-scoring
    non-project memory — boost is order-only, never additive."""
    # Scores inside the injection score-spread window (max_score_drop 0.15:
    # every candidate must sit within 0.15 of the window's best — the rule
    # came after these hardening tests; intent unchanged, just calibrated).
    project_mem = _memory("33333333-3333-3333-3333-333333333333", 0.72, "project goal memory")
    strong_mem = _memory("44444444-4444-4444-4444-444444444444", 0.80, "strong unrelated memory")
    weak_mem = _memory("55555555-5555-5555-5555-555555555555", 0.68, "weak unrelated memory")
    bridge = FakeBridge([strong_mem, weak_mem, project_mem])
    state: dict[str, Any] = {
        "_project_context": {
            "project": {"id": "p", "name": "Apollo"},
            "files": [],
            "memory_ids": ["33333333-3333-3333-3333-333333333333"],
        },
    }
    manager = HookManager(session_state=state)
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "how does threshold filtering work?"}],
        memory_bridge=bridge,
    )
    assert len(outcome.injectable_executions) == 1
    execution = outcome.injectable_executions[0]
    # Cap is 3 (default max_memories=3 in the default hook config) and all
    # three candidates qualify — nothing is displaced here; the assertion is
    # on ORDER: the project memory fills a slot first.
    assert execution.metadata["memory_count"] == 3
    assert "project goal memory" in execution.text
    assert "strong unrelated memory" in execution.text
    assert "weak unrelated memory" in execution.text
    # Injected order: project memory first, server order preserved otherwise.
    assert execution.metadata["memory_ids"][0].startswith("33333333")
    assert execution.metadata["memory_ids"][1].startswith("44444444")
    # Search payload unchanged by the boost.
    search = [p for name, p in bridge.calls if name == "search_memories_full"][0]
    assert search["query"] == "threshold filtering work"
    assert search["limit"] == 10


@pytest.mark.asyncio
async def test_boost_off_without_project_context():
    """No threaded project context → no reordering, legacy behavior intact."""
    strong_mem = _memory("44444444-4444-4444-4444-444444444444", 0.80, "strong unrelated memory")
    weak_mem = _memory("55555555-5555-5555-5555-555555555555", 0.70, "weak unrelated memory")
    project_mem = _memory("33333333-3333-3333-3333-333333333333", 0.68, "project goal memory")
    bridge = FakeBridge([strong_mem, weak_mem, project_mem])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "how does threshold filtering work?"}],
        memory_bridge=bridge,
    )
    execution = outcome.injectable_executions[0]
    # Cap is 3 (default max_memories=3 in the default hook config) and all
    # three qualify; without a boost the server's ranking order wins.
    assert execution.metadata["memory_count"] == 3
    assert execution.metadata["memory_ids"][0].startswith("44444444")
    assert execution.metadata["memory_ids"][-1].startswith("33333333")


@pytest.mark.asyncio
async def test_boost_never_injects_non_candidate_ids():
    """The id set only reorders existing candidates — never adds new ones."""
    bridge = FakeBridge([
        _memory("66666666-6666-6666-6666-666666666666", 0.55, "returned candidate"),
    ])
    state: dict[str, Any] = {
        "_project_context": {
            "project": {"id": "p", "name": "Apollo"},
            "files": [],
            # An id the search never returned (foreign row or stale attach).
            "memory_ids": ["99999999-9999-9999-9999-999999999999"],
        },
    }
    manager = HookManager(session_state=state)
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "how does threshold filtering work?"}],
        memory_bridge=bridge,
    )
    execution = outcome.injectable_executions[0]
    assert execution.metadata["memory_count"] == 1
    assert execution.metadata["memory_ids"] == ["66666666"]
    assert "99999999" not in execution.text


@pytest.mark.asyncio
async def test_boost_respects_threshold_and_dedup():
    """Boost reorders only; threshold and dedup still gate injection."""
    project_mem = _memory("33333333-3333-3333-3333-333333333333", 0.10, "project but weak")
    bridge = FakeBridge([
        _memory("77777777-7777-7777-7777-777777777777", 0.55, "strong one"),
        project_mem,
    ])
    state: dict[str, Any] = {
        "_project_context": {
            "project": {"id": "p", "name": "Apollo"},
            "files": [],
            "memory_ids": ["33333333-3333-3333-3333-333333333333"],
        },
        "injected_memory_ids": {"77777777-7777-7777-7777-777777777777"},
    }
    manager = HookManager(session_state=state)
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "how does threshold filtering work?"}],
        memory_bridge=bridge,
    )
    execution = outcome.injectable_executions[0]
    # The boosted project memory is below threshold → still not injected;
    # the deduped strong one is skipped; result is a silence line.
    assert execution.metadata["memory_count"] == 0
    assert execution.metadata["below_threshold"] == 1
    assert execution.metadata["deduped"] == 1


def test_boost_helper_fail_closed():
    from lucent.llm.hooks import _project_memory_ids_from_session_state

    assert _project_memory_ids_from_session_state(None) == set()
    assert _project_memory_ids_from_session_state({}) == set()
    assert _project_memory_ids_from_session_state({"_project_context": "junk"}) == set()
    assert _project_memory_ids_from_session_state({"_project_context": {}}) == set()
    assert _project_memory_ids_from_session_state(
        {"_project_context": {"memory_ids": "not-a-list"}}
    ) == set()
    assert _project_memory_ids_from_session_state(
        {"_project_context": {"memory_ids": ["a", 7, None, "b"]}}
    ) == {"a", "7", "b"}


@pytest.mark.asyncio
async def test_boost_displaces_lowest_when_cap_binding():
    """With the cap binding, a project-attached memory genuinely displaces the
    lowest-ranked non-project candidate — not merely a reorder of survivors."""
    # Score-spread-compatible (all within 0.15 of the 0.80 window best).
    a = _memory("44444444-4444-4444-4444-444444444444", 0.80, "strong a")
    b = _memory("55555555-5555-5555-5555-555555555555", 0.72, "strong b")
    c = _memory("66666666-6666-6666-6666-666666666666", 0.68, "strong c")
    project = _memory("33333333-3333-3333-3333-333333333333", 0.66, "project goal")
    state: dict[str, Any] = {
        "_project_context": {
            "project": {"id": "p", "name": "Apollo"},
            "files": [],
            "memory_ids": ["33333333-3333-3333-3333-333333333333"],
        },
    }

    # Cap is 3: without the boost the top-3 server-ranked candidates fill it
    # and the project memory is dropped entirely.
    plain_bridge = FakeBridge([a, b, c, project])
    plain = await HookManager(session_state={}).before_model_call(
        messages=[{"role": "user", "content": "how does threshold filtering work?"}],
        memory_bridge=plain_bridge,
    )
    plain_meta = plain.injectable_executions[0].metadata
    assert plain_meta["memory_count"] == 3
    assert plain_meta["memory_ids"] == ["44444444", "55555555", "66666666"]
    assert plain_meta["below_threshold"] == 0 and plain_meta["deduped"] == 0

    # With the boost the project memory takes a slot and the third-ranked
    # non-project candidate is displaced.
    boosted_bridge = FakeBridge([a, b, c, project])
    boosted = await HookManager(session_state=state).before_model_call(
        messages=[{"role": "user", "content": "how does threshold filtering work?"}],
        memory_bridge=boosted_bridge,
    )
    boosted_meta = boosted.injectable_executions[0].metadata
    assert boosted_meta["memory_count"] == 3
    assert boosted_meta["memory_ids"] == ["33333333", "44444444", "55555555"]
    assert "66666666" not in boosted_meta["memory_ids"]
    assert "strong c" not in boosted.injectable_executions[0].text
    # The boost never changed the search itself: same query, same limit.
    plain_search = [p for name, p in plain_bridge.calls if name == "search_memories_full"][0]
    boosted_search = [p for name, p in boosted_bridge.calls if name == "search_memories_full"][0]
    assert plain_search == boosted_search
    assert boosted_meta["below_threshold"] == 0 and boosted_meta["deduped"] == 0


@pytest.mark.asyncio
async def test_boost_fills_byte_budget_in_boosted_order():
    """Full-content byte budget is spent in boosted order: the project memory
    gets the full slot that the stronger non-project memory would otherwise
    take, while the other result keeps its search-preview line."""
    a_id = "44444444-4444-4444-4444-444444444444"
    p_id = "33333333-3333-3333-3333-333333333333"
    # Score-spread-compatible: the project memory is within 0.15 of the best.
    results = [_memory(a_id, 0.80, "alpha preview"), _memory(p_id, 0.75, "project preview")]

    def bridge_with_full_contents() -> FakeBridge:
        bridge = FakeBridge(results)
        bridge.full_contents = {a_id: "x" * 5000, p_id: "y" * 2000}
        return bridge

    plain = await HookManager(session_state={}).before_model_call(
        messages=[{"role": "user", "content": "how does threshold filtering work?"}],
        memory_bridge=bridge_with_full_contents(),
    )
    plain_meta = plain.injectable_executions[0].metadata
    assert plain_meta["memory_count"] == 2
    assert plain_meta["full_content_count"] == 1
    assert [m["id"] for m in plain_meta["memories"] if m["full"]] == ["44444444"]

    boosted_state: dict[str, Any] = {
        "_project_context": {
            "project": {"id": "p", "name": "Apollo"},
            "files": [],
            "memory_ids": [p_id],
        },
    }
    boosted = await HookManager(session_state=boosted_state).before_model_call(
        messages=[{"role": "user", "content": "how does threshold filtering work?"}],
        memory_bridge=bridge_with_full_contents(),
    )
    boosted_meta = boosted.injectable_executions[0].metadata
    assert boosted_meta["memory_count"] == 2
    assert boosted_meta["full_content_count"] == 1
    assert [m["id"] for m in boosted_meta["memories"] if m["full"]] == ["33333333"]
    # Injected order flipped too (project memory first).
    assert boosted_meta["memory_ids"] == ["33333333", "44444444"]


@pytest.mark.asyncio
async def test_boost_end_to_end_from_real_attachment_state(pool, env):
    """Full plumbing on real rows: repo attach → get_context_for_session
    threading → hook reorder. The boost fires on genuinely attached state and
    never on another user's memory in the same org."""
    repo = _repo(pool)
    session_id = await pool.fetchval(
        """INSERT INTO llm_sessions (organization_id, user_id, kind, status, title, project_id)
           VALUES ($1, $2, 'chat', 'active', 'boost e2e', $3) RETURNING id""",
        env["org_id"], env["user_a"], env["project"]["id"],
    )
    attached_id = await _seed_memory(
        pool, env["org_id"], env["user_a"], content="project launch checklist",
    )
    moved = await repo.set_memories_project_bulk(
        [attached_id], env["project"]["id"],
        org_id=env["org_id"], user_id=env["user_a"],
    )
    assert moved == 1
    context = await repo.get_context_for_session(
        session_id, org_id=env["org_id"], user_id=env["user_a"]
    )
    assert context is not None and str(attached_id) in context["memory_ids"]

    # Server-ranked [strong non-project, weaker project memory]; boost must
    # put the attached memory first purely from the threaded context.
    bridge = FakeBridge([
        _memory("77777777-7777-7777-7777-777777777777", 0.80, "unrelated strong"),
        _memory(str(attached_id), 0.75, "project launch checklist"),
    ])
    manager = HookManager(session_state={"_project_context": context})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "what is the launch checklist?"}],
        memory_bridge=bridge,
    )
    execution = outcome.injectable_executions[0]
    assert execution.metadata["memory_ids"] == [
        str(attached_id)[:8], "77777777",
    ]
    # Search untouched by the boost.
    search = [p for name, p in bridge.calls if name == "search_memories_full"][0]
    assert search["limit"] == 10 and "launch checklist" in search["query"]