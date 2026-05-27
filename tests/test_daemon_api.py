"""API endpoint tests for daemon task and message routes.

Tests the FastAPI HTTP layer for:
- /api/daemon/tasks  (CRUD, claiming, status transitions)
- /api/daemon/messages  (send, list, acknowledge, threading)

Existing test_coordination.py and test_feedback_review.py cover the DB/repository
layer; these tests verify the HTTP endpoints, serialization, and auth gating.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import MemoryRepository, OrganizationRepository, UserRepository

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def api_prefix(db_pool):
    """Create and clean up test data for API tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_api_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memory_audit_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memory_access_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM memories WHERE username LIKE $1", f"{prefix}%")
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def api_user(db_pool, api_prefix):
    """Create a test user for API tests."""
    from lucent.db import OrganizationRepository, UserRepository

    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{api_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{api_prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{api_prefix}user@test.com",
        display_name=f"{api_prefix}User",
    )
    return user


@pytest_asyncio.fixture
async def client(db_pool, api_user):
    """Create an httpx AsyncClient with auth dependency overridden."""
    app = create_app()

    fake_user = CurrentUser(
        id=api_user["id"],
        organization_id=api_user["organization_id"],
        role=api_user.get("role", "member"),
        email=api_user.get("email"),
        display_name=api_user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["daemon-tasks", "read", "write"],
    )

    async def override_get_current_user():
        return fake_user

    app.dependency_overrides[get_current_user] = override_get_current_user

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()


# ============================================================================
# Daemon Task Endpoint Tests
# ============================================================================


class TestDaemonTaskCreate:
    """POST /api/daemon/tasks"""

    async def test_create_task(self, client, api_prefix):
        resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Review auth module",
                "agent_type": "code",
                "priority": "high",
                "context": "Focus on token refresh logic",
                "tags": ["auth"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["description"] == f"{api_prefix}Review auth module"
        assert data["agent_type"] == "code"
        assert data["priority"] == "high"
        assert data["status"] == "pending"
        assert "auth" in data["tags"]
        assert data["id"] is not None

    async def test_create_task_defaults(self, client, api_prefix):
        """Defaults to agent_type=code, priority=medium."""
        resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Default task",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["agent_type"] == "code"
        assert data["priority"] == "medium"

    async def test_create_task_invalid_agent_type(self, client, api_prefix):
        resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Bad agent",
                "agent_type": "invalid_type",
            },
        )
        assert resp.status_code == 400
        assert "agent_type" in resp.json()["detail"].lower()

    async def test_create_task_invalid_priority(self, client, api_prefix):
        resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Bad priority",
                "priority": "urgent",
            },
        )
        assert resp.status_code == 400
        assert "priority" in resp.json()["detail"].lower()

    async def test_create_task_empty_description(self, client):
        resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": "",
            },
        )
        assert resp.status_code == 422  # pydantic validation


class TestDaemonTaskList:
    """GET /api/daemon/tasks"""

    async def test_list_tasks(self, client, api_prefix):
        # Create two tasks
        await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Task A",
            },
        )
        await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Task B",
            },
        )

        resp = await client.get("/api/daemon/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert "tasks" in data
        assert "total_count" in data
        descs = [t["description"] for t in data["tasks"]]
        assert f"{api_prefix}Task A" in descs
        assert f"{api_prefix}Task B" in descs

    async def test_list_tasks_filter_pending(self, client, api_prefix):
        await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Pending one",
            },
        )
        resp = await client.get("/api/daemon/tasks", params={"status": "pending"})
        assert resp.status_code == 200
        tasks = resp.json()["tasks"]
        for t in tasks:
            assert t["status"] == "pending"

    async def test_list_tasks_limit(self, client, api_prefix):
        for i in range(5):
            await client.post(
                "/api/daemon/tasks",
                json={
                    "description": f"{api_prefix}Lim task {i}",
                },
            )
        resp = await client.get("/api/daemon/tasks", params={"limit": 2})
        assert resp.status_code == 200
        assert len(resp.json()["tasks"]) <= 2


class TestDaemonTaskGetById:
    """GET /api/daemon/tasks/{task_id}"""

    async def test_get_task(self, client, api_prefix):
        create_resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Get me",
            },
        )
        task_id = create_resp.json()["id"]

        resp = await client.get(f"/api/daemon/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == task_id
        assert resp.json()["description"] == f"{api_prefix}Get me"

    async def test_get_task_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.get(f"/api/daemon/tasks/{fake_id}")
        assert resp.status_code == 404


class TestDaemonTaskResult:
    """GET /api/daemon/tasks/{task_id}/result"""

    async def test_get_result_pending_returns_202(self, client, api_prefix):
        create_resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Not done yet",
            },
        )
        task_id = create_resp.json()["id"]

        resp = await client.get(f"/api/daemon/tasks/{task_id}/result")
        assert resp.status_code == 202
        assert resp.json()["status"] == "pending"

    async def test_get_result_completed_returns_200(self, client, db_pool, api_user, api_prefix):
        """Complete a task via DB then check the result endpoint returns 200."""
        create_resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Will complete",
            },
        )
        task_id = create_resp.json()["id"]

        # Complete the task directly via the repository
        repo = MemoryRepository(db_pool)
        memory = await repo.get(UUID(task_id))
        new_tags = [t for t in memory["tags"] if t != "pending"]
        new_tags.append("completed")
        meta = dict(memory.get("metadata") or {})
        meta["result"] = "Found 3 patterns."
        await repo.update(memory_id=UUID(task_id), tags=new_tags, metadata=meta)

        resp = await client.get(f"/api/daemon/tasks/{task_id}/result")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
        assert resp.json()["result"] == "Found 3 patterns."

    async def test_get_result_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.get(f"/api/daemon/tasks/{fake_id}/result")
        assert resp.status_code == 404


class TestDaemonTaskCancel:
    """DELETE /api/daemon/tasks/{task_id}"""

    async def test_cancel_pending_task(self, client, api_prefix):
        create_resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Cancel me",
            },
        )
        task_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/daemon/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # Verify it's gone
        get_resp = await client.get(f"/api/daemon/tasks/{task_id}")
        assert get_resp.status_code == 404

    async def test_cancel_nonpending_task_fails(self, client, db_pool, api_prefix):
        """Cannot cancel a task that is no longer pending (e.g. claimed)."""
        create_resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Already claimed",
            },
        )
        task_id = create_resp.json()["id"]

        # Claim the task via DB
        repo = MemoryRepository(db_pool)
        await repo.claim_task(UUID(task_id), "test-instance")

        resp = await client.delete(f"/api/daemon/tasks/{task_id}")
        assert resp.status_code == 400
        assert "pending" in resp.json()["detail"].lower()

    async def test_cancel_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.delete(f"/api/daemon/tasks/{fake_id}")
        assert resp.status_code == 404


# ============================================================================
# Daemon Message Endpoint Tests
# ============================================================================


class TestDaemonMessageSend:
    """POST /api/daemon/messages"""

    async def test_send_message(self, client, api_prefix):
        resp = await client.post(
            "/api/daemon/messages",
            json={
                "content": f"{api_prefix}Hello from daemon",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["content"] == f"{api_prefix}Hello from daemon"
        assert data["sender"] == "daemon"
        assert data["acknowledged"] is False
        assert data["id"] is not None

    async def test_send_message_with_reply(self, client, api_prefix):
        # Create a first message
        first = await client.post(
            "/api/daemon/messages",
            json={
                "content": f"{api_prefix}Original message",
            },
        )
        first_id = first.json()["id"]

        # Reply to it
        resp = await client.post(
            "/api/daemon/messages",
            json={
                "content": f"{api_prefix}Reply message",
                "in_reply_to": first_id,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["in_reply_to"] == first_id

    async def test_send_message_empty_content(self, client):
        resp = await client.post(
            "/api/daemon/messages",
            json={
                "content": "",
            },
        )
        assert resp.status_code == 422  # pydantic validation


class TestDaemonMessageList:
    """GET /api/daemon/messages"""

    async def test_list_messages(self, client, api_prefix):
        await client.post(
            "/api/daemon/messages",
            json={
                "content": f"{api_prefix}Msg 1",
            },
        )
        await client.post(
            "/api/daemon/messages",
            json={
                "content": f"{api_prefix}Msg 2",
            },
        )

        resp = await client.get("/api/daemon/messages")
        assert resp.status_code == 200
        data = resp.json()
        assert "messages" in data
        assert "total_count" in data
        contents = [m["content"] for m in data["messages"]]
        assert f"{api_prefix}Msg 1" in contents
        assert f"{api_prefix}Msg 2" in contents

    async def test_list_pending_only(self, client, db_pool, api_user, api_prefix):
        """pending_only=true returns only unacknowledged human messages."""
        repo = MemoryRepository(db_pool)

        # Create a pending human message directly (the API only creates daemon messages)
        await repo.create(
            username=f"{api_prefix}user",
            type="experience",
            content=f"{api_prefix}Human pending",
            tags=["daemon-message", "daemon", "from-human", "pending"],
            importance=5,
            user_id=api_user["id"],
            organization_id=api_user["organization_id"],
        )
        # Create a daemon message via API
        await client.post(
            "/api/daemon/messages",
            json={
                "content": f"{api_prefix}Daemon msg",
            },
        )

        resp = await client.get("/api/daemon/messages", params={"pending_only": "true"})
        assert resp.status_code == 200
        messages = resp.json()["messages"]
        contents = [m["content"] for m in messages]
        assert f"{api_prefix}Human pending" in contents
        # Daemon messages should not appear in pending-only
        assert f"{api_prefix}Daemon msg" not in contents

    async def test_list_messages_limit(self, client, api_prefix):
        for i in range(5):
            await client.post(
                "/api/daemon/messages",
                json={
                    "content": f"{api_prefix}Lim msg {i}",
                },
            )
        resp = await client.get("/api/daemon/messages", params={"limit": 2})
        assert resp.status_code == 200
        assert len(resp.json()["messages"]) <= 2


class TestDaemonMessageAcknowledge:
    """POST /api/daemon/messages/{message_id}/acknowledge"""

    async def test_acknowledge_message(self, client, db_pool, api_user, api_prefix):
        """Acknowledge a pending human message."""
        repo = MemoryRepository(db_pool)

        # Create a human pending message directly
        msg = await repo.create(
            username=f"{api_prefix}user",
            type="experience",
            content=f"{api_prefix}Ack me",
            tags=["daemon-message", "daemon", "from-human", "pending"],
            importance=5,
            user_id=api_user["id"],
            organization_id=api_user["organization_id"],
        )

        resp = await client.post(f"/api/daemon/messages/{msg['id']}/acknowledge")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # Verify the message is now acknowledged
        updated = await repo.get(msg["id"])
        assert "acknowledged" in updated["tags"]
        assert "pending" not in updated["tags"]
        assert "acknowledged_at" in (updated.get("metadata") or {})

    async def test_acknowledge_not_found(self, client):
        fake_id = str(uuid4())
        resp = await client.post(f"/api/daemon/messages/{fake_id}/acknowledge")
        assert resp.status_code == 404

    async def test_acknowledge_daemon_message(self, client, api_prefix):
        """Acknowledging a daemon message (no pending tag) still succeeds."""
        create_resp = await client.post(
            "/api/daemon/messages",
            json={
                "content": f"{api_prefix}Daemon says hi",
            },
        )
        msg_id = create_resp.json()["id"]

        resp = await client.post(f"/api/daemon/messages/{msg_id}/acknowledge")
        assert resp.status_code == 200

    async def test_acknowledge_idempotent(self, client, db_pool, api_user, api_prefix):
        """Acknowledging an already-acknowledged message succeeds."""
        repo = MemoryRepository(db_pool)
        msg = await repo.create(
            username=f"{api_prefix}user",
            type="experience",
            content=f"{api_prefix}Ack twice",
            tags=["daemon-message", "daemon", "from-human", "pending"],
            importance=5,
            user_id=api_user["id"],
            organization_id=api_user["organization_id"],
        )

        resp1 = await client.post(f"/api/daemon/messages/{msg['id']}/acknowledge")
        assert resp1.status_code == 200
        resp2 = await client.post(f"/api/daemon/messages/{msg['id']}/acknowledge")
        assert resp2.status_code == 200

        updated = await repo.get(msg["id"])
        assert updated["tags"].count("acknowledged") == 1


# ============================================================================
# Authorization / Scope Gating Tests
# ============================================================================


class TestDaemonAuthorizationScopes:
    """Verify endpoints reject requests missing 'daemon-tasks' scope."""

    @pytest_asyncio.fixture
    async def no_scope_client(self, db_pool, api_user):
        """Client whose API key lacks the daemon-tasks scope."""
        app = create_app()

        fake_user = CurrentUser(
            id=api_user["id"],
            organization_id=api_user["organization_id"],
            role=api_user.get("role", "member"),
            email=api_user.get("email"),
            display_name=api_user.get("display_name"),
            auth_method="api_key",
            api_key_scopes=["read"],  # no daemon-tasks scope
        )

        async def override():
            return fake_user

        app.dependency_overrides[get_current_user] = override
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
        app.dependency_overrides.clear()

    async def test_create_task_requires_scope(self, no_scope_client):
        resp = await no_scope_client.post(
            "/api/daemon/tasks",
            json={
                "description": "Should be rejected",
            },
        )
        assert resp.status_code == 403

    async def test_list_tasks_requires_scope(self, no_scope_client):
        resp = await no_scope_client.get("/api/daemon/tasks")
        assert resp.status_code == 403

    async def test_send_message_requires_scope(self, no_scope_client):
        resp = await no_scope_client.post(
            "/api/daemon/messages",
            json={
                "content": "Should be rejected",
            },
        )
        assert resp.status_code == 403

    async def test_list_messages_requires_scope(self, no_scope_client):
        resp = await no_scope_client.get("/api/daemon/messages")
        assert resp.status_code == 403


# ============================================================================
# Task List Filtering Tests
# ============================================================================


class TestDaemonTaskListFiltering:
    """Additional filtering scenarios for GET /api/daemon/tasks."""

    async def test_list_tasks_filter_completed(self, client, db_pool, api_user, api_prefix):
        """Filter tasks by status=completed."""
        create_resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Will be completed",
            },
        )
        task_id = create_resp.json()["id"]

        # Mark completed via repo
        repo = MemoryRepository(db_pool)
        memory = await repo.get(UUID(task_id))
        new_tags = [t for t in memory["tags"] if t != "pending"]
        new_tags.append("completed")
        await repo.update(memory_id=UUID(task_id), tags=new_tags)

        resp = await client.get("/api/daemon/tasks", params={"status": "completed"})
        assert resp.status_code == 200
        tasks = resp.json()["tasks"]
        for t in tasks:
            assert t["status"] == "completed"
        ids = [t["id"] for t in tasks]
        assert task_id in ids

    async def test_list_tasks_filter_claimed(self, client, db_pool, api_prefix):
        """Filter tasks by status=claimed."""
        create_resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Will be claimed",
            },
        )
        task_id = create_resp.json()["id"]

        # Claim via repo
        repo = MemoryRepository(db_pool)
        await repo.claim_task(UUID(task_id), "test-instance-xyz")

        resp = await client.get("/api/daemon/tasks", params={"status": "claimed"})
        assert resp.status_code == 200
        tasks = resp.json()["tasks"]
        claimed_ids = [t["id"] for t in tasks]
        assert task_id in claimed_ids
        for t in tasks:
            assert t["status"] == "claimed"

    async def test_list_tasks_since_filter(self, client, db_pool, api_user, api_prefix):
        """The `since` parameter filters to tasks updated after the timestamp."""
        # Create a task
        create_resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Since filter task",
            },
        )
        task_id = create_resp.json()["id"]

        # Use a future timestamp — nothing should be returned
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        resp = await client.get("/api/daemon/tasks", params={"since": future})
        assert resp.status_code == 200
        ids = [t["id"] for t in resp.json()["tasks"]]
        assert task_id not in ids

        # Use a past timestamp — our task should appear
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        resp = await client.get("/api/daemon/tasks", params={"since": past})
        assert resp.status_code == 200
        ids = [t["id"] for t in resp.json()["tasks"]]
        assert task_id in ids


# ============================================================================
# Cross-User Isolation Tests
# ============================================================================


class TestCrossUserIsolation:
    """Verify user A cannot access user B's tasks or messages."""

    @pytest_asyncio.fixture
    async def other_client(self, db_pool, api_prefix):
        """A second user/client for cross-user tests."""
        org_repo = OrganizationRepository(db_pool)
        org = await org_repo.create(name=f"{api_prefix}other_org")
        user_repo = UserRepository(db_pool)
        other_user = await user_repo.create(
            external_id=f"{api_prefix}other_user",
            provider="local",
            organization_id=org["id"],
            email=f"{api_prefix}other@test.com",
            display_name=f"{api_prefix}OtherUser",
        )

        app = create_app()
        fake_user = CurrentUser(
            id=other_user["id"],
            organization_id=other_user["organization_id"],
            role="member",
            email=other_user.get("email"),
            display_name=other_user.get("display_name"),
            auth_method="api_key",
            api_key_scopes=["daemon-tasks", "read", "write"],
        )

        async def override():
            return fake_user

        app.dependency_overrides[get_current_user] = override
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
        app.dependency_overrides.clear()

    async def test_other_user_cannot_see_task(self, client, other_client, api_prefix):
        """User B cannot retrieve user A's task by ID."""
        create_resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}Private task",
            },
        )
        task_id = create_resp.json()["id"]

        resp = await other_client.get(f"/api/daemon/tasks/{task_id}")
        assert resp.status_code == 404

    async def test_other_user_cannot_cancel_task(self, client, other_client, api_prefix):
        """User B cannot cancel user A's task."""
        create_resp = await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}No cancel for you",
            },
        )
        task_id = create_resp.json()["id"]

        resp = await other_client.delete(f"/api/daemon/tasks/{task_id}")
        assert resp.status_code in (403, 404)

    async def test_other_user_tasks_not_in_list(self, client, other_client, api_prefix):
        """User B's task list does not include user A's tasks."""
        await client.post(
            "/api/daemon/tasks",
            json={
                "description": f"{api_prefix}User A task",
            },
        )

        resp = await other_client.get("/api/daemon/tasks")
        assert resp.status_code == 200
        descs = [t["description"] for t in resp.json()["tasks"]]
        assert f"{api_prefix}User A task" not in descs
