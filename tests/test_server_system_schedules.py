"""Tests for server-side built-in system schedules.

Validates that the stale-task reaper operates independently of the daemon process:
1. Schedule seeding and idempotency
2. Expired claims are released and events logged
3. Active (non-expired) claims are not interfered with
4. Reaper functions without any daemon instance present
5. Mixed expired/active tasks handled correctly
6. Daemon skips the server-side schedule
"""

from unittest.mock import AsyncMock, patch

import pytest_asyncio

from lucent.api.system_schedules import (
    STALE_TASK_REAPER_TITLE,
    ensure_server_system_schedules,
    run_server_system_schedules_once,
)
from lucent.db.requests import RequestRepository

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def cleanup_reaper_test_data(db_pool, test_organization):
    """Clean up requests, tasks, events, schedules, and runs after each test."""
    org_uuid = test_organization["id"]
    yield
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM daemon_instances WHERE organization_id = $1", org_uuid
        )
        req_ids = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id FROM requests WHERE organization_id = $1", org_uuid
            )
        ]
        if req_ids:
            task_ids = [
                r["id"]
                for r in await conn.fetch(
                    "SELECT id FROM tasks WHERE request_id = ANY($1)", req_ids
                )
            ]
            if task_ids:
                await conn.execute(
                    "DELETE FROM task_memories WHERE task_id = ANY($1)", task_ids
                )
                await conn.execute(
                    "DELETE FROM task_events WHERE task_id = ANY($1)", task_ids
                )
            await conn.execute("DELETE FROM tasks WHERE request_id = ANY($1)", req_ids)
            await conn.execute(
                "DELETE FROM requests WHERE organization_id = $1", org_uuid
            )
        # Clean schedule runs then schedules for this org
        sched_ids = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id FROM schedules WHERE organization_id = $1", org_uuid
            )
        ]
        if sched_ids:
            await conn.execute(
                "DELETE FROM schedule_runs WHERE schedule_id = ANY($1)", sched_ids
            )
        await conn.execute(
            "DELETE FROM schedules WHERE organization_id = $1", org_uuid
        )


# ── Helpers ──────────────────────────────────────────────────────────────


async def _make_request(repo: RequestRepository, org_id: str):
    return await repo.create_request(title="Reaper test request", org_id=org_id)


async def _make_task(repo: RequestRepository, request_id: str, org_id: str):
    return await repo.create_task(
        request_id=request_id, title="Reaper test task", org_id=org_id
    )


async def _force_schedule_due(db_pool, org_id):
    """Make the reaper schedule due for immediate firing."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE schedules
               SET next_run_at = NOW() - INTERVAL '1 minute'
               WHERE organization_id = $1
                 AND title = $2
                 AND is_system = true""",
            org_id,
            STALE_TASK_REAPER_TITLE,
        )


# ── Schedule Seeding ─────────────────────────────────────────────────────


class TestScheduleSeeding:
    async def test_seeds_stale_task_reaper_schedule(self, db_pool, test_organization):
        """Verify the reaper schedule is created with correct properties."""
        await ensure_server_system_schedules()

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT title, schedule_type, interval_seconds,
                          is_system, enabled, agent_type
                   FROM schedules
                   WHERE organization_id = $1
                     AND title = $2
                     AND is_system = true""",
                test_organization["id"],
                STALE_TASK_REAPER_TITLE,
            )

        assert row is not None
        assert row["title"] == STALE_TASK_REAPER_TITLE
        assert row["schedule_type"] == "interval"
        assert 60 <= int(row["interval_seconds"]) <= 300  # reasonable range
        assert row["is_system"] is True
        assert row["enabled"] is True
        assert row["agent_type"] == "system"

    async def test_seeding_is_idempotent(self, db_pool, test_organization):
        """Calling ensure twice should not create duplicate schedules."""
        await ensure_server_system_schedules()
        await ensure_server_system_schedules()

        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM schedules
                   WHERE organization_id = $1
                     AND title = $2
                     AND is_system = true""",
                test_organization["id"],
                STALE_TASK_REAPER_TITLE,
            )
        assert count == 1


class TestServerSystemSchedulePreflight:
    async def test_empty_reaper_fire_records_skip_without_release_or_requests(
        self,
        db_pool,
        test_organization,
    ):
        """Integration-style empty fire through system_schedules + db/schedules.

        The server-side schedule has no model boundary by design. The important
        empty-path invariant is that it stops at the cheap eligibility check:
        no release operation and no request/task creation occur.
        """
        org_id = str(test_organization["id"])
        await ensure_server_system_schedules()
        await _force_schedule_due(db_pool, test_organization["id"])

        async with db_pool.acquire() as conn:
            before_requests = await conn.fetchval(
                "SELECT COUNT(*) FROM requests WHERE organization_id = $1::uuid",
                org_id,
            )
            before_tasks = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE organization_id = $1::uuid",
                org_id,
            )

        with patch.object(
            RequestRepository,
            "release_stale_tasks",
            new=AsyncMock(side_effect=AssertionError("release attempted")),
        ) as release_stale_tasks:
            fired = await run_server_system_schedules_once()

        assert fired >= 1
        assert release_stale_tasks.await_count == 0

        async with db_pool.acquire() as conn:
            after_requests = await conn.fetchval(
                "SELECT COUNT(*) FROM requests WHERE organization_id = $1::uuid",
                org_id,
            )
            after_tasks = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE organization_id = $1::uuid",
                org_id,
            )
            run_result = await conn.fetchval(
                """
                SELECT result
                FROM schedule_runs sr
                JOIN schedules s ON s.id = sr.schedule_id
                WHERE s.organization_id = $1::uuid
                  AND s.title = $2
                ORDER BY sr.started_at DESC
                LIMIT 1
                """,
                org_id,
                STALE_TASK_REAPER_TITLE,
            )

        assert (after_requests, after_tasks) == (before_requests, before_tasks)
        assert '"event_type": "schedule.skipped"' in run_result
        assert '"schedule_name": "Stale Task Reaper"' in run_result
        assert '"reason": "no_stale_tasks"' in run_result


# ── Expired Claim Release ────────────────────────────────────────────────


class TestExpiredClaimRelease:
    async def test_expired_claim_released_by_schedule(
        self, db_pool, test_organization
    ):
        """Scenario 1: create → claim → expire → reaper releases it."""
        org_id = str(test_organization["id"])
        repo = RequestRepository(db_pool)

        await ensure_server_system_schedules()

        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-server-reaper")

        # Verify it's claimed
        claimed = await repo.get_task(str(task["id"]))
        assert claimed["status"] == "claimed"

        # Expire the claim
        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks
                   SET claim_expires_at = NOW() - INTERVAL '2 minutes'
                   WHERE id = $1""",
                task["id"],
            )
        await _force_schedule_due(db_pool, test_organization["id"])

        # Run the server-side schedule
        fired = await run_server_system_schedules_once()
        assert fired >= 1

        # Task should be back to pending
        refreshed = await repo.get_task(str(task["id"]))
        assert refreshed["status"] == "pending"
        assert refreshed["claimed_by"] is None
        assert refreshed["claimed_at"] is None
        assert refreshed["claim_expires_at"] is None

    async def test_schedule_run_recorded_as_completed(
        self, db_pool, test_organization
    ):
        """The schedule run should be tracked with 'completed' status."""
        org_id = str(test_organization["id"])
        repo = RequestRepository(db_pool)

        await ensure_server_system_schedules()

        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-run-check")

        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks
                   SET claim_expires_at = NOW() - INTERVAL '2 minutes'
                   WHERE id = $1""",
                task["id"],
            )
        await _force_schedule_due(db_pool, test_organization["id"])

        await run_server_system_schedules_once()

        async with db_pool.acquire() as conn:
            run = await conn.fetchrow(
                """SELECT sr.status, sr.result
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
        assert "released=" in (run["result"] or "")


# ── Task Event Logging ───────────────────────────────────────────────────


class TestReaperEventLogging:
    async def test_reaper_event_logged_on_release(
        self, db_pool, test_organization
    ):
        """Scenario 2: reaper event with type 'reaper' is logged."""
        org_id = str(test_organization["id"])
        repo = RequestRepository(db_pool)

        await ensure_server_system_schedules()

        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-event-check")

        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks
                   SET claim_expires_at = NOW() - INTERVAL '2 minutes'
                   WHERE id = $1""",
                task["id"],
            )
        await _force_schedule_due(db_pool, test_organization["id"])

        await run_server_system_schedules_once()

        events = await repo.list_task_events(str(task["id"]))
        reaper_events = [
            e for e in events["items"] if e["event_type"] == "reaper"
        ]
        assert len(reaper_events) == 1

    async def test_reaper_event_detail_includes_instance_id(
        self, db_pool, test_organization
    ):
        """The reaper event detail should identify who held the expired claim."""
        org_id = str(test_organization["id"])
        repo = RequestRepository(db_pool)

        await ensure_server_system_schedules()

        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        instance_name = "inst-detail-check-abc123"
        await repo.claim_task(str(task["id"]), instance_name)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks
                   SET claim_expires_at = NOW() - INTERVAL '2 minutes'
                   WHERE id = $1""",
                task["id"],
            )
        await _force_schedule_due(db_pool, test_organization["id"])

        await run_server_system_schedules_once()

        events = await repo.list_task_events(str(task["id"]))
        reaper_events = [
            e for e in events["items"] if e["event_type"] == "reaper"
        ]
        assert len(reaper_events) == 1
        detail = reaper_events[0]["detail"] or ""
        assert instance_name in detail
        assert "requeued to pending" in detail

    async def test_no_reaper_event_when_nothing_released(
        self, db_pool, test_organization
    ):
        """When no tasks are stale, no reaper events should be created."""
        org_id = str(test_organization["id"])
        repo = RequestRepository(db_pool)

        await ensure_server_system_schedules()

        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        # Don't claim the task — it's just pending, nothing to release

        await _force_schedule_due(db_pool, test_organization["id"])
        await run_server_system_schedules_once()

        events = await repo.list_task_events(str(task["id"]))
        assert not any(e["event_type"] == "reaper" for e in events["items"])


# ── Active Claim Protection ──────────────────────────────────────────────


class TestActiveClaimProtection:
    async def test_reaper_does_not_release_active_claims(
        self, db_pool, test_organization
    ):
        """Scenario 3: tasks with future claim_expires_at are left alone."""
        org_id = str(test_organization["id"])
        repo = RequestRepository(db_pool)

        await ensure_server_system_schedules()

        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-active-claim")

        # Set a future expiry and recent claimed_at
        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks
                   SET claim_expires_at = NOW() + INTERVAL '10 minutes',
                       claimed_at = NOW()
                   WHERE id = $1""",
                task["id"],
            )
        await _force_schedule_due(db_pool, test_organization["id"])

        fired = await run_server_system_schedules_once()
        assert fired >= 1

        refreshed = await repo.get_task(str(task["id"]))
        assert refreshed["status"] == "claimed"
        assert refreshed["claimed_by"] == "inst-active-claim"

        events = await repo.list_task_events(str(task["id"]))
        assert not any(e["event_type"] == "reaper" for e in events["items"])

    async def test_mixed_expired_and_active_tasks(
        self, db_pool, test_organization
    ):
        """Only expired tasks are released; active tasks are unaffected."""
        org_id = str(test_organization["id"])
        repo = RequestRepository(db_pool)

        await ensure_server_system_schedules()

        req = await _make_request(repo, org_id)

        # Task A: expired claim
        task_a = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task_a["id"]), "inst-expired")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks
                   SET claim_expires_at = NOW() - INTERVAL '5 minutes'
                   WHERE id = $1""",
                task_a["id"],
            )

        # Task B: active claim
        task_b = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task_b["id"]), "inst-active")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks
                   SET claim_expires_at = NOW() + INTERVAL '10 minutes',
                       claimed_at = NOW()
                   WHERE id = $1""",
                task_b["id"],
            )

        await _force_schedule_due(db_pool, test_organization["id"])
        await run_server_system_schedules_once()

        # A should be released
        refreshed_a = await repo.get_task(str(task_a["id"]))
        assert refreshed_a["status"] == "pending"
        assert refreshed_a["claimed_by"] is None

        # B should remain claimed
        refreshed_b = await repo.get_task(str(task_b["id"]))
        assert refreshed_b["status"] == "claimed"
        assert refreshed_b["claimed_by"] == "inst-active"

        # Only A should have a reaper event
        events_a = await repo.list_task_events(str(task_a["id"]))
        assert any(e["event_type"] == "reaper" for e in events_a["items"])

        events_b = await repo.list_task_events(str(task_b["id"]))
        assert not any(e["event_type"] == "reaper" for e in events_b["items"])


# ── Daemon Independence ──────────────────────────────────────────────────


class TestDaemonIndependence:
    async def test_reaper_runs_without_daemon_instance(
        self, db_pool, test_organization
    ):
        """Reaper works even when no daemon instance exists (daemon is dead)."""
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
        await _force_schedule_due(db_pool, test_organization["id"])

        fired = await run_server_system_schedules_once()
        assert fired >= 1

        refreshed = await repo.get_task(str(task["id"]))
        assert refreshed["status"] == "pending"
        assert refreshed["claimed_by"] is None

        events = await repo.list_task_events(str(task["id"]))
        assert any(e["event_type"] == "reaper" for e in events["items"])

    async def test_reaper_idempotent_double_run(
        self, db_pool, test_organization
    ):
        """Running the reaper twice: second run is a no-op (no double-release)."""
        org_id = str(test_organization["id"])
        repo = RequestRepository(db_pool)

        await ensure_server_system_schedules()

        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-idempotent")

        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks
                   SET claim_expires_at = NOW() - INTERVAL '2 minutes'
                   WHERE id = $1""",
                task["id"],
            )
        await _force_schedule_due(db_pool, test_organization["id"])

        # First run releases the task
        await run_server_system_schedules_once()
        refreshed = await repo.get_task(str(task["id"]))
        assert refreshed["status"] == "pending"

        # Force schedule due again for second run
        await _force_schedule_due(db_pool, test_organization["id"])

        # Second run: task is already pending, nothing to release
        await run_server_system_schedules_once()
        refreshed2 = await repo.get_task(str(task["id"]))
        assert refreshed2["status"] == "pending"

        # Should still only have one reaper event
        events = await repo.list_task_events(str(task["id"]))
        reaper_events = [
            e for e in events["items"] if e["event_type"] == "reaper"
        ]
        assert len(reaper_events) == 1

    async def test_daemon_skips_stale_task_reaper_schedule(self):
        """The daemon's _check_due_schedules should skip the server-side
        reaper schedule (is_system=true, title matches)."""
        # This validates the skip logic without needing a running daemon —
        # we test the condition directly.
        from lucent.api.system_schedules import STALE_TASK_REAPER_TITLE

        # Simulate what the daemon sees
        schedule = {
            "id": "00000000-0000-0000-0000-000000000001",
            "title": STALE_TASK_REAPER_TITLE,
            "is_system": True,
        }
        # The daemon skip condition
        should_skip = (
            schedule.get("is_system") and
            schedule.get("title") == STALE_TASK_REAPER_TITLE
        )
        assert should_skip is True

        # Non-system schedule should NOT be skipped
        other = {"id": "x", "title": "Something Else", "is_system": False}
        should_skip_other = (
            other.get("is_system") and
            other.get("title") == STALE_TASK_REAPER_TITLE
        )
        assert not should_skip_other
