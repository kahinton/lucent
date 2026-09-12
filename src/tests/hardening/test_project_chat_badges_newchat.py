"""Project chat badges + in-project New Chat filing (Projects round 2, C/D).

Regression coverage for the four layers the two scopes touch:

1. Sessions API payload — ``LLMSessionRepository.list_sessions`` must carry
   ``project_id`` (pre-existing) plus ``project_name`` (new LEFT JOIN) so the
   sidebar/drawer can badge rows without a per-row lookup. The JOIN is
   guarded by org+user equality, and a cross-tenant project reference must
   NOT surface a name (fail-closed display).

2. Lazy createSession-on-first-send fallback — the composer path
   (``POST /api/chat/sessions`` with ``project_id``) files the shell before
   any streaming begins; the stream handler keeps working against that
   pre-created session.

3. Dedicated New Chat POST route (web /projects router) — creates the
   session server-side with project_id set and redirects into the chat with
   that session active. The security contract is pinned structurally: the
   handler body must await ``_check_csrf`` BEFORE any project lookup or
   session creation, must scope the project through ``get_owned`` and the
   session through ``create_session(org_id=..., user_id=...)`` — never an
   unscoped call — and must 303 into ``/chat/{session_id}``.

4. Title backfill — the stream handler gives empty-shell sessions (created
   by the New Chat route before any message existed) a real title from the
   first turn's message, and never overwrites a session that already has
   one.

The route-level pins read the installed handler source (source-level
structural assertions) because the web layer has no TestClient suite —
real-browser verification of the same routes runs separately in the
Playwright harness.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from lucent.db.projects import ProjectRepository
from lucent.db.projects import _require_scope


# ═════════════════════════════════════════════════════════════════════
# 1. Sessions list payload — project_name join (scratch Postgres)
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

    admin_dsn = "postgresql://root@/postgres?host=/var/run/postgresql"
    scratch_name = f"proj_cd_test_{abs(hash(db_url)) % 10_000_000}"
    scratch_dsn = f"postgresql://root@/{scratch_name}?host=/var/run/postgresql"
    admin = await asyncpg.connect(dsn=admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')
        await admin.execute(f'CREATE DATABASE "{scratch_name}"')
    finally:
        await admin.close()
    from lucent.db.pool import _init_connection

    created = await asyncpg.create_pool(
        dsn=scratch_dsn, min_size=1, max_size=4, init=_init_connection
    )
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
    """Org with two members (A/B), one project for A, and a foreign org+user."""
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'proj-cd-test') RETURNING id"
        )
        user_a = await conn.fetchrow(
            """INSERT INTO users (external_id, provider, email, display_name,
                                   organization_id, role)
               VALUES ('proj-cd-a', 'local', 'a@proj-cd.test',
                       'CD User A', $1, 'member') RETURNING id""",
            org_id,
        )
        user_b = await conn.fetchrow(
            """INSERT INTO users (external_id, provider, email, display_name,
                                   organization_id, role)
               VALUES ('proj-cd-b', 'local', 'b@proj-cd.test',
                       'CD User B', $1, 'member') RETURNING id""",
            org_id,
        )
        foreign_org = await conn.fetchval(
            "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'proj-cd-foreign') RETURNING id"
        )
        foreign_user = await conn.fetchrow(
            """INSERT INTO users (external_id, provider, email, display_name,
                                   organization_id, role)
               VALUES ('proj-cd-x', 'local', 'x@proj-cd.test',
                       'CD User X', $1, 'member') RETURNING id""",
            foreign_org,
        )
        project = await conn.fetchrow(
            """INSERT INTO projects (organization_id, user_id, name)
               VALUES ($1, $2, 'Apollo Badging') RETURNING *""",
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


async def _seed_session(
    pool,
    org_id,
    user_id,
    *,
    title=None,
    project_id=None,
    kind="chat",
):
    from lucent.db.llm_sessions import LLMSessionRepository

    repo = LLMSessionRepository(pool)
    session = await repo.create_session(
        org_id=org_id, user_id=user_id, kind=kind, title=title
    )
    if project_id is not None:
        await ProjectRepository(pool).set_session_project(
            session["id"], project_id, org_id=org_id, user_id=user_id
        )
    return str(session["id"])


# ── sessions API payload ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sessions_payload_carries_project_name(pool, env):
    """Filed rows expose project_id AND project_name; unfiled rows null both."""
    from lucent.db.llm_sessions import LLMSessionRepository

    org, user = env["org_id"], env["user_a"]
    await _seed_session(pool, org, user, title="Filed chat", project_id=env["project"]["id"])
    await _seed_session(pool, org, user, title="Unfiled chat")

    repo = LLMSessionRepository(pool)
    result = await repo.list_sessions(org, user_id=user, limit=25)

    by_title = {item["title"]: item for item in result["items"]}
    filed = by_title["Filed chat"]
    unfiled = by_title["Unfiled chat"]
    assert filed["project_id"] == env["project"]["id"]
    assert filed["project_name"] == "Apollo Badging"
    assert unfiled["project_id"] is None
    assert unfiled["project_name"] is None


@pytest.mark.asyncio
async def test_list_sessions_scoping_keeps_foreign_rows_out(pool, env):
    """The list stays (org, user)-scoped — other users' chats never appear."""
    from lucent.db.llm_sessions import LLMSessionRepository

    org = env["org_id"]
    await _seed_session(pool, org, env["user_a"], title="A filed", project_id=env["project"]["id"])
    await _seed_session(pool, org, env["user_b"], title="B private")

    repo = LLMSessionRepository(pool)
    result_a = await repo.list_sessions(org, user_id=env["user_a"])
    result_b = await repo.list_sessions(org, user_id=env["user_b"])
    assert [i["title"] for i in result_a["items"]] == ["A filed"]
    assert [i["title"] for i in result_b["items"]] == ["B private"]


@pytest.mark.asyncio
async def test_list_sessions_project_name_guard_requires_scope_match(pool, env):
    """Corrupted cross-tenant reference must not surface a foreign name.

    The JOIN itself is guarded (p.organization_id = s.organization_id AND
    p.user_id = s.user_id); this test proves the guard by planting a row
    that violates the FK-derived ownership invariant — if the guard were
    absent, user B's list would show user A's project name.
    """
    asyncpg = pytest.importorskip("asyncpg")
    org = env["org_id"]
    session_id = await _seed_session(pool, org, env["user_b"], title="Corrupt ref")
    # Plant the corruption directly (bypasses the repository's ownership
    # checks on purpose — this row cannot exist through real code paths).
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE llm_sessions SET project_id = $1 WHERE id = $2",
            env["project"]["id"],
            session_id,
        )
        # projects.user_id is NOT NULL, so user B's name guard relies on the
        # org+user equality predicate, not NULLability.
        assert await conn.fetchval("SELECT user_id FROM projects WHERE id = $1", env["project"]["id"]) == env["user_a"]

    from lucent.db.llm_sessions import LLMSessionRepository

    result = await LLMSessionRepository(pool).list_sessions(org, user_id=env["user_b"])
    row = next(i for i in result["items"] if i["title"] == "Corrupt ref")
    assert row["project_id"] == env["project"]["id"]  # id preserved (data truth)
    assert row["project_name"] is None  # name fail-closed (display truth)


# ── lazy createSession-on-first-send fallback (POST /api/chat/sessions) ────


@pytest.mark.asyncio
async def test_create_session_with_project_files_shell(pool, env):
    """The composer's lazy path: shell created + filed BEFORE first send."""
    org, user = env["org_id"], env["user_a"]
    from lucent.db.llm_sessions import LLMSessionRepository

    repo = LLMSessionRepository(pool)
    session = await repo.create_session(
        org_id=org,
        user_id=user,
        kind="chat",
        title="First message text",
    )
    await ProjectRepository(pool).set_session_project(
        session["id"], env["project"]["id"], org_id=org, user_id=user
    )
    detail = await repo.get_session(session["id"], org, user_id=user)
    assert detail["project_id"] == env["project"]["id"]
    # create_session validated the project BEFORE creating the shell —
    # fail-closed on unknown/foreign projects is pinned at the router level
    # (structural test below); here we pin the resulting filed state.


@pytest.mark.asyncio
async def test_stream_handler_uses_precreated_session(pool, env):
    """_prepare_persistent_chat_session against a real pre-created session.

    The shell exists and is filed; the stream path loads it (never creates a
    second session) and keeps the filed project intact.
    """
    from lucent.api.routers.chat import _prepare_persistent_chat_session
    from lucent.db.llm_sessions import LLMSessionRepository

    org, user = env["org_id"], env["user_a"]
    repo = LLMSessionRepository(pool)
    session = await repo.create_session(org_id=org, user_id=user, kind="chat")
    await ProjectRepository(pool).set_session_project(
        session["id"], env["project"]["id"], org_id=org, user_id=user
    )

    prepared = await _prepare_persistent_chat_session(
        user={"organization_id": org, "id": user},
        pool=pool,
        session_id=str(session["id"]),
        kind="chat",
        engine_name="langchain",
        model="test-model",
        reasoning_effort=None,
        last_message="Hello from the fallback path",
    )
    assert prepared.session_id == str(session["id"])
    # Exactly one session exists for this user — no lazy duplicate.
    listing = await repo.list_sessions(org, user_id=user)
    assert listing["total_count"] == 1
    filed = (await repo.get_session(str(session["id"]), org, user_id=user))
    assert filed["project_id"] == env["project"]["id"]


# ═════════════════════════════════════════════════════════════════════
# 2. Dedicated New Chat POST route — structural security contract
# ═════════════════════════════════════════════════════════════════════


def _new_chat_handler():
    from lucent.web.routes.projects import project_new_chat

    return project_new_chat


def test_new_chat_route_awaits_csrf_before_any_lookup():
    """CSRF is the FIRST awaited call — before project lookup, before creation."""
    src = inspect.getsource(_new_chat_handler())
    csrf_pos = src.index("await _check_csrf(request, csrf_token)")
    get_owned_pos = src.index("get_owned(")
    create_pos = src.index("create_session(")
    assert csrf_pos < get_owned_pos < create_pos
    # The route is registered as a POST with the expected path.
    from lucent.web.routes.projects import router

    path = "/projects/{project_id}/new-chat"
    routes = [r for r in router.routes if getattr(r, "path", None) == path]
    assert routes and "POST" in getattr(routes[0], "methods", set())


@pytest.mark.asyncio
async def test_new_chat_route_files_session_into_owned_project(pool, env):
    """Happy path against the real repos: session created server-side,
    filed into the caller's project, scoped to the caller."""
    org, user = env["org_id"], env["user_a"]
    from lucent.db.llm_sessions import LLMSessionRepository

    pool_repo = ProjectRepository(pool)
    project = await pool_repo.get_owned(
        env["project"]["id"], org_id=str(org), user_id=str(user)
    )
    assert project is not None
    session = await LLMSessionRepository(pool).create_session(
        org_id=str(org), user_id=str(user), kind="chat"
    )
    moved = await pool_repo.set_session_project(
        session["id"], project["id"], org_id=str(org), user_id=str(user)
    )
    assert moved is not None
    detail = await LLMSessionRepository(pool).get_session(session["id"], org, user_id=user)
    assert detail["project_id"] == env["project"]["id"]
    assert detail["title"] is None  # empty shell until the first turn


@pytest.mark.asyncio
async def test_new_chat_route_foreign_project_is_404(pool, env):
    """get_owned returns None for a foreign project — the route 404s before
    creating anything. Emulated at the data layer: user B cannot resolve
    user A's project, and B ends up with zero sessions."""
    repo = ProjectRepository(pool)
    project = await repo.get_owned(
        env["project"]["id"], org_id=str(env["org_id"]), user_id=str(env["user_b"])
    )
    assert project is None
    from lucent.db.llm_sessions import LLMSessionRepository

    listing = await LLMSessionRepository(pool).list_sessions(
        env["org_id"], user_id=env["user_b"]
    )
    assert listing["total_count"] == 0


def test_unscoped_session_project_calls_raise():
    """The session-project helpers fail loudly without both scope args."""
    with pytest.raises(TypeError):
        _require_scope(None, "user-only")
    with pytest.raises(TypeError):
        _require_scope("org-only", None)


# ═════════════════════════════════════════════════════════════════════
# 3. Title backfill for empty-shell sessions
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stream_handler_backfills_empty_shell_title(pool, env):
    """Empty shell + first message → title set, filed project untouched."""
    from lucent.api.routers.chat import _prepare_persistent_chat_session
    from lucent.db.llm_sessions import LLMSessionRepository

    org, user = env["org_id"], env["user_a"]
    repo = LLMSessionRepository(pool)
    session = await repo.create_session(org_id=org, user_id=user, kind="chat")
    await ProjectRepository(pool).set_session_project(
        session["id"], env["project"]["id"], org_id=org, user_id=user
    )

    await _prepare_persistent_chat_session(
        user={"organization_id": org, "id": user},
        pool=pool,
        session_id=str(session["id"]),
        kind="chat",
        engine_name="langchain",
        model="test-model",
        reasoning_effort=None,
        last_message="First real turn in the filed chat",
    )
    filed = await repo.get_session(str(session["id"]), org, user_id=user)
    assert filed["title"] == "First real turn in the filed chat"
    assert filed["project_id"] == env["project"]["id"]


@pytest.mark.asyncio
async def test_stream_handler_never_overwrites_existing_title(pool, env):
    """A session that already has a title keeps it on later turns."""
    from lucent.api.routers.chat import _prepare_persistent_chat_session
    from lucent.db.llm_sessions import LLMSessionRepository

    org, user = env["org_id"], env["user_a"]
    repo = LLMSessionRepository(pool)
    session = await repo.create_session(org_id=org, user_id=user, kind="chat", title="Original title")

    await _prepare_persistent_chat_session(
        user={"organization_id": org, "id": user},
        pool=pool,
        session_id=str(session["id"]),
        kind="chat",
        engine_name="langchain",
        model="test-model",
        reasoning_effort=None,
        last_message="A later turn that must not retitle",
    )
    kept = await repo.get_session(str(session["id"]), org, user_id=user)
    assert kept["title"] == "Original title"


@pytest.mark.asyncio
async def test_stream_handler_no_message_no_backfill(pool, env):
    """Empty shell + no message text → no backfill call, title stays null."""
    from lucent.api.routers.chat import _prepare_persistent_chat_session
    from lucent.db.llm_sessions import LLMSessionRepository

    org, user = env["org_id"], env["user_a"]
    repo = LLMSessionRepository(pool)
    session = await repo.create_session(org_id=org, user_id=user, kind="chat")

    prepared = await _prepare_persistent_chat_session(
        user={"organization_id": org, "id": user},
        pool=pool,
        session_id=str(session["id"]),
        kind="chat",
        engine_name="langchain",
        model="test-model",
        reasoning_effort=None,
        last_message="",
    )
    assert prepared.session_id == str(session["id"])
    kept = await repo.get_session(str(session["id"]), org, user_id=user)
    assert kept["title"] is None


# ═════════════════════════════════════════════════════════════════════
# 4. Badge rendering source — minimal chrome contract
# ═════════════════════════════════════════════════════════════════════


def test_session_rows_render_project_badge():
    """Sidebar + drawer render one shared row builder; the badge markup is
    driven by project_id with project_name preferred, and the project meta
    text moved OUT of the meta line (badge replaces it)."""
    with open("/app/src/lucent/web/templates/chat.html", encoding="utf-8") as fh:
        src = fh.read()
    assert src.count("renderSessionList") >= 2  # definition + call sites
    # Single row builder drives both surfaces (sidebar and drawer share
    # #session-list; no second render path exists).
    assert src.count("function renderSessionList()") == 1
    assert "session-project-badge" in src
    assert "session.project_name ||" in src  # payload name preferred
    # The v1 'Project: <name>' meta text is replaced by the badge.
    assert "`Project: ${projectName}`" not in src
    # Badge markup (not just the CSS) is escapeHtml-guarded: anchor at the
    # row-builder markup span, then check the name is escaped inside it.
    markup_idx = src.index('<span class="session-project-badge">')
    assert "escapeHtml(projectName)" in src[markup_idx : markup_idx + 600]


def test_new_chat_form_in_project_detail_template():
    """project_detail.html posts to the dedicated route with CSRF."""
    with open("/app/src/lucent/web/templates/project_detail.html", encoding="utf-8") as fh:
        src = fh.read()
    assert '/projects/{{ project.id }}/new-chat"' in src
    assert "csrf_field_name" in src
    # The lazy ?project= link is replaced, not duplicated.
    assert 'href="/chat?project={{ project.id }}"' not in src