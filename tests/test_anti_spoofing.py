"""Anti-spoofing integration tests for identity enforcement.

Threat Model Summary
====================

Audit Scope: All files in src/lucent/api/routers/, src/lucent/tools/,
and src/lucent/web/routes/.

Vectors Identified and Mitigated
---------------------------------

V1 - MCP create_memory username parameter:
    The MCP tool accepted a `username` parameter that set the display name on
    created memories. An attacker could attribute memories to arbitrary display
    names. Mitigated: authenticated user's display name always takes precedence
    over the caller-supplied `username` parameter.

V2 - API MemoryCreate username in request body:
    Same as V1 but via REST API POST /api/memories. The `username` field in the
    request body could override the display name. Mitigated: always derived from
    authenticated user context, request body value ignored.

V3 - MCP claim_task / release_claim missing org check:
    The claim_task and release_claim MCP tools operated on any memory_id without
    verifying the task belongs to the caller's organization. An attacker with a
    valid API key could claim tasks from another org if they knew the UUID.
    Mitigated: added get_accessible() check before claim/release operations.

V4 - Web add_member missing org validation:
    The web route for adding group members accepted any user_id UUID from form
    data without verifying the target user belongs to the same organization.
    Mitigated: added UserRepository lookup + organization_id check.

V5 - MCP search/filter username parameter (SAFE):
    Search functions accept `username` as a filter parameter. This is safe because
    `requesting_user_id` from auth context always controls access boundaries.
    The `username` filter only narrows results within the user's access scope.

V6 - MCP log_task_event / link_task_memory missing auth:
    These tools had no authentication check, allowing unauthenticated callers to
    log events and link memories to tasks. Mitigated: added auth context check.

Additional Findings (SAFE):
    - All API routers use AuthenticatedUser/AdminUser dependency injection
    - No custom X-User-Id or X-Username headers accepted anywhere
    - Organization isolation enforced consistently via requesting_user_id
    - Import always re-attributes memories to authenticated user
    - Daemon identity is from service account, not user input
    - Web routes use session cookies + CSRF, never form-based identity
    - Impersonation is cookie-based with role checks (owner/admin only)
"""

import json
from uuid import UUID, uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.auth import set_current_user
from lucent.db import (
    GroupRepository,
    MemoryRepository,
    OrganizationRepository,
    UserRepository,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def spoof_prefix(db_pool):
    """Create and clean up test data for anti-spoofing tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_spoof_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        # Clean up in dependency order
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
        await conn.execute(
            "DELETE FROM user_groups WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM user_groups WHERE group_id IN "
            "(SELECT id FROM groups WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM groups WHERE name LIKE $1",
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
async def org_a(db_pool, spoof_prefix):
    """Organization A."""
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{spoof_prefix}org_a")


@pytest_asyncio.fixture
async def org_b(db_pool, spoof_prefix):
    """Organization B (separate org for cross-org tests)."""
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{spoof_prefix}org_b")


@pytest_asyncio.fixture
async def user_a(db_pool, org_a, spoof_prefix):
    """User A - regular member in org A."""
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{spoof_prefix}user_a",
        provider="local",
        organization_id=org_a["id"],
        email=f"{spoof_prefix}usera@test.com",
        display_name=f"{spoof_prefix}UserA",
    )


@pytest_asyncio.fixture
async def user_b(db_pool, org_a, spoof_prefix):
    """User B - regular member in org A (same org as user A)."""
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{spoof_prefix}user_b",
        provider="local",
        organization_id=org_a["id"],
        email=f"{spoof_prefix}userb@test.com",
        display_name=f"{spoof_prefix}UserB",
    )


@pytest_asyncio.fixture
async def user_c(db_pool, org_b, spoof_prefix):
    """User C - member in org B (different org)."""
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{spoof_prefix}user_c",
        provider="local",
        organization_id=org_b["id"],
        email=f"{spoof_prefix}userc@test.com",
        display_name=f"{spoof_prefix}UserC",
    )


@pytest_asyncio.fixture
async def admin_a(db_pool, org_a, spoof_prefix):
    """Admin user in org A."""
    repo = UserRepository(db_pool)
    user = await repo.create(
        external_id=f"{spoof_prefix}admin_a",
        provider="local",
        organization_id=org_a["id"],
        email=f"{spoof_prefix}admin@test.com",
        display_name=f"{spoof_prefix}AdminA",
    )
    await repo.update_role(user["id"], "admin")
    user["role"] = "admin"
    return user


def _make_current_user(user, role="member", **kwargs):
    """Create a CurrentUser dependency from a user dict."""
    return CurrentUser(
        id=user["id"],
        organization_id=user.get("organization_id"),
        role=user.get("role", role),
        email=user.get("email"),
        display_name=user.get("display_name"),
        **kwargs,
    )


def _make_client(app, user, role="member", **kwargs):
    """Create an authenticated async client for a user."""
    fake = _make_current_user(user, role=role, **kwargs)

    async def override():
        return fake

    app.dependency_overrides[get_current_user] = override
    return httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


# ============================================================================
# V1/V2: Username spoofing in create_memory
# ============================================================================


class TestMemoryUsernameSpoofing:
    """V1/V2: Caller-supplied username must not override authenticated identity."""

    async def test_api_create_memory_ignores_username_in_body(
        self, db_pool, user_a, user_b, spoof_prefix
    ):
        """POST /api/memories with a different username in the body should
        attribute the memory to the authenticated user, not the body value."""
        app = create_app()
        async with _make_client(app, user_a) as client:
            resp = await client.post(
                "/api/memories",
                json={
                    "type": "experience",
                    "content": f"{spoof_prefix}Spoofed memory",
                    "username": user_b["display_name"],  # Trying to impersonate User B
                    "tags": ["test"],
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        # Memory should be attributed to User A's display name, not User B's
        assert data["username"] == user_a["display_name"]
        assert data["user_id"] == str(user_a["id"])

        # Clean up
        repo = MemoryRepository(db_pool)
        await repo.delete(UUID(data["id"]))

    async def test_mcp_create_memory_ignores_username_param(
        self, db_pool, user_a, user_b, spoof_prefix
    ):
        """MCP create_memory with a different username should use the
        authenticated user's name, not the parameter value."""
        from mcp.server.fastmcp import FastMCP

        from lucent.tools.memories import register_tools

        mcp = FastMCP("test")
        register_tools(mcp)

        # Set auth context to user_a
        set_current_user(
            {
                "id": user_a["id"],
                "organization_id": user_a["organization_id"],
                "role": "member",
                "display_name": user_a["display_name"],
                "email": user_a["email"],
            }
        )
        try:
            result = await mcp._tool_manager.call_tool(
                "create_memory",
                {
                    "type": "experience",
                    "content": f"{spoof_prefix}MCP spoofed memory",
                    "username": user_b["display_name"],  # Trying to impersonate
                    "tags": ["test"],
                },
            )
            data = json.loads(result)
            assert "error" not in data
            # Username should be User A's, not User B's
            assert data["username"] == user_a["display_name"]

            # Clean up
            repo = MemoryRepository(db_pool)
            await repo.delete(UUID(data["id"]))
        finally:
            set_current_user(None)


# ============================================================================
# V2: Cross-user memory operations
# ============================================================================


class TestCrossUserMemoryOps:
    """User A trying to modify/delete User B's memories."""

    async def test_user_a_cannot_delete_user_b_memory(
        self, db_pool, user_a, user_b, spoof_prefix
    ):
        """User A should not be able to delete User B's memory."""
        # Create a memory owned by User B
        repo = MemoryRepository(db_pool)
        mem = await repo.create(
            username=f"{spoof_prefix}userb",
            type="experience",
            content=f"{spoof_prefix}User B private memory",
            tags=["test"],
            importance=5,
            user_id=user_b["id"],
            organization_id=user_b["organization_id"],
        )

        # User A tries to delete it
        app = create_app()
        async with _make_client(app, user_a) as client:
            resp = await client.delete(f"/api/memories/{mem['id']}")

        # Should be 403 or 404 (not leaking existence)
        assert resp.status_code in (403, 404)

        # Verify memory still exists
        existing = await repo.get(mem["id"])
        assert existing is not None
        assert existing.get("deleted_at") is None

        # Clean up
        await repo.delete(mem["id"])

    async def test_user_a_cannot_update_user_b_memory(
        self, db_pool, user_a, user_b, spoof_prefix
    ):
        """User A should not be able to update User B's memory."""
        repo = MemoryRepository(db_pool)
        mem = await repo.create(
            username=f"{spoof_prefix}userb",
            type="experience",
            content=f"{spoof_prefix}User B memory",
            tags=["test"],
            importance=5,
            user_id=user_b["id"],
            organization_id=user_b["organization_id"],
            shared=True,  # Shared, so A can see it but not modify
        )

        app = create_app()
        async with _make_client(app, user_a) as client:
            resp = await client.patch(
                f"/api/memories/{mem['id']}",
                json={"content": "Hijacked by User A"},
            )

        assert resp.status_code == 403

        # Verify content unchanged
        existing = await repo.get(mem["id"])
        assert existing["content"] == f"{spoof_prefix}User B memory"

        await repo.delete(mem["id"])


# ============================================================================
# V3: Cross-org task operations
# ============================================================================


class TestCrossOrgTaskClaim:
    """V3: MCP claim_task must verify org membership."""

    async def test_claim_task_rejects_cross_org(
        self, db_pool, user_a, user_c, spoof_prefix
    ):
        """User C (org B) should not be able to claim a task owned by User A (org A)."""
        # Create a daemon task as User A
        repo = MemoryRepository(db_pool)
        task = await repo.create(
            username=f"{spoof_prefix}usera",
            type="technical",
            content=f"{spoof_prefix}Task for org A",
            tags=["daemon-task", "daemon", "pending", "code", "medium"],
            importance=5,
            user_id=user_a["id"],
            organization_id=user_a["organization_id"],
        )

        from mcp.server.fastmcp import FastMCP

        from lucent.tools.memories import register_tools

        mcp = FastMCP("test")
        register_tools(mcp)

        # Set auth context to User C (different org)
        set_current_user(
            {
                "id": user_c["id"],
                "organization_id": user_c["organization_id"],
                "role": "member",
                "display_name": user_c["display_name"],
                "email": user_c["email"],
            }
        )
        try:
            result = await mcp._tool_manager.call_tool(
                "claim_task",
                {"memory_id": str(task["id"]), "instance_id": "attacker-instance"},
            )
            data = json.loads(result)
            # Should fail — task is in org A, user C is in org B
            assert "error" in data
            assert "not found" in data["error"].lower() or "not accessible" in data["error"].lower()
        finally:
            set_current_user(None)
            await repo.delete(task["id"])

    async def test_release_claim_rejects_cross_org(
        self, db_pool, user_a, user_c, spoof_prefix
    ):
        """User C (org B) should not be able to release a claimed task from org A."""
        repo = MemoryRepository(db_pool)
        task = await repo.create(
            username=f"{spoof_prefix}usera",
            type="technical",
            content=f"{spoof_prefix}Claimed task for org A",
            tags=["daemon-task", "daemon", "pending", "code", "medium"],
            importance=5,
            user_id=user_a["id"],
            organization_id=user_a["organization_id"],
        )
        # Claim it legitimately
        claimed = await repo.claim_task(task["id"], "legitimate-instance")
        assert claimed is not None

        from mcp.server.fastmcp import FastMCP

        from lucent.tools.memories import register_tools

        mcp = FastMCP("test")
        register_tools(mcp)

        # Try to release as User C (different org)
        set_current_user(
            {
                "id": user_c["id"],
                "organization_id": user_c["organization_id"],
                "role": "member",
                "display_name": user_c["display_name"],
                "email": user_c["email"],
            }
        )
        try:
            result = await mcp._tool_manager.call_tool(
                "release_claim",
                {"memory_id": str(task["id"]), "instance_id": "legitimate-instance"},
            )
            data = json.loads(result)
            assert "error" in data
        finally:
            set_current_user(None)
            await repo.delete(task["id"])


# ============================================================================
# X-User-Id header spoofing
# ============================================================================


class TestHeaderSpoofing:
    """Verify that custom identity headers are ignored by the API."""

    async def test_x_user_id_header_ignored(self, db_pool, user_a, user_b, spoof_prefix):
        """Sending X-User-Id header should not change the authenticated identity."""
        app = create_app()
        async with _make_client(app, user_a) as client:
            resp = await client.post(
                "/api/memories",
                headers={
                    "X-User-Id": str(user_b["id"]),
                    "X-Username": user_b["display_name"],
                },
                json={
                    "type": "experience",
                    "content": f"{spoof_prefix}Header spoof attempt",
                    "tags": ["test"],
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        # Memory should be attributed to User A regardless of headers
        assert data["user_id"] == str(user_a["id"])
        assert data["username"] == user_a["display_name"]

        repo = MemoryRepository(db_pool)
        await repo.delete(UUID(data["id"]))

    async def test_x_user_id_does_not_affect_search(
        self, db_pool, user_a, user_b, spoof_prefix
    ):
        """X-User-Id header should not affect search access boundaries."""
        # Create a private memory for User B
        repo = MemoryRepository(db_pool)
        mem = await repo.create(
            username=f"{spoof_prefix}userb",
            type="experience",
            content=f"{spoof_prefix}Private to B",
            tags=["test", "secret"],
            importance=5,
            user_id=user_b["id"],
            organization_id=user_b["organization_id"],
            shared=False,
        )

        app = create_app()
        async with _make_client(app, user_a) as client:
            resp = await client.post(
                "/api/search",
                headers={"X-User-Id": str(user_b["id"])},
                json={
                    "query": f"{spoof_prefix}Private to B",
                    "limit": 10,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        # User A should NOT see User B's private memory despite the header
        found_ids = [m["id"] for m in data["memories"]]
        assert str(mem["id"]) not in found_ids

        await repo.delete(mem["id"])


# ============================================================================
# V4: Web add_member cross-org validation
# ============================================================================


class TestGroupMemberOrgValidation:
    """V4: Group add_member must validate target user is in same org."""

    async def test_api_add_member_rejects_cross_org_user(
        self, db_pool, user_a, user_c, admin_a, spoof_prefix
    ):
        """Adding a user from a different org to a group should fail."""
        pool = db_pool
        group_repo = GroupRepository(pool)
        org_id = str(admin_a["organization_id"])

        group = await group_repo.create_group(
            name=f"{spoof_prefix}test_group",
            org_id=org_id,
            created_by=str(admin_a["id"]),
        )

        app = create_app()
        async with _make_client(app, admin_a, role="admin") as client:
            resp = await client.post(
                f"/api/groups/{group['id']}/members",
                json={"user_id": str(user_c["id"]), "role": "member"},
            )
        # User C is in org B, should be rejected
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

        await group_repo.delete_group(str(group["id"]), org_id)


# ============================================================================
# V6: MCP tools requiring auth
# ============================================================================


class TestMCPToolsRequireAuth:
    """V6: MCP tools that previously lacked auth checks."""

    async def test_log_task_event_requires_auth(self, db_pool):
        """log_task_event should fail without authentication."""
        from mcp.server.fastmcp import FastMCP

        from lucent.tools.requests import register_request_tools

        mcp = FastMCP("test")
        register_request_tools(mcp)

        # No auth context set
        set_current_user(None)
        result = await mcp._tool_manager.call_tool(
            "log_task_event",
            {"task_id": str(uuid4()), "event_type": "progress", "detail": "test"},
        )
        data = json.loads(result)
        assert "error" in data
        assert "authentication" in data["error"].lower()

    async def test_link_task_memory_requires_auth(self, db_pool):
        """link_task_memory should fail without authentication."""
        from mcp.server.fastmcp import FastMCP

        from lucent.tools.requests import register_request_tools

        mcp = FastMCP("test")
        register_request_tools(mcp)

        set_current_user(None)
        result = await mcp._tool_manager.call_tool(
            "link_task_memory",
            {"task_id": str(uuid4()), "memory_id": str(uuid4())},
        )
        data = json.loads(result)
        assert "error" in data
        assert "authentication" in data["error"].lower()


# ============================================================================
# Admin impersonation (legitimate path)
# ============================================================================


class TestAdminImpersonation:
    """Admin performing legitimate impersonation with audit trail."""

    async def test_impersonated_user_creates_memory_with_audit(
        self, db_pool, admin_a, user_b, spoof_prefix
    ):
        """When admin impersonates User B, memory is attributed to User B
        but audit context includes the impersonator."""
        app = create_app()
        impersonated = _make_current_user(
            user_b,
            role="member",
            impersonator_id=admin_a["id"],
            impersonator_display_name=admin_a["display_name"],
        )

        async def override():
            return impersonated

        app.dependency_overrides[get_current_user] = override
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/memories",
                json={
                    "type": "experience",
                    "content": f"{spoof_prefix}Impersonated memory",
                    "tags": ["test"],
                },
            )
        app.dependency_overrides.clear()

        assert resp.status_code == 201
        data = resp.json()
        # Memory attributed to User B (the impersonated user)
        assert data["user_id"] == str(user_b["id"])

        # Verify audit trail records the impersonator
        from lucent.db import AuditRepository

        audit_repo = AuditRepository(db_pool)
        result = await audit_repo.get_by_memory_id(UUID(data["id"]))
        logs = result["entries"]
        assert len(logs) > 0
        audit_entry = logs[0]
        ctx = audit_entry.get("context") or {}
        assert ctx.get("impersonator_id") == str(admin_a["id"])
        assert ctx.get("is_impersonated") is True

        repo = MemoryRepository(db_pool)
        await repo.delete(UUID(data["id"]))


# ============================================================================
# Cross-user daemon task operations via API
# ============================================================================


class TestDaemonTaskAntiSpoofing:
    """Daemon task API endpoints enforce ownership."""

    async def test_user_cannot_cancel_other_users_task(
        self, db_pool, user_a, user_b, spoof_prefix
    ):
        """User A should not be able to cancel User B's daemon task."""
        repo = MemoryRepository(db_pool)
        task = await repo.create(
            username=f"{spoof_prefix}userb",
            type="technical",
            content=f"{spoof_prefix}User B daemon task",
            tags=["daemon-task", "daemon", "pending", "code", "medium"],
            importance=5,
            user_id=user_b["id"],
            organization_id=user_b["organization_id"],
        )

        app = create_app()
        # Authenticate as User A with daemon-tasks scope
        async with _make_client(
            app, user_a, api_key_scopes=["read", "write", "daemon-tasks"]
        ) as client:
            resp = await client.delete(f"/api/daemon/tasks/{task['id']}")

        # Should be 403 (ownership check) or 404
        assert resp.status_code in (403, 404)

        # Verify task still exists
        existing = await repo.get(task["id"])
        assert existing is not None
        assert existing.get("deleted_at") is None

        await repo.delete(task["id"])
