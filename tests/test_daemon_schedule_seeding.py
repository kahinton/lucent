"""Tests for daemon system schedule seeding."""

from datetime import datetime, timezone
import sys
import types
from uuid import UUID

import pytest

import daemon.daemon as daemon_module
from daemon.daemon import LucentDaemon


@pytest.mark.asyncio
async def test_seed_system_schedules_includes_procedural_consolidation(monkeypatch):
    inserted_rows: list[tuple] = []

    class FakeConn:
        async def fetchrow(self, query, *args):
            if "FROM users" in query:
                return {
                    "id": UUID("11111111-1111-1111-1111-111111111111"),
                    "organization_id": UUID("22222222-2222-2222-2222-222222222222"),
                }
            if "FROM schedules" in query:
                return None
            return None

        async def execute(self, query, *args):
            if "INSERT INTO schedules" in query:
                inserted_rows.append(args)
            return "OK"

        async def close(self):
            return None

    async def _connect(_database_url):
        return FakeConn()

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=_connect))

    daemon = LucentDaemon()
    await daemon._seed_system_schedules()

    procedural = [row for row in inserted_rows if row[0] == "Procedural Consolidation"]
    assert len(procedural) == 1

    proc_row = procedural[0]
    assert proc_row[3] == "memory"  # agent_type
    assert proc_row[4] == "interval"  # schedule_type
    assert proc_row[5] == daemon_module.PROCEDURAL_CONSOLIDATION_MINUTES * 60
    assert proc_row[9] == daemon_module.PROCEDURAL_CONSOLIDATION_PROMPT
    assert isinstance(proc_row[7], datetime)
    assert proc_row[7].tzinfo == timezone.utc
