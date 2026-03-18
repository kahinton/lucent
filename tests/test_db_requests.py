"""Tests for db/requests.py — RequestRepository.

Covers: request CRUD, task lifecycle, events, memory links, dashboard queries.
"""

import pytest
import pytest_asyncio

from lucent.db.requests import RequestRepository


@pytest_asyncio.fixture
async def repo(db_pool):
    return RequestRepository(db_pool)


@pytest_asyncio.fixture
async def req(repo, test_organization):
    """Create a test request."""
    return await repo.create_request(
        title="Test Request",
        org_id=str(test_organization["id"]),
        description="A test request",
        source="user",
        priority="medium",
    )


@pytest_asyncio.fixture
async def task(repo, req, test_organization):
    """Create a test task within the test request."""
    return await repo.create_task(
        request_id=str(req["id"]),
        title="Test Task",
        org_id=str(test_organization["id"]),
        description="A test task",
        agent_type="code",
        priority="medium",
    )


@pytest_asyncio.fixture
async def other_org(db_pool, clean_test_data):
    """Create a second organization for cross-org isolation tests."""
    from lucent.db import OrganizationRepository

    prefix = clean_test_data
    repo = OrganizationRepository(db_pool)
    org = await repo.create(name=f"{prefix}other_org")
    return org


@pytest_asyncio.fixture(autouse=True)
async def cleanup_requests(db_pool, test_organization, clean_test_data):
    """Clean up request tracking data after each test."""
    yield
    prefix = clean_test_data
    async with db_pool.acquire() as conn:
        # task_events and task_memories cascade from tasks, tasks cascade from requests
        await conn.execute(
            "DELETE FROM requests WHERE organization_id = $1",
            test_organization["id"],
        )
        # Also clean up any other-org requests created during cross-org tests
        other_orgs = await conn.fetch(
            "SELECT id FROM organizations WHERE name LIKE $1 AND id != $2",
            f"{prefix}%",
            test_organization["id"],
        )
        for org in other_orgs:
            await conn.execute("DELETE FROM requests WHERE organization_id = $1", org["id"])


class TestCreateRequest:
    @pytest.mark.asyncio
    async def test_create_basic(self, repo, test_organization):
        r = await repo.create_request(
            title="My Request",
            org_id=str(test_organization["id"]),
        )
        assert r["title"] == "My Request"
        assert r["status"] == "pending"
        assert r["priority"] == "medium"
        assert r["source"] == "user"
        assert r["organization_id"] == test_organization["id"]

    @pytest.mark.asyncio
    async def test_create_with_all_fields(self, repo, test_organization, test_user):
        r = await repo.create_request(
            title="Full Request",
            org_id=str(test_organization["id"]),
            description="Detailed description",
            source="cognitive",
            priority="urgent",
            created_by=str(test_user["id"]),
        )
        assert r["description"] == "Detailed description"
        assert r["source"] == "cognitive"
        assert r["priority"] == "urgent"
        assert r["created_by"] == test_user["id"]


class TestGetRequest:
    @pytest.mark.asyncio
    async def test_get_existing(self, repo, req, test_organization):
        found = await repo.get_request(str(req["id"]), str(test_organization["id"]))
        assert found is not None
        assert found["id"] == req["id"]
        assert found["title"] == "Test Request"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repo, test_organization):
        found = await repo.get_request(
            "00000000-0000-0000-0000-000000000000",
            str(test_organization["id"]),
        )
        assert found is None

    @pytest.mark.asyncio
    async def test_get_wrong_org(self, repo, req, db_pool, clean_test_data):
        """Request from org A not visible to org B."""
        from lucent.db import OrganizationRepository

        prefix = clean_test_data
        other_org = await OrganizationRepository(db_pool).create(name=f"{prefix}other_org")
        found = await repo.get_request(str(req["id"]), str(other_org["id"]))
        assert found is None


class TestListRequests:
    @pytest.mark.asyncio
    async def test_list_basic(self, repo, req, test_organization):
        results = await repo.list_requests(str(test_organization["id"]))
        assert len(results) >= 1
        assert any(r["id"] == req["id"] for r in results)

    @pytest.mark.asyncio
    async def test_list_filter_status(self, repo, req, test_organization):
        org = str(test_organization["id"])
        results = await repo.list_requests(org, status="pending")
        assert any(r["id"] == req["id"] for r in results)

        results = await repo.list_requests(org, status="completed")
        assert not any(r["id"] == req["id"] for r in results)

    @pytest.mark.asyncio
    async def test_list_filter_source(self, repo, req, test_organization):
        org = str(test_organization["id"])
        results = await repo.list_requests(org, source="user")
        assert any(r["id"] == req["id"] for r in results)

        results = await repo.list_requests(org, source="daemon")
        assert not any(r["id"] == req["id"] for r in results)

    @pytest.mark.asyncio
    async def test_list_pagination(self, repo, test_organization):
        org = str(test_organization["id"])
        for i in range(3):
            await repo.create_request(title=f"Req {i}", org_id=org)
        page1 = await repo.list_requests(org, limit=2, offset=0)
        page2 = await repo.list_requests(org, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) >= 1
        ids1 = {r["id"] for r in page1}
        ids2 = {r["id"] for r in page2}
        assert ids1.isdisjoint(ids2)


class TestUpdateRequestStatus:
    @pytest.mark.asyncio
    async def test_update_to_in_progress(self, repo, req):
        updated = await repo.update_request_status(str(req["id"]), "in_progress")
        assert updated["status"] == "in_progress"
        assert updated["completed_at"] is None

    @pytest.mark.asyncio
    async def test_update_to_completed(self, repo, req):
        updated = await repo.update_request_status(str(req["id"]), "completed")
        assert updated["status"] == "completed"
        assert updated["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_update_to_failed(self, repo, req):
        updated = await repo.update_request_status(str(req["id"]), "failed")
        assert updated["status"] == "failed"
        assert updated["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, repo):
        result = await repo.update_request_status(
            "00000000-0000-0000-0000-000000000000", "completed"
        )
        assert result is None


class TestCreateTask:
    @pytest.mark.asyncio
    async def test_create_basic(self, repo, req, test_organization):
        t = await repo.create_task(
            request_id=str(req["id"]),
            title="Task 1",
            org_id=str(test_organization["id"]),
        )
        assert t["title"] == "Task 1"
        assert t["status"] == "pending"
        assert t["request_id"] == req["id"]

    @pytest.mark.asyncio
    async def test_create_with_all_fields(self, repo, req, test_organization):
        t = await repo.create_task(
            request_id=str(req["id"]),
            title="Full Task",
            org_id=str(test_organization["id"]),
            description="Detailed task",
            agent_type="security",
            priority="high",
            sequence_order=2,
            model="claude-sonnet-4",
            sandbox_config={"image": "python:3.12"},
        )
        assert t["description"] == "Detailed task"
        assert t["agent_type"] == "security"
        assert t["priority"] == "high"
        assert t["sequence_order"] == 2
        assert t["model"] == "claude-sonnet-4"

    @pytest.mark.asyncio
    async def test_create_logs_event(self, repo, req, test_organization):
        t = await repo.create_task(
            request_id=str(req["id"]),
            title="Task With Event",
            org_id=str(test_organization["id"]),
        )
        events = await repo.list_task_events(str(t["id"]))
        assert len(events) == 1
        assert events[0]["event_type"] == "created"

    @pytest.mark.asyncio
    async def test_create_subtask(self, repo, req, task, test_organization):
        sub = await repo.create_task(
            request_id=str(req["id"]),
            title="Sub Task",
            org_id=str(test_organization["id"]),
            parent_task_id=str(task["id"]),
        )
        assert sub["parent_task_id"] == task["id"]


class TestGetTask:
    @pytest.mark.asyncio
    async def test_get_existing(self, repo, task):
        found = await repo.get_task(str(task["id"]))
        assert found is not None
        assert found["title"] == "Test Task"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repo):
        found = await repo.get_task("00000000-0000-0000-0000-000000000000")
        assert found is None


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_by_request(self, repo, req, task, test_organization):
        tasks = await repo.list_tasks(str(req["id"]))
        assert len(tasks) >= 1
        assert any(t["id"] == task["id"] for t in tasks)

    @pytest.mark.asyncio
    async def test_list_filter_status(self, repo, req, task, test_organization):
        tasks = await repo.list_tasks(str(req["id"]), status="pending")
        assert any(t["id"] == task["id"] for t in tasks)

        tasks = await repo.list_tasks(str(req["id"]), status="completed")
        assert not any(t["id"] == task["id"] for t in tasks)

    @pytest.mark.asyncio
    async def test_list_ordered_by_sequence(self, repo, req, test_organization):
        org = str(test_organization["id"])
        _t0 = await repo.create_task(
            request_id=str(req["id"]), title="First", org_id=org, sequence_order=0
        )
        _t1 = await repo.create_task(
            request_id=str(req["id"]), title="Second", org_id=org, sequence_order=1
        )
        tasks = await repo.list_tasks(str(req["id"]))
        titles = [t["title"] for t in tasks if t["title"] in ("First", "Second")]
        assert titles.index("First") < titles.index("Second")


class TestListPendingRequests:
    @pytest.mark.asyncio
    async def test_includes_pending(self, repo, req, test_organization):
        results = await repo.list_pending_requests(str(test_organization["id"]))
        assert any(r["id"] == req["id"] for r in results)

    @pytest.mark.asyncio
    async def test_excludes_completed(self, repo, req, test_organization):
        await repo.update_request_status(str(req["id"]), "completed")
        results = await repo.list_pending_requests(str(test_organization["id"]))
        assert not any(r["id"] == req["id"] for r in results)

    @pytest.mark.asyncio
    async def test_includes_task_count(self, repo, req, task, test_organization):
        results = await repo.list_pending_requests(str(test_organization["id"]))
        matching = [r for r in results if r["id"] == req["id"]]
        assert matching[0]["task_count"] >= 1

    @pytest.mark.asyncio
    async def test_priority_ordering(self, repo, test_organization):
        org = str(test_organization["id"])
        low = await repo.create_request(title="Low", org_id=org, priority="low")
        urgent = await repo.create_request(title="Urgent", org_id=org, priority="urgent")
        results = await repo.list_pending_requests(org)
        ids = [r["id"] for r in results]
        assert ids.index(urgent["id"]) < ids.index(low["id"])


class TestListPendingTasks:
    @pytest.mark.asyncio
    async def test_includes_pending_tasks(self, repo, req, task, test_organization):
        results = await repo.list_pending_tasks(str(test_organization["id"]))
        assert any(t["id"] == task["id"] for t in results)

    @pytest.mark.asyncio
    async def test_excludes_claimed_tasks(self, repo, req, task, test_organization):
        await repo.claim_task(str(task["id"]), "test-instance")
        results = await repo.list_pending_tasks(str(test_organization["id"]))
        assert not any(t["id"] == task["id"] for t in results)

    @pytest.mark.asyncio
    async def test_sequence_gating(self, repo, req, test_organization):
        """Task at seq 1 not dispatchable until seq 0 is done."""
        org = str(test_organization["id"])
        t0 = await repo.create_task(
            request_id=str(req["id"]), title="Seq0", org_id=org, sequence_order=0
        )
        t1 = await repo.create_task(
            request_id=str(req["id"]), title="Seq1", org_id=org, sequence_order=1
        )
        pending = await repo.list_pending_tasks(org)
        pending_ids = [t["id"] for t in pending]
        assert t0["id"] in pending_ids
        assert t1["id"] not in pending_ids

        # Complete t0, now t1 should be dispatchable
        await repo.claim_task(str(t0["id"]), "inst")
        await repo.start_task(str(t0["id"]))
        await repo.complete_task(str(t0["id"]), "done")
        pending = await repo.list_pending_tasks(org)
        pending_ids = [t["id"] for t in pending]
        assert t1["id"] in pending_ids

    @pytest.mark.asyncio
    async def test_includes_request_title(self, repo, req, task, test_organization):
        results = await repo.list_pending_tasks(str(test_organization["id"]))
        matching = [t for t in results if t["id"] == task["id"]]
        assert matching[0]["request_title"] == "Test Request"


class TestTaskLifecycle:
    @pytest.mark.asyncio
    async def test_claim_task(self, repo, task):
        claimed = await repo.claim_task(str(task["id"]), "daemon-1")
        assert claimed is not None
        assert claimed["status"] == "claimed"
        assert claimed["claimed_by"] == "daemon-1"
        assert claimed["claimed_at"] is not None

    @pytest.mark.asyncio
    async def test_claim_already_claimed(self, repo, task):
        await repo.claim_task(str(task["id"]), "daemon-1")
        second = await repo.claim_task(str(task["id"]), "daemon-2")
        assert second is None

    @pytest.mark.asyncio
    async def test_claim_sets_request_in_progress(self, repo, req, task):
        await repo.claim_task(str(task["id"]), "daemon-1")
        org_id = str(req["organization_id"])
        updated_req = await repo.get_request(str(req["id"]), org_id)
        assert updated_req["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_start_task(self, repo, task):
        await repo.claim_task(str(task["id"]), "daemon-1")
        started = await repo.start_task(str(task["id"]))
        assert started is not None
        assert started["status"] == "running"

    @pytest.mark.asyncio
    async def test_start_unclaimed_task(self, repo, task):
        result = await repo.start_task(str(task["id"]))
        assert result is None

    @pytest.mark.asyncio
    async def test_complete_task(self, repo, task):
        await repo.claim_task(str(task["id"]), "daemon-1")
        completed = await repo.complete_task(str(task["id"]), "All done")
        assert completed is not None
        assert completed["status"] == "completed"
        assert completed["result"] == "All done"
        assert completed["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_fail_task(self, repo, task):
        await repo.claim_task(str(task["id"]), "daemon-1")
        failed = await repo.fail_task(str(task["id"]), "Something broke")
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["error"] == "Something broke"
        assert failed["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_release_claimed_task(self, repo, task):
        await repo.claim_task(str(task["id"]), "daemon-1")
        released = await repo.release_task(str(task["id"]))
        assert released is not None
        assert released["status"] == "pending"
        assert released["claimed_by"] is None
        assert released["claimed_at"] is None

    @pytest.mark.asyncio
    async def test_release_running_task(self, repo, task):
        await repo.claim_task(str(task["id"]), "daemon-1")
        await repo.start_task(str(task["id"]))
        released = await repo.release_task(str(task["id"]))
        assert released is not None
        assert released["status"] == "pending"

    @pytest.mark.asyncio
    async def test_release_pending_task_noop(self, repo, task):
        result = await repo.release_task(str(task["id"]))
        assert result is None

    @pytest.mark.asyncio
    async def test_retry_failed_task(self, repo, task):
        await repo.claim_task(str(task["id"]), "daemon-1")
        await repo.fail_task(str(task["id"]), "error")
        retried = await repo.retry_task(str(task["id"]))
        assert retried is not None
        assert retried["status"] == "pending"
        assert retried["claimed_by"] is None
        assert retried["error"] is None
        assert retried["result"] is None

    @pytest.mark.asyncio
    async def test_retry_non_failed_noop(self, repo, task):
        result = await repo.retry_task(str(task["id"]))
        assert result is None

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, repo, task):
        """pending → claimed → running → completed."""
        tid = str(task["id"])
        assert task["status"] == "pending"
        await repo.claim_task(tid, "d1")
        await repo.start_task(tid)
        result = await repo.complete_task(tid, "Success!")
        assert result["status"] == "completed"


class TestReleaseStale:
    @pytest.mark.asyncio
    async def test_releases_stale_tasks(self, repo, task, db_pool):
        tid = str(task["id"])
        await repo.claim_task(tid, "daemon-1")
        # Manually backdate claimed_at
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET claimed_at = NOW() - interval '60 minutes' WHERE id = $1",
                task["id"],
            )
        count = await repo.release_stale_tasks(stale_minutes=30)
        assert count >= 1
        refreshed = await repo.get_task(tid)
        assert refreshed["status"] == "pending"

    @pytest.mark.asyncio
    async def test_does_not_release_fresh_tasks(self, repo, task):
        await repo.claim_task(str(task["id"]), "daemon-1")
        count = await repo.release_stale_tasks(stale_minutes=30)
        assert count == 0


class TestRequestCompletion:
    @pytest.mark.asyncio
    async def test_auto_complete_when_all_tasks_done(self, repo, req, task):
        """Request auto-completes when all tasks are completed."""
        await repo.claim_task(str(task["id"]), "d1")
        await repo.complete_task(str(task["id"]), "done")
        org_id = str(req["organization_id"])
        updated = await repo.get_request(str(req["id"]), org_id)
        assert updated["status"] == "completed"

    @pytest.mark.asyncio
    async def test_auto_fail_when_any_task_failed(self, repo, test_organization):
        """When all tasks are done and at least one failed, request is failed.

        Note: only complete_task triggers _check_request_completion,
        so the completing task must be last.
        """
        org = str(test_organization["id"])
        r = await repo.create_request(title="Fail Test", org_id=org)
        t1 = await repo.create_task(request_id=str(r["id"]), title="T1", org_id=org)
        t2 = await repo.create_task(request_id=str(r["id"]), title="T2", org_id=org)
        # Fail t1 first
        await repo.claim_task(str(t1["id"]), "d1")
        await repo.fail_task(str(t1["id"]), "oops")
        # Then complete t2 — this triggers the completion check
        await repo.claim_task(str(t2["id"]), "d1")
        await repo.complete_task(str(t2["id"]), "ok")
        updated = await repo.get_request(str(r["id"]), org)
        assert updated["status"] == "failed"

    @pytest.mark.asyncio
    async def test_retry_resets_pending_request_to_in_progress(self, repo, test_organization):
        """Retrying a task on a pending request moves it to in_progress."""
        org = str(test_organization["id"])
        r = await repo.create_request(title="Retry Test", org_id=org)
        t = await repo.create_task(request_id=str(r["id"]), title="T", org_id=org)
        tid = str(t["id"])
        await repo.claim_task(tid, "d1")
        await repo.fail_task(tid, "oops")
        # Reset request to pending to simulate a scenario where retry should activate it
        await repo.update_request_status(str(r["id"]), "pending")
        await repo.retry_task(tid)
        updated = await repo.get_request(str(r["id"]), org)
        assert updated["status"] == "in_progress"


class TestTaskEvents:
    @pytest.mark.asyncio
    async def test_add_event(self, repo, task):
        event = await repo.add_task_event(
            str(task["id"]), "progress", "50% done", metadata={"pct": 50}
        )
        assert event["event_type"] == "progress"
        assert event["detail"] == "50% done"

    @pytest.mark.asyncio
    async def test_list_events(self, repo, task):
        tid = str(task["id"])
        # Task creation already logged one event
        await repo.add_task_event(tid, "info", "extra")
        events = await repo.list_task_events(tid)
        assert len(events) >= 2
        types = [e["event_type"] for e in events]
        assert "created" in types
        assert "info" in types

    @pytest.mark.asyncio
    async def test_lifecycle_events(self, repo, task):
        """Full lifecycle produces expected event trail."""
        tid = str(task["id"])
        await repo.claim_task(tid, "d1")
        await repo.start_task(tid)
        await repo.complete_task(tid, "done")
        events = await repo.list_task_events(tid)
        types = [e["event_type"] for e in events]
        assert "created" in types
        assert "claimed" in types
        assert "running" in types
        assert "completed" in types


class TestTaskMemoryLinks:
    @pytest.mark.asyncio
    async def test_link_memory(self, repo, task, test_memory):
        await repo.link_memory(str(task["id"]), str(test_memory["id"]), relation="created")
        memories = await repo.list_task_memories(str(task["id"]))
        assert len(memories) == 1
        assert memories[0]["relation"] == "created"
        assert memories[0]["memory_id"] == test_memory["id"]

    @pytest.mark.asyncio
    async def test_link_memory_idempotent(self, repo, task, test_memory):
        """Linking same memory twice with same relation is a no-op (ON CONFLICT)."""
        tid, mid = str(task["id"]), str(test_memory["id"])
        await repo.link_memory(tid, mid, "read")
        await repo.link_memory(tid, mid, "read")
        memories = await repo.list_task_memories(tid)
        read_links = [m for m in memories if m["relation"] == "read"]
        assert len(read_links) == 1

    @pytest.mark.asyncio
    async def test_link_different_relations(self, repo, task, test_memory):
        tid, mid = str(task["id"]), str(test_memory["id"])
        await repo.link_memory(tid, mid, "read")
        await repo.link_memory(tid, mid, "updated")
        memories = await repo.list_task_memories(tid)
        relations = {m["relation"] for m in memories}
        assert "read" in relations
        assert "updated" in relations

    @pytest.mark.asyncio
    async def test_link_creates_event(self, repo, task, test_memory):
        await repo.link_memory(str(task["id"]), str(test_memory["id"]), "created")
        events = await repo.list_task_events(str(task["id"]))
        link_events = [e for e in events if e["event_type"] == "memory_created"]
        assert len(link_events) == 1


class TestGetRequestWithTasks:
    @pytest.mark.asyncio
    async def test_full_tree(self, repo, req, task, test_organization, test_memory):
        org = str(test_organization["id"])
        tid = str(task["id"])

        # Add an event and memory link
        await repo.add_task_event(tid, "info", "test event")
        await repo.link_memory(tid, str(test_memory["id"]), "created")

        result = await repo.get_request_with_tasks(str(req["id"]), org)
        assert result is not None
        assert result["title"] == "Test Request"
        assert len(result["tasks"]) >= 1
        assert "task_tree" in result
        assert "stats" in result

        # Check task has events and memories attached
        t = next(t for t in result["tasks"] if t["id"] == task["id"])
        assert len(t["events"]) >= 1
        assert len(t["memories"]) >= 1

    @pytest.mark.asyncio
    async def test_task_tree_structure(self, repo, req, task, test_organization):
        """Sub-tasks nested under parent in task_tree."""
        org = str(test_organization["id"])
        sub = await repo.create_task(
            request_id=str(req["id"]),
            title="Sub",
            org_id=org,
            parent_task_id=str(task["id"]),
        )
        result = await repo.get_request_with_tasks(str(req["id"]), org)
        # Root task_tree should have the parent, with sub nested
        root_ids = [str(t["id"]) for t in result["task_tree"]]
        assert str(task["id"]) in root_ids
        assert str(sub["id"]) not in root_ids
        parent = next(t for t in result["task_tree"] if t["id"] == task["id"])
        assert any(s["id"] == sub["id"] for s in parent["sub_tasks"])

    @pytest.mark.asyncio
    async def test_stats(self, repo, req, task, test_organization):
        org = str(test_organization["id"])
        result = await repo.get_request_with_tasks(str(req["id"]), org)
        assert result["stats"]["total"] >= 1
        assert result["stats"]["pending"] >= 1

    @pytest.mark.asyncio
    async def test_nonexistent(self, repo, test_organization):
        result = await repo.get_request_with_tasks(
            "00000000-0000-0000-0000-000000000000",
            str(test_organization["id"]),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_tasks(self, repo, test_organization):
        org = str(test_organization["id"])
        r = await repo.create_request(title="Empty", org_id=org)
        result = await repo.get_request_with_tasks(str(r["id"]), org)
        assert result is not None
        assert result["tasks"] == []
        assert result["task_tree"] == []
        assert result["stats"]["total"] == 0


class TestDashboardQueries:
    @pytest.mark.asyncio
    async def test_get_active_summary(self, repo, req, task, test_organization):
        org = str(test_organization["id"])
        summary = await repo.get_active_summary(org)
        assert "requests" in summary
        assert "tasks" in summary
        assert summary["requests"]["pending"] >= 1
        assert summary["tasks"]["queued"] >= 1

    @pytest.mark.asyncio
    async def test_get_recent_events(self, repo, req, task, test_organization):
        org = str(test_organization["id"])
        events = await repo.get_recent_events(org)
        assert len(events) >= 1
        # Events should include task and request context
        assert events[0].get("task_title") is not None
        assert events[0].get("request_title") is not None

    @pytest.mark.asyncio
    async def test_recent_events_limit(self, repo, req, task, test_organization):
        org = str(test_organization["id"])
        tid = str(task["id"])
        for i in range(5):
            await repo.add_task_event(tid, "progress", f"step {i}")
        events = await repo.get_recent_events(org, limit=3)
        assert len(events) == 3


class TestCrossOrgIsolation:
    """Verify that org_id filtering prevents cross-org access to tasks and requests.

    Each test creates data in org_A (test_organization), then attempts to
    access/mutate it using org_B (other_org). The cross-org attempt must fail,
    while the same-org attempt must succeed.
    """

    # ── 1. get_task ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_task_cross_org_blocked(self, repo, task, other_org):
        """get_task with wrong org_id returns None."""
        result = await repo.get_task(str(task["id"]), org_id=str(other_org["id"]))
        assert result is None

    @pytest.mark.asyncio
    async def test_get_task_same_org_allowed(self, repo, task, test_organization):
        """get_task with correct org_id returns the task."""
        result = await repo.get_task(str(task["id"]), org_id=str(test_organization["id"]))
        assert result is not None
        assert result["id"] == task["id"]

    # ── 2. claim_task ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_claim_task_cross_org_blocked(self, repo, task, other_org):
        """claim_task with wrong org_id returns None (task stays pending)."""
        result = await repo.claim_task(
            str(task["id"]), "attacker-instance", org_id=str(other_org["id"])
        )
        assert result is None
        # Verify task is still pending (not mutated)
        original = await repo.get_task(str(task["id"]))
        assert original["status"] == "pending"

    @pytest.mark.asyncio
    async def test_claim_task_same_org_allowed(self, repo, task, test_organization):
        """claim_task with correct org_id succeeds."""
        result = await repo.claim_task(
            str(task["id"]), "legit-instance", org_id=str(test_organization["id"])
        )
        assert result is not None
        assert result["status"] == "claimed"

    # ── 3. start_task ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_start_task_cross_org_blocked(self, repo, task, other_org):
        """start_task with wrong org_id returns None."""
        await repo.claim_task(str(task["id"]), "daemon-1")
        result = await repo.start_task(str(task["id"]), org_id=str(other_org["id"]))
        assert result is None
        # Verify task is still claimed (not mutated)
        original = await repo.get_task(str(task["id"]))
        assert original["status"] == "claimed"

    @pytest.mark.asyncio
    async def test_start_task_same_org_allowed(self, repo, task, test_organization):
        """start_task with correct org_id succeeds."""
        await repo.claim_task(str(task["id"]), "daemon-1")
        result = await repo.start_task(
            str(task["id"]), org_id=str(test_organization["id"])
        )
        assert result is not None
        assert result["status"] == "running"

    # ── 4. complete_task ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_complete_task_cross_org_blocked(self, repo, task, other_org):
        """complete_task with wrong org_id returns None."""
        await repo.claim_task(str(task["id"]), "daemon-1")
        result = await repo.complete_task(
            str(task["id"]), "hacked", org_id=str(other_org["id"])
        )
        assert result is None
        original = await repo.get_task(str(task["id"]))
        assert original["status"] == "claimed"

    @pytest.mark.asyncio
    async def test_complete_task_same_org_allowed(self, repo, task, test_organization):
        """complete_task with correct org_id succeeds."""
        await repo.claim_task(str(task["id"]), "daemon-1")
        result = await repo.complete_task(
            str(task["id"]), "done", org_id=str(test_organization["id"])
        )
        assert result is not None
        assert result["status"] == "completed"

    # ── 5. fail_task ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fail_task_cross_org_blocked(self, repo, task, other_org):
        """fail_task with wrong org_id returns None."""
        await repo.claim_task(str(task["id"]), "daemon-1")
        result = await repo.fail_task(
            str(task["id"]), "sabotage", org_id=str(other_org["id"])
        )
        assert result is None
        original = await repo.get_task(str(task["id"]))
        assert original["status"] == "claimed"

    @pytest.mark.asyncio
    async def test_fail_task_same_org_allowed(self, repo, task, test_organization):
        """fail_task with correct org_id succeeds."""
        await repo.claim_task(str(task["id"]), "daemon-1")
        result = await repo.fail_task(
            str(task["id"]), "real error", org_id=str(test_organization["id"])
        )
        assert result is not None
        assert result["status"] == "failed"

    # ── 6. release_task ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_release_task_cross_org_blocked(self, repo, task, other_org):
        """release_task with wrong org_id returns None."""
        await repo.claim_task(str(task["id"]), "daemon-1")
        result = await repo.release_task(str(task["id"]), org_id=str(other_org["id"]))
        assert result is None
        original = await repo.get_task(str(task["id"]))
        assert original["status"] == "claimed"

    @pytest.mark.asyncio
    async def test_release_task_same_org_allowed(self, repo, task, test_organization):
        """release_task with correct org_id succeeds."""
        await repo.claim_task(str(task["id"]), "daemon-1")
        result = await repo.release_task(
            str(task["id"]), org_id=str(test_organization["id"])
        )
        assert result is not None
        assert result["status"] == "pending"

    # ── 7. retry_task ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_retry_task_cross_org_blocked(self, repo, task, other_org):
        """retry_task with wrong org_id returns None."""
        await repo.claim_task(str(task["id"]), "daemon-1")
        await repo.fail_task(str(task["id"]), "error")
        result = await repo.retry_task(str(task["id"]), org_id=str(other_org["id"]))
        assert result is None
        original = await repo.get_task(str(task["id"]))
        assert original["status"] == "failed"

    @pytest.mark.asyncio
    async def test_retry_task_same_org_allowed(self, repo, task, test_organization):
        """retry_task with correct org_id succeeds."""
        await repo.claim_task(str(task["id"]), "daemon-1")
        await repo.fail_task(str(task["id"]), "error")
        result = await repo.retry_task(
            str(task["id"]), org_id=str(test_organization["id"])
        )
        assert result is not None
        assert result["status"] == "pending"

    # ── 8. release_stale_tasks ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_release_stale_tasks_cross_org_blocked(
        self, repo, task, other_org, db_pool
    ):
        """release_stale_tasks with wrong org_id returns 0 affected rows."""
        await repo.claim_task(str(task["id"]), "daemon-1")
        # Backdate claimed_at to make it stale
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET claimed_at = NOW() - interval '60 minutes' WHERE id = $1",
                task["id"],
            )
        count = await repo.release_stale_tasks(
            stale_minutes=30, org_id=str(other_org["id"])
        )
        assert count == 0
        # Verify task is still claimed (not released by wrong org)
        original = await repo.get_task(str(task["id"]))
        assert original["status"] == "claimed"

    @pytest.mark.asyncio
    async def test_release_stale_tasks_same_org_allowed(
        self, repo, task, test_organization, db_pool
    ):
        """release_stale_tasks with correct org_id releases the task."""
        await repo.claim_task(str(task["id"]), "daemon-1")
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET claimed_at = NOW() - interval '60 minutes' WHERE id = $1",
                task["id"],
            )
        count = await repo.release_stale_tasks(
            stale_minutes=30, org_id=str(test_organization["id"])
        )
        assert count >= 1
        refreshed = await repo.get_task(str(task["id"]))
        assert refreshed["status"] == "pending"

    # ── 9. update_request_status ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_request_status_cross_org_blocked(self, repo, req, other_org):
        """update_request_status with wrong org_id returns None."""
        result = await repo.update_request_status(
            str(req["id"]), "in_progress", org_id=str(other_org["id"])
        )
        assert result is None
        # Verify request is still pending (not mutated)
        original = await repo.get_request(
            str(req["id"]), str(req["organization_id"])
        )
        assert original["status"] == "pending"

    @pytest.mark.asyncio
    async def test_update_request_status_same_org_allowed(
        self, repo, req, test_organization
    ):
        """update_request_status with correct org_id succeeds."""
        result = await repo.update_request_status(
            str(req["id"]), "in_progress", org_id=str(test_organization["id"])
        )
        assert result is not None
        assert result["status"] == "in_progress"


# ── list_active_work tests ────────────────────────────────────────────────


class TestListActiveWork:
    """Tests for list_active_work() — complex aggregation of active requests + task counts."""

    @pytest.mark.asyncio
    async def test_empty_when_no_requests(self, repo, test_organization):
        org = str(test_organization["id"])
        result = await repo.list_active_work(org)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_pending_request(self, repo, req, test_organization):
        org = str(test_organization["id"])
        result = await repo.list_active_work(org)
        assert len(result) >= 1
        ids = [str(r["id"]) for r in result]
        assert str(req["id"]) in ids

    @pytest.mark.asyncio
    async def test_excludes_completed_request(self, repo, req, test_organization):
        org = str(test_organization["id"])
        await repo.update_request_status(str(req["id"]), "completed", org_id=org)
        result = await repo.list_active_work(org)
        ids = [str(r["id"]) for r in result]
        assert str(req["id"]) not in ids

    @pytest.mark.asyncio
    async def test_excludes_failed_request(self, repo, req, test_organization):
        org = str(test_organization["id"])
        await repo.update_request_status(str(req["id"]), "failed", org_id=org)
        result = await repo.list_active_work(org)
        ids = [str(r["id"]) for r in result]
        assert str(req["id"]) not in ids

    @pytest.mark.asyncio
    async def test_excludes_cancelled_request(self, repo, req, test_organization):
        org = str(test_organization["id"])
        await repo.update_request_status(str(req["id"]), "cancelled", org_id=org)
        result = await repo.list_active_work(org)
        ids = [str(r["id"]) for r in result]
        assert str(req["id"]) not in ids

    @pytest.mark.asyncio
    async def test_task_count_aggregation(self, repo, req, test_organization):
        """Verify that task status counts are correctly aggregated."""
        org = str(test_organization["id"])
        rid = str(req["id"])
        # Create 3 tasks in different states
        t1 = await repo.create_task(request_id=rid, title="Pending", org_id=org)
        t2 = await repo.create_task(request_id=rid, title="Running", org_id=org)
        t3 = await repo.create_task(request_id=rid, title="Done", org_id=org)
        # Move t2 to claimed
        await repo.claim_task(str(t2["id"]), "daemon-1")
        # Move t3 to completed
        await repo.claim_task(str(t3["id"]), "daemon-2")
        await repo.start_task(str(t3["id"]))
        await repo.complete_task(str(t3["id"]), result="done")

        result = await repo.list_active_work(org)
        row = next(r for r in result if str(r["id"]) == rid)
        assert row["tasks_pending"] >= 1
        assert row["tasks_running"] >= 1  # claimed counts as running
        assert row["tasks_completed"] >= 1
        assert row["tasks_total"] >= 3

    @pytest.mark.asyncio
    async def test_priority_ordering(self, repo, test_organization):
        """Urgent requests should appear before medium ones."""
        org = str(test_organization["id"])
        medium = await repo.create_request(
            title="Medium", org_id=org, priority="medium"
        )
        urgent = await repo.create_request(
            title="Urgent", org_id=org, priority="urgent"
        )
        result = await repo.list_active_work(org)
        ids = [str(r["id"]) for r in result]
        assert ids.index(str(urgent["id"])) < ids.index(str(medium["id"]))

    @pytest.mark.asyncio
    async def test_cross_org_isolation(self, repo, req, other_org):
        """list_active_work with wrong org_id returns no data from other org."""
        result = await repo.list_active_work(str(other_org["id"]))
        ids = [str(r["id"]) for r in result]
        assert str(req["id"]) not in ids
