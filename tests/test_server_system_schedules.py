"""Tests for server-side built-in system schedules."""

from lucent.api.system_schedules import (
    STALE_TASK_REAPER_TITLE,
    ensure_server_system_schedules,
    run_server_system_schedules_once,
)
from lucent.db.requests import RequestRepository


async def _make_request(repo: RequestRepository, org_id: str):
    return await repo.create_request(title="Reaper test request", org_id=org_id)


async def _make_task(repo: RequestRepository, request_id: str, org_id: str):
    return await repo.create_task(request_id=request_id, title="Reaper test task", org_id=org_id)


class TestServerSystemSchedules:
    async def test_seeds_stale_task_reaper_schedule(self, db_pool, test_organization):
        created = await ensure_server_system_schedules()
        assert created >= 0

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT title, schedule_type, interval_seconds, is_system, enabled
                   FROM schedules
                   WHERE organization_id = $1
                     AND title = $2
                     AND is_system = true""",
                test_organization["id"],
                STALE_TASK_REAPER_TITLE,
            )

        assert row is not None
        assert row["schedule_type"] == "interval"
        assert int(row["interval_seconds"]) in {120, 180}
        assert row["is_system"] is True
        assert row["enabled"] is True

    async def test_runs_stale_task_reaper_server_side(self, db_pool, test_organization):
        org_id = str(test_organization["id"])
        repo = RequestRepository(db_pool)

        await ensure_server_system_schedules()

        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-server-reaper")

        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks
                   SET claim_expires_at = NOW() - INTERVAL '2 minutes'
                   WHERE id = $1""",
                task["id"],
            )
            await conn.execute(
                """UPDATE schedules
                   SET next_run_at = NOW() - INTERVAL '1 minute'
                   WHERE organization_id = $1
                     AND title = $2
                     AND is_system = true""",
                test_organization["id"],
                STALE_TASK_REAPER_TITLE,
            )

        fired = await run_server_system_schedules_once()
        assert fired >= 1

        refreshed = await repo.get_task(str(task["id"]))
        assert refreshed["status"] == "pending"
        assert refreshed["claimed_by"] is None

        events = await repo.list_task_events(str(task["id"]))
        assert any(e["event_type"] == "reaper" for e in events["items"])

        async with db_pool.acquire() as conn:
            run = await conn.fetchrow(
                """SELECT sr.status
                   FROM schedule_runs sr
                   JOIN schedules s ON s.id = sr.schedule_id
                   WHERE s.organization_id = $1
                     AND s.title = $2
                     AND s.is_system = true
                   ORDER BY sr.created_at DESC
                   LIMIT 1""",
                test_organization["id"],
                STALE_TASK_REAPER_TITLE,
            )
        assert run is not None
        assert run["status"] == "completed"

    async def test_reaper_does_not_release_active_claims(self, db_pool, test_organization):
        org_id = str(test_organization["id"])
        repo = RequestRepository(db_pool)

        await ensure_server_system_schedules()

        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-active-claim")

        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks
                   SET claim_expires_at = NOW() + INTERVAL '5 minutes'
                   WHERE id = $1""",
                task["id"],
            )
            await conn.execute(
                """UPDATE schedules
                   SET next_run_at = NOW() - INTERVAL '1 minute'
                   WHERE organization_id = $1
                     AND title = $2
                     AND is_system = true""",
                test_organization["id"],
                STALE_TASK_REAPER_TITLE,
            )

        fired = await run_server_system_schedules_once()
        assert fired >= 1

        refreshed = await repo.get_task(str(task["id"]))
        assert refreshed["status"] == "claimed"
        assert refreshed["claimed_by"] == "inst-active-claim"

        events = await repo.list_task_events(str(task["id"]))
        assert not any(e["event_type"] == "reaper" for e in events["items"])

    async def test_reaper_runs_without_daemon_instance(self, db_pool, test_organization):
        org_id = str(test_organization["id"])
        repo = RequestRepository(db_pool)

        await ensure_server_system_schedules()

        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-daemon-down")

        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM daemon_instances WHERE organization_id = $1",
                test_organization["id"],
            )
            await conn.execute(
                """UPDATE tasks
                   SET claim_expires_at = NOW() - INTERVAL '2 minutes'
                   WHERE id = $1""",
                task["id"],
            )
            await conn.execute(
                """UPDATE schedules
                   SET next_run_at = NOW() - INTERVAL '1 minute'
                   WHERE organization_id = $1
                     AND title = $2
                     AND is_system = true""",
                test_organization["id"],
                STALE_TASK_REAPER_TITLE,
            )

        fired = await run_server_system_schedules_once()
        assert fired >= 1

        refreshed = await repo.get_task(str(task["id"]))
        assert refreshed["status"] == "pending"
        assert refreshed["claimed_by"] is None

        events = await repo.list_task_events(str(task["id"]))
        assert any(e["event_type"] == "reaper" for e in events["items"])
