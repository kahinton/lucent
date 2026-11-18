"""Integration tests for the engine backends repository and migration 136.

Auto-marked ``integration`` (fixture graph uses ``db_pool``). Run with
``pytest -m integration`` against the isolated test database.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from lucent.db.backends import (
    BackendRepository,
    MissingOrganizationScope,
    resolve_backend_for_model,
)

_MIGRATION_165 = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "lucent"
    / "db"
    / "migrations"
    / "165_backends_org_required.sql"
)


async def _backends_org_id_nullable(conn) -> bool:
    value = await conn.fetchval(
        """SELECT is_nullable FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'backends'
             AND column_name = 'organization_id'"""
    )
    assert value is not None, "backends.organization_id column is missing"
    return value == "YES"


@pytest.mark.asyncio
async def test_backends_table_and_model_column_exist(db_pool):
    async with db_pool.acquire() as conn:
        table = await conn.fetchval("SELECT to_regclass('public.backends')")
        column = await conn.fetchval(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'models' AND column_name = 'backend_id'"""
        )
        task_column = await conn.fetchval(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'tasks' AND column_name = 'backend_id'"""
        )
    assert table == "backends"
    assert column == "backend_id"
    assert task_column == "backend_id"


@pytest.mark.asyncio
async def test_backend_crud_roundtrip(db_pool, test_organization):
    org_id = str(test_organization["id"])
    repo = BackendRepository(db_pool)

    created = await repo.create(
        name="Test backend",
        engine="langchain",
        provider="openai",
        config={"base_url": "http://localhost:11434/v1"},
        secret_refs={"api_key": "engine.langchain.Test backend.api_key"},
        org_id=org_id,
    )
    assert created["engine"] == "langchain"
    assert created["config"]["base_url"] == "http://localhost:11434/v1"

    fetched = await repo.get(created["id"], org_id=org_id)
    assert fetched is not None
    assert fetched["name"] == "Test backend"

    listed = await repo.list(org_id=org_id, engine="langchain")
    assert any(row["id"] == created["id"] for row in listed)

    updated = await repo.update(
        created["id"], config={"base_url": "http://example:1234/v1"}, org_id=org_id
    )
    assert updated["config"]["base_url"] == "http://example:1234/v1"

    deleted = await repo.delete(created["id"], org_id=org_id)
    assert deleted is True
    assert await repo.get(created["id"], org_id=org_id) is None


@pytest.mark.asyncio
async def test_resolve_backend_against_real_rows(db_pool, test_organization):
    org_id = str(test_organization["id"])
    repo = BackendRepository(db_pool)
    created = await repo.create(
        name="Org openai langchain", engine="langchain", provider="openai", org_id=org_id
    )
    try:
        candidates = await repo.list(org_id=org_id, enabled_only=True)
        model = {"id": "gpt-5.1", "provider": "openai", "engine": "langchain", "backend_id": None}
        resolved = resolve_backend_for_model(model, org_id=org_id, candidates=candidates)
        assert resolved is not None
        assert resolved["id"] == created["id"]
    finally:
        await repo.delete(created["id"], org_id=org_id)


@pytest.mark.asyncio
async def test_reads_require_an_organization(db_pool):
    """No org id is an error, never a broader (shared or cross-tenant) read."""
    repo = BackendRepository(db_pool)
    with pytest.raises(MissingOrganizationScope, match="organization id"):
        await repo.list(org_id=None)
    with pytest.raises(MissingOrganizationScope, match="organization id"):
        await repo.get(str(uuid4()), org_id=None)
    with pytest.raises(MissingOrganizationScope, match="organization id"):
        await repo.delete(str(uuid4()), org_id="")


@pytest.mark.asyncio
async def test_other_org_backend_is_unreachable(db_pool, test_organization):
    """A row owned by one org cannot be read or listed by another."""
    org_id = str(test_organization["id"])
    other_org = str(uuid4())
    repo = BackendRepository(db_pool)
    created = await repo.create(
        name="Scoped backend", engine="langchain", provider="openai", org_id=org_id
    )
    try:
        assert await repo.get(created["id"], org_id=other_org) is None
        assert await repo.list(org_id=other_org) == []
        assert await repo.update(created["id"], name="nope", org_id=other_org) is None
        assert await repo.delete(created["id"], org_id=other_org) is False
        assert await repo.get(created["id"], org_id=org_id) is not None
    finally:
        await repo.delete(created["id"], org_id=org_id)


@pytest.mark.asyncio
async def test_other_org_backend_cannot_serve_resolution(db_pool, test_organization):
    """Isolation is not just invisibility: B's resolution never routes through A's
    backend, even when (engine, provider) match exactly."""
    org_a = str(test_organization["id"])
    org_b = str(uuid4())
    repo = BackendRepository(db_pool)
    created = await repo.create(
        name="Only A owns this", engine="langchain", provider="openai", org_id=org_a
    )
    model = {"id": "gpt-5.1", "provider": "openai", "engine": "langchain", "backend_id": None}
    try:
        # B lists nothing, so B cannot resolve A's provider.
        candidates_b = await repo.list(org_id=org_b, enabled_only=True)
        assert candidates_b == []
        assert resolve_backend_for_model(model, org_id=org_b, candidates=candidates_b) is None
        # Handed A's row directly, B still resolves nothing.
        candidates_a = await repo.list(org_id=org_a, enabled_only=True)
        assert [str(row["id"]) for row in candidates_a] == [str(created["id"])]
        assert resolve_backend_for_model(model, org_id=org_b, candidates=candidates_a) is None
        # An explicit pin to A's backend does not transfer ownership to B.
        pinned = dict(model, backend_id=str(created["id"]))
        assert resolve_backend_for_model(pinned, org_id=org_b, candidates=candidates_a) is None
        assert (
            resolve_backend_for_model(pinned, org_id=org_a, candidates=candidates_a)["id"]
            == created["id"]
        )
        # A itself does resolve it.
        assert (
            resolve_backend_for_model(model, org_id=org_a, candidates=candidates_a)["id"]
            == created["id"]
        )
    finally:
        await repo.delete(created["id"], org_id=org_a)


@pytest.mark.asyncio
async def test_null_org_backend_never_surfaced(db_pool, test_organization):
    """No read path surfaces a NULL-org backend.

    Before migration 165 is applied the column is still nullable, so a shared
    row is inserted directly to prove it cannot reach a tenant. Once the
    migration is applied the database itself refuses that row, and the
    resolver/repository still refuse one handed to them as data.
    """
    org_id = str(test_organization["id"])
    repo = BackendRepository(db_pool)
    model = {"id": "gpt-5.1", "provider": "openai", "engine": "langchain", "backend_id": None}
    async with db_pool.acquire() as conn:
        nullable = await _backends_org_id_nullable(conn)

    null_candidate = {
        "id": str(uuid4()),
        "organization_id": None,
        "engine": "langchain",
        "provider": "openai",
        "is_enabled": True,
    }
    # Pure resolution refuses an org-NULL row regardless of schema state.
    assert resolve_backend_for_model(model, org_id=org_id, candidates=[null_candidate]) is None
    assert await repo.get(null_candidate["id"], org_id=org_id) is None

    if not nullable:
        # Migration 165 applied: a shared row is structurally impossible.
        async with db_pool.acquire() as conn:
            with pytest.raises(asyncpg.exceptions.NotNullViolationError):
                await conn.execute(
                    """INSERT INTO backends (organization_id, name, engine, provider)
                       VALUES (NULL, 'shared probe', 'langchain', 'openai')"""
                )
        return

    # Pre-migration window: insert a shared row directly and prove it stays
    # unreachable from every repository read path.
    async with db_pool.acquire() as conn:
        null_id = await conn.fetchval(
            """INSERT INTO backends (organization_id, name, engine, provider)
               VALUES (NULL, 'shared probe', 'langchain', 'openai') RETURNING id"""
        )
    try:
        assert await repo.get(str(null_id), org_id=org_id) is None
        listed = await repo.list(org_id=org_id, enabled_only=True)
        assert all(str(row["id"]) != str(null_id) for row in listed)
        assert all(row["organization_id"] is not None for row in listed)
        assert resolve_backend_for_model(model, org_id=org_id, candidates=listed) is None
        with pytest.raises(MissingOrganizationScope):
            await repo.list(org_id=None)
        # The row does exist — invisibility is the repository's behaviour, not
        # a test that forgot to write anything.
        async with db_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM backends WHERE id = $1 AND organization_id IS NULL",
                null_id,
            ) == 1
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM backends WHERE id = $1", null_id)


@pytest.mark.asyncio
async def test_migration_165_backfill_fans_out_to_every_org(db_pool):
    """Migration 165's own SQL: N shared rows x M orgs -> zero NULL rows, exactly
    one owned row per (org, original backend), and an existing same-key row in
    one org is kept (the documented skip) without raising.

    The migration text is executed verbatim on this scratch connection against
    temporary copies of ``backends``/``organizations``. The test connection is
    the same engine and the same SQL; only the tables are controlled so the
    outcome is asserted exactly.
    """
    migration_sql = _MIGRATION_165.read_text()
    assert "organization_id IS NULL" in migration_sql
    conn = await db_pool.acquire()
    try:
        await conn.execute(
            """
            CREATE TEMP TABLE organizations (id uuid PRIMARY KEY);
            CREATE TEMP TABLE backends (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                organization_id uuid,
                name text NOT NULL,
                engine text NOT NULL,
                provider text NOT NULL,
                config jsonb NOT NULL DEFAULT '{}'::jsonb,
                secret_refs jsonb NOT NULL DEFAULT '{}'::jsonb,
                is_enabled boolean NOT NULL DEFAULT true,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT backends_organization_id_engine_provider_name_key
                    UNIQUE NULLS NOT DISTINCT
                    (organization_id, engine, provider, name)
            );
            """
        )
        org_ids = [str(uuid4()) for _ in range(3)]
        for org in org_ids:
            await conn.execute("INSERT INTO organizations (id) VALUES ($1)", org)
        # Two shared backends; the third org already owns one same-key row.
        await conn.execute(
            """INSERT INTO backends (organization_id, name, engine, provider)
               VALUES (NULL, 'copilot via copilot', 'langchain', 'copilot'),
                      (NULL, 'shared azure', 'langchain', 'azure')"""
        )
        existing_id = await conn.fetchval(
            """INSERT INTO backends
                 (organization_id, name, engine, provider, config)
               VALUES ($1, 'copilot via copilot', 'langchain', 'copilot', $2)
               RETURNING id""",
            org_ids[2],
            {"keep": True},
        )

        async with conn.transaction():
            await conn.execute(migration_sql)

        null_rows = await conn.fetchval(
            "SELECT count(*) FROM backends WHERE organization_id IS NULL"
        )
        assert null_rows == 0

        pairs = await conn.fetch(
            """SELECT organization_id, engine, provider, name, count(*) AS n
                 FROM backends
                GROUP BY organization_id, engine, provider, name"""
        )
        # 2 shared backends x 3 orgs, one satisfied by the pre-existing row.
        assert {str(row["organization_id"]) for row in pairs} == set(org_ids)
        assert len(pairs) == 6
        assert all(row["n"] == 1 for row in pairs)
        for row in pairs:
            assert row["organization_id"] is not None

        # Collision resolution: the org's own row was kept, not overwritten.
        kept = await conn.fetchrow(
            """SELECT id, config FROM backends
                WHERE organization_id = $1 AND name = 'copilot via copilot'""",
            org_ids[2],
        )
        assert str(kept["id"]) == str(existing_id)
        assert kept["config"] == {"keep": True}

        # The column is now NOT NULL: a shared row cannot be re-created.
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await conn.execute(
                """INSERT INTO backends (organization_id, name, engine, provider)
                   VALUES (NULL, 'late shared', 'langchain', 'azure')"""
            )
    finally:
        if await conn.fetchval("SELECT to_regclass('pg_temp.backends') IS NOT NULL"):
            await conn.execute("DROP TABLE pg_temp.backends")
        if await conn.fetchval("SELECT to_regclass('pg_temp.organizations') IS NOT NULL"):
            await conn.execute("DROP TABLE pg_temp.organizations")
        await db_pool.release(conn)


@pytest.mark.asyncio
async def test_backfill_backend_ids_reference_real_backends(db_pool):
    """The migration backfill only points models at shared backends."""
    async with db_pool.acquire() as conn:
        orphan = await conn.fetchval(
            """SELECT COUNT(*) FROM models m
               WHERE m.backend_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM backends b WHERE b.id = m.backend_id)"""
        )
    assert orphan == 0
