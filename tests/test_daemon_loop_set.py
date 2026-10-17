"""The loop set a process runs, given its roles.

The always-on process supervises and schedules; only a worker dispatches, and
only a worker idle-exits.
"""

import pytest

import daemon.daemon as daemon_module
from daemon.daemon import LucentDaemon


class LoopHarness(LucentDaemon):
    """LucentDaemon with every loop body stubbed to a no-op coroutine."""

    def __init__(self, roles):
        self.roles = roles

    async def _heartbeat_loop(self):
        return None

    async def _dispatch_loop(self):
        return None

    async def _scheduler_loop(self):
        return None

    async def _decomposition_loop(self):
        return None

    async def _reload_watcher(self):
        return None

    async def _supervisor_loop(self):
        return None

    async def _idle_exit_loop(self):
        return None


async def _build(monkeypatch, roles, *, worker=False, source_reload=False):
    monkeypatch.setattr(daemon_module, "DAEMON_WORKER", worker)
    monkeypatch.setattr(daemon_module, "DAEMON_SOURCE_RELOAD_ENABLED", source_reload)
    loops = LoopHarness(roles)._build_loops()
    names = sorted(task.get_name() for task in loops)
    for task in loops:
        task.cancel()
    return names


@pytest.mark.asyncio
async def test_always_on_process_supervises_and_schedules(monkeypatch):
    names = await _build(monkeypatch, {"scheduler", "controller"})

    assert names == ["heartbeat", "request-decomposition", "scheduler", "worker-supervisor"]
    assert "dispatch" not in names


@pytest.mark.asyncio
async def test_a_worker_dispatches_and_idle_exits(monkeypatch):
    names = await _build(monkeypatch, {"dispatcher"}, worker=True)

    assert names == ["dispatch", "heartbeat", "worker-idle-exit"]
    assert "worker-supervisor" not in names


@pytest.mark.asyncio
async def test_a_hand_run_dispatcher_does_not_idle_exit(monkeypatch):
    """An in-process dispatcher someone started deliberately keeps running."""
    names = await _build(monkeypatch, {"dispatcher"}, worker=False)

    assert names == ["dispatch", "heartbeat"]
    assert "worker-idle-exit" not in names


@pytest.mark.asyncio
async def test_source_reload_adds_only_the_reload_watcher(monkeypatch):
    names = await _build(monkeypatch, {"scheduler", "controller"}, source_reload=True)

    assert "reload-watcher" in names
