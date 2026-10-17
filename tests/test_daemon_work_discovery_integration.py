"""Cross-org work discovery against a real database.

The controller cannot use the daemon API to learn which orgs have work — the
daemon API key is org-bound and there is no cross-org endpoint. So it reads
aggregate counts directly, and this test proves that path returns exactly the
orgs with work and nothing about the work itself.
"""

import os

import pytest

import daemon.daemon as daemon_module
from daemon.daemon import LucentDaemon
from lucent.db import OrganizationRepository
from lucent.db.requests import RequestRepository


def _controller():
    return LucentDaemon.__new__(LucentDaemon)


@pytest.fixture(autouse=True)
def _point_daemon_at_test_db(monkeypatch):
    # The daemon module resolves its DB URL once at import; pin it to the
    # isolated test database so connect_scoped reads the right place.
    monkeypatch.setattr(daemon_module, "DATABASE_URL", os.environ["DATABASE_URL"])


@pytest.mark.asyncio
async def test_discovery_returns_orgs_with_pending_work(
    db_pool, test_organization, clean_test_data
):
    org_with_work = str(test_organization["id"])
    # A second org with no work at all.
    await OrganizationRepository(db_pool).create(name=f"{clean_test_data}quiet-org")

    repo = RequestRepository(db_pool)
    request = await repo.create_request(title="Needs doing", org_id=org_with_work)
    await repo.create_task(request_id=str(request["id"]), title="Step 1", org_id=org_with_work)

    found = await _controller()._discover_orgs_with_work()
    by_org = {entry["org_id"]: entry for entry in found}

    assert org_with_work in by_org
    assert by_org[org_with_work]["pending"] >= 1
    # The quiet org may still be listed — its warm floor entitles it to a worker —
    # but no work may be attributed to it.
    quiet = by_org.get(f"{clean_test_data}quiet-org")
    assert quiet is None or (quiet["pending"] == 0 and quiet["running"] == 0)


@pytest.mark.asyncio
async def test_discovery_omits_orgs_with_neither_work_nor_a_floor(
    db_pool, test_organization, clean_test_data, monkeypatch
):
    """With no warm floor, an idle org is invisible to the controller."""
    import daemon.runtime.supervisor as supervisor_module

    monkeypatch.setattr(
        supervisor_module.runtime_settings, "daemon_worker_min_warm", lambda **_: 0
    )

    org_with_work = str(test_organization["id"])
    org_without = await OrganizationRepository(db_pool).create(
        name=f"{clean_test_data}quiet-org"
    )

    repo = RequestRepository(db_pool)
    request = await repo.create_request(title="Needs doing", org_id=org_with_work)
    await repo.create_task(request_id=str(request["id"]), title="Step 1", org_id=org_with_work)

    found = await _controller()._discover_orgs_with_work()
    by_org = {entry["org_id"]: entry for entry in found}

    assert org_with_work in by_org
    assert str(org_without["id"]) not in by_org


@pytest.mark.asyncio
async def test_discovery_returns_counts_only_never_content(
    db_pool, test_organization, clean_test_data
):
    org_id = str(test_organization["id"])
    secret_title = f"{clean_test_data}SECRET-TITLE"

    repo = RequestRepository(db_pool)
    request = await repo.create_request(title=secret_title, org_id=org_id)
    await repo.create_task(request_id=str(request["id"]), title=secret_title, org_id=org_id)

    found = await _controller()._discover_orgs_with_work()

    assert found, "expected the org with work to be discovered"
    for entry in found:
        assert set(entry) == {"org_id", "name", "pending", "running", "min_warm"}
        assert secret_title not in str(entry)


@pytest.mark.asyncio
async def test_undispatchable_pending_work_is_not_discovered(
    db_pool, test_organization, clean_test_data
):
    """A pending task that can never be claimed must not spawn a worker.

    Counting raw `pending` rows would spawn workers for a cancelled request's
    leftovers, which then idle out and get respawned forever. Discovery uses the
    dispatcher's own predicate so it only sees work the dispatcher would claim.
    """
    org_id = str(test_organization["id"])
    repo = RequestRepository(db_pool)
    request = await repo.create_request(title="Doomed", org_id=org_id)
    await repo.create_task(request_id=str(request["id"]), title="Step", org_id=org_id)
    await repo.update_request_status(str(request["id"]), "cancelled", org_id=org_id)

    found = await _controller()._discover_orgs_with_work()
    by_org = {entry["org_id"]: entry for entry in found}

    assert by_org.get(org_id, {}).get("pending", 0) == 0


@pytest.mark.asyncio
async def test_stage_gated_tasks_are_not_discovered(
    db_pool, test_organization, clean_test_data
):
    """Only the currently-runnable stage counts, not the whole pipeline."""
    org_id = str(test_organization["id"])
    repo = RequestRepository(db_pool)
    request = await repo.create_request(title="Staged", org_id=org_id)
    await repo.create_task(
        request_id=str(request["id"]), title="Stage 0", org_id=org_id, sequence_order=0
    )
    await repo.create_task(
        request_id=str(request["id"]), title="Stage 1", org_id=org_id, sequence_order=1
    )

    found = await _controller()._discover_orgs_with_work()
    by_org = {entry["org_id"]: entry for entry in found}

    assert by_org[org_id]["pending"] == 1


@pytest.mark.asyncio
async def test_in_flight_work_is_discovered_so_a_dead_worker_can_be_replaced(
    db_pool, test_organization, clean_test_data
):
    """An org whose only task is claimed must still be discovered.

    That is how a crashed worker's task gets recovered: the controller sees the
    in-flight count, spawns a worker, and the worker's dispatch loop releases the
    stale claim and retries.
    """
    org_id = str(test_organization["id"])
    repo = RequestRepository(db_pool)
    request = await repo.create_request(title="In flight", org_id=org_id)
    task = await repo.create_task(request_id=str(request["id"]), title="Step", org_id=org_id)

    await repo.claim_task(str(task["id"]), "dead-worker", org_id=org_id)

    found = await _controller()._discover_orgs_with_work()
    by_org = {entry["org_id"]: entry for entry in found}

    assert org_id in by_org
    assert by_org[org_id]["running"] >= 1
    assert by_org[org_id]["pending"] == 0
