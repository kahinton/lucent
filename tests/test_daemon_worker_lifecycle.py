"""Worker idle-exit: an on-demand worker ends itself once it has no work."""

import pytest

import daemon.daemon as daemon_module
from daemon.runtime.supervisor import WorkerLifecycleMixin


class _FrozenTime:
    """Minimal stand-in for the time module used by the idle clock."""

    def __init__(self, now: float):
        self._now = now

    def monotonic(self) -> float:
        return self._now


class WorkerHarness(WorkerLifecycleMixin):
    def __init__(self, *, owned_active=0, may_release=True):
        self.active_sessions: list = []
        self._claim_operations = 0
        self._owned_active = owned_active
        self._last_work_at = 0.0
        self.running = True
        self.draining = False
        self._may_release = may_release

    async def _has_owned_active_tasks(self) -> int:
        return self._owned_active

    async def _release_warm_slot(self) -> bool:
        return self._may_release


def _set_idle_seconds(monkeypatch, seconds: int) -> None:
    monkeypatch.setattr(daemon_module, "WORKER_IDLE_SECONDS", seconds)


@pytest.mark.asyncio
async def test_idle_exit_is_false_while_a_session_is_active(monkeypatch):
    import daemon.runtime.supervisor as supervisor

    daemon = WorkerHarness()
    daemon.active_sessions.append("engine-session-x")
    daemon._last_work_at = 0.0
    monkeypatch.setattr(supervisor, "time", _FrozenTime(now=10_000.0))
    _set_idle_seconds(monkeypatch, 300)

    assert await daemon._should_idle_exit() is False
    assert daemon._last_work_at == 10_000.0  # clock refreshed while busy


@pytest.mark.asyncio
async def test_idle_exit_is_false_while_a_claim_is_in_flight(monkeypatch):
    daemon = WorkerHarness()
    daemon._claim_operations = 1
    daemon._last_work_at = 5.0

    assert await daemon._should_idle_exit() is False


@pytest.mark.asyncio
async def test_idle_exit_is_false_while_the_db_reports_owned_tasks():
    """Catches the claim -> session-append window the in-process flags miss."""
    daemon = WorkerHarness(owned_active=1)

    assert await daemon._should_idle_exit() is False


@pytest.mark.asyncio
async def test_idle_exit_becomes_true_once_idle_past_the_window(monkeypatch):
    import daemon.runtime.supervisor as supervisor

    monkeypatch.setattr(supervisor, "time", _FrozenTime(now=1_000.0))
    daemon = WorkerHarness()
    daemon._last_work_at = 1_000.0 - 301.0
    _set_idle_seconds(monkeypatch, 300)

    assert await daemon._should_idle_exit() is True


@pytest.mark.asyncio
async def test_idle_exit_is_false_before_the_window_elapses(monkeypatch):
    import daemon.runtime.supervisor as supervisor

    monkeypatch.setattr(supervisor, "time", _FrozenTime(now=1_000.0))
    daemon = WorkerHarness()
    daemon._last_work_at = 1_000.0 - 10.0
    _set_idle_seconds(monkeypatch, 300)

    assert await daemon._should_idle_exit() is False


@pytest.mark.asyncio
async def test_idle_exit_loop_stops_the_worker(monkeypatch):
    daemon = WorkerHarness()

    async def should_exit():
        return True

    monkeypatch.setattr(daemon, "_should_idle_exit", should_exit)

    await daemon._idle_exit_loop()

    assert daemon.running is False
    assert daemon.draining is True


@pytest.mark.asyncio
async def test_the_warm_floor_vetoes_an_idle_exit(monkeypatch):
    """Idle past the window is not enough — the org's floor must allow it."""
    import daemon.runtime.supervisor as supervisor

    monkeypatch.setattr(supervisor, "time", _FrozenTime(now=1_000.0))
    daemon = WorkerHarness(may_release=False)
    daemon._last_work_at = 1_000.0 - 999.0
    _set_idle_seconds(monkeypatch, 300)

    assert await daemon._should_idle_exit() is False


@pytest.mark.asyncio
async def test_no_warm_floor_releases_without_the_registry(monkeypatch):
    """With a floor of 0 there is nothing to coordinate."""
    from daemon.runtime.supervisor import WorkerLifecycleMixin

    daemon = WorkerLifecycleMixin()
    daemon.instance_id = "w-1"

    async def bound_org():
        return "org-1"

    daemon._get_daemon_org_id = bound_org  # type: ignore[method-assign]
    monkeypatch.setattr(daemon_module, "WORKER_MIN_WARM", 0)

    class _Settings:
        @staticmethod
        def daemon_worker_min_warm(**kwargs):
            return 0

    monkeypatch.setattr("daemon.runtime.supervisor.runtime_settings", _Settings)

    assert await daemon._release_warm_slot() is True
