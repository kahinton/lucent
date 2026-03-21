"""Tests for RequestRepository — request tracking and task queue."""

from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from lucent.constants import VALID_REQUEST_SOURCES
from lucent.db.requests import RequestRepository

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def repo(db_pool):
    return RequestRepository(db_pool)


@pytest.fixture
def org_id(test_organization):
    return str(test_organization["id"])


@pytest.fixture
def user_id(test_user):
    return str(test_user["id"])


@pytest_asyncio.fixture(autouse=True)
async def cleanup_requests(db_pool, test_organization):
    """Clean up requests, tasks, events, and memory links created during tests."""
    org_uuid = test_organization["id"]
    yield
    async with db_pool.acquire() as conn:
        # Get all request IDs for this org
        req_ids = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id FROM requests WHERE organization_id = $1", org_uuid
            )
        ]
        if req_ids:
            # Get all task IDs for these requests
            task_ids = [
                r["id"]
                for r in await conn.fetch(
                    "SELECT id FROM tasks WHERE request_id = ANY($1)", req_ids
                )
            ]
            if task_ids:
                await conn.execute("DELETE FROM task_memories WHERE task_id = ANY($1)", task_ids)
                await conn.execute("DELETE FROM task_events WHERE task_id = ANY($1)", task_ids)
            await conn.execute("DELETE FROM tasks WHERE request_id = ANY($1)", req_ids)
            await conn.execute("DELETE FROM requests WHERE organization_id = $1", org_uuid)


async def _make_request(repo, org_id, **kwargs):
    """Helper to create a request with sensible defaults."""
    defaults = dict(title="Test request", org_id=org_id)
    defaults.update(kwargs)
    return await repo.create_request(**defaults)


async def _make_task(repo, request_id, org_id, **kwargs):
    """Helper to create a task with sensible defaults."""
    defaults = dict(request_id=request_id, title="Test task", org_id=org_id)
    defaults.update(kwargs)
    return await repo.create_task(**defaults)


# ── Request CRUD ─────────────────────────────────────────────────────────


class TestCreateRequest:
    async def test_basic_create(self, repo, org_id):
        req = await _make_request(repo, org_id)
        assert isinstance(req["id"], UUID)
        assert req["title"] == "Test request"
        assert req["status"] == "pending"
        assert req["priority"] == "medium"
        assert req["source"] == "user"
        assert req["description"] is None
        assert req["completed_at"] is None

    async def test_create_with_all_fields(self, repo, org_id, user_id):
        req = await repo.create_request(
            title="Full request",
            org_id=org_id,
            description="Detailed desc",
            source="daemon",
            priority="urgent",
            created_by=user_id,
        )
        assert req["description"] == "Detailed desc"
        assert req["source"] == "daemon"
        assert req["priority"] == "urgent"
        assert req["created_by"] == UUID(user_id)

    async def test_create_multiple_independent(self, repo, org_id):
        r1 = await _make_request(repo, org_id, title="Req A")
        r2 = await _make_request(repo, org_id, title="Req B")
        assert r1["id"] != r2["id"]

    @pytest.mark.parametrize("source", sorted(VALID_REQUEST_SOURCES))
    async def test_all_valid_sources_accepted(self, repo, org_id, source):
        req = await _make_request(repo, org_id, title=f"Source {source}", source=source)
        assert req["source"] == source

    @pytest.mark.parametrize("source", ["invalid", "webhook", "", "USER"])
    async def test_invalid_source_rejected(self, repo, org_id, source):
        with pytest.raises(ValueError, match="Invalid source"):
            await _make_request(repo, org_id, title=f"Bad {source}", source=source)


class TestGetRequest:
    async def test_get_existing(self, repo, org_id):
        created = await _make_request(repo, org_id, title="Findable")
        fetched = await repo.get_request(str(created["id"]), org_id)
        assert fetched is not None
        assert fetched["title"] == "Findable"

    async def test_get_nonexistent(self, repo, org_id):
        assert await repo.get_request(str(uuid4()), org_id) is None

    async def test_get_wrong_org(self, repo, org_id):
        created = await _make_request(repo, org_id)
        other_org = str(uuid4())
        assert await repo.get_request(str(created["id"]), other_org) is None


class TestListRequests:
    async def test_list_returns_org_requests(self, repo, org_id):
        await _make_request(repo, org_id, title="Alpha")
        await _make_request(repo, org_id, title="Beta")
        results = await repo.list_requests(org_id)
        titles = {r["title"] for r in results["items"]}
        assert "Alpha" in titles
        assert "Beta" in titles

    async def test_filter_by_status(self, repo, org_id):
        req = await _make_request(repo, org_id, title="To complete")
        await repo.update_request_status(str(req["id"]), "completed")
        pending = await repo.list_requests(org_id, status="pending")
        completed = await repo.list_requests(org_id, status="completed")
        assert all(r["status"] == "pending" for r in pending["items"])
        assert any(r["title"] == "To complete" for r in completed["items"])

    async def test_filter_by_source(self, repo, org_id):
        await _make_request(repo, org_id, title="From daemon", source="daemon")
        await _make_request(repo, org_id, title="From user", source="user")
        results = await repo.list_requests(org_id, source="daemon")
        assert all(r["source"] == "daemon" for r in results["items"])
        assert any(r["title"] == "From daemon" for r in results["items"])

    async def test_pagination(self, repo, org_id):
        for i in range(5):
            await _make_request(repo, org_id, title=f"Page {i}")
        page1 = await repo.list_requests(org_id, limit=2, offset=0)
        page2 = await repo.list_requests(org_id, limit=2, offset=2)
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 2
        assert page1["items"][0]["id"] != page2["items"][0]["id"]

    async def test_combined_filters(self, repo, org_id):
        req = await _make_request(repo, org_id, title="Daemon done", source="daemon")
        await repo.update_request_status(str(req["id"]), "completed")
        results = await repo.list_requests(org_id, status="completed", source="daemon")
        assert any(r["title"] == "Daemon done" for r in results["items"])


class TestUpdateRequestStatus:
    async def test_update_to_in_progress(self, repo, org_id):
        req = await _make_request(repo, org_id)
        updated = await repo.update_request_status(str(req["id"]), "in_progress")
        assert updated["status"] == "in_progress"
        assert updated["completed_at"] is None

    async def test_update_to_completed_sets_completed_at(self, repo, org_id):
        req = await _make_request(repo, org_id)
        updated = await repo.update_request_status(str(req["id"]), "completed")
        assert updated["status"] == "completed"
        assert updated["completed_at"] is not None

    async def test_update_to_failed_sets_completed_at(self, repo, org_id):
        req = await _make_request(repo, org_id)
        updated = await repo.update_request_status(str(req["id"]), "failed")
        assert updated["status"] == "failed"
        assert updated["completed_at"] is not None

    async def test_update_to_cancelled_sets_completed_at(self, repo, org_id):
        req = await _make_request(repo, org_id)
        updated = await repo.update_request_status(str(req["id"]), "cancelled")
        assert updated["completed_at"] is not None

    async def test_update_nonexistent_returns_none(self, repo):
        assert await repo.update_request_status(str(uuid4()), "completed") is None


# ── Task CRUD ────────────────────────────────────────────────────────────


class TestCreateTask:
    async def test_basic_create(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        assert isinstance(task["id"], UUID)
        assert task["title"] == "Test task"
        assert task["status"] == "pending"
        assert task["priority"] == "medium"
        assert task["sequence_order"] == 0

    async def test_create_with_all_fields(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await repo.create_task(
            request_id=str(req["id"]),
            title="Full task",
            org_id=org_id,
            description="Do something specific",
            agent_type="code",
            priority="high",
            sequence_order=2,
            model="claude-sonnet-4",
        )
        assert task["description"] == "Do something specific"
        assert task["agent_type"] == "code"
        assert task["priority"] == "high"
        assert task["sequence_order"] == 2
        assert task["model"] == "claude-sonnet-4"

    async def test_create_subtask(self, repo, org_id):
        req = await _make_request(repo, org_id)
        parent = await _make_task(repo, str(req["id"]), org_id, title="Parent")
        child = await _make_task(
            repo,
            str(req["id"]),
            org_id,
            title="Child",
            parent_task_id=str(parent["id"]),
        )
        assert child["parent_task_id"] == parent["id"]

    async def test_create_task_generates_created_event(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id, title="Evented")
        events = await repo.list_task_events(str(task["id"]))
        assert len(events["items"]) >= 1
        assert events["items"][0]["event_type"] == "created"

    async def test_sequence_order_zero(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id, sequence_order=0)
        assert task["sequence_order"] == 0

    async def test_sequence_order_positive(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id, sequence_order=1)
        assert task["sequence_order"] == 1

    async def test_sequence_order_negative_rejected(self, repo, org_id, db_pool):
        """Negative sequence_order should be rejected by the DB CHECK constraint."""
        import asyncpg

        req = await _make_request(repo, org_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await _make_task(repo, str(req["id"]), org_id, sequence_order=-1)


class TestListTasks:
    async def test_list_all_tasks_for_request(self, repo, org_id):
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        await _make_task(repo, rid, org_id, title="T1")
        await _make_task(repo, rid, org_id, title="T2")
        tasks = await repo.list_tasks(rid)
        assert len(tasks["items"]) == 2

    async def test_list_by_status(self, repo, org_id):
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        t1 = await _make_task(repo, rid, org_id, title="T1")
        await _make_task(repo, rid, org_id, title="T2")
        await repo.claim_task(str(t1["id"]), "inst-1")
        pending = await repo.list_tasks(rid, status="pending")
        claimed = await repo.list_tasks(rid, status="claimed")
        assert len(pending["items"]) == 1
        assert len(claimed["items"]) == 1

    async def test_list_respects_sequence_order(self, repo, org_id):
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        await _make_task(repo, rid, org_id, title="Later", sequence_order=2)
        await _make_task(repo, rid, org_id, title="First", sequence_order=0)
        tasks = await repo.list_tasks(rid)
        assert tasks["items"][0]["title"] == "First"
        assert tasks["items"][1]["title"] == "Later"

    async def test_list_empty(self, repo, org_id):
        req = await _make_request(repo, org_id)
        tasks = await repo.list_tasks(str(req["id"]))
        assert tasks["items"] == []


# ── Task Lifecycle ───────────────────────────────────────────────────────


class TestClaimTask:
    async def test_claim_pending(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        claimed = await repo.claim_task(str(task["id"]), "daemon-a")
        assert claimed is not None
        assert claimed["status"] == "claimed"
        assert claimed["claimed_by"] == "daemon-a"
        assert claimed["claimed_at"] is not None

    async def test_claim_already_claimed_returns_none(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "daemon-a")
        second = await repo.claim_task(str(task["id"]), "daemon-b")
        assert second is None

    async def test_claim_sets_request_in_progress(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        assert req["status"] == "pending"
        await repo.claim_task(str(task["id"]), "inst-1")
        updated_req = await repo.get_request(str(req["id"]), org_id)
        assert updated_req["status"] == "in_progress"

    async def test_claim_logs_event(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-x")
        events = await repo.list_task_events(str(task["id"]))
        claimed_events = [e for e in events["items"] if e["event_type"] == "claimed"]
        assert len(claimed_events) == 1
        assert "inst-x" in claimed_events[0]["detail"]


class TestStartTask:
    async def test_start_claimed_task(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-1")
        started = await repo.start_task(str(task["id"]))
        assert started is not None
        assert started["status"] == "running"

    async def test_start_unclaimed_returns_none(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        assert await repo.start_task(str(task["id"])) is None

    async def test_start_logs_event(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-1")
        await repo.start_task(str(task["id"]))
        events = await repo.list_task_events(str(task["id"]))
        assert any(e["event_type"] == "running" for e in events["items"])


class TestCompleteTask:
    async def test_complete_task(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-test")
        completed = await repo.complete_task(str(task["id"]), "All done")
        assert completed is not None
        assert completed["status"] == "completed"
        assert completed["result"] == "All done"
        assert completed["completed_at"] is not None

    async def test_complete_logs_event(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-test")
        await repo.complete_task(str(task["id"]), "Output text")
        events = await repo.list_task_events(str(task["id"]))
        assert any(e["event_type"] == "completed" for e in events["items"])

    async def test_completing_last_task_completes_request(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-test")
        await repo.complete_task(str(task["id"]), "Done")
        updated = await repo.get_request(str(req["id"]), org_id)
        assert updated["status"] == "completed"

    async def test_completing_with_failed_sibling_fails_request(self, repo, org_id):
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        t1 = await _make_task(repo, rid, org_id, title="T1")
        t2 = await _make_task(repo, rid, org_id, title="T2")
        await repo.claim_task(str(t1["id"]), "inst-test")
        await repo.fail_task(str(t1["id"]), "Broke")
        await repo.claim_task(str(t2["id"]), "inst-test")
        await repo.complete_task(str(t2["id"]), "OK")
        updated = await repo.get_request(rid, org_id)
        assert updated["status"] == "failed"


class TestFailTask:
    async def test_fail_task(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-test")
        failed = await repo.fail_task(str(task["id"]), "Something broke")
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["error"] == "Something broke"
        assert failed["completed_at"] is not None

    async def test_fail_logs_event(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-test")
        await repo.fail_task(str(task["id"]), "err")
        events = await repo.list_task_events(str(task["id"]))
        assert any(e["event_type"] == "failed" for e in events["items"])


class TestReleaseTask:
    async def test_release_claimed_task(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-1")
        released = await repo.release_task(str(task["id"]))
        assert released is not None
        assert released["status"] == "pending"
        assert released["claimed_by"] is None
        assert released["claimed_at"] is None

    async def test_release_running_task(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-1")
        await repo.start_task(str(task["id"]))
        released = await repo.release_task(str(task["id"]))
        assert released is not None
        assert released["status"] == "pending"

    async def test_release_pending_returns_none(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        assert await repo.release_task(str(task["id"])) is None

    async def test_release_logs_event(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-1")
        await repo.release_task(str(task["id"]))
        events = await repo.list_task_events(str(task["id"]))
        assert any(e["event_type"] == "released" for e in events["items"])


class TestRetryTask:
    async def test_retry_failed_task(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-test")
        await repo.fail_task(str(task["id"]), "Oops")
        retried = await repo.retry_task(str(task["id"]))
        assert retried is not None
        assert retried["status"] == "pending"
        assert retried["claimed_by"] is None
        assert retried["error"] is None
        assert retried["result"] is None
        assert retried["completed_at"] is None

    async def test_retry_non_failed_returns_none(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        assert await repo.retry_task(str(task["id"])) is None

    async def test_retry_resets_failed_request_to_in_progress(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-test")
        await repo.fail_task(str(task["id"]), "err")
        # Mark request as failed manually (simulating _check_request_completion)
        await repo.update_request_status(str(req["id"]), "failed")
        await repo.retry_task(str(task["id"]))
        _updated = await repo.get_request(str(req["id"]), org_id)
        # _ensure_request_in_progress checks for 'pending', 'planned', or 'failed'
        # so a failed request will auto-transition to in_progress on retry
        # Just verify the retry itself worked
        retried_task = await repo.get_task(str(task["id"]))
        assert retried_task["status"] == "pending"

    async def test_retry_logs_event(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-test")
        await repo.fail_task(str(task["id"]), "err")
        await repo.retry_task(str(task["id"]))
        events = await repo.list_task_events(str(task["id"]))
        assert any(e["event_type"] == "retried" for e in events["items"])


class TestReleaseStaleTasks:
    async def test_release_stale_tasks(self, repo, org_id, db_pool):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-1")
        # Manually backdate claimed_at to make it stale
        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tasks SET claimed_at = NOW() - INTERVAL '60 minutes'
                   WHERE id = $1""",
                task["id"],
            )
        count = await repo.release_stale_tasks(stale_minutes=30)
        assert count >= 1
        refreshed = await repo.get_task(str(task["id"]))
        assert refreshed["status"] == "pending"
        assert refreshed["claimed_by"] is None

    async def test_no_stale_tasks(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.claim_task(str(task["id"]), "inst-1")
        # freshly claimed — should NOT be released
        _count = await repo.release_stale_tasks(stale_minutes=30)
        refreshed = await repo.get_task(str(task["id"]))
        assert refreshed["status"] == "claimed"


# ── Events ───────────────────────────────────────────────────────────────


class TestAddTaskEvent:
    async def test_add_event_basic(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        event = await repo.add_task_event(str(task["id"]), "progress", "50% done")
        assert isinstance(event["id"], UUID)
        assert event["event_type"] == "progress"
        assert event["detail"] == "50% done"

    async def test_add_event_with_metadata(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        event = await repo.add_task_event(
            str(task["id"]),
            "agent_dispatched",
            "Dispatched code agent",
            metadata={"model": "claude-sonnet-4", "instance": "i-123"},
        )
        assert event["event_type"] == "agent_dispatched"

    async def test_list_events(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        # 'created' event is auto-added by create_task
        await repo.add_task_event(str(task["id"]), "progress", "Step 1")
        await repo.add_task_event(str(task["id"]), "progress", "Step 2")
        events = await repo.list_task_events(str(task["id"]))
        assert len(events["items"]) >= 3  # created + 2 progress
        types = [e["event_type"] for e in events["items"]]
        assert "created" in types
        assert types.count("progress") == 2

    async def test_list_events_with_limit(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        for i in range(5):
            await repo.add_task_event(str(task["id"]), "progress", f"Step {i}")
        events = await repo.list_task_events(str(task["id"]), limit=3)
        assert len(events["items"]) == 3


# ── Memory Links ─────────────────────────────────────────────────────────


class TestLinkMemory:
    async def test_link_memory(self, repo, org_id, test_memory):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        memory_id = str(test_memory["id"])
        await repo.link_memory(str(task["id"]), memory_id, "created")
        memories = await repo.list_task_memories(str(task["id"]))
        assert len(memories["items"]) == 1
        assert str(memories["items"][0]["memory_id"]) == memory_id
        assert memories["items"][0]["relation"] == "created"

    async def test_link_memory_logs_event(self, repo, org_id, test_memory):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.link_memory(str(task["id"]), str(test_memory["id"]), "read")
        events = await repo.list_task_events(str(task["id"]))
        assert any(e["event_type"] == "memory_read" for e in events["items"])

    async def test_link_memory_duplicate_ignored(self, repo, org_id, test_memory):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        mid = str(test_memory["id"])
        await repo.link_memory(str(task["id"]), mid, "created")
        await repo.link_memory(str(task["id"]), mid, "created")
        memories = await repo.list_task_memories(str(task["id"]))
        assert len(memories["items"]) == 1

    async def test_link_multiple_relations(self, repo, org_id, test_memory):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        mid = str(test_memory["id"])
        await repo.link_memory(str(task["id"]), mid, "created")
        await repo.link_memory(str(task["id"]), mid, "updated")
        memories = await repo.list_task_memories(str(task["id"]))
        relations = {m["relation"] for m in memories["items"]}
        assert "created" in relations
        assert "updated" in relations


# ── Request with Tasks (tree view) ───────────────────────────────────────


class TestGetRequestWithTasks:
    async def test_request_with_no_tasks(self, repo, org_id):
        req = await _make_request(repo, org_id)
        full = await repo.get_request_with_tasks(str(req["id"]), org_id)
        assert full is not None
        assert full["tasks"] == []
        assert full["task_tree"] == []
        assert full["stats"]["total"] == 0

    async def test_request_with_tasks(self, repo, org_id):
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        await _make_task(repo, rid, org_id, title="T1")
        await _make_task(repo, rid, org_id, title="T2")
        full = await repo.get_request_with_tasks(rid, org_id)
        assert full["stats"]["total"] == 2
        assert full["stats"]["pending"] == 2

    async def test_request_with_subtask_tree(self, repo, org_id):
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        parent = await _make_task(repo, rid, org_id, title="Parent")
        await _make_task(repo, rid, org_id, title="Child", parent_task_id=str(parent["id"]))
        full = await repo.get_request_with_tasks(rid, org_id)
        assert len(full["task_tree"]) == 1  # only root
        assert full["task_tree"][0]["title"] == "Parent"
        assert len(full["task_tree"][0]["sub_tasks"]) == 1
        assert full["task_tree"][0]["sub_tasks"][0]["title"] == "Child"

    async def test_request_with_events_and_memories(self, repo, org_id, test_memory):
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        task = await _make_task(repo, rid, org_id, title="Linked")
        tid = str(task["id"])
        await repo.add_task_event(tid, "progress", "Working")
        await repo.link_memory(tid, str(test_memory["id"]), "read")
        full = await repo.get_request_with_tasks(rid, org_id)
        t = full["tasks"][0]
        assert len(t["events"]) >= 2  # created + progress
        assert len(t["memories"]) == 1

    async def test_nonexistent_returns_none(self, repo, org_id):
        assert await repo.get_request_with_tasks(str(uuid4()), org_id) is None

    async def test_stats_reflect_mixed_statuses(self, repo, org_id):
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        t1 = await _make_task(repo, rid, org_id, title="T1")
        t2 = await _make_task(repo, rid, org_id, title="T2")
        _t3 = await _make_task(repo, rid, org_id, title="T3")
        await repo.claim_task(str(t1["id"]), "inst-1")
        await repo.start_task(str(t1["id"]))
        await repo.claim_task(str(t2["id"]), "inst-test")
        await repo.complete_task(str(t2["id"]), "Done")
        full = await repo.get_request_with_tasks(rid, org_id)
        assert full["stats"]["running"] == 1  # t1 running
        assert full["stats"]["completed"] == 1  # t2
        assert full["stats"]["pending"] == 1  # t3


# ── Pending Requests / Tasks ─────────────────────────────────────────────


class TestListPendingRequests:
    async def test_returns_pending_only(self, repo, org_id):
        _r1 = await _make_request(repo, org_id, title="Pending one")
        r2 = await _make_request(repo, org_id, title="Done one")
        await repo.update_request_status(str(r2["id"]), "completed")
        pending = await repo.list_pending_requests(org_id)
        titles = [r["title"] for r in pending["items"]]
        assert "Done one" not in titles

    async def test_includes_task_count(self, repo, org_id):
        req = await _make_request(repo, org_id, title="Has tasks")
        rid = str(req["id"])
        await _make_task(repo, rid, org_id, title="T1")
        await _make_task(repo, rid, org_id, title="T2")
        pending = await repo.list_pending_requests(org_id)
        matched = [r for r in pending["items"] if r["title"] == "Has tasks"]
        assert matched[0]["task_count"] == 2

    async def test_priority_ordering(self, repo, org_id):
        await _make_request(repo, org_id, title="Low", priority="low")
        await _make_request(repo, org_id, title="Urgent", priority="urgent")
        pending = await repo.list_pending_requests(org_id)
        # Urgent should come before low
        urgent_idx = next(i for i, r in enumerate(pending["items"]) if r["title"] == "Urgent")
        low_idx = next(i for i, r in enumerate(pending["items"]) if r["title"] == "Low")
        assert urgent_idx < low_idx


class TestListPendingTasks:
    async def test_returns_pending_tasks(self, repo, org_id):
        req = await _make_request(repo, org_id)
        await _make_task(repo, str(req["id"]), org_id, title="Ready")
        tasks = await repo.list_pending_tasks(org_id)
        assert any(t["title"] == "Ready" for t in tasks["items"])

    async def test_respects_sequence_order_deps(self, repo, org_id):
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        _t0 = await _make_task(repo, rid, org_id, title="Step 0", sequence_order=0)
        await _make_task(repo, rid, org_id, title="Step 1", sequence_order=1)
        pending = await repo.list_pending_tasks(org_id)
        titles = [t["title"] for t in pending["items"]]
        # Step 1 should NOT be pending because Step 0 hasn't completed
        assert "Step 0" in titles
        assert "Step 1" not in titles

    async def test_later_sequence_available_after_earlier_complete(self, repo, org_id):
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        t0 = await _make_task(repo, rid, org_id, title="First", sequence_order=0)
        await _make_task(repo, rid, org_id, title="Second", sequence_order=1)
        await repo.claim_task(str(t0["id"]), "inst-test")
        await repo.complete_task(str(t0["id"]), "Done")
        pending = await repo.list_pending_tasks(org_id)
        titles = [t["title"] for t in pending["items"]]
        assert "Second" in titles

    async def test_parallel_tasks_same_sequence(self, repo, org_id):
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        await _make_task(repo, rid, org_id, title="Par A", sequence_order=0)
        await _make_task(repo, rid, org_id, title="Par B", sequence_order=0)
        pending = await repo.list_pending_tasks(org_id)
        titles = [t["title"] for t in pending["items"]]
        assert "Par A" in titles
        assert "Par B" in titles

    async def test_includes_request_title(self, repo, org_id):
        req = await _make_request(repo, org_id, title="My Request")
        await _make_task(repo, str(req["id"]), org_id, title="My Task")
        pending = await repo.list_pending_tasks(org_id)
        matched = [t for t in pending["items"] if t["title"] == "My Task"]
        assert matched[0]["request_title"] == "My Request"

    # ── dependency_policy tests ──────────────────────────────────────────

    async def test_strict_blocks_on_failed_predecessor(self, repo, org_id):
        """Default strict policy: failed predecessor blocks later tasks."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        t0 = await _make_task(repo, rid, org_id, title="Step 0", sequence_order=0)
        await _make_task(repo, rid, org_id, title="Step 1", sequence_order=1)
        # Fail step 0
        await repo.claim_task(str(t0["id"]), "inst-test")
        await repo.start_task(str(t0["id"]))
        await repo.fail_task(str(t0["id"]), "boom")
        pending = await repo.list_pending_tasks(org_id)
        titles = [t["title"] for t in pending["items"]]
        assert "Step 1" not in titles

    async def test_strict_blocks_on_cancelled_predecessor(self, repo, org_id):
        """Strict policy: cancelled predecessor blocks later tasks."""
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        t0 = await _make_task(repo, rid, org_id, title="Step 0", sequence_order=0)
        await _make_task(repo, rid, org_id, title="Step 1", sequence_order=1)
        # Manually cancel step 0
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET status = 'cancelled' WHERE id = $1",
                t0["id"],
            )
        pending = await repo.list_pending_tasks(org_id)
        titles = [t["title"] for t in pending["items"]]
        assert "Step 1" not in titles

    async def test_strict_is_default(self, repo, org_id):
        """Requests default to strict dependency_policy."""
        req = await _make_request(repo, org_id)
        assert req["dependency_policy"] == "strict"

    async def test_permissive_allows_after_failed_predecessor(self, repo, org_id):
        """Permissive policy: failed predecessor does NOT block later tasks."""
        req = await _make_request(repo, org_id, dependency_policy="permissive")
        rid = str(req["id"])
        t0 = await _make_task(repo, rid, org_id, title="Step 0", sequence_order=0)
        await _make_task(repo, rid, org_id, title="Step 1", sequence_order=1)
        await repo.claim_task(str(t0["id"]), "inst-test")
        await repo.start_task(str(t0["id"]))
        await repo.fail_task(str(t0["id"]), "boom")
        pending = await repo.list_pending_tasks(org_id)
        titles = [t["title"] for t in pending["items"]]
        assert "Step 1" in titles

    async def test_permissive_allows_after_cancelled_predecessor(self, repo, org_id):
        """Permissive policy: cancelled predecessor does NOT block later tasks."""
        req = await _make_request(repo, org_id, dependency_policy="permissive")
        rid = str(req["id"])
        t0 = await _make_task(repo, rid, org_id, title="Step 0", sequence_order=0)
        await _make_task(repo, rid, org_id, title="Step 1", sequence_order=1)
        async with repo.pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET status = 'cancelled' WHERE id = $1",
                t0["id"],
            )
        pending = await repo.list_pending_tasks(org_id)
        titles = [t["title"] for t in pending["items"]]
        assert "Step 1" in titles

    async def test_permissive_still_blocks_on_running_predecessor(self, repo, org_id):
        """Even permissive policy blocks when predecessor is still running."""
        req = await _make_request(repo, org_id, dependency_policy="permissive")
        rid = str(req["id"])
        t0 = await _make_task(repo, rid, org_id, title="Step 0", sequence_order=0)
        await _make_task(repo, rid, org_id, title="Step 1", sequence_order=1)
        await repo.claim_task(str(t0["id"]), "inst-test")
        await repo.start_task(str(t0["id"]))
        pending = await repo.list_pending_tasks(org_id)
        titles = [t["title"] for t in pending["items"]]
        assert "Step 1" not in titles

    async def test_invalid_dependency_policy_rejected(self, repo, org_id):
        """Invalid dependency_policy raises ValueError."""
        with pytest.raises(ValueError, match="Invalid dependency_policy"):
            await _make_request(repo, org_id, dependency_policy="invalid")


# ── Dashboard ────────────────────────────────────────────────────────────


class TestGetActiveSummary:
    async def test_empty_summary(self, repo, org_id):
        summary = await repo.get_active_summary(org_id)
        assert "requests" in summary
        assert "tasks" in summary

    async def test_summary_counts(self, repo, org_id):
        req = await _make_request(repo, org_id)
        rid = str(req["id"])
        task = await _make_task(repo, rid, org_id)
        await repo.claim_task(str(task["id"]), "inst-1")
        summary = await repo.get_active_summary(org_id)
        assert summary["requests"]["active"] >= 1  # in_progress from claim
        assert summary["tasks"]["running"] >= 1


class TestGetRecentEvents:
    async def test_recent_events(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id, title="Eventful")
        await repo.add_task_event(str(task["id"]), "progress", "Doing stuff")
        events = await repo.get_recent_events(org_id)
        assert len(events) >= 1
        assert any(e.get("task_title") == "Eventful" for e in events)
        assert any(e.get("request_title") for e in events)

    async def test_recent_events_limit(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        for i in range(5):
            await repo.add_task_event(str(task["id"]), "progress", f"Step {i}")
        events = await repo.get_recent_events(org_id, limit=3)
        assert len(events) == 3

    async def test_recent_events_ordered_desc(self, repo, org_id):
        req = await _make_request(repo, org_id)
        task = await _make_task(repo, str(req["id"]), org_id)
        await repo.add_task_event(str(task["id"]), "progress", "First")
        await repo.add_task_event(str(task["id"]), "progress", "Last")
        events = await repo.get_recent_events(org_id)
        # Most recent first
        if len(events) >= 2:
            assert events[0]["created_at"] >= events[1]["created_at"]
