"""Decomposition ownership: who may break a request into tasks.

The regression these tests pin: the background composer used to decide purely
on "does this request have zero tasks", so a chat-authored *partial* plan was
invisible to it and got a second, divergent breakdown appended. Ownership is now
explicit on the request, and these tests assert the two paths can never race.
"""

import inspect
import uuid

import pytest
import pytest_asyncio

from lucent.constants import (
    DECOMPOSITION_MODE_AUTHOR,
    DECOMPOSITION_MODE_PLANNER,
    VALID_DECOMPOSITION_MODES,
)


@pytest_asyncio.fixture(autouse=True)
async def cleanup_requests(db_pool, test_organization):
    """Remove request rows before the shared user cleanup runs.

    ``requests.created_by`` references ``users`` with a restrictive delete, so
    the generic fixture cannot drop the test user while these rows exist.
    """
    org_uuid = test_organization["id"]
    yield
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM daemon_instances WHERE organization_id = $1", org_uuid
        )
        request_ids = [
            row["id"]
            for row in await conn.fetch(
                "SELECT id FROM requests WHERE organization_id = $1", org_uuid
            )
        ]
        if request_ids:
            task_ids = [
                row["id"]
                for row in await conn.fetch(
                    "SELECT id FROM tasks WHERE request_id = ANY($1)", request_ids
                )
            ]
            if task_ids:
                await conn.execute(
                    "DELETE FROM task_memories WHERE task_id = ANY($1)", task_ids
                )
                await conn.execute(
                    "DELETE FROM task_events WHERE task_id = ANY($1)", task_ids
                )
                await conn.execute(
                    "DELETE FROM task_outputs WHERE task_id = ANY($1)", task_ids
                )
            await conn.execute(
                "DELETE FROM llm_session_requests WHERE request_id = ANY($1)",
                request_ids,
            )
            await conn.execute("DELETE FROM tasks WHERE request_id = ANY($1)", request_ids)
            await conn.execute(
                "DELETE FROM requests WHERE organization_id = $1", org_uuid
            )
        await conn.execute("DELETE FROM llm_sessions WHERE organization_id = $1", org_uuid)


def test_decomposition_mode_values_are_closed():
    assert VALID_DECOMPOSITION_MODES == {"planner", "author"}


def test_composer_queries_require_planner_ownership():
    """A structural check on the two composer predicates.

    Both the candidate listing and the under-lock re-check must exclude
    author-owned requests before the zero-task test even runs; a gate on only
    one of them leaves a race window.
    """
    from lucent.db import daemon as daemon_repo

    for method in (
        daemon_repo.DaemonRepository.list_backfill_decomposition_requests,
        daemon_repo.DaemonRepository.request_is_still_undecomposed,
    ):
        source = inspect.getsource(method)
        assert "decomposition_mode = 'planner'" in source, method.__name__


@pytest.mark.asyncio
async def test_create_request_defaults_to_planner(db_pool, test_user):
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"planner-owned {uuid.uuid4().hex[:8]}",
        org_id=str(test_user["organization_id"]),
        created_by=str(test_user["id"]),
        source="user",
    )
    assert request["decomposition_mode"] == DECOMPOSITION_MODE_PLANNER


@pytest.mark.asyncio
async def test_create_request_with_tasks_is_author_owned(db_pool, test_user):
    from lucent.db.requests import RequestRepository

    org_id = str(test_user["organization_id"])
    repo = RequestRepository(db_pool)
    request = await repo.create_request_with_tasks(
        org_id=org_id,
        title=f"authored {uuid.uuid4().hex[:8]}",
        created_by=str(test_user["id"]),
        source="user",
        task_specs=[
            {
                "title": "First step",
                "description": "Do the first thing.",
                "agent_type": "code",
                "sequence_order": 0,
            },
            {
                "title": "Second step",
                "description": "Do the second thing.",
                "agent_type": "code",
                "sequence_order": 1,
            },
        ],
    )
    assert request["decomposition_mode"] == DECOMPOSITION_MODE_AUTHOR
    assert request["tasks_created"] == 2
    assert len(request["task_ids"]) == 2


@pytest.mark.asyncio
async def test_partial_plan_is_not_redecomposed(db_pool, test_user):
    """The exact regression: one authored task must not invite a second plan."""
    from lucent.db.daemon import DaemonRepository
    from lucent.db.requests import RequestRepository

    org_id = str(test_user["organization_id"])
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"partial {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
        decomposition_mode=DECOMPOSITION_MODE_AUTHOR,
    )
    await repo.create_task(
        request_id=str(request["id"]),
        title="The only step the user asked for",
        org_id=org_id,
        description="A single, deliberately scoped step.",
        agent_type="code",
    )

    async with db_pool.acquire() as conn:
        candidates = await DaemonRepository(
            conn
        ).list_backfill_decomposition_requests(org_id, min_age_seconds=0)
    assert str(request["id"]) not in {row["request_id"] for row in candidates}


@pytest.mark.asyncio
async def test_subtask_parent_index_links_within_plan(db_pool, test_user):
    """parent_task_index resolves against the plan, not pre-existing rows."""
    from lucent.db.requests import RequestRepository

    org_id = str(test_user["organization_id"])
    repo = RequestRepository(db_pool)
    request = await repo.create_request_with_tasks(
        org_id=org_id,
        title=f"tree {uuid.uuid4().hex[:8]}",
        created_by=str(test_user["id"]),
        source="user",
        task_specs=[
            {
                "title": "Parent step",
                "description": "The umbrella task.",
                "agent_type": "code",
                "sequence_order": 0,
            },
            {
                "title": "Child step",
                "description": "A sub-task of the parent.",
                "agent_type": "code",
                "sequence_order": 0,
                "parent_task_index": 0,
            },
        ],
    )
    parent_id, child_id = request["task_ids"]
    child = await repo.get_task(child_id, org_id=org_id)
    assert str(child["parent_task_id"]) == parent_id


@pytest.mark.asyncio
async def test_failed_spec_writes_nothing(db_pool, test_user):
    """Validation happens before any write, so a bad plan cannot half-land."""
    from lucent.db.requests import RequestRepository

    org_id = str(test_user["organization_id"])
    repo = RequestRepository(db_pool)
    title = f"atomic {uuid.uuid4().hex[:8]}"

    with pytest.raises(ValueError):
        await repo.create_request_with_tasks(
            org_id=org_id,
            title=title,
            created_by=str(test_user["id"]),
            source="user",
            task_specs=[
                {
                    "title": "Valid first step",
                    "description": "Fine.",
                    "agent_type": "code",
                },
                {
                    "title": "",  # invalid — must abort the whole plan
                    "description": "Missing a title.",
                    "agent_type": "code",
                },
            ],
        )

    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM requests WHERE organization_id = $1 AND title = $2",
            org_id,
            title,
        )
    assert count == 0


@pytest.mark.asyncio
async def test_switching_to_planner_with_tasks_is_refused(db_pool, test_user):
    """Handing an already-planned request back to the composer would double it."""
    from lucent.db.requests import RequestRepository

    org_id = str(test_user["organization_id"])
    repo = RequestRepository(db_pool)
    request = await repo.create_request_with_tasks(
        org_id=org_id,
        title=f"guarded {uuid.uuid4().hex[:8]}",
        created_by=str(test_user["id"]),
        source="user",
        task_specs=[
            {
                "title": "Only step",
                "description": "Do the thing.",
                "agent_type": "code",
            }
        ],
    )
    with pytest.raises(ValueError):
        await repo.update_request_decomposition_mode(
            str(request["id"]), DECOMPOSITION_MODE_PLANNER, org_id=org_id
        )

    updated = await repo.update_request_decomposition_mode(
        str(request["id"]), DECOMPOSITION_MODE_AUTHOR, org_id=org_id
    )
    assert updated["decomposition_mode"] == DECOMPOSITION_MODE_AUTHOR


@pytest.mark.asyncio
async def test_authoring_flip_requires_the_origin_session(db_pool, test_user):
    """Only the request's originating session may claim authorship of it."""
    from lucent.db.llm_sessions import LLMSessionRepository
    from lucent.db.requests import RequestRepository

    org_id = str(test_user["organization_id"])
    sessions = LLMSessionRepository(db_pool)
    origin = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    other = await sessions.create_session(
        org_id=org_id, user_id=str(test_user["id"]), kind="chat"
    )
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"origin {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    await sessions.link_request(
        str(origin["id"]), str(request["id"]), org_id=org_id, relation="created"
    )

    # A different session adding a task must not take ownership.
    await repo.create_task(
        request_id=str(request["id"]),
        title="Task from elsewhere",
        org_id=org_id,
        description="Should not flip ownership.",
        agent_type="code",
        authored_by_llm_session_id=str(other["id"]),
    )
    unchanged = await repo.get_request(str(request["id"]), org_id)
    assert unchanged["decomposition_mode"] == DECOMPOSITION_MODE_PLANNER

    # The originating session does.
    await repo.create_task(
        request_id=str(request["id"]),
        title="Task from the origin",
        org_id=org_id,
        description="Owns the decomposition now.",
        agent_type="code",
        authored_by_llm_session_id=str(origin["id"]),
    )
    flipped = await repo.get_request(str(request["id"]), org_id)
    assert flipped["decomposition_mode"] == DECOMPOSITION_MODE_AUTHOR
