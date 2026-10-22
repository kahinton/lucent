"""Hard-cancel signalling: a cancel must stop work without breaking dispatch.

The regression: `cancel_task` silently refused claimed/running tasks, so a user
who clicked Stop watched the work continue. Cancellation is now a durable
request on the row plus a `task_cancel` notification, and the two failure modes
that matter are covered here:

* a cancel for a running task does not transition it out from under its owner,
  it records intent;
* a cancel that arrives for a task nobody is running still settles.
"""

import inspect
import uuid

import pytest
import pytest_asyncio

from lucent.constants import INTERRUPTIBLE_TASK_STATUSES, TERMINAL_TASK_STATUSES


@pytest_asyncio.fixture(autouse=True)
async def cleanup_requests(db_pool, test_organization):
    """Remove request rows before the shared user cleanup runs.

    ``requests.created_by`` references ``users`` with a restrictive delete; the
    generic fixture cannot drop the test user while these rows exist.
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
            await conn.execute("DELETE FROM tasks WHERE request_id = ANY($1)", request_ids)
            await conn.execute(
                "DELETE FROM requests WHERE organization_id = $1", org_uuid
            )


def test_cancel_channel_and_sweep_exist():
    """The notification, the listener, and the durability sweep must all exist.

    Any one of them missing reintroduces a silent-cancel path, so this asserts
    the three together rather than testing them in isolation.
    """
    from daemon.runtime import loops

    assert hasattr(loops.RuntimeLoopsMixin, "_on_task_cancel")
    assert hasattr(loops.RuntimeLoopsMixin, "_sweep_requested_cancels")
    assert "task_cancel" in inspect.getsource(loops.RuntimeLoopsMixin._setup_listen)


def test_dispatch_predicate_skips_cancelled_tasks():
    from lucent.db.requests import (
        DISPATCHABLE_TASK_PREDICATE,
        REMOTE_DISPATCHABLE_TASK_PREDICATE,
    )

    assert "cancel_requested_at IS NULL" in DISPATCHABLE_TASK_PREDICATE
    assert "cancel_requested_at IS NULL" in REMOTE_DISPATCHABLE_TASK_PREDICATE


def test_terminal_and_interruptible_status_sets_do_not_overlap():
    assert not (TERMINAL_TASK_STATUSES & INTERRUPTIBLE_TASK_STATUSES)


async def _make_request_with_task(db_pool, test_user, status: str):
    from lucent.db.requests import RequestRepository

    org_id = str(test_user["organization_id"])
    repo = RequestRepository(db_pool)
    request = await repo.create_request(
        title=f"cancel test {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    task = await repo.create_task(
        request_id=str(request["id"]),
        title="Cancellable task",
        org_id=org_id,
        description="Work that will be cancelled.",
        agent_type="code",
    )
    if status != "pending":
        claimed = await repo.claim_task(
            str(task["id"]), f"test-instance-{uuid.uuid4().hex[:6]}", org_id=org_id
        )
        assert claimed, "claim must succeed for a running-state test"
        if status == "running":
            await repo.start_task(
                str(task["id"]),
                org_id=org_id,
                instance_id=claimed["claimed_by"],
            )
    return repo, request, task


@pytest.mark.asyncio
async def test_cancel_pending_task_transitions_immediately(db_pool, test_user):
    repo, _request, task = await _make_request_with_task(db_pool, test_user, "pending")

    cancelled = await repo.cancel_task(
        str(task["id"]), org_id=str(test_user["organization_id"])
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["claimed", "running"])
async def test_cancel_running_task_records_intent_not_transition(
    db_pool, test_user, status
):
    """The owner still holds the claim, so cancel must signal rather than steal."""
    repo, _request, task = await _make_request_with_task(db_pool, test_user, status)

    result = await repo.cancel_task(
        str(task["id"]),
        org_id=str(test_user["organization_id"]),
        requested_by=str(test_user["id"]),
    )
    assert result is not None
    # Status is untouched — the running session finalizes it.
    assert result["status"] == status
    assert result["cancel_requested_at"] is not None
    assert str(result["cancel_requested_by"]) == str(test_user["id"])


@pytest.mark.asyncio
async def test_finalize_requires_a_cancel_request(db_pool, test_user):
    """Finalization must be gated, so a normal completion race is never mislabeled."""
    repo, _request, task = await _make_request_with_task(db_pool, test_user, "running")

    # No cancel was requested: finalize is a no-op.
    assert (
        await repo.finalize_requested_cancel(
            str(task["id"]), org_id=str(test_user["organization_id"])
        )
        is None
    )

    await repo.cancel_task(str(task["id"]), org_id=str(test_user["organization_id"]))
    finalized = await repo.finalize_requested_cancel(
        str(task["id"]), org_id=str(test_user["organization_id"])
    )
    assert finalized["status"] == "cancelled"
    assert finalized["claimed_by"] is None


@pytest.mark.asyncio
async def test_cancel_is_idempotent(db_pool, test_user):
    repo, _request, task = await _make_request_with_task(db_pool, test_user, "running")

    first = await repo.cancel_task(
        str(task["id"]), org_id=str(test_user["organization_id"])
    )
    second = await repo.cancel_task(
        str(task["id"]), org_id=str(test_user["organization_id"])
    )
    assert first is not None
    assert second is None, "a second cancel click must not re-fire the signal"


@pytest.mark.asyncio
async def test_request_cancel_signals_every_running_task(db_pool, test_user):
    """Cancelling a request is a hard stop for its whole tree, and only its tree."""
    from lucent.db.requests import RequestRepository

    org_id = str(test_user["organization_id"])
    repo = RequestRepository(db_pool)
    target = await repo.create_request(
        title=f"tree cancel {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    survivor_request = await repo.create_request(
        title=f"untouched {uuid.uuid4().hex[:8]}",
        org_id=org_id,
        created_by=str(test_user["id"]),
        source="user",
    )
    running = await repo.create_task(
        request_id=str(target["id"]),
        title="Running sibling",
        org_id=org_id,
        description="In flight when the request is cancelled.",
        agent_type="code",
    )
    queued = await repo.create_task(
        request_id=str(target["id"]),
        title="Queued sibling",
        org_id=org_id,
        description="Never started.",
        agent_type="code",
        sequence_order=1,
    )
    other = await repo.create_task(
        request_id=str(survivor_request["id"]),
        title="Other request",
        org_id=org_id,
        description="Must keep running.",
        agent_type="code",
    )
    claimed = await repo.claim_task(
        str(running["id"]), f"test-instance-{uuid.uuid4().hex[:6]}", org_id=org_id
    )
    await repo.claim_task(
        str(other["id"]), f"test-instance-{uuid.uuid4().hex[:6]}", org_id=org_id
    )

    await repo.update_request_status(str(target["id"]), "cancelled", org_id=org_id)

    running_after = await repo.get_task(str(running["id"]), org_id=org_id)
    queued_after = await repo.get_task(str(queued["id"]), org_id=org_id)
    other_after = await repo.get_task(str(other["id"]), org_id=org_id)

    assert queued_after["status"] == "cancelled"
    # The in-flight task is signalled, not transitioned underneath its owner.
    assert running_after["status"] == claimed["status"]
    assert running_after["cancel_requested_at"] is not None
    assert other_after["status"] == "claimed"
    assert other_after["cancel_requested_at"] is None


@pytest.mark.asyncio
async def test_expired_claim_finalizes_outstanding_cancel(db_pool, test_user):
    """A cancel whose owner died must still settle — the reaper completes it."""
    org_id = str(test_user["organization_id"])
    repo, _request, task = await _make_request_with_task(db_pool, test_user, "running")
    await repo.cancel_task(str(task["id"]), org_id=org_id)

    # Simulate the owning instance disappearing mid-cancel.
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE tasks
               SET claim_expires_at = NOW() - INTERVAL '1 hour'
               WHERE id = $1""",
            uuid.UUID(str(task["id"])),
        )

    finalized = await repo.finalize_stale_cancels(org_id=org_id)
    assert finalized == 1
    refreshed = await repo.get_task(str(task["id"]), org_id=org_id)
    assert refreshed["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_task_is_not_dispatchable(db_pool, test_user):
    """A requested cancel must never be handed to a worker in the meantime."""
    org_id = str(test_user["organization_id"])
    repo, request, task = await _make_request_with_task(db_pool, test_user, "running")
    await repo.cancel_task(str(task["id"]), org_id=org_id)
    # Release back to pending as a stale reaper would — the cancel intent must
    # still keep it out of the queue.
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                      claimed_at = NULL, claim_expires_at = NULL
               WHERE id = $1""",
            uuid.UUID(str(task["id"])),
        )
    pending = await repo.list_pending_tasks(org_id)
    assert str(task["id"]) not in {row["id"] for row in pending["items"]}
