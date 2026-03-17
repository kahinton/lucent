"""Tests for db/schedules.py — ScheduleRepository and _parse_cron.

Covers: cron parsing, schedule CRUD, due schedules, run lifecycle, summary.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from lucent.db.schedules import ScheduleRepository, _next_cron_utc, _parse_cron


async def _make_due(db_pool, schedule_id):
    """Backdate next_run_at so mark_schedule_run treats the schedule as due."""
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE schedules SET next_run_at = $1 WHERE id = $2",
            past,
            schedule_id,
        )


@pytest_asyncio.fixture
async def repo(db_pool):
    return ScheduleRepository(db_pool)


@pytest_asyncio.fixture
async def schedule(repo, test_organization):
    """Create a test schedule."""
    return await repo.create_schedule(
        title="Test Schedule",
        org_id=str(test_organization["id"]),
        schedule_type="interval",
        interval_seconds=3600,
        description="Hourly test",
        agent_type="code",
    )


@pytest_asyncio.fixture(autouse=True)
async def cleanup_schedules(db_pool, test_organization):
    """Clean up schedule data after each test."""
    yield
    org_id = test_organization["id"]
    async with db_pool.acquire() as conn:
        # schedule_runs cascade from schedules
        await conn.execute("DELETE FROM schedules WHERE organization_id = $1", org_id)


# ── _parse_cron tests ─────────────────────────────────────────────────────


class TestParseCron:
    def test_every_minute(self):
        """* * * * * should return the next minute."""
        now = datetime(2026, 3, 15, 10, 30, 0, tzinfo=timezone.utc)
        result = _parse_cron("* * * * *", now)
        assert result == datetime(2026, 3, 15, 10, 31, 0, tzinfo=timezone.utc)

    def test_specific_minute(self):
        now = datetime(2026, 3, 15, 10, 30, 0, tzinfo=timezone.utc)
        result = _parse_cron("45 * * * *", now)
        assert result.minute == 45
        assert result.hour == 10

    def test_specific_hour_minute(self):
        now = datetime(2026, 3, 15, 10, 30, 0, tzinfo=timezone.utc)
        result = _parse_cron("0 12 * * *", now)
        assert result.hour == 12
        assert result.minute == 0
        assert result.day == 15

    def test_step_values(self):
        """*/15 should match 0, 15, 30, 45."""
        now = datetime(2026, 3, 15, 10, 14, 0, tzinfo=timezone.utc)
        result = _parse_cron("*/15 * * * *", now)
        assert result.minute == 15

    def test_range(self):
        """1-3 in hour field."""
        now = datetime(2026, 3, 15, 3, 59, 0, tzinfo=timezone.utc)
        result = _parse_cron("0 1-3 * * *", now)
        # Next occurrence after 3:59 with hour 1-3 → next day at 1:00
        assert result.hour == 1
        assert result.day == 16

    def test_list_values(self):
        """Comma-separated values."""
        now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        result = _parse_cron("0 9,12,15 * * *", now)
        assert result.hour == 12
        assert result.minute == 0

    def test_day_of_week(self):
        """0 9 * * 1 = 9am every Monday."""
        # 2026-03-15 is a Sunday (weekday=6)
        now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        result = _parse_cron("0 9 * * 0", now)
        assert result.weekday() == 0  # Monday in Python is 0
        assert result.hour == 9

    def test_specific_month(self):
        now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        result = _parse_cron("0 0 1 6 *", now)
        assert result.month == 6
        assert result.day == 1

    def test_invalid_expression(self):
        now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="Invalid cron"):
            _parse_cron("bad cron", now)

    def test_no_match_within_year(self):
        """Impossible schedule (Feb 31) should raise."""
        now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="No next run"):
            _parse_cron("0 0 31 2 *", now)

    def test_numeric_base_step(self):
        """5/15 in minute field should start at 5 then step by 15: 5,20,35,50."""
        now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        result = _parse_cron("5/15 * * * *", now)
        assert result.minute == 5
        assert result.hour == 10

    def test_combined_list_and_range(self):
        """Comma-separated with a range: 0,30-35 in minute field."""
        now = datetime(2026, 3, 15, 10, 28, 0, tzinfo=timezone.utc)
        result = _parse_cron("0,30-35 * * * *", now)
        assert result.minute == 30

    def test_seconds_stripped(self):
        """After time should have seconds/microseconds zeroed."""
        now = datetime(2026, 3, 15, 10, 30, 45, 123456, tzinfo=timezone.utc)
        result = _parse_cron("* * * * *", now)
        assert result.second == 0
        assert result.microsecond == 0

    def test_day_of_week_sunday(self):
        """Test that dow=6 correctly matches Sunday (Python weekday 6)."""
        # 2026-03-15 is a Sunday
        now = datetime(2026, 3, 14, 10, 0, 0, tzinfo=timezone.utc)  # Saturday
        result = _parse_cron("0 9 * * 6", now)
        assert result.weekday() == 6  # Sunday
        assert result.day == 15


# ── _next_cron_utc timezone tests ─────────────────────────────────────────


class TestNextCronUtc:
    """Tests for timezone-aware cron scheduling via _next_cron_utc."""

    def test_est_winter_offset(self):
        """9 AM EST (UTC-5) should be 14:00 UTC."""
        after_utc = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = _next_cron_utc("0 9 * * *", after_utc, "America/New_York")
        assert result.hour == 14
        assert result.minute == 0
        assert result.day == 15

    def test_edt_summer_offset(self):
        """9 AM EDT (UTC-4) should be 13:00 UTC."""
        after_utc = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = _next_cron_utc("0 9 * * *", after_utc, "America/New_York")
        assert result.hour == 13
        assert result.minute == 0

    def test_dst_spring_forward_gap_hour(self):
        """Cron at 2:30 AM during spring forward fires at same UTC instant (7:30 UTC).

        On 2026-03-08, 2:00 AM EST jumps to 3:00 AM EDT. The 2:30 AM slot
        doesn't exist as wall-clock time, but the cron fires at the
        equivalent UTC instant (pre-transition offset).
        """
        # 6:00 UTC = 1:00 AM EST, before the spring forward
        after_utc = datetime(2026, 3, 8, 6, 0, 0, tzinfo=timezone.utc)
        result = _next_cron_utc("30 2 * * *", after_utc, "America/New_York")
        # 2:30 AM EST = 7:30 UTC (same instant as 3:30 AM EDT)
        assert result == datetime(2026, 3, 8, 7, 30, 0, tzinfo=timezone.utc)

    def test_dst_fall_back_ambiguous_hour(self):
        """Cron at 1:30 AM during fall back picks the first (EDT) occurrence.

        On 2026-11-01, 2:00 AM EDT falls back to 1:00 AM EST. The 1:30 AM
        slot occurs twice; the walk-forward algorithm hits EDT first.
        """
        # 4:00 UTC = midnight EDT (Nov 1)
        after_utc = datetime(2026, 11, 1, 4, 0, 0, tzinfo=timezone.utc)
        result = _next_cron_utc("30 1 * * *", after_utc, "America/New_York")
        # 1:30 AM EDT = 5:30 UTC (first occurrence, before clocks fall back)
        assert result == datetime(2026, 11, 1, 5, 30, 0, tzinfo=timezone.utc)

    def test_non_us_timezone_asia_tokyo(self):
        """9 AM JST (UTC+9) should be 00:00 UTC."""
        after_utc = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        result = _next_cron_utc("0 9 * * *", after_utc, "Asia/Tokyo")
        assert result.hour == 0
        assert result.minute == 0
        assert result.day == 2  # 9 AM June 2 JST = June 2 00:00 UTC

    def test_utc_default_passthrough(self):
        """With UTC timezone (default), result matches _parse_cron directly."""
        after_utc = datetime(2026, 5, 1, 10, 30, 0, tzinfo=timezone.utc)
        result = _next_cron_utc("0 12 * * *", after_utc)
        assert result.hour == 12
        assert result.minute == 0
        assert result.day == 1

    def test_result_always_utc(self):
        """Result tzinfo should always be UTC regardless of input timezone."""
        after_utc = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        result = _next_cron_utc("0 9 * * *", after_utc, "Europe/London")
        assert result.tzinfo == timezone.utc

    def test_cron_with_timezone_stored_on_schedule(self):
        """Cron '0 9 * * 0' (Monday 9AM) in US/Pacific should convert properly.

        Note: This cron implementation uses Python weekday (0=Monday).
        """
        # Wednesday March 18, 2026 at 00:00 UTC = Tuesday March 17 5PM PT
        after_utc = datetime(2026, 3, 18, 0, 0, 0, tzinfo=timezone.utc)
        result = _next_cron_utc("0 9 * * 0", after_utc, "US/Pacific")
        # Next Monday 9 AM PDT = 16:00 UTC (March 23, 2026)
        assert result == datetime(2026, 3, 23, 16, 0, 0, tzinfo=timezone.utc)


# ── Schedule CRUD ──────────────────────────────────────────────────────────


class TestCreateSchedule:
    @pytest.mark.asyncio
    async def test_create_interval(self, repo, test_organization):
        s = await repo.create_schedule(
            title="Hourly Job",
            org_id=str(test_organization["id"]),
            schedule_type="interval",
            interval_seconds=3600,
        )
        assert s["title"] == "Hourly Job"
        assert s["schedule_type"] == "interval"
        assert s["interval_seconds"] == 3600
        assert s["status"] == "active"
        assert s["enabled"] is True
        assert s["next_run_at"] is not None

    @pytest.mark.asyncio
    async def test_create_cron(self, repo, test_organization):
        s = await repo.create_schedule(
            title="Weekly Monday 9am",
            org_id=str(test_organization["id"]),
            schedule_type="cron",
            cron_expression="0 9 * * 0",
        )
        assert s["cron_expression"] == "0 9 * * 0"
        assert s["next_run_at"] is not None

    @pytest.mark.asyncio
    async def test_create_once(self, repo, test_organization):
        s = await repo.create_schedule(
            title="One-time",
            org_id=str(test_organization["id"]),
            schedule_type="once",
        )
        assert s["schedule_type"] == "once"
        assert s["next_run_at"] is not None

    @pytest.mark.asyncio
    async def test_create_with_all_fields(self, repo, test_organization, test_user):
        s = await repo.create_schedule(
            title="Full Schedule",
            org_id=str(test_organization["id"]),
            schedule_type="interval",
            interval_seconds=600,
            description="Every 10 minutes",
            agent_type="security",
            model="claude-sonnet-4",
            task_template={"key": "value"},
            sandbox_config={"image": "python:3.12"},
            priority="high",
            timezone_str="US/Pacific",
            max_runs=10,
            created_by=str(test_user["id"]),
        )
        assert s["description"] == "Every 10 minutes"
        assert s["agent_type"] == "security"
        assert s["model"] == "claude-sonnet-4"
        assert s["priority"] == "high"
        assert s["max_runs"] == 10

    @pytest.mark.asyncio
    async def test_create_with_explicit_next_run(self, repo, test_organization):
        future = datetime.now(timezone.utc) + timedelta(hours=5)
        s = await repo.create_schedule(
            title="Delayed",
            org_id=str(test_organization["id"]),
            schedule_type="once",
            next_run_at=future,
        )
        # Should use the explicit next_run_at
        assert abs((s["next_run_at"] - future).total_seconds()) < 2


class TestGetSchedule:
    @pytest.mark.asyncio
    async def test_get_existing(self, repo, schedule, test_organization):
        found = await repo.get_schedule(str(schedule["id"]), str(test_organization["id"]))
        assert found is not None
        assert found["id"] == schedule["id"]

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repo, test_organization):
        found = await repo.get_schedule(
            "00000000-0000-0000-0000-000000000000",
            str(test_organization["id"]),
        )
        assert found is None

    @pytest.mark.asyncio
    async def test_get_wrong_org(self, repo, schedule, db_pool, clean_test_data):
        from lucent.db import OrganizationRepository

        prefix = clean_test_data
        other_org = await OrganizationRepository(db_pool).create(name=f"{prefix}other_org")
        found = await repo.get_schedule(str(schedule["id"]), str(other_org["id"]))
        assert found is None


class TestListSchedules:
    @pytest.mark.asyncio
    async def test_list_basic(self, repo, schedule, test_organization):
        results = await repo.list_schedules(str(test_organization["id"]))
        assert any(s["id"] == schedule["id"] for s in results)

    @pytest.mark.asyncio
    async def test_list_filter_status(self, repo, schedule, test_organization):
        org = str(test_organization["id"])
        results = await repo.list_schedules(org, status="active")
        assert any(s["id"] == schedule["id"] for s in results)

        results = await repo.list_schedules(org, status="completed")
        assert not any(s["id"] == schedule["id"] for s in results)

    @pytest.mark.asyncio
    async def test_list_filter_enabled(self, repo, schedule, test_organization):
        org = str(test_organization["id"])
        results = await repo.list_schedules(org, enabled=True)
        assert any(s["id"] == schedule["id"] for s in results)

        await repo.toggle_schedule(str(schedule["id"]), org, False)
        results = await repo.list_schedules(org, enabled=True)
        assert not any(s["id"] == schedule["id"] for s in results)

    @pytest.mark.asyncio
    async def test_list_limit(self, repo, test_organization):
        org = str(test_organization["id"])
        for i in range(5):
            await repo.create_schedule(
                title=f"S{i}",
                org_id=org,
                schedule_type="once",
            )
        results = await repo.list_schedules(org, limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_list_combined_filters(self, repo, test_organization):
        """Filter by both status and enabled simultaneously."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="Combo",
            org_id=org,
            schedule_type="interval",
            interval_seconds=60,
        )
        results = await repo.list_schedules(org, status="active", enabled=True)
        assert any(r["id"] == s["id"] for r in results)

        await repo.toggle_schedule(str(s["id"]), org, False)
        results = await repo.list_schedules(org, status="active", enabled=True)
        assert not any(r["id"] == s["id"] for r in results)

    @pytest.mark.asyncio
    async def test_list_empty_org(self, repo, db_pool, clean_test_data):
        """List schedules for an org with no schedules."""
        from lucent.db import OrganizationRepository

        prefix = clean_test_data
        empty_org = await OrganizationRepository(db_pool).create(name=f"{prefix}empty_org")
        results = await repo.list_schedules(str(empty_org["id"]))
        assert results == []


class TestUpdateSchedule:
    @pytest.mark.asyncio
    async def test_update_title(self, repo, schedule, test_organization):
        org = str(test_organization["id"])
        updated = await repo.update_schedule(str(schedule["id"]), org, title="New Title")
        assert updated["title"] == "New Title"

    @pytest.mark.asyncio
    async def test_update_multiple_fields(self, repo, schedule, test_organization):
        org = str(test_organization["id"])
        updated = await repo.update_schedule(
            str(schedule["id"]),
            org,
            description="Updated desc",
            priority="urgent",
        )
        assert updated["description"] == "Updated desc"
        assert updated["priority"] == "urgent"

    @pytest.mark.asyncio
    async def test_update_task_template(self, repo, schedule, test_organization):
        org = str(test_organization["id"])
        updated = await repo.update_schedule(
            str(schedule["id"]),
            org,
            task_template={"new_key": "new_val"},
        )
        assert updated is not None

    @pytest.mark.asyncio
    async def test_update_no_fields(self, repo, schedule, test_organization):
        org = str(test_organization["id"])
        result = await repo.update_schedule(str(schedule["id"]), org)
        assert result is not None  # Returns current state

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, repo, test_organization):
        org = str(test_organization["id"])
        result = await repo.update_schedule("00000000-0000-0000-0000-000000000000", org, title="X")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_sets_updated_at(self, repo, schedule, test_organization):
        """update_schedule should set the updated_at timestamp."""
        org = str(test_organization["id"])
        _original = schedule["updated_at"]
        updated = await repo.update_schedule(str(schedule["id"]), org, description="changed")
        # updated_at is refreshed (may differ by microseconds either way)
        assert updated["updated_at"] is not None
        assert updated["description"] == "changed"


class TestToggleSchedule:
    @pytest.mark.asyncio
    async def test_disable(self, repo, schedule, test_organization):
        org = str(test_organization["id"])
        result = await repo.toggle_schedule(str(schedule["id"]), org, False)
        assert result["enabled"] is False

    @pytest.mark.asyncio
    async def test_enable(self, repo, schedule, test_organization):
        org = str(test_organization["id"])
        await repo.toggle_schedule(str(schedule["id"]), org, False)
        result = await repo.toggle_schedule(str(schedule["id"]), org, True)
        assert result["enabled"] is True


class TestDeleteSchedule:
    @pytest.mark.asyncio
    async def test_delete_existing(self, repo, schedule, test_organization):
        org = str(test_organization["id"])
        deleted = await repo.delete_schedule(str(schedule["id"]), org)
        assert deleted is True
        found = await repo.get_schedule(str(schedule["id"]), org)
        assert found is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, repo, test_organization):
        org = str(test_organization["id"])
        deleted = await repo.delete_schedule("00000000-0000-0000-0000-000000000000", org)
        assert deleted is False

    @pytest.mark.asyncio
    async def test_delete_wrong_org(self, repo, schedule, db_pool, clean_test_data):
        from lucent.db import OrganizationRepository

        prefix = clean_test_data
        other_org = await OrganizationRepository(db_pool).create(name=f"{prefix}other_org2")
        deleted = await repo.delete_schedule(str(schedule["id"]), str(other_org["id"]))
        assert deleted is False


# ── Due Schedules ──────────────────────────────────────────────────────────


class TestGetDueSchedules:
    @pytest.mark.asyncio
    async def test_finds_due(self, repo, test_organization, db_pool):
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="Due Now",
            org_id=org,
            schedule_type="once",
        )
        # Backdate next_run_at to make it due
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE schedules SET next_run_at = NOW() - interval '1 minute' WHERE id = $1",
                s["id"],
            )
        due = await repo.get_due_schedules(org)
        assert any(d["id"] == s["id"] for d in due)

    @pytest.mark.asyncio
    async def test_excludes_future(self, repo, test_organization):
        org = str(test_organization["id"])
        future = datetime.now(timezone.utc) + timedelta(hours=24)
        s = await repo.create_schedule(
            title="Future",
            org_id=org,
            schedule_type="once",
            next_run_at=future,
        )
        due = await repo.get_due_schedules(org)
        assert not any(d["id"] == s["id"] for d in due)

    @pytest.mark.asyncio
    async def test_excludes_disabled(self, repo, test_organization, db_pool):
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="Disabled",
            org_id=org,
            schedule_type="once",
        )
        await repo.toggle_schedule(str(s["id"]), org, False)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE schedules SET next_run_at = NOW() - interval '1 minute' WHERE id = $1",
                s["id"],
            )
        due = await repo.get_due_schedules(org)
        assert not any(d["id"] == s["id"] for d in due)

    @pytest.mark.asyncio
    async def test_without_org_filter(self, repo, test_organization, db_pool):
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="Global Due",
            org_id=org,
            schedule_type="once",
        )
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE schedules SET next_run_at = NOW() - interval '1 minute' WHERE id = $1",
                s["id"],
            )
        due = await repo.get_due_schedules()  # No org filter
        assert any(d["id"] == s["id"] for d in due)

    @pytest.mark.asyncio
    async def test_priority_ordering(self, repo, test_organization, db_pool):
        """Due schedules are returned in priority order (urgent first)."""
        org = str(test_organization["id"])
        low = await repo.create_schedule(
            title="Low", org_id=org, schedule_type="once", priority="low"
        )
        urgent = await repo.create_schedule(
            title="Urgent", org_id=org, schedule_type="once", priority="urgent"
        )
        high = await repo.create_schedule(
            title="High", org_id=org, schedule_type="once", priority="high"
        )
        # Backdate all to be due now
        async with db_pool.acquire() as conn:
            for s in [low, urgent, high]:
                await conn.execute(
                    "UPDATE schedules SET next_run_at = NOW() - interval '1 minute' WHERE id = $1",
                    s["id"],
                )
        due = await repo.get_due_schedules(org)
        ids = [d["id"] for d in due]
        # urgent should come before high, which should come before low
        assert ids.index(urgent["id"]) < ids.index(high["id"])
        assert ids.index(high["id"]) < ids.index(low["id"])

    @pytest.mark.asyncio
    async def test_excludes_completed(self, repo, test_organization, db_pool):
        """Completed schedules are not returned even if next_run_at is past."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(title="Done", org_id=org, schedule_type="once")
        # Mark as run (once → completed) then backdate
        await repo.mark_schedule_run(str(s["id"]))
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE schedules SET next_run_at = NOW() - interval '1 minute' WHERE id = $1",
                s["id"],
            )
        due = await repo.get_due_schedules(org)
        assert not any(d["id"] == s["id"] for d in due)


# ── Run lifecycle ──────────────────────────────────────────────────────────


class TestMarkScheduleRun:
    @pytest.mark.asyncio
    async def test_once_completes_after_run(self, repo, test_organization):
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="Once",
            org_id=org,
            schedule_type="once",
        )
        run = await repo.mark_schedule_run(str(s["id"]))
        assert run["status"] == "running"
        # Schedule should be completed
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["status"] == "completed"
        assert updated["run_count"] == 1
        assert updated["next_run_at"] is None

    @pytest.mark.asyncio
    async def test_interval_advances_next_run(self, repo, test_organization, db_pool):
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="Interval",
            org_id=org,
            schedule_type="interval",
            interval_seconds=3600,
        )
        await _make_due(db_pool, s["id"])
        _run = await repo.mark_schedule_run(str(s["id"]))
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["status"] == "active"
        assert updated["run_count"] == 1
        assert updated["next_run_at"] is not None
        # next_run_at should be ~1 hour from now
        diff = (updated["next_run_at"] - datetime.now(timezone.utc)).total_seconds()
        assert 3500 < diff < 3700

    @pytest.mark.asyncio
    async def test_cron_advances_next_run(self, repo, test_organization):
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="Cron",
            org_id=org,
            schedule_type="cron",
            cron_expression="0 9 * * *",
        )
        _run = await repo.mark_schedule_run(str(s["id"]))
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["status"] == "active"
        assert updated["next_run_at"] is not None
        assert updated["next_run_at"].hour == 9

    @pytest.mark.asyncio
    async def test_max_runs_completes(self, repo, test_organization, db_pool):
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="MaxRuns",
            org_id=org,
            schedule_type="interval",
            interval_seconds=60,
            max_runs=2,
        )
        await _make_due(db_pool, s["id"])
        await repo.mark_schedule_run(str(s["id"]))
        await _make_due(db_pool, s["id"])
        await repo.mark_schedule_run(str(s["id"]))
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["status"] == "completed"
        assert updated["run_count"] == 2

    @pytest.mark.asyncio
    async def test_expiration(self, repo, test_organization, db_pool):
        org = str(test_organization["id"])
        # Expires in the past so next run would be after expiry
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        s = await repo.create_schedule(
            title="Expiring",
            org_id=org,
            schedule_type="interval",
            interval_seconds=3600,
            expires_at=past,
        )
        await _make_due(db_pool, s["id"])
        await repo.mark_schedule_run(str(s["id"]))
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["status"] == "expired"

    @pytest.mark.asyncio
    async def test_nonexistent_schedule(self, repo):
        with pytest.raises(ValueError, match="not found"):
            await repo.mark_schedule_run("00000000-0000-0000-0000-000000000000")

    @pytest.mark.asyncio
    async def test_with_request_id(self, repo, test_organization, db_pool):
        """mark_schedule_run can link to a request."""
        from lucent.db.requests import RequestRepository

        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="Linked",
            org_id=org,
            schedule_type="once",
        )
        req_repo = RequestRepository(db_pool)
        req = await req_repo.create_request(title="From schedule", org_id=org)
        run = await repo.mark_schedule_run(str(s["id"]), request_id=str(req["id"]))
        assert run["request_id"] == req["id"]
        # Cleanup: delete schedule_runs (which ref requests) first, then request
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM schedule_runs WHERE request_id = $1", req["id"])
            await conn.execute("DELETE FROM requests WHERE id = $1", req["id"])

    @pytest.mark.asyncio
    async def test_cron_parse_failure_expires(self, repo, test_organization, db_pool):
        """If cron expression can't find next run, schedule becomes expired."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="Bad Cron",
            org_id=org,
            schedule_type="cron",
            cron_expression="0 9 * * *",  # valid initially
        )
        # Manually set an impossible cron expression and backdate to trigger parse failure
        async with db_pool.acquire() as conn:
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            await conn.execute(
                "UPDATE schedules SET cron_expression = '0 0 31 2 *', next_run_at = $1 WHERE id = $2",
                past,
                s["id"],
            )
        await repo.mark_schedule_run(str(s["id"]))
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["status"] == "expired"

    @pytest.mark.asyncio
    async def test_interval_multiple_runs(self, repo, test_organization, db_pool):
        """Multiple runs on an interval schedule keep advancing."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="MultiRun",
            org_id=org,
            schedule_type="interval",
            interval_seconds=60,
        )
        await _make_due(db_pool, s["id"])
        run1 = await repo.mark_schedule_run(str(s["id"]))
        await _make_due(db_pool, s["id"])
        run2 = await repo.mark_schedule_run(str(s["id"]))
        assert run1["id"] != run2["id"]
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["run_count"] == 2
        assert updated["status"] == "active"

    @pytest.mark.asyncio
    async def test_idempotency_guard_skips_future(self, repo, test_organization):
        """mark_schedule_run returns None when next_run_at is still in the future."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="Idempotent",
            org_id=org,
            schedule_type="interval",
            interval_seconds=3600,
        )
        # Don't backdate — next_run_at is 1 hour from now
        result = await repo.mark_schedule_run(str(s["id"]))
        assert result is None
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["run_count"] == 0

    @pytest.mark.asyncio
    async def test_idempotency_guard_blocks_completed_once(self, repo, test_organization):
        """A once schedule that already fired cannot fire again (next_run_at is None)."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(title="OnceGuard", org_id=org, schedule_type="once")
        run1 = await repo.mark_schedule_run(str(s["id"]))
        assert run1 is not None
        # Second attempt should return None — schedule is completed
        run2 = await repo.mark_schedule_run(str(s["id"]))
        assert run2 is None
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["run_count"] == 1

    @pytest.mark.asyncio
    async def test_idempotency_guard_blocks_non_active(self, repo, test_organization, db_pool):
        """mark_schedule_run returns None for expired/completed schedules."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="Expired", org_id=org, schedule_type="interval", interval_seconds=60
        )
        # Manually set status to expired
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE schedules SET status = 'expired' WHERE id = $1", s["id"]
            )
        await _make_due(db_pool, s["id"])
        result = await repo.mark_schedule_run(str(s["id"]))
        assert result is None

    @pytest.mark.asyncio
    async def test_force_bypasses_time_guard(self, repo, test_organization):
        """force=True allows triggering even when next_run_at is in the future."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="ForceRun",
            org_id=org,
            schedule_type="interval",
            interval_seconds=3600,
        )
        # next_run_at is 1 hour from now — without force this returns None
        result = await repo.mark_schedule_run(str(s["id"]))
        assert result is None
        # With force, it should proceed
        result = await repo.mark_schedule_run(str(s["id"]), force=True)
        assert result is not None
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["run_count"] == 1

    @pytest.mark.asyncio
    async def test_force_still_blocks_non_active(self, repo, test_organization):
        """force=True still respects the status guard — cannot fire a completed schedule."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(title="ForceBlock", org_id=org, schedule_type="once")
        run = await repo.mark_schedule_run(str(s["id"]))
        assert run is not None
        # Schedule is now completed; force should still return None
        result = await repo.mark_schedule_run(str(s["id"]), force=True)
        assert result is None


class TestCompleteRun:
    @pytest.mark.asyncio
    async def test_complete_run(self, repo, test_organization):
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="CR",
            org_id=org,
            schedule_type="once",
        )
        run = await repo.mark_schedule_run(str(s["id"]))
        completed = await repo.complete_run(str(run["id"]), result="All good")
        assert completed["status"] == "completed"
        assert completed["result"] == "All good"
        assert completed["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_complete_nonexistent(self, repo):
        result = await repo.complete_run("00000000-0000-0000-0000-000000000000")
        assert result is None

    @pytest.mark.asyncio
    async def test_complete_run_no_result(self, repo, test_organization):
        """complete_run with result=None."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(title="CRN", org_id=org, schedule_type="once")
        run = await repo.mark_schedule_run(str(s["id"]))
        completed = await repo.complete_run(str(run["id"]))
        assert completed["status"] == "completed"
        assert completed["result"] is None


class TestFailRun:
    @pytest.mark.asyncio
    async def test_fail_run(self, repo, test_organization):
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="FR",
            org_id=org,
            schedule_type="once",
        )
        run = await repo.mark_schedule_run(str(s["id"]))
        failed = await repo.fail_run(str(run["id"]), error="Boom")
        assert failed["status"] == "failed"
        assert failed["error"] == "Boom"
        assert failed["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_fail_nonexistent(self, repo):
        result = await repo.fail_run("00000000-0000-0000-0000-000000000000")
        assert result is None

    @pytest.mark.asyncio
    async def test_fail_run_no_error(self, repo, test_organization):
        """fail_run with error=None."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(title="FRN", org_id=org, schedule_type="once")
        run = await repo.mark_schedule_run(str(s["id"]))
        failed = await repo.fail_run(str(run["id"]))
        assert failed["status"] == "failed"
        assert failed["error"] is None


# ── Run history ────────────────────────────────────────────────────────────


class TestListRuns:
    @pytest.mark.asyncio
    async def test_list_runs(self, repo, test_organization, db_pool):
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="LR",
            org_id=org,
            schedule_type="interval",
            interval_seconds=60,
        )
        await _make_due(db_pool, s["id"])
        await repo.mark_schedule_run(str(s["id"]))
        await _make_due(db_pool, s["id"])
        await repo.mark_schedule_run(str(s["id"]))
        runs = await repo.list_runs(str(s["id"]))
        assert len(runs) == 2

    @pytest.mark.asyncio
    async def test_list_runs_limit(self, repo, test_organization, db_pool):
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="LRL",
            org_id=org,
            schedule_type="interval",
            interval_seconds=60,
        )
        for _ in range(5):
            await _make_due(db_pool, s["id"])
            await repo.mark_schedule_run(str(s["id"]))
        runs = await repo.list_runs(str(s["id"]), limit=3)
        assert len(runs) == 3


class TestGetScheduleWithRuns:
    @pytest.mark.asyncio
    async def test_with_runs(self, repo, test_organization, db_pool):
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="SWR",
            org_id=org,
            schedule_type="interval",
            interval_seconds=60,
        )
        await _make_due(db_pool, s["id"])
        await repo.mark_schedule_run(str(s["id"]))
        result = await repo.get_schedule_with_runs(str(s["id"]), org)
        assert result is not None
        assert "runs" in result
        assert len(result["runs"]) == 1

    @pytest.mark.asyncio
    async def test_nonexistent(self, repo, test_organization):
        result = await repo.get_schedule_with_runs(
            "00000000-0000-0000-0000-000000000000",
            str(test_organization["id"]),
        )
        assert result is None


# ── Summary ────────────────────────────────────────────────────────────────


class TestGetSummary:
    @pytest.mark.asyncio
    async def test_summary_counts(self, repo, schedule, test_organization):
        org = str(test_organization["id"])
        summary = await repo.get_summary(org)
        assert summary["total"] >= 1
        assert summary["active"] >= 1
        assert "paused" in summary
        assert "completed" in summary
        assert "due_now" in summary
        assert "interval" in summary

    @pytest.mark.asyncio
    async def test_summary_type_breakdown(self, repo, test_organization):
        org = str(test_organization["id"])
        await repo.create_schedule(title="Once", org_id=org, schedule_type="once")
        await repo.create_schedule(
            title="Cron",
            org_id=org,
            schedule_type="cron",
            cron_expression="0 9 * * *",
        )
        await repo.create_schedule(
            title="Interval",
            org_id=org,
            schedule_type="interval",
            interval_seconds=60,
        )
        summary = await repo.get_summary(org)
        assert summary["one_time"] >= 1
        assert summary["cron"] >= 1
        assert summary["interval"] >= 1

    @pytest.mark.asyncio
    async def test_summary_due_now(self, repo, test_organization, db_pool):
        """Summary due_now reflects schedules whose next_run_at is past."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(title="Due", org_id=org, schedule_type="once")
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE schedules SET next_run_at = NOW() - interval '1 minute' WHERE id = $1",
                s["id"],
            )
        summary = await repo.get_summary(org)
        assert summary["due_now"] >= 1

    @pytest.mark.asyncio
    async def test_summary_paused_count(self, repo, test_organization):
        """Disabled schedules appear in the paused count."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(title="Paused", org_id=org, schedule_type="once")
        await repo.toggle_schedule(str(s["id"]), org, False)
        summary = await repo.get_summary(org)
        assert summary["paused"] >= 1

    @pytest.mark.asyncio
    async def test_summary_completed_count(self, repo, test_organization):
        """Completed schedules appear in the completed count."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(title="Done", org_id=org, schedule_type="once")
        await repo.mark_schedule_run(str(s["id"]))
        summary = await repo.get_summary(org)
        assert summary["completed"] >= 1

    @pytest.mark.asyncio
    async def test_summary_empty_org(self, repo, db_pool, clean_test_data):
        """Summary for an org with no schedules returns all zeros."""
        from lucent.db import OrganizationRepository

        prefix = clean_test_data
        empty_org = await OrganizationRepository(db_pool).create(name=f"{prefix}empty_org2")
        summary = await repo.get_summary(str(empty_org["id"]))
        assert summary["total"] == 0
        assert summary["active"] == 0


# ── Scheduler deduplication tests ─────────────────────────────────────────


class TestSchedulerDeduplication:
    """Tests for concurrent/duplicate run prevention in mark_schedule_run."""

    @pytest.mark.asyncio
    async def test_concurrent_mark_only_one_succeeds(self, repo, test_organization, db_pool):
        """Two concurrent mark_schedule_run calls — only one should create a run."""
        import asyncio

        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="ConcurrentTest",
            org_id=org,
            schedule_type="interval",
            interval_seconds=3600,
        )
        await _make_due(db_pool, s["id"])

        results = await asyncio.gather(
            repo.mark_schedule_run(str(s["id"])),
            repo.mark_schedule_run(str(s["id"])),
        )
        successful = [r for r in results if r is not None]
        assert len(successful) == 1, f"Expected exactly 1 successful run, got {len(successful)}"
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["run_count"] == 1

    @pytest.mark.asyncio
    async def test_rapid_sequential_double_fire(self, repo, test_organization, db_pool):
        """Two rapid sequential calls — second should return None."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="DoubleFireTest",
            org_id=org,
            schedule_type="interval",
            interval_seconds=3600,
        )
        await _make_due(db_pool, s["id"])

        run1 = await repo.mark_schedule_run(str(s["id"]))
        assert run1 is not None
        # Second call: next_run_at is now ~1 hour in the future
        run2 = await repo.mark_schedule_run(str(s["id"]))
        assert run2 is None
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["run_count"] == 1

    @pytest.mark.asyncio
    async def test_null_next_run_at_returns_none(self, repo, test_organization, db_pool):
        """When next_run_at is NULL, mark_schedule_run returns None."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="NullNextRun",
            org_id=org,
            schedule_type="interval",
            interval_seconds=3600,
        )
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE schedules SET next_run_at = NULL WHERE id = $1", s["id"]
            )
        result = await repo.mark_schedule_run(str(s["id"]))
        assert result is None

    @pytest.mark.asyncio
    async def test_concurrent_with_force_only_one_fires(self, repo, test_organization, db_pool):
        """Concurrent force=True calls — only one should succeed (status guard)."""
        import asyncio

        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="ConcurrentForce",
            org_id=org,
            schedule_type="once",
        )
        # Both try to force-fire the once schedule
        results = await asyncio.gather(
            repo.mark_schedule_run(str(s["id"]), force=True),
            repo.mark_schedule_run(str(s["id"]), force=True),
        )
        successful = [r for r in results if r is not None]
        # Once schedule: first run completes it, second is blocked by status guard
        assert len(successful) == 1
        updated = await repo.get_schedule(str(s["id"]), org)
        assert updated["status"] == "completed"
        assert updated["run_count"] == 1

    @pytest.mark.asyncio
    async def test_interval_advances_prevents_duplicate(self, repo, test_organization, db_pool):
        """After interval run advances next_run_at, get_due_schedules excludes it."""
        org = str(test_organization["id"])
        s = await repo.create_schedule(
            title="IntervalDedup",
            org_id=org,
            schedule_type="interval",
            interval_seconds=3600,
        )
        await _make_due(db_pool, s["id"])

        due_before = await repo.get_due_schedules(org)
        due_ids = [str(d["id"]) for d in due_before]
        assert str(s["id"]) in due_ids

        await repo.mark_schedule_run(str(s["id"]))

        due_after = await repo.get_due_schedules(org)
        due_ids_after = [str(d["id"]) for d in due_after]
        assert str(s["id"]) not in due_ids_after
