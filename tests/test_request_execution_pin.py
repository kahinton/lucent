"""Integration tests for request-level execution pins (migration 140)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from lucent.db.requests import RequestRepository


@pytest_asyncio.fixture
async def pin_backends(db_pool, test_organization):
    """Two org-scoped backend rows so overrides are distinguishable."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """INSERT INTO backends (organization_id, name, engine, provider)
               VALUES ($1::uuid, 'pin-primary', 'langchain', 'openai'),
                      ($1::uuid, 'pin-secondary', 'langchain', 'openai')
               RETURNING id, name""",
            str(test_organization["id"]),
        )
    return {row["name"]: str(row["id"]) for row in rows}


@pytest_asyncio.fixture
async def repo(db_pool):
    return RequestRepository(db_pool)


async def test_create_request_persists_execution_pin(repo, test_organization, pin_backends):
    request = await repo.create_request(
        title="Pinned request",
        org_id=str(test_organization["id"]),
        description="Run this on a specific engine.",
        backend_id=pin_backends["pin-primary"],
        requested_model="qwen3-coder",
    )

    assert str(request["backend_id"]) == pin_backends["pin-primary"]
    assert request["requested_model"] == "qwen3-coder"


async def test_create_task_inherits_request_pin(repo, test_organization, pin_backends):
    request = await repo.create_request(
        title="Inherited pin",
        org_id=str(test_organization["id"]),
        backend_id=pin_backends["pin-primary"],
        requested_model="qwen3-coder",
    )

    task = await repo.create_task(
        request_id=str(request["id"]),
        title="Inherits the pin",
        org_id=str(test_organization["id"]),
    )

    assert str(task["backend_id"]) == pin_backends["pin-primary"]
    assert task["model"] == "qwen3-coder"


async def test_explicit_task_values_override_request_pin(
    repo, test_organization, pin_backends
):
    request = await repo.create_request(
        title="Overridden pin",
        org_id=str(test_organization["id"]),
        backend_id=pin_backends["pin-primary"],
        requested_model="qwen3-coder",
    )

    task = await repo.create_task(
        request_id=str(request["id"]),
        title="Overrides the pin",
        org_id=str(test_organization["id"]),
        model="claude-opus-4.7",
        backend_id=pin_backends["pin-secondary"],
    )

    assert str(task["backend_id"]) == pin_backends["pin-secondary"]
    assert task["model"] == "claude-opus-4.7"


async def test_task_under_unpinned_request_stays_null(repo, test_organization):
    request = await repo.create_request(
        title="Unpinned request",
        org_id=str(test_organization["id"]),
    )

    task = await repo.create_task(
        request_id=str(request["id"]),
        title="Unpinned task",
        org_id=str(test_organization["id"]),
    )

    assert task["backend_id"] is None
    assert task["model"] is None
