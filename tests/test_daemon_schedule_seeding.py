"""Tests for daemon system schedule seeding."""

import sys
import types
from uuid import UUID

import pytest

import daemon.daemon as daemon_module
from daemon.daemon import LucentDaemon


@pytest.mark.asyncio
async def test_seed_system_schedules_excludes_retired_procedure_cleanup(monkeypatch):
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

    memory_consolidation = [row for row in inserted_rows if row[0] == "Memory Consolidation"]
    assert memory_consolidation == []

    retired_cleanup = [row for row in inserted_rows if row[0] == "Procedural Consolidation"]
    assert retired_cleanup == []

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
    assert memory_updates == []

    learning_updates = [row for row in updates if row[2].startswith("Process recent work results")]
    assert len(learning_updates) == 1
