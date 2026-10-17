"""Controller supervision: desired worker count and one reconcile pass."""

import asyncio

import pytest

from daemon.launcher.base import WorkerHandle
from daemon.runtime.supervisor import SupervisorMixin, desired_worker_count


class FakeLauncher:
    """Records ensure/prune calls; reports a configurable running count."""

    def __init__(self, running: dict[str, int] | None = None):
        self.running_by_org = dict(running or {})
        self.ensured: list[tuple[str, int]] = []
        self.pruned: list[str] = []
        self.ensure_error: Exception | None = None

    async def ensure(self, org_id, count, spec):
        self.ensured.append((org_id, count))
        if self.ensure_error:
            raise self.ensure_error
        started = max(0, count - self.running_by_org.get(org_id, 0))
        self.running_by_org[org_id] = self.running_by_org.get(org_id, 0) + started
        return started

    async def running(self, org_id):
        return self.running_by_org.get(org_id, 0)

    async def list_workers(self, org_id):
        return [
            WorkerHandle(worker_id=f"w{i}", org_id=org_id, status="running", name="w")
            for i in range(self.running_by_org.get(org_id, 0))
        ]

    async def stop(self, worker_id, *, graceful=True):
        return None

    async def prune(self, org_id=None):
        self.pruned.append(org_id)
        return 0


class SupervisorHarness(SupervisorMixin):
    def __init__(self, launcher, organizations):
        self._launcher = launcher
        self._organizations = organizations
        self.draining = False
        self.running = True
        self._task_ready = asyncio.Event()

    async def _discover_orgs_with_work(self):
        return self._organizations

    async def _setup_listen(self):
        return True


def test_no_work_means_no_workers():
    assert desired_worker_count(0, 0) == 0


def test_the_warm_floor_holds_an_empty_org_open():
    """min_warm keeps workers alive with nothing queued."""
    assert desired_worker_count(0, 0, min_warm=1) == 1
    assert desired_worker_count(0, 0, min_warm=2) == 2


def test_work_above_the_floor_still_scales():
    assert desired_worker_count(2, 0, min_warm=1, max_per_org=4) == 2


def test_the_warm_floor_is_not_capped_by_the_work_limit():
    """The floor is an explicit operator setting; max_per_org bounds work only."""
    assert desired_worker_count(0, 0, min_warm=5, max_per_org=2) == 5
    assert desired_worker_count(1, 0, min_warm=5, max_per_org=2) == 5


def test_pending_work_gets_at_least_one_worker():
    assert desired_worker_count(1, 0) == 1


def test_in_flight_work_never_scales_to_zero():
    """An org whose worker died leaves a claimed task; it must get a worker back."""
    assert desired_worker_count(0, 1) == 1


def test_desired_count_is_capped_per_org():
    assert desired_worker_count(50, 0, max_per_org=2) == 2


def test_more_tasks_per_worker_needs_fewer_workers():
    assert desired_worker_count(4, 0, tasks_per_worker=4, max_per_org=10) == 1
    assert desired_worker_count(5, 0, tasks_per_worker=4, max_per_org=10) == 2


@pytest.mark.asyncio
async def test_reconcile_spawns_for_orgs_with_work_only():
    launcher = FakeLauncher()
    daemon = SupervisorHarness(
        launcher,
        [
            {"org_id": "org-a", "pending": 3, "running": 0},
            {"org_id": "org-b", "pending": 0, "running": 1},
        ],
    )

    summary = await daemon._reconcile_workers_once()

    assert summary["orgs"] == 2
    # org-a wants 2 (3 pending, capped at 2 per org); org-b wants 1 (in flight).
    assert summary["spawned"] == 3
    assert {org for org, _ in launcher.ensured} == {"org-a", "org-b"}


@pytest.mark.asyncio
async def test_reconcile_honours_the_warm_floor_for_an_empty_org():
    launcher = FakeLauncher()
    daemon = SupervisorHarness(
        launcher, [{"org_id": "org-a", "pending": 0, "running": 0, "min_warm": 1}]
    )

    summary = await daemon._reconcile_workers_once()

    assert summary["spawned"] == 1
    assert launcher.ensured == [("org-a", 1)]


@pytest.mark.asyncio
async def test_reconcile_does_not_respawn_when_a_worker_is_already_live():
    launcher = FakeLauncher({"org-a": 1})
    daemon = SupervisorHarness(launcher, [{"org_id": "org-a", "pending": 1, "running": 0}])

    summary = await daemon._reconcile_workers_once()

    assert summary["spawned"] == 0
    assert launcher.ensured == []


@pytest.mark.asyncio
async def test_reconcile_respects_the_global_ceiling(monkeypatch):
    import daemon.runtime.supervisor as supervisor

    monkeypatch.setattr(supervisor, "WORKER_MAX_TOTAL", 1)
    launcher = FakeLauncher()
    daemon = SupervisorHarness(
        launcher,
        [
            {"org_id": "org-a", "pending": 1, "running": 0},
            {"org_id": "org-b", "pending": 1, "running": 0},
        ],
    )

    summary = await daemon._reconcile_workers_once()

    assert summary["spawned"] == 1


@pytest.mark.asyncio
async def test_a_launcher_error_for_one_org_does_not_abort_the_pass():
    launcher = FakeLauncher()
    launcher.ensure_error = RuntimeError("docker unavailable")
    daemon = SupervisorHarness(launcher, [{"org_id": "org-a", "pending": 1, "running": 0}])

    summary = await daemon._reconcile_workers_once()

    assert summary["spawned"] == 0
    # The pass continued past the failure and still pruned.
    assert launcher.pruned == [None]


@pytest.mark.asyncio
async def test_reconcile_prunes_across_all_orgs():
    """Pruning is not limited to orgs that still need workers.

    An org that just stopped needing workers is absent from the discovered list,
    and its exited containers are exactly the ones left to collect.
    """
    launcher = FakeLauncher({"org-a": 1})
    daemon = SupervisorHarness(launcher, [{"org_id": "org-a", "pending": 1, "running": 0}])

    await daemon._reconcile_workers_once()

    assert launcher.pruned == [None]


@pytest.mark.asyncio
async def test_reconcile_still_prunes_with_nothing_discovered():
    launcher = FakeLauncher()
    daemon = SupervisorHarness(launcher, [])

    await daemon._reconcile_workers_once()

    assert launcher.pruned == [None]


@pytest.mark.asyncio
async def test_supervisor_loop_reconciles_then_stops(monkeypatch):
    launcher = FakeLauncher()
    daemon = SupervisorHarness(launcher, [])
    calls = []

    async def once():
        calls.append(1)
        daemon.running = False
        daemon._task_ready.set()  # skip the wait so the loop can exit
        return {}

    monkeypatch.setattr(daemon, "_reconcile_workers_once", once)

    await daemon._supervisor_loop()

    assert calls == [1]
