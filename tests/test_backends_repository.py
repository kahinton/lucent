"""Integration tests for the engine backends repository and migration 136.

Auto-marked ``integration`` (fixture graph uses ``db_pool``). Run with
``pytest -m integration`` against the isolated test database.
"""

from __future__ import annotations

import pytest

from lucent.db.backends import BackendRepository, resolve_backend_for_model


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
async def test_backfill_backend_ids_reference_real_backends(db_pool):
    """The migration backfill only points models at shared backends."""
    async with db_pool.acquire() as conn:
        orphan = await conn.fetchval(
            """SELECT COUNT(*) FROM models m
               WHERE m.backend_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM backends b WHERE b.id = m.backend_id)"""
        )
    assert orphan == 0
