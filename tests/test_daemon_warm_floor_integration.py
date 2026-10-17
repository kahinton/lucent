"""The warm floor is reserved atomically.

Two idle workers must not both decide to leave and drop an organization below
its floor. The decision is serialized per org against the instance registry, and
these tests exercise it there.
"""

import pytest

from lucent.db.daemon import DaemonRepository
from lucent.db.requests import RequestRepository


async def _register(db_pool, org_id, instance_id, roles):
    await RequestRepository(db_pool).register_instance(
        org_id=str(org_id), instance_id=instance_id, roles=roles
    )


async def _reserve(db_pool, org_id, instance_id, min_warm):
    async with db_pool.acquire() as conn:
        return await DaemonRepository(conn).try_reserve_idle_exit(
            str(org_id), instance_id, min_warm
        )


@pytest.mark.asyncio
async def test_a_lone_worker_cannot_leave_below_a_floor_of_one(
    db_pool, test_organization, clean_test_data
):
    org = test_organization["id"]
    await _register(db_pool, org, f"{clean_test_data}w1", ["dispatcher"])

    assert await _reserve(db_pool, org, f"{clean_test_data}w1", 1) is False


@pytest.mark.asyncio
async def test_the_first_of_two_may_leave_and_the_second_may_not(
    db_pool, test_organization, clean_test_data
):
    """The race this guards: both see 'there are two of us' and both leave."""
    org = test_organization["id"]
    first, second = f"{clean_test_data}w1", f"{clean_test_data}w2"
    await _register(db_pool, org, first, ["dispatcher"])
    await _register(db_pool, org, second, ["dispatcher"])

    assert await _reserve(db_pool, org, first, 1) is True
    # The first is now 'stopping', so it no longer counts toward the floor.
    assert await _reserve(db_pool, org, second, 1) is False


@pytest.mark.asyncio
async def test_a_floor_of_zero_lets_everyone_leave(
    db_pool, test_organization, clean_test_data
):
    org = test_organization["id"]
    first, second = f"{clean_test_data}w1", f"{clean_test_data}w2"
    await _register(db_pool, org, first, ["dispatcher"])
    await _register(db_pool, org, second, ["dispatcher"])

    assert await _reserve(db_pool, org, first, 0) is True
    assert await _reserve(db_pool, org, second, 0) is True


@pytest.mark.asyncio
async def test_the_controller_does_not_count_toward_the_floor(
    db_pool, test_organization, clean_test_data
):
    """A controller registers for the org it supervises, but it is not a worker."""
    org = test_organization["id"]
    await _register(db_pool, org, f"{clean_test_data}ctl", ["controller", "scheduler"])
    worker = f"{clean_test_data}w1"
    await _register(db_pool, org, worker, ["dispatcher"])

    # Only the worker counts, so it is alone and must stay.
    assert await _reserve(db_pool, org, worker, 1) is False
