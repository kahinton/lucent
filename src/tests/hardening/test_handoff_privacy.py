"""Handoff privacy hardening tests (migration 113, fail-closed by construction).

Kyle's standing directive (2026-09-12): handoffs are strictly per-user
private. Migration 113 added CHECK (user_id IS NOT NULL) to
``user_interactions`` and the dead org-addressable class was removed from
every read path. These tests pin the invariant at all three layers:

1. Application guard — ``create_interaction(user_id=None)`` raises and
   writes nothing.
2. DB CHECK — a raw NULL-user_id INSERT is rejected by Postgres
   (ck_user_interactions_user_not_null).
3. Read paths — every handoff read path filters on an exact owner match;
   there is no org-addressable arm left anywhere, and a synthetic
   NULL-user_id row (raw-SQL insert for test setup only) is invisible to
   every read path including the project-attachment listing methods.
"""

from __future__ import annotations

import pytest

from lucent.db.projects import ProjectNotFoundError, ProjectRepository
from lucent.db.user_interactions import UserInteractionRepository


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
    # application's own migration chain (001..113), dropped in teardown.
    # Zero live-DB writes; the scratch cluster is harness-only infrastructure.
    admin_dsn = "postgresql://root@/postgres?host=/var/run/postgresql"
    scratch_name = f"handoff_privacy_test_{abs(hash(db_url)) % 10_000_000}"
    scratch_dsn = f"postgresql://root@/{scratch_name}?host=/var/run/postgresql"
    admin = await asyncpg.connect(dsn=admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')
        await admin.execute(f'CREATE DATABASE "{scratch_name}"')
    finally:
        await admin.close()
    created = await asyncpg.create_pool(dsn=scratch_dsn, min_size=1, max_size=4)
    await init_db(scratch_dsn, run_migrations=False)
    from lucent.db.pool import _run_migrations as _run
    await _run(created)
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
    """Org with two members (A/B) and a project owned by A."""
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'handoff-privacy-test') RETURNING id"
        )
        user_a = await conn.fetchrow(
            """INSERT INTO users (external_id, provider, email, display_name,
                                   organization_id, role)
               VALUES ('handoff-privacy-a', 'local', 'a@handoff-privacy.test',
                       'Privacy User A', $1, 'member') RETURNING id""",
            org_id,
        )
        user_b = await conn.fetchrow(
            """INSERT INTO users (external_id, provider, email, display_name,
                                   organization_id, role)
               VALUES ('handoff-privacy-b', 'local', 'b@handoff-privacy.test',
                       'Privacy User B', $1, 'member') RETURNING id""",
            org_id,
        )
        project = await conn.fetchrow(
            """INSERT INTO projects (organization_id, user_id, name)
               VALUES ($1, $2, 'HandoffPrivacy') RETURNING *""",
            org_id,
            user_a["id"],
        )
    return {
        "org_id": org_id,
        "user_a": user_a["id"],
        "user_b": user_b["id"],
        "project": dict(project),
    }


def _urepo(pool) -> UserInteractionRepository:
    return UserInteractionRepository(pool)


def _prepo(pool) -> ProjectRepository:
    return ProjectRepository(pool)


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


async def _seed_null_user_row(pool, org_id, title="legacy null-user row"):
    """Insert a NULL-user_id row via raw SQL — test-setup bypass ONLY.

    Post-migration-113 this can only exist in a pre-113 scratch lineage, but
    the read-path invisibility guarantee must hold regardless of how such a
    row came to exist.
    """
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO user_interactions (organization_id, user_id, source,
                                   interaction_type, status, priority, title, body)
               VALUES ($1, NULL, 'daemon', 'handoff', 'open', 'medium', $2, 'body text')
               RETURNING id""",
            org_id,
            title,
        )


# ── 1. application guard ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_interaction_null_user_raises_and_writes_nothing(pool, env):
    repo = _urepo(pool)
    before = await pool.fetchval("SELECT COUNT(*) FROM user_interactions")
    with pytest.raises(ValueError, match="user_id is required"):
        await repo.create_interaction(
            org_id=env["org_id"],
            user_id=None,
            title="broadcast attempt",
            body="must never persist",
            source="daemon",
        )
    after = await pool.fetchval("SELECT COUNT(*) FROM user_interactions")
    assert after == before, "refused create must not write any rows"
    # No orphaned messages/references either.
    assert await pool.fetchval("SELECT COUNT(*) FROM user_interaction_messages") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM user_interaction_references") == 0


@pytest.mark.asyncio
async def test_create_interaction_with_concrete_user_roundtrip(pool, env):
    repo = _urepo(pool)
    detail = await repo.create_interaction(
        org_id=env["org_id"],
        user_id=env["user_a"],
        title="normal handoff",
        body="owned row",
        source="daemon",
    )
    assert detail["user_id"] == env["user_a"]
    row = await pool.fetchrow(
        "SELECT user_id FROM user_interactions WHERE id = $1", detail["id"]
    )
    assert row["user_id"] == env["user_a"]


# ── 2. DB CHECK ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_check_blocks_raw_null_user_insert(pool, env):
    # NOTE: this scratch DB ran the FULL migration chain, so 113 is applied
    # and the raw insert must be rejected by the CHECK constraint. (The
    # NULL-row invisibility tests below use their own pre-constraint
    # scratch pool — see the fixture there.)
    with pytest.raises(Exception) as excinfo:
        await _seed_null_user_row(pool, env["org_id"])
    assert "ck_user_interactions_user_not_null" in str(excinfo.value)


@pytest.mark.asyncio
async def test_migration_113_constraint_validated(pool):
    row = await pool.fetchrow(
        """SELECT convalidated FROM pg_constraint
           WHERE conrelid = 'user_interactions'::regclass
             AND conname = 'ck_user_interactions_user_not_null'"""
    )
    assert row is not None, "migration 113 constraint missing"
    assert row["convalidated"] is True


# ── 3. read paths (exact owner match, no org-addressable arm) ─────────────


@pytest.fixture
async def precheck_pool(db_url):
    """Scratch pool with migrations 001..112 ONLY — no migration 113 — so a
    NULL-user_id row can be inserted via raw SQL to prove read-path
    invisibility independent of the DB CHECK."""
    asyncpg = pytest.importorskip("asyncpg")
    from lucent.db.pool import init_db

    admin_dsn = "postgresql://root@/postgres?host=/var/run/postgresql"
    scratch_name = f"handoff_precheck_{abs(hash(db_url)) % 10_000_000}"
    scratch_dsn = f"postgresql://root@/{scratch_name}?host=/var/run/postgresql"
    admin = await asyncpg.connect(dsn=admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')
        await admin.execute(f'CREATE DATABASE "{scratch_name}"')
    finally:
        await admin.close()
    created = await asyncpg.create_pool(dsn=scratch_dsn, min_size=1, max_size=4)
    await init_db(scratch_dsn, run_migrations=False)
    # Apply migrations 001..112 only: run the real chain, then drop 113's
    # constraint so a NULL row is insertable for this bypass-only fixture.
    from lucent.db.pool import _run_migrations as _run
    await _run(created)
    async with created.acquire() as conn:
        await conn.execute(
            "ALTER TABLE user_interactions DROP CONSTRAINT IF EXISTS ck_user_interactions_user_not_null"
        )
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
async def precheck_env(precheck_pool):
    async with precheck_pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'handoff-precheck') RETURNING id"
        )
        user_a = await conn.fetchrow(
            """INSERT INTO users (external_id, provider, email, display_name,
                                   organization_id, role)
               VALUES ('handoff-precheck-a', 'local', 'a@handoff-precheck.test',
                       'Precheck User A', $1, 'member') RETURNING id""",
            org_id,
        )
        user_b = await conn.fetchrow(
            """INSERT INTO users (external_id, provider, email, display_name,
                                   organization_id, role)
               VALUES ('handoff-precheck-b', 'local', 'b@handoff-precheck.test',
                       'Precheck User B', $1, 'member') RETURNING id""",
            org_id,
        )
        project = await conn.fetchrow(
            """INSERT INTO projects (organization_id, user_id, name)
               VALUES ($1, $2, 'PrecheckProject') RETURNING *""",
            org_id,
            user_a["id"],
        )
    return {
        "org_id": org_id,
        "user_a": user_a["id"],
        "user_b": user_b["id"],
        "project": dict(project),
    }


@pytest.mark.asyncio
async def test_null_user_row_invisible_to_all_read_paths(precheck_pool, precheck_env):
    """A synthetic NULL-user_id row is invisible to every read path."""
    env = precheck_env
    urepo = _urepo(precheck_pool)
    prepo = _prepo(precheck_pool)
    null_id = await _seed_null_user_row(precheck_pool, env["org_id"])
    owned_id = await _seed_interaction(precheck_pool, env["org_id"], env["user_a"])

    # list_interactions (viewer A): only A's own row.
    listing = await urepo.list_interactions(
        org_id=env["org_id"], user_id=env["user_a"]
    )
    ids = {str(i["id"]) for i in listing["items"]}
    assert str(owned_id) in ids
    assert str(null_id) not in ids

    # count_attention_needed (A): never counts the NULL row.
    count = await urepo.count_attention_needed(
        org_id=env["org_id"], user_id=env["user_a"]
    )
    assert count == 1  # only the owned open handoff

    # get_interaction (viewer A and viewer B): NULL row unreachable.
    assert await urepo.get_interaction(null_id, env["org_id"], user_id=env["user_a"]) is None
    assert await urepo.get_interaction(null_id, env["org_id"], user_id=env["user_b"]) is None

    # mark_viewed (A): NULL row is not viewable.
    assert await urepo.mark_viewed(
        interaction_id=null_id, org_id=env["org_id"], user_id=env["user_a"]
    ) is None

    # resolve (A) on the NULL row: nothing to close.
    assert await urepo.resolve_interaction(
        interaction_id=null_id, org_id=env["org_id"], user_id=env["user_a"]
    ) is None

    # Project-attachment listing methods: NULL row never appears in
    # A's project listing or unfiled picker.
    in_project = await prepo.list_interactions_in_project(
        env["project"]["id"], org_id=env["org_id"], user_id=env["user_a"]
    )
    assert str(null_id) not in {str(i["id"]) for i in in_project["items"]}
    unfiled = await prepo.list_unfiled_interactions(
        org_id=env["org_id"], user_id=env["user_a"]
    )
    assert str(null_id) not in {str(i["id"]) for i in unfiled["items"]}

    # And it cannot be filed into A's project either (bulk move).
    moved = await prepo.set_interactions_project_bulk(
        [null_id], env["project"]["id"], org_id=env["org_id"], user_id=env["user_a"]
    )
    assert moved == 0


@pytest.mark.asyncio
async def test_read_paths_owner_scoped(precheck_pool, precheck_env):
    """Every handoff read path returns rows only for the owning user."""
    env = precheck_env
    urepo = _urepo(precheck_pool)
    prepo = _prepo(precheck_pool)
    a_row = await _seed_interaction(precheck_pool, env["org_id"], env["user_a"], title="A's")
    b_row = await _seed_interaction(precheck_pool, env["org_id"], env["user_b"], title="B's")

    # A's listing: only A's row.
    listing = await urepo.list_interactions(org_id=env["org_id"], user_id=env["user_a"])
    assert {str(i["id"]) for i in listing["items"]} == {str(a_row)}
    # B's listing: only B's row.
    listing_b = await urepo.list_interactions(org_id=env["org_id"], user_id=env["user_b"])
    assert {str(i["id"]) for i in listing_b["items"]} == {str(b_row)}
    # get_interaction cross-user: invisible.
    assert await urepo.get_interaction(a_row, env["org_id"], user_id=env["user_b"]) is None

    # Project attachment methods return owner-only rows: A files A's row,
    # then B (owning nothing in this project — and unable to even see the
    # project) gets zero rows through any path.
    await prepo.set_interactions_project_bulk(
        [a_row], env["project"]["id"], org_id=env["org_id"], user_id=env["user_a"]
    )
    in_project = await prepo.list_interactions_in_project(
        env["project"]["id"], org_id=env["org_id"], user_id=env["user_a"]
    )
    assert {str(i["id"]) for i in in_project["items"]} == {str(a_row)}
    with pytest.raises(ProjectNotFoundError):
        await prepo.list_interactions_in_project(
            env["project"]["id"], org_id=env["org_id"], user_id=env["user_b"]
        )
    unfiled_b = await prepo.list_unfiled_interactions(
        org_id=env["org_id"], user_id=env["user_b"]
    )
    assert {str(i["id"]) for i in unfiled_b["items"]} == {str(b_row)}
    # B cannot file A's handoff anywhere (silent not-moved).
    moved = await prepo.set_interactions_project_bulk(
        [a_row], env["project"]["id"], org_id=env["org_id"], user_id=env["user_b"]
    )
    assert moved == 0


@pytest.mark.asyncio
async def test_dedupe_lookup_still_owner_scoped(pool, env):
    """Dedupe remains per-user: the same key for two users creates two rows."""
    repo = _urepo(pool)
    first = await repo.create_interaction(
        org_id=env["org_id"], user_id=env["user_a"],
        title="dedupe test", body="x", source="daemon", dedupe_key="k1",
    )
    second = await repo.create_interaction(
        org_id=env["org_id"], user_id=env["user_b"],
        title="dedupe test", body="same key other user", source="daemon", dedupe_key="k1",
    )
    assert str(first["id"]) != str(second["id"])
    again = await repo.create_interaction(
        org_id=env["org_id"], user_id=env["user_a"],
        title="dedupe test", body="same key same user", source="daemon", dedupe_key="k1",
    )
    assert str(again["id"]) == str(first["id"])
    assert again.get("deduplicated") is True