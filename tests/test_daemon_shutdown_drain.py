"""Shutdown behavior: SIGTERM drains in-flight work instead of cancelling it."""

import asyncio

import pytest

import daemon.daemon as daemon_module
from daemon.runtime.loops import RuntimeLoopsMixin


class DrainHarness(RuntimeLoopsMixin):
    def __init__(self, sessions=None):
        self.active_sessions = list(sessions or [])
        self.DRAIN_TIMEOUT = 60
        self._tracer = None
        self._drain_active = _Counter()
        self.restarted = False
        self.running = True
        self.draining = False

    def _restart_self(self):
        self.restarted = True


class _Counter:
    def add(self, _n):
        return None


class Session:
    """A session that clears itself after a couple of drain polls."""

    def __init__(self, harness, polls=2):
        self._harness = harness
        self._polls = polls

    def destroy(self):
        return None

    async def tick(self):
        self._polls -= 1
        if self._polls <= 0 and self in self._harness.active_sessions:
            self._harness.active_sessions.remove(self)


@pytest.mark.asyncio
async def test_drain_returns_true_immediately_when_nothing_is_active():
    assert await DrainHarness()._drain_active_sessions() is True


@pytest.mark.asyncio
async def test_drain_waits_for_sessions_to_finish(monkeypatch):
    harness = DrainHarness()
    session = Session(harness)
    harness.active_sessions.append(session)

    async def fake_sleep(_seconds):
        await session.tick()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    assert await harness._drain_active_sessions() is True
    assert harness.active_sessions == []


@pytest.mark.asyncio
async def test_drain_gives_up_at_the_deadline(monkeypatch):
    harness = DrainHarness()
    harness.DRAIN_TIMEOUT = 0
    harness.active_sessions.append(object())

    assert await harness._drain_active_sessions() is False


@pytest.mark.asyncio
async def test_graceful_restart_still_restarts(monkeypatch):
    harness = DrainHarness()

    async def drain():
        return True

    monkeypatch.setattr(harness, "_drain_active_sessions", drain)
    monkeypatch.setattr(daemon_module, "DAEMON_SOURCE_RELOAD_ENABLED", True)

    await harness._graceful_restart()

    assert harness.restarted is True
    assert harness.running is False


@pytest.mark.asyncio
async def test_shutdown_signal_schedules_a_drain(monkeypatch):
    """SIGTERM must not cancel in-flight work; it starts a drain instead."""
    from daemon.daemon import LucentDaemon

    daemon = LucentDaemon.__new__(LucentDaemon)
    daemon._shutdown_started = False
    daemon.draining = False
    drained = asyncio.Event()

    async def drain():
        drained.set()

    monkeypatch.setattr(daemon, "_drain_active_sessions", drain)

    daemon._handle_shutdown()
    await asyncio.wait_for(drained.wait(), timeout=1)

    assert daemon.draining is True
    assert daemon._shutdown_started is True


@pytest.mark.asyncio
async def test_shutdown_signal_is_idempotent(monkeypatch):
    from daemon.daemon import LucentDaemon

    daemon = LucentDaemon.__new__(LucentDaemon)
    daemon._shutdown_started = False
    daemon.draining = False
    calls = []

    async def drain():
        calls.append(1)

    monkeypatch.setattr(daemon, "_drain_active_sessions", drain)

    daemon._handle_shutdown()
    daemon._handle_shutdown()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == [1]
