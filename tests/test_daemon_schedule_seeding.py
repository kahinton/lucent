"""Tests for daemon system schedule seeding."""

import sys
import types
from datetime import datetime, timezone
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

    vitality = [row for row in inserted_rows if row[0] == "Memory Vitality Scoring"]
    assert len(vitality) == 1
    vit_row = vitality[0]
    assert vit_row[3] == "memory"  # agent_type
    assert vit_row[4] == "interval"  # schedule_type
    assert vit_row[5] == daemon_module.VITALITY_SCORING_MINUTES * 60
    assert vit_row[9] == daemon_module.MEMORY_VITALITY_SCORING_PROMPT

    shadow = [row for row in inserted_rows if row[0] == "Shadow Forget Scoring"]
    assert len(shadow) == 1
    shadow_row = shadow[0]
    assert shadow_row[3] == "memory"
    assert shadow_row[4] == "interval"
    assert shadow_row[5] == daemon_module.SHADOW_FORGET_SCORING_MINUTES * 60
    assert shadow_row[9] == daemon_module.SHADOW_FORGET_SCORING_PROMPT
    offset_seconds = (shadow_row[7] - vit_row[7]).total_seconds()
    assert offset_seconds >= (daemon_module.SHADOW_FORGET_OFFSET_MINUTES * 60) - 5


@pytest.mark.asyncio
async def test_seed_system_schedules_refreshes_existing_prompts(monkeypatch):
    updates: list[tuple] = []

    class FakeConn:
        async def fetchrow(self, query, *args):
            if "FROM users" in query:
                return {
                    "id": UUID("11111111-1111-1111-1111-111111111111"),
                    "organization_id": UUID("22222222-2222-2222-2222-222222222222"),
                }
            if "FROM schedules" in query:
                title = args[0]
                return {"id": UUID(int=len(str(title)))}
            return None

        async def execute(self, query, *args):
            if "UPDATE schedules SET" in query:
                updates.append(args)
            return "OK"

        async def close(self):
            return None

    async def _connect(_database_url):
        return FakeConn()

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=_connect))

    daemon = LucentDaemon()
    await daemon._seed_system_schedules()

    memory_updates = [row for row in updates if row[2].startswith("Autonomic memory maintenance")]
    assert len(memory_updates) == 1
    memory_update = memory_updates[0]
    assert memory_update[3] == "memory"
    assert memory_update[4] == "interval"
    assert memory_update[5] == daemon_module.AUTONOMIC_MINUTES * 60
    assert memory_update[8] == daemon_module.MEMORY_CONSOLIDATION_PROMPT
    assert "Desired Content Contract" in memory_update[8]
