"""Per-user cognitive planning fan-out tests.

Covers:
- Two users with private goals get separate requests
- created_by is set to the owning user, not the daemon-service user
- Requests require user approval (pending_approval, not auto-approved)
- Scoped key isolation: user A cannot see user B's private memories
- Users with no active goals get no fan-out iteration
- Error isolation: one user's failure doesn't block another
"""

import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from lucent.auth import set_current_user
from lucent.db import MemoryRepository, OrganizationRepository, UserRepository
from lucent.db.requests import APPROVAL_AUTO, APPROVAL_PENDING, RequestRepository

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def prefix(db_pool):
    """Unique prefix + comprehensive cleanup for per-user fan-out tests."""
    test_id = str(uuid4())[:8]
    pfx = f"test_fanout_{test_id}_"
    yield pfx
    async with db_pool.acquire() as conn:
        # Clean in FK-safe order
        user_ids = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id FROM users WHERE external_id LIKE $1", f"{pfx}%"
            )
        ]
        org_ids = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id FROM organizations WHERE name LIKE $1", f"{pfx}%"
            )
        ]
        if org_ids:
            await conn.execute(
                "DELETE FROM tasks WHERE request_id IN "
                "(SELECT id FROM requests WHERE organization_id = ANY($1))",
                org_ids,
            )
            await conn.execute(
                "DELETE FROM request_memories WHERE request_id IN "
                "(SELECT id FROM requests WHERE organization_id = ANY($1))",
                org_ids,
            )
            await conn.execute(
                "DELETE FROM requests WHERE organization_id = ANY($1)", org_ids
            )
        if user_ids:
            await conn.execute(
                "DELETE FROM memory_audit_log WHERE memory_id IN "
                "(SELECT id FROM memories WHERE user_id = ANY($1))",
                user_ids,
            )
            await conn.execute(
                "DELETE FROM memory_access_log WHERE memory_id IN "
                "(SELECT id FROM memories WHERE user_id = ANY($1))",
                user_ids,
            )
            await conn.execute(
                "DELETE FROM memories WHERE user_id = ANY($1)", user_ids
            )
            await conn.execute(
                "DELETE FROM api_keys WHERE user_id = ANY($1)", user_ids
            )
            await conn.execute(
                "DELETE FROM user_groups WHERE user_id = ANY($1)", user_ids
            )
        await conn.execute(
            "DELETE FROM users WHERE external_id LIKE $1", f"{pfx}%"
        )
        await conn.execute(
            "DELETE FROM organizations WHERE name LIKE $1", f"{pfx}%"
        )


@pytest_asyncio.fixture
async def org(db_pool, prefix):
    """Create a test organization."""
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{prefix}org")


@pytest_asyncio.fixture
async def user_a(db_pool, org, prefix):
    """Create user A (member)."""
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{prefix}user_a",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}user_a@test.com",
        display_name="User A",
        role="member",
    )


@pytest_asyncio.fixture
async def user_b(db_pool, org, prefix):
    """Create user B (member)."""
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{prefix}user_b",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}user_b@test.com",
        display_name="User B",
        role="member",
    )


@pytest_asyncio.fixture
async def daemon_user(db_pool, org, prefix):
    """Create a daemon-service user (simulating the real one)."""
    repo = UserRepository(db_pool)
    return await repo.create(
        external_id=f"{prefix}daemon-service",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}daemon@test.com",
        display_name="Daemon Service",
        role="member",
    )


@pytest_asyncio.fixture
async def mem_repo(db_pool):
    return MemoryRepository(db_pool)


@pytest_asyncio.fixture
async def req_repo(db_pool):
    return RequestRepository(db_pool)


async def _create_active_goal(
    mem_repo, user, org, prefix, title="Test Goal", metadata=None
):
    """Helper to create an active goal memory for a user."""
    return await mem_repo.create(
        username=f"{prefix}{user['external_id']}",
        type="goal",
        content=f"## {title}\n\nAn active goal for testing.",
        tags=["test"],
        importance=7,
        user_id=user["id"],
        organization_id=org["id"],
        shared=False,
        metadata=metadata or {"status": "active"},
    )


# ============================================================================
# Test: _list_active_goal_users
# ============================================================================


class TestListActiveGoalUsers:
    """Tests for the daemon's _list_active_goal_users query."""

    @pytest.mark.asyncio
    async def test_two_users_with_goals_both_returned(
        self, db_pool, org, user_a, user_b, mem_repo, prefix
    ):
        """Two users each with an active goal → both appear in the result."""
        from daemon.daemon import LucentDaemon

        await _create_active_goal(mem_repo, user_a, org, prefix, "Goal A")
        await _create_active_goal(mem_repo, user_b, org, prefix, "Goal B")

        daemon = LucentDaemon()
        users = await daemon._list_active_goal_users(str(org["id"]))

        user_ids = {u["user_id"] for u in users}
        assert str(user_a["id"]) in user_ids
        assert str(user_b["id"]) in user_ids

    @pytest.mark.asyncio
    async def test_goals_scanned_count_correct(
        self, db_pool, org, user_a, mem_repo, prefix
    ):
        """goals_scanned reflects the number of active goals per user."""
        from daemon.daemon import LucentDaemon

        await _create_active_goal(mem_repo, user_a, org, prefix, "Goal 1")
        await _create_active_goal(mem_repo, user_a, org, prefix, "Goal 2")
        await _create_active_goal(mem_repo, user_a, org, prefix, "Goal 3")

        daemon = LucentDaemon()
        users = await daemon._list_active_goal_users(str(org["id"]))

        user_a_row = next(
            (u for u in users if u["user_id"] == str(user_a["id"])), None
        )
        assert user_a_row is not None
        assert user_a_row["goals_scanned"] == 3

    @pytest.mark.asyncio
    async def test_no_active_goals_returns_empty(self, db_pool, org, user_a, prefix):
        """User with no active goals → not returned."""
        from daemon.daemon import LucentDaemon

        # Create no goals at all
        daemon = LucentDaemon()
        users = await daemon._list_active_goal_users(str(org["id"]))

        user_ids = {u["user_id"] for u in users}
        assert str(user_a["id"]) not in user_ids

    @pytest.mark.asyncio
    async def test_inactive_goal_excluded(
        self, db_pool, org, user_a, mem_repo, prefix
    ):
        """A goal with metadata.status != 'active' is excluded."""
        from daemon.daemon import LucentDaemon

        await mem_repo.create(
            username=f"{prefix}user_a",
            type="goal",
            content="## Completed Goal\n\nAlready done.",
            tags=["test"],
            importance=5,
            user_id=user_a["id"],
            organization_id=org["id"],
            shared=False,
            metadata={"status": "completed"},
        )

        daemon = LucentDaemon()
        users = await daemon._list_active_goal_users(str(org["id"]))

        user_ids = {u["user_id"] for u in users}
        assert str(user_a["id"]) not in user_ids

    @pytest.mark.asyncio
    async def test_abandoned_goal_excluded(
        self, db_pool, org, user_a, mem_repo, prefix
    ):
        """A goal with metadata.status='abandoned' is excluded."""
        from daemon.daemon import LucentDaemon

        await mem_repo.create(
            username=f"{prefix}user_a",
            type="goal",
            content="## Abandoned Goal",
            tags=["test"],
            importance=5,
            user_id=user_a["id"],
            organization_id=org["id"],
            shared=False,
            metadata={"status": "abandoned"},
        )

        daemon = LucentDaemon()
        users = await daemon._list_active_goal_users(str(org["id"]))

        user_ids = {u["user_id"] for u in users}
        assert str(user_a["id"]) not in user_ids


# ============================================================================
# Test: Request attribution (created_by) and approval
# ============================================================================


class TestRequestAttributionAndApproval:
    """Tests that requests created via scoped keys are correctly attributed
    and require user approval."""

    @pytest.mark.asyncio
    async def test_created_by_is_scoped_user_not_daemon(
        self, db_pool, org, user_a, daemon_user, mem_repo, req_repo, prefix
    ):
        """Request created via user-scoped context has created_by = user_a,
        not the daemon-service user."""
        goal = await _create_active_goal(mem_repo, user_a, org, prefix, "User A Goal")

        # Simulate the scoped key context: memory_scope='user',
        # memory_scope_user_id=user_a → effective user_id = user_a
        req = await req_repo.create_request(
            title=f"{prefix}Scoped request for A",
            org_id=str(org["id"]),
            description="Test scoped request",
            source="cognitive",
            priority="medium",
            created_by=str(user_a["id"]),
            memory_ids=[{"id": str(goal["id"]), "relation": "goal"}],
            force_pending_approval=True,
        )

        assert str(req["created_by"]) == str(user_a["id"])
        assert str(req["created_by"]) != str(daemon_user["id"])

    @pytest.mark.asyncio
    async def test_approval_status_is_pending(
        self, db_pool, org, user_a, mem_repo, req_repo, prefix
    ):
        """Request created with force_pending_approval=True has
        approval_status=pending_approval."""
        goal = await _create_active_goal(mem_repo, user_a, org, prefix, "Pending Goal")

        req = await req_repo.create_request(
            title=f"{prefix}Pending approval request",
            org_id=str(org["id"]),
            description="Should require approval",
            source="cognitive",
            priority="medium",
            created_by=str(user_a["id"]),
            memory_ids=[{"id": str(goal["id"]), "relation": "goal"}],
            force_pending_approval=True,
        )

        assert req["approval_status"] == APPROVAL_PENDING

    @pytest.mark.asyncio
    async def test_approval_status_not_auto_approved_with_force(
        self, db_pool, org, user_a, req_repo, prefix
    ):
        """Even with LUCENT_AUTO_APPROVE=true, force_pending_approval overrides."""
        with patch.dict("os.environ", {"LUCENT_AUTO_APPROVE": "true"}):
            req = await req_repo.create_request(
                title=f"{prefix}Force pending despite auto-approve",
                org_id=str(org["id"]),
                description="Should still be pending",
                source="cognitive",
                priority="medium",
                created_by=str(user_a["id"]),
                force_pending_approval=True,
            )

        assert req["approval_status"] == APPROVAL_PENDING

    @pytest.mark.asyncio
    async def test_non_scoped_cognitive_request_uses_normal_approval(
        self, db_pool, org, daemon_user, req_repo, prefix
    ):
        """A cognitive request WITHOUT force_pending_approval follows
        the normal approval logic (pending unless auto-approve is set)."""
        req = await req_repo.create_request(
            title=f"{prefix}Normal cognitive request",
            org_id=str(org["id"]),
            description="Standard cognitive request",
            source="cognitive",
            priority="medium",
            created_by=str(daemon_user["id"]),
            force_pending_approval=False,
        )

        # Default LUCENT_AUTO_APPROVE is false → cognitive source requires approval
        assert req["approval_status"] == APPROVAL_PENDING


# ============================================================================
# Test: Two users with private goals get separate requests
# ============================================================================


class TestTwoUsersGetSeparateRequests:
    """Two users each with private goals → each gets their own request."""

    @pytest.mark.asyncio
    async def test_separate_requests_via_repository(
        self, db_pool, org, user_a, user_b, mem_repo, req_repo, prefix
    ):
        """Simulates the fan-out at the repository level: creating a request
        per user for their goal results in distinct requests."""
        goal_a = await _create_active_goal(
            mem_repo, user_a, org, prefix, "Private Goal A"
        )
        goal_b = await _create_active_goal(
            mem_repo, user_b, org, prefix, "Private Goal B"
        )

        req_a = await req_repo.create_request(
            title=f"{prefix}Plan for User A",
            org_id=str(org["id"]),
            source="cognitive",
            created_by=str(user_a["id"]),
            memory_ids=[{"id": str(goal_a["id"]), "relation": "goal"}],
            force_pending_approval=True,
        )
        req_b = await req_repo.create_request(
            title=f"{prefix}Plan for User B",
            org_id=str(org["id"]),
            source="cognitive",
            created_by=str(user_b["id"]),
            memory_ids=[{"id": str(goal_b["id"]), "relation": "goal"}],
            force_pending_approval=True,
        )

        # Each user gets their own request
        assert req_a["id"] != req_b["id"]
        assert str(req_a["created_by"]) == str(user_a["id"])
        assert str(req_b["created_by"]) == str(user_b["id"])
        # Both require approval
        assert req_a["approval_status"] == APPROVAL_PENDING
        assert req_b["approval_status"] == APPROVAL_PENDING


# ============================================================================
# Test: Scoped key isolation
# ============================================================================


class TestScopedKeyIsolation:
    """Verify that user-scoped memory access prevents cross-user visibility."""

    @pytest.mark.asyncio
    async def test_user_a_cannot_see_user_b_private_memories(
        self, db_pool, org, user_a, user_b, mem_repo, prefix
    ):
        """User A's scoped search cannot see User B's private goal."""
        await _create_active_goal(mem_repo, user_b, org, prefix, "Secret Goal B")

        # Search as user A with memory_scope='user' — should NOT see user B's goal
        results = await mem_repo.search(
            type="goal",
            requesting_user_id=user_a["id"],
            requesting_org_id=org["id"],
            memory_scope="user",
            limit=50,
        )

        result_user_ids = {
            str(m["user_id"]) for m in results["memories"]
        }
        assert str(user_b["id"]) not in result_user_ids

    @pytest.mark.asyncio
    async def test_user_a_sees_own_goals_only(
        self, db_pool, org, user_a, user_b, mem_repo, prefix
    ):
        """User A's scoped search sees only their own goals."""
        await _create_active_goal(mem_repo, user_a, org, prefix, "My Goal A")
        await _create_active_goal(mem_repo, user_b, org, prefix, "My Goal B")

        results = await mem_repo.search(
            type="goal",
            requesting_user_id=user_a["id"],
            requesting_org_id=org["id"],
            memory_scope="user",
            limit=50,
        )

        for m in results["memories"]:
            assert str(m["user_id"]) == str(user_a["id"]), (
                f"User A's scoped search returned a memory owned by {m['user_id']}"
            )

    @pytest.mark.asyncio
    async def test_user_b_sees_own_goals_only(
        self, db_pool, org, user_a, user_b, mem_repo, prefix
    ):
        """User B's scoped search sees only their own goals."""
        await _create_active_goal(mem_repo, user_a, org, prefix, "My Goal A")
        await _create_active_goal(mem_repo, user_b, org, prefix, "My Goal B")

        results = await mem_repo.search(
            type="goal",
            requesting_user_id=user_b["id"],
            requesting_org_id=org["id"],
            memory_scope="user",
            limit=50,
        )

        for m in results["memories"]:
            assert str(m["user_id"]) == str(user_b["id"]), (
                f"User B's scoped search returned a memory owned by {m['user_id']}"
            )

    @pytest.mark.asyncio
    async def test_scoped_request_attribution_cannot_cross_users(
        self, db_pool, org, user_a, user_b, req_repo, prefix
    ):
        """A request created with created_by=user_a cannot claim user_b's identity.
        The repository sets created_by from whatever the caller provides — the
        security boundary is that the scoped key resolves to the correct user_id
        upstream in _get_current_user_context()."""
        req = await req_repo.create_request(
            title=f"{prefix}A request for A",
            org_id=str(org["id"]),
            source="cognitive",
            created_by=str(user_a["id"]),
            force_pending_approval=True,
        )
        # created_by should be user A, not user B
        assert str(req["created_by"]) == str(user_a["id"])
        assert str(req["created_by"]) != str(user_b["id"])


# ============================================================================
# Test: No goals = no fan-out iteration
# ============================================================================


class TestNoGoalsNoIteration:
    """Users without active goals should not appear in the fan-out."""

    @pytest.mark.asyncio
    async def test_user_with_no_goals_excluded(
        self, db_pool, org, user_a, user_b, mem_repo, prefix
    ):
        """Only users with active goals appear; users without are excluded."""
        from daemon.daemon import LucentDaemon

        # Only user_a has an active goal
        await _create_active_goal(mem_repo, user_a, org, prefix, "Goal A")
        # user_b has nothing

        daemon = LucentDaemon()
        users = await daemon._list_active_goal_users(str(org["id"]))

        user_ids = {u["user_id"] for u in users}
        assert str(user_a["id"]) in user_ids
        assert str(user_b["id"]) not in user_ids

    @pytest.mark.asyncio
    async def test_empty_org_returns_no_users(self, db_pool, org, prefix):
        """An org with no goal memories at all returns empty list."""
        from daemon.daemon import LucentDaemon

        daemon = LucentDaemon()
        users = await daemon._list_active_goal_users(str(org["id"]))
        assert users == []

    @pytest.mark.asyncio
    async def test_fanout_with_no_users_returns_early(self, db_pool, org, prefix):
        """_run_cognitive_planning_fanout returns early message when no users."""
        from daemon.daemon import LucentDaemon

        daemon = LucentDaemon()
        result = await daemon._run_cognitive_planning_fanout(
            task_id=str(uuid4()),
            org_id=str(org["id"]),
            system_message="test system message",
            mcp_config_base={},
        )
        assert "no users with active goals" in result.lower()


# ============================================================================
# Test: Direct target request creation
# ============================================================================


class TestDirectTargetRequestCreation:
    """The daemon should create plannable target requests directly.

    This avoids relying on mutating MCP tool calls from Copilot sessions, which
    can fail at the permission protocol layer before Lucent sees the request.
    """

    @pytest.mark.asyncio
    async def test_fanout_creates_pending_request_from_target(
        self, db_pool, org, user_a, mem_repo, prefix
    ):
        from daemon.daemon import LucentDaemon

        goal = await _create_active_goal(
            mem_repo,
            user_a,
            org,
            prefix,
            "Milestoned Goal",
            metadata={
                "status": "active",
                "milestones": [
                    {"description": "First milestone", "status": "completed"},
                    {"description": "Second milestone", "status": "active"},
                ],
            },
        )
        target = {
            "goal_id": str(goal["id"]),
            "goal_title": "Milestoned Goal",
            "next_milestone_index": 2,
            "next_milestone_description": "Second milestone",
            "suggested_title": f"{prefix}M2 direct fanout request",
            "target_repo": "kahinton/example-planning-repo",
            "target_paths": ["docs/m2.md"],
        }

        daemon = LucentDaemon()
        with (
            patch(
                "daemon.daemon._mint_scoped_api_key",
                new_callable=AsyncMock,
                return_value="hs_fake_key",
            ),
            patch(
                "daemon.daemon.RequestAPI.list_planning_targets",
                new_callable=AsyncMock,
                return_value=[target],
            ),
            patch.object(daemon, "run_session", new_callable=AsyncMock) as run_session,
        ):
            result = await daemon._run_cognitive_planning_fanout(
                task_id=str(uuid4()),
                org_id=str(org["id"]),
                system_message="test",
                mcp_config_base={},
            )

        run_session.assert_not_called()
        assert "requests=1" in result
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT created_by, source, approval_status,
                          goal_memory_id, goal_milestone_index,
                          target_repo, target_paths
                   FROM requests WHERE title = $1""",
                target["suggested_title"],
            )
        assert row is not None
        assert str(row["created_by"]) == str(user_a["id"])
        assert row["source"] == "cognitive"
        assert row["approval_status"] == APPROVAL_PENDING
        assert str(row["goal_memory_id"]) == str(goal["id"])
        assert row["goal_milestone_index"] == 2
        assert row["target_repo"] == "kahinton/example-planning-repo"
        assert row["target_paths"] == ["docs/m2.md"]


# ============================================================================
# Test: Error isolation
# ============================================================================


class TestErrorIsolation:
    """If one user's planning session fails, others should still succeed."""

    @pytest.mark.asyncio
    async def test_error_in_user_a_does_not_block_user_b(
        self, db_pool, org, user_a, user_b, mem_repo, prefix
    ):
        """When minting a scoped key fails for user A, user B still gets processed."""
        from daemon.daemon import LucentDaemon

        await _create_active_goal(mem_repo, user_a, org, prefix, "Goal A")
        await _create_active_goal(mem_repo, user_b, org, prefix, "Goal B")

        daemon = LucentDaemon()
        call_count = 0

        async def _mock_mint(*, memory_scope, memory_scope_user_id, org_id, ttl_minutes):
            nonlocal call_count
            call_count += 1
            if memory_scope_user_id == str(user_a["id"]):
                return None  # Simulate failure for user A
            return "hs_fake_key_for_user_b"

        with (
            patch("daemon.daemon._mint_scoped_api_key", side_effect=_mock_mint),
            patch(
                "daemon.daemon.RequestAPI.list_planning_targets",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await daemon._run_cognitive_planning_fanout(
                task_id=str(uuid4()),
                org_id=str(org["id"]),
                system_message="test",
                mcp_config_base={},
            )

        # Both users were attempted
        assert call_count == 2
        # Result includes both users' summaries
        assert "fan-out complete" in result.lower()

    @pytest.mark.asyncio
    async def test_target_creation_exception_is_caught(
        self, db_pool, org, user_a, mem_repo, prefix
    ):
        """An exception during direct target creation is caught and logged."""
        from daemon.daemon import LucentDaemon

        await _create_active_goal(mem_repo, user_a, org, prefix, "Goal A")

        daemon = LucentDaemon()

        async def _boom(*args, **kwargs):
            raise RuntimeError("target create exploded")

        with (
            patch(
                "daemon.daemon._mint_scoped_api_key",
                new_callable=AsyncMock,
                return_value="hs_fake_key",
            ),
            patch(
                "daemon.daemon.RequestAPI.list_planning_targets",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "goal_id": str(uuid4()),
                        "next_milestone_index": 1,
                        "suggested_title": "Goal A M1",
                    }
                ],
            ),
            patch.object(
                daemon,
                "_create_cognitive_request_for_target",
                side_effect=_boom,
            ),
        ):
            result = await daemon._run_cognitive_planning_fanout(
                task_id=str(uuid4()),
                org_id=str(org["id"]),
                system_message="test",
                mcp_config_base={},
            )

        # Should complete without raising, with error count > 0
        assert "errors=1" in result

    @pytest.mark.asyncio
    async def test_multiple_users_mixed_success_failure(
        self, db_pool, org, user_a, user_b, mem_repo, prefix
    ):
        """User A fails, user B succeeds — result reports both."""
        from daemon.daemon import LucentDaemon

        await _create_active_goal(mem_repo, user_a, org, prefix, "Goal A")
        await _create_active_goal(mem_repo, user_b, org, prefix, "Goal B")

        daemon = LucentDaemon()
        create_calls = []

        async def _mock_create(*, org_id, user_id, target):
            create_calls.append(user_id)
            if user_id == str(user_a["id"]):
                raise RuntimeError("User A target create failed")
            return "created", {"id": str(uuid4()), "status": "pending"}

        with (
            patch(
                "daemon.daemon._mint_scoped_api_key",
                new_callable=AsyncMock,
                return_value="hs_fake_key",
            ),
            patch(
                "daemon.daemon.RequestAPI.list_planning_targets",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "goal_id": str(uuid4()),
                        "next_milestone_index": 1,
                        "suggested_title": "Goal M1",
                    }
                ],
            ),
            patch.object(
                daemon,
                "_create_cognitive_request_for_target",
                side_effect=_mock_create,
            ),
        ):
            result = await daemon._run_cognitive_planning_fanout(
                task_id=str(uuid4()),
                org_id=str(org["id"]),
                system_message="test",
                mcp_config_base={},
            )

        assert len(create_calls) == 2
        assert "fan-out complete" in result.lower()


# ============================================================================
# Test: MCP tool layer — create_request with scoped context
# ============================================================================


class TestMCPScopedRequestCreation:
    """Tests that the MCP create_request tool correctly uses memory_scope
    to set force_pending_approval and created_by."""

    @pytest.mark.asyncio
    async def test_scoped_user_context_forces_pending_approval(
        self, db_pool, org, user_a, mem_repo, req_repo, prefix
    ):
        """When set_current_user has memory_scope='user', the MCP create_request
        tool should create a request with force_pending_approval=True."""
        from mcp.server.fastmcp import FastMCP

        from lucent.tools.requests import register_request_tools

        goal = await _create_active_goal(mem_repo, user_a, org, prefix, "MCP Goal")

        mcp = FastMCP("test")
        register_request_tools(mcp)

        # Set auth context with memory_scope='user' — simulating a scoped key
        set_current_user(
            {
                "id": user_a["id"],
                "organization_id": org["id"],
                "role": "member",
                "display_name": "User A",
                "email": "a@test.com",
                "memory_scope": "user",
                "memory_scope_user_id": user_a["id"],
            }
        )

        try:
            result_text = await mcp._tool_manager.call_tool(
                "create_request",
                {
                    "title": f"{prefix}MCP scoped request",
                    "description": "Created via MCP with scoped key",
                    "source": "cognitive",
                    "goal_id": str(goal["id"]),
                },
            )
            result = json.loads(result_text)
            request_id = result["id"]

            # Verify via repository — the MCP tool returns a slim response,
            # so we check the full DB row for approval_status and created_by
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT created_by, approval_status FROM requests WHERE id = $1",
                    __import__("uuid").UUID(request_id),
                )
            assert row is not None
            assert str(row["created_by"]) == str(user_a["id"])
            assert row["approval_status"] == APPROVAL_PENDING
        finally:
            set_current_user(None)

    @pytest.mark.asyncio
    async def test_unscoped_context_does_not_force_pending(
        self, db_pool, org, user_a, req_repo, prefix
    ):
        """When set_current_user has no memory_scope, the MCP create_request
        tool should not force pending_approval (uses normal logic)."""
        from mcp.server.fastmcp import FastMCP

        from lucent.tools.requests import register_request_tools

        mcp = FastMCP("test")
        register_request_tools(mcp)

        # No memory_scope set — normal user context
        set_current_user(
            {
                "id": user_a["id"],
                "organization_id": org["id"],
                "role": "member",
                "display_name": "User A",
                "email": "a@test.com",
            }
        )

        try:
            result_text = await mcp._tool_manager.call_tool(
                "create_request",
                {
                    "title": f"{prefix}Normal request",
                    "description": "Created without scoping",
                    "source": "user",
                },
            )
            result = json.loads(result_text)
            request_id = result["id"]

            # Verify via repository
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT approval_status FROM requests WHERE id = $1",
                    __import__("uuid").UUID(request_id),
                )
            assert row is not None
            # User-sourced requests are auto-approved
            assert row["approval_status"] == APPROVAL_AUTO
        finally:
            set_current_user(None)


# ============================================================================
# Test: Prompt construction
# ============================================================================


class TestPromptConstruction:
    """Verify the user-scoped cognitive prompt includes key instructions."""

    def test_prompt_mentions_per_user_mode(self):
        from daemon.daemon import LucentDaemon

        daemon = LucentDaemon()
        prompt = daemon._build_user_scoped_cognitive_prompt()

        assert "per-user fan-out" in prompt.lower() or "user-scoped" in prompt.lower()
        assert "goal_id" in prompt
        assert "search_memories" in prompt

    def test_prompt_instructs_no_task_creation(self):
        from daemon.daemon import LucentDaemon

        daemon = LucentDaemon()
        prompt = daemon._build_user_scoped_cognitive_prompt()

        assert "do not create tasks" in prompt.lower() or "do NOT create tasks" in prompt


# ============================================================================
# Test: Deduplication still works with per-user requests
# ============================================================================


class TestDeduplication:
    """Verify that goal_id deduplication works for per-user requests."""

    @pytest.mark.asyncio
    async def test_duplicate_request_returns_existing(
        self, db_pool, org, user_a, mem_repo, req_repo, prefix
    ):
        """Creating a second request for the same goal_id returns the existing one."""
        goal = await _create_active_goal(mem_repo, user_a, org, prefix, "Dedup Goal")

        req1 = await req_repo.create_request(
            title=f"{prefix}First request",
            org_id=str(org["id"]),
            source="cognitive",
            created_by=str(user_a["id"]),
            memory_ids=[{"id": str(goal["id"]), "relation": "goal"}],
            force_pending_approval=True,
        )
        req2 = await req_repo.create_request(
            title=f"{prefix}Duplicate request",
            org_id=str(org["id"]),
            source="cognitive",
            created_by=str(user_a["id"]),
            memory_ids=[{"id": str(goal["id"]), "relation": "goal"}],
            force_pending_approval=True,
        )

        # Dedup returns the same request
        assert req1["id"] == req2["id"]

    @pytest.mark.asyncio
    async def test_different_users_same_shared_goal_deduplicates(
        self, db_pool, org, user_a, user_b, mem_repo, req_repo, prefix
    ):
        """If a shared goal already has an active request from user A,
        user B's request for the same goal returns the existing one."""
        # Create a shared goal (both users can see it)
        goal = await mem_repo.create(
            username=f"{prefix}user_a",
            type="goal",
            content="## Shared Goal\n\nShared across users.",
            tags=["test"],
            importance=7,
            user_id=user_a["id"],
            organization_id=org["id"],
            shared=True,
            metadata={"status": "active"},
        )

        req_a = await req_repo.create_request(
            title=f"{prefix}Plan from A",
            org_id=str(org["id"]),
            source="cognitive",
            created_by=str(user_a["id"]),
            memory_ids=[{"id": str(goal["id"]), "relation": "goal"}],
            force_pending_approval=True,
        )
        req_b = await req_repo.create_request(
            title=f"{prefix}Plan from B",
            org_id=str(org["id"]),
            source="cognitive",
            created_by=str(user_b["id"]),
            memory_ids=[{"id": str(goal["id"]), "relation": "goal"}],
            force_pending_approval=True,
        )

        # Dedup returns the same request
        assert req_a["id"] == req_b["id"]
