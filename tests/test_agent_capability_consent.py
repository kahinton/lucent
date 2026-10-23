"""Per-user capability consent for agents.

The agent is the structural hub for capability access. A skill, managed tool,
hook, or MCP server reaches a running agent only when all three hold:

1. it is attached to the agent,
2. the requesting user can read it, and
3. the requesting user approved it — or is exempt (built-in, the attacher, or
   the capability's owner).

These tests pin the Alice/Bob scenario end to end, including the on-demand
paths (``get_skill_definition``, ``run_managed_tool``) where a model could
otherwise fetch a capability by id without ever being given it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from mcp.server import MCPServer as FastMCP

from lucent.auth import set_current_user
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.definitions import (
    DefinitionRepository,
    GrantOutcome,
    get_authorized_definitions_pool,
)
from lucent.llm.context import set_llm_context
from lucent.tools import definitions as definition_tools

pytestmark = pytest.mark.integration


# ── Helpers ──────────────────────────────────────────────────────────────


async def _set_clearance_read(conn, table: str, resource_id, *, org_id) -> None:
    """File an org-wide read clearance, making a definition visible to everyone."""
    await conn.execute(
        "INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id) "
        f"SELECT auth_id, 'read', 'org', $2 FROM {table} WHERE id = $1 "
        "ON CONFLICT DO NOTHING",
        resource_id,
        org_id,
    )


async def _add_user_clearance(conn, table: str, resource_id, user_id) -> None:
    await conn.execute(
        "INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id) "
        f"SELECT auth_id, 'read', 'user', $2 FROM {table} WHERE id = $1 "
        "ON CONFLICT DO NOTHING",
        resource_id,
        user_id,
    )


async def _strip_clearances(conn, table: str, resource_id) -> None:
    await conn.execute(
        f"DELETE FROM auth_clearances WHERE auth_id = "
        f"(SELECT auth_id FROM {table} WHERE id = $1)",
        resource_id,
    )


async def _call(mcp: FastMCP, tool: str, arguments: dict):
    result = await mcp.call_tool(tool, arguments)
    # FastMCP may return either (content, structured) or a CallToolResult.
    if isinstance(result, tuple):
        result = result[0]
    content = getattr(result, "content", result)
    first = content[0] if isinstance(content, list) else content
    return json.loads(getattr(first, "text", first))


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def consent_prefix(request):
    return f"consent_{request.node.name[:24]}_{UUID(int=abs(hash(request.node.name)) % 10**8)}"


@pytest_asyncio.fixture
async def consent_org(db_pool, consent_prefix):
    return await OrganizationRepository(db_pool).create(name=consent_prefix)


@pytest_asyncio.fixture
async def alice(db_pool, consent_org, consent_prefix):
    user = await UserRepository(db_pool).create(
        external_id=f"{consent_prefix}_alice",
        provider="local",
        organization_id=consent_org["id"],
        email=f"{consent_prefix}_alice@test.com",
        display_name="Alice",
        role="member",
    )
    return user


@pytest_asyncio.fixture
async def bob(db_pool, consent_org, consent_prefix):
    user = await UserRepository(db_pool).create(
        external_id=f"{consent_prefix}_bob",
        provider="local",
        organization_id=consent_org["id"],
        email=f"{consent_prefix}_bob@test.com",
        display_name="Bob",
        role="member",
    )
    return user


@pytest_asyncio.fixture
async def shared_agent(db_pool, consent_org, alice):
    """An org-readable agent Alice owns ("Super Duper Coder").

    Owned, not owner-less: an org-shared (unowned) agent is managed by org
    admins, which is a different authorization case than the one this suite is
    pinning down.
    """
    org_id = str(consent_org["id"])
    repo = DefinitionRepository(db_pool)
    agent = await repo.create_agent(
        name="Super Duper Coder",
        description="Shared agent",
        content="# Super Duper Coder",
        org_id=org_id,
        created_by=str(alice["id"]),
        status="active",
        owner_user_id=str(alice["id"]),
    )
    async with db_pool.acquire() as conn:
        await _set_clearance_read(conn, "agent_definitions", agent["id"], org_id=org_id)
    return agent


@pytest_asyncio.fixture
async def code_review_skill(db_pool, consent_org, alice):
    """A skill Alice owns but shares with the org, so Bob can read it too."""
    org_id = str(consent_org["id"])
    repo = DefinitionRepository(db_pool)
    skill = await repo.create_skill(
        name="Code Review",
        description="Review code",
        content="# Code Review",
        org_id=org_id,
        created_by=str(alice["id"]),
        status="active",
        shared_with_org=True,
    )
    async with db_pool.acquire() as conn:
        await _set_clearance_read(conn, "skill_definitions", skill["id"], org_id=org_id)
    return skill


@pytest_asyncio.fixture
async def pep_talk_skill(db_pool, consent_org, alice):
    """A skill only Alice can see."""
    org_id = str(consent_org["id"])
    repo = DefinitionRepository(db_pool)
    skill = await repo.create_skill(
        name="Pep Talk",
        description="Encouragement",
        content="# Pep Talk",
        org_id=org_id,
        created_by=str(alice["id"]),
        status="active",
        owner_user_id=str(alice["id"]),
    )
    return skill


@pytest_asyncio.fixture
async def bug_finder_skill(db_pool, consent_org, bob):
    """A skill only Bob can see."""
    org_id = str(consent_org["id"])
    repo = DefinitionRepository(db_pool)
    skill = await repo.create_skill(
        name="Bug Finder",
        description="Find bugs",
        content="# Bug Finder",
        org_id=org_id,
        created_by=str(bob["id"]),
        status="active",
        owner_user_id=str(bob["id"]),
    )
    return skill


# ── The Alice/Bob scenario ───────────────────────────────────────────────


class TestAliceBobCapabilityVisibility:
    """Alice and Bob share an agent but see different capability sets."""

    async def test_resolution_differences(
        self,
        db_pool,
        consent_org,
        alice,
        bob,
        shared_agent,
        code_review_skill,
        pep_talk_skill,
        bug_finder_skill,
    ):
        from lucent.agent_capabilities import resolve_agent_capabilities

        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)

        # Alice attaches her two skills; Bob attaches his.
        assert (
            await repo.grant_capability(
                agent_id=agent_id,
                capability_type="skill",
                capability_id=str(code_review_skill["id"]),
                org_id=org_id,
                actor_user_id=str(alice["id"]),
            )
        ).ok
        assert (
            await repo.grant_capability(
                agent_id=agent_id,
                capability_type="skill",
                capability_id=str(pep_talk_skill["id"]),
                org_id=org_id,
                actor_user_id=str(alice["id"]),
            )
        ).ok
        assert (
            await repo.grant_capability(
                agent_id=agent_id,
                capability_type="skill",
                capability_id=str(bug_finder_skill["id"]),
                org_id=org_id,
                actor_user_id=str(bob["id"]),
            )
        ).ok

        alice_view = await resolve_agent_capabilities(
            pool=db_pool,
            user={"id": str(alice["id"]), "organization_id": org_id},
            agent_id=agent_id,
            org_id=org_id,
        )
        bob_view = await resolve_agent_capabilities(
            pool=db_pool,
            user={"id": str(bob["id"]), "organization_id": org_id},
            agent_id=agent_id,
            org_id=org_id,
        )

        # Alice: everything she attached. Bob's private skill is invisible.
        assert set(alice_view.skill_names) == {"Code Review", "Pep Talk"}

        # Bob: his own skill, plus Code Review once he approves it. Pep Talk is
        # not even reachable — he cannot read it.
        assert "Pep Talk" not in bob_view.skill_names
        assert "Bug Finder" in bob_view.skill_names
        assert "Code Review" not in bob_view.skill_names
        pending_names = {item["capability_name"] for item in bob_view.pending}
        assert pending_names == {"Code Review"}

        # Bob approves Code Review for himself.
        assert await repo.upsert_capability_consent(
            org_id=org_id,
            agent_id=agent_id,
            user_id=str(bob["id"]),
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            state="approved",
        )
        bob_after = await resolve_agent_capabilities(
            pool=db_pool,
            user={"id": str(bob["id"]), "organization_id": org_id},
            agent_id=agent_id,
            org_id=org_id,
        )
        assert set(bob_after.skill_names) == {"Code Review", "Bug Finder"}
        assert bob_after.pending == []

    async def test_declined_capability_stays_off(
        self,
        db_pool,
        consent_org,
        alice,
        bob,
        shared_agent,
        code_review_skill,
    ):
        from lucent.agent_capabilities import resolve_agent_capabilities

        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )
        await repo.upsert_capability_consent(
            org_id=org_id,
            agent_id=agent_id,
            user_id=str(bob["id"]),
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            state="declined",
        )

        bob_view = await resolve_agent_capabilities(
            pool=db_pool,
            user={"id": str(bob["id"]), "organization_id": org_id},
            agent_id=agent_id,
            org_id=org_id,
        )
        assert bob_view.skill_names == []
        assert [item["capability_name"] for item in bob_view.declined] == ["Code Review"]
        # Declined is not "pending" — it never re-prompts on its own.
        assert bob_view.pending == []

        declined = await repo.list_declined_capability_consents(org_id, str(bob["id"]))
        assert [item["capability_name"] for item in declined] == ["Code Review"]

    async def test_owner_and_attacher_are_exempt(
        self,
        db_pool,
        consent_org,
        alice,
        bob,
        shared_agent,
        code_review_skill,
        bug_finder_skill,
    ):
        """You never approve your own capability, nor one you attached."""
        from lucent.agent_capabilities import resolve_agent_capabilities

        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)

        # Make Bob's private skill readable so this test isolates *consent*
        # from *visibility*.
        async with db_pool.acquire() as conn:
            await _add_user_clearance(conn, "skill_definitions", bug_finder_skill["id"], bob["id"])
            await _add_user_clearance(
                conn, "skill_definitions", bug_finder_skill["id"], alice["id"]
            )

        # Alice attaches Bob's skill. Both are exempt, for different reasons:
        # Alice as the attacher, Bob as the capability's owner.
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(bug_finder_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(bob["id"]),
        )

        alice_view = await resolve_agent_capabilities(
            pool=db_pool,
            user={"id": str(alice["id"]), "organization_id": org_id},
            agent_id=agent_id,
            org_id=org_id,
        )
        bob_view = await resolve_agent_capabilities(
            pool=db_pool,
            user={"id": str(bob["id"]), "organization_id": org_id},
            agent_id=agent_id,
            org_id=org_id,
        )
        assert "Bug Finder" in alice_view.skill_names
        assert "Bug Finder" in bob_view.skill_names
        assert "Code Review" in bob_view.skill_names

    async def test_builtin_capability_needs_no_consent(
        self, db_pool, consent_org, bob, shared_agent, consent_prefix
    ):
        from lucent.agent_capabilities import resolve_agent_capabilities

        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        builtin = await repo.create_skill(
            name="Builtin Skill",
            description="",
            content="# Builtin",
            org_id=org_id,
            created_by=str(bob["id"]),
            status="active",
            scope="built-in",
        )
        async with db_pool.acquire() as conn:
            await _set_clearance_read(conn, "skill_definitions", builtin["id"], org_id=org_id)
            # Attached by somebody else, so the grantor exemption cannot apply.
            await conn.execute(
                "INSERT INTO agent_skills (agent_id, skill_id) VALUES ($1, $2)",
                shared_agent["id"],
                builtin["id"],
            )

        view = await resolve_agent_capabilities(
            pool=db_pool,
            user={"id": str(bob["id"]), "organization_id": org_id},
            agent_id=agent_id,
            org_id=org_id,
        )
        assert "Builtin Skill" in view.skill_names
        assert view.pending == []


# ── Attach authorization ─────────────────────────────────────────────────


class TestAttachAuthorization:
    async def test_cannot_attach_unreadable_capability(
        self, db_pool, consent_org, alice, bob, shared_agent, pep_talk_skill
    ):
        org_id = str(consent_org["id"])
        repo = DefinitionRepository(db_pool)
        outcome = await repo.grant_capability(
            agent_id=str(shared_agent["id"]),
            capability_type="skill",
            capability_id=str(pep_talk_skill["id"]),
            org_id=org_id,
            actor_user_id=str(bob["id"]),
        )
        assert outcome is GrantOutcome.DENIED

    async def test_cannot_attach_to_unreadable_agent(
        self, db_pool, consent_org, alice, bob, code_review_skill
    ):
        org_id = str(consent_org["id"])
        repo = DefinitionRepository(db_pool)
        private_agent = await repo.create_agent(
            name="Alice Private Agent",
            description="",
            content="# Private",
            org_id=org_id,
            created_by=str(alice["id"]),
            status="active",
            owner_user_id=str(alice["id"]),
        )
        outcome = await repo.grant_capability(
            agent_id=str(private_agent["id"]),
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(bob["id"]),
        )
        assert outcome is GrantOutcome.DENIED

    async def test_second_attach_is_already_attached_not_duplicated(
        self, db_pool, consent_org, alice, bob, shared_agent, code_review_skill
    ):
        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)

        first = await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )
        second = await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(bob["id"]),
        )
        assert first is GrantOutcome.ATTACHED
        assert second is GrantOutcome.ALREADY_ATTACHED
        assert second.ok is True  # the capability is attached; message differs only

        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_skills WHERE agent_id = $1 AND skill_id = $2",
                shared_agent["id"],
                code_review_skill["id"],
            )
            grantor = await conn.fetchval(
                "SELECT granted_by FROM agent_skills WHERE agent_id = $1 AND skill_id = $2",
                shared_agent["id"],
                code_review_skill["id"],
            )
        assert count == 1
        # Provenance is preserved: Bob's failed attach must not steal it.
        assert str(grantor) == str(alice["id"])

    async def test_grant_without_actor_is_refused(
        self, db_pool, consent_org, shared_agent, code_review_skill
    ):
        org_id = str(consent_org["id"])
        repo = DefinitionRepository(db_pool)
        outcome = await repo.grant_capability(
            agent_id=str(shared_agent["id"]),
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=None,
        )
        assert outcome is GrantOutcome.DENIED


# ── Detach authorization ─────────────────────────────────────────────────


class TestDetachAuthorization:
    async def test_agent_owner_and_capability_owner_may_detach(
        self, db_pool, consent_org, alice, bob, shared_agent, code_review_skill
    ):
        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )

        # Bob is neither the agent's owner nor the skill's owner.
        assert not await repo.can_revoke_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            user_id=str(bob["id"]),
            user_role="member",
        )
        # Alice owns both ends.
        assert await repo.can_revoke_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            user_id=str(alice["id"]),
            user_role="member",
        )
        assert await repo.revoke_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            user_id=str(alice["id"]),
            user_role="member",
        )

    async def test_detaching_clears_recorded_consent(
        self, db_pool, consent_org, alice, bob, shared_agent, code_review_skill
    ):
        """A stale approval must not silently apply if the skill is re-attached."""
        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )
        await repo.upsert_capability_consent(
            org_id=org_id,
            agent_id=agent_id,
            user_id=str(bob["id"]),
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            state="approved",
        )
        await repo.revoke_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            user_id=str(alice["id"]),
            user_role="member",
        )
        async with db_pool.acquire() as conn:
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_capability_consents WHERE agent_id = $1",
                shared_agent["id"],
            )
        assert remaining == 0


# ── On-demand path (the gate that makes consent real) ────────────────────


class TestOnDemandCapabilityGate:
    @pytest_asyncio.fixture
    async def mcp(self):
        server = FastMCP(name="consent-gate-test")
        definition_tools.register_definition_tools(server)
        return server

    async def test_fetch_by_id_denied_without_consent(
        self,
        db_pool,
        consent_org,
        alice,
        bob,
        shared_agent,
        code_review_skill,
        mcp,
    ):
        """Bob must not read a skill body he never approved, even given the id."""
        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )
        set_current_user(bob)
        set_llm_context(agent_definition_id=agent_id)
        try:
            result = await _call(
                mcp, "get_skill_definition", {"skill_id": str(code_review_skill["id"])}
            )
        finally:
            set_llm_context(agent_definition_id=None)
            set_current_user(None)
        assert result["code"] == 403
        assert "not approved" in result["error"]

    async def test_fetch_by_id_allowed_after_approval(
        self,
        db_pool,
        consent_org,
        alice,
        bob,
        shared_agent,
        code_review_skill,
        mcp,
    ):
        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )
        await repo.upsert_capability_consent(
            org_id=org_id,
            agent_id=agent_id,
            user_id=str(bob["id"]),
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            state="approved",
        )
        set_current_user(bob)
        set_llm_context(agent_definition_id=agent_id)
        try:
            result = await _call(
                mcp, "get_skill_definition", {"skill_id": str(code_review_skill["id"])}
            )
        finally:
            set_llm_context(agent_definition_id=None)
            set_current_user(None)
        assert result.get("name") == "Code Review"

    async def test_fetch_by_id_denied_when_declined(
        self,
        db_pool,
        consent_org,
        alice,
        bob,
        shared_agent,
        code_review_skill,
        mcp,
    ):
        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )
        await repo.upsert_capability_consent(
            org_id=org_id,
            agent_id=agent_id,
            user_id=str(bob["id"]),
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            state="declined",
        )
        set_current_user(bob)
        set_llm_context(agent_definition_id=agent_id)
        try:
            result = await _call(
                mcp, "get_skill_definition", {"skill_id": str(code_review_skill["id"])}
            )
        finally:
            set_llm_context(agent_definition_id=None)
            set_current_user(None)
        assert result["code"] == 403

    async def test_fetch_by_id_unaffected_without_agent_context(
        self, db_pool, consent_org, bob, code_review_skill, mcp
    ):
        """Bare chat has no agent, so clearance alone decides (unchanged)."""
        set_current_user(bob)
        try:
            result = await _call(
                mcp, "get_skill_definition", {"skill_id": str(code_review_skill["id"])}
            )
        finally:
            set_current_user(None)
        assert result.get("name") == "Code Review"


class TestCapabilityPermissionGate:
    """The narrow per-capability check used by the on-demand paths."""

    async def test_denied_when_not_attached_to_the_agent(
        self, db_pool, consent_org, bob, shared_agent, code_review_skill
    ):
        from lucent.agent_capabilities import assert_capability_permitted

        org_id = str(consent_org["id"])
        permitted = await assert_capability_permitted(
            pool=db_pool,
            user={"id": str(bob["id"]), "organization_id": org_id},
            agent_id=str(shared_agent["id"]),
            org_id=org_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
        )
        assert permitted is False

    async def test_denied_when_capability_is_unreadable(
        self, db_pool, consent_org, alice, bob, shared_agent, pep_talk_skill
    ):
        """Attached but invisible: no clearance, so no permission."""
        from lucent.agent_capabilities import assert_capability_permitted

        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        # Attach as a system actor so the attachment exists without granting
        # Bob any read clearance on the skill.
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(pep_talk_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),  # Alice can read her own skill
        )
        permitted = await assert_capability_permitted(
            pool=db_pool,
            user={"id": str(bob["id"]), "organization_id": org_id},
            agent_id=agent_id,
            org_id=org_id,
            capability_type="skill",
            capability_id=str(pep_talk_skill["id"]),
        )
        assert permitted is False

    async def test_allowed_for_exempt_owner(
        self, db_pool, consent_org, bob, shared_agent, bug_finder_skill
    ):
        from lucent.agent_capabilities import assert_capability_permitted

        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(bug_finder_skill["id"]),
            org_id=org_id,
            actor_user_id=str(bob["id"]),
        )
        # Bob owns the skill and attached it, so no consent row is required.
        assert await assert_capability_permitted(
            pool=db_pool,
            user={"id": str(bob["id"]), "organization_id": org_id},
            agent_id=agent_id,
            org_id=org_id,
            capability_type="skill",
            capability_id=str(bug_finder_skill["id"]),
        )

    async def test_unknown_capability_type_denies(self, db_pool, consent_org, bob):
        from lucent.agent_capabilities import assert_capability_permitted

        assert not await assert_capability_permitted(
            pool=db_pool,
            user={"id": str(bob["id"]), "organization_id": str(consent_org["id"])},
            agent_id="00000000-0000-0000-0000-000000000000",
            org_id=str(consent_org["id"]),
            capability_type="not-a-capability",
            capability_id="00000000-0000-0000-0000-000000000000",
        )


# ── Managed tools ────────────────────────────────────────────────────────


class TestManagedToolConsent:
    async def test_declined_managed_tool_cannot_be_run(
        self, db_pool, consent_org, alice, bob, shared_agent
    ):
        """Attachment is not consent: a declined tool must not execute."""
        from uuid import uuid4

        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        tool = await repo.create_managed_tool(
            name=f"consent-tool-{uuid4().hex[:8]}",
            description="Echo",
            source_code="def run(payload):\n    return payload\n",
            input_schema={"type": "object", "properties": {}},
            org_id=org_id,
            created_by=str(alice["id"]),
            status="active",
            owner_user_id=str(alice["id"]),
        )
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="managed_tool",
            capability_id=str(tool["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )
        await repo.upsert_capability_consent(
            org_id=org_id,
            agent_id=agent_id,
            user_id=str(bob["id"]),
            capability_type="managed_tool",
            capability_id=str(tool["id"]),
            state="declined",
        )

        from lucent.agent_capabilities import assert_capability_permitted

        permitted = await assert_capability_permitted(
            pool=db_pool,
            user={"id": str(bob["id"]), "organization_id": org_id},
            agent_id=agent_id,
            org_id=org_id,
            capability_type="managed_tool",
            capability_id=str(tool["id"]),
        )
        assert permitted is False


# ── Fail-closed behavior ─────────────────────────────────────────────────


class TestFailClosed:
    async def test_unknown_agent_yields_no_capabilities(self, db_pool, consent_org, bob):
        from lucent.agent_capabilities import resolve_agent_capabilities

        view = await resolve_agent_capabilities(
            pool=db_pool,
            user={"id": str(bob["id"]), "organization_id": str(consent_org["id"])},
            agent_id=None,
            org_id=str(consent_org["id"]),
        )
        assert view.skills == []
        assert view.managed_tools == []
        assert view.hooks == []
        assert view.mcp_servers == []

    async def test_missing_user_identity_yields_no_capabilities(
        self, db_pool, consent_org, shared_agent
    ):
        from lucent.agent_capabilities import resolve_agent_capabilities

        view = await resolve_agent_capabilities(
            pool=db_pool,
            user={"organization_id": str(consent_org["id"])},
            agent_id=str(shared_agent["id"]),
            org_id=str(consent_org["id"]),
        )
        assert view.skills == []

    async def test_resolver_does_not_fall_back_to_an_unfiltered_read(
        self, db_pool, consent_org, bob, shared_agent, code_review_skill
    ):
        """A clearance failure must narrow the set, never widen it."""
        from lucent.agent_capabilities import resolve_agent_capabilities

        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(bob["id"]),
        )

        with patch(
            "lucent.agent_capabilities.get_authorized_definitions_pool",
            new_callable=AsyncMock,
            side_effect=RuntimeError("pool unavailable"),
        ):
            view = await resolve_agent_capabilities(
                pool=db_pool,
                user={"id": str(bob["id"]), "organization_id": org_id},
                agent_id=agent_id,
                org_id=org_id,
            )
        assert view.skills == []
        assert view.pending == []


# ── Badge counts ─────────────────────────────────────────────────────────


class TestConsentCounts:
    async def test_pending_count_matches_pending_listing(
        self, db_pool, consent_org, alice, bob, shared_agent, code_review_skill
    ):
        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )

        pending = await repo.list_pending_capability_consents(org_id, str(bob["id"]))
        count = await repo.count_pending_capability_consents(org_id, str(bob["id"]))
        assert len(pending) == 1
        assert count == 1

        await repo.upsert_capability_consent(
            org_id=org_id,
            agent_id=agent_id,
            user_id=str(bob["id"]),
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            state="declined",
        )
        assert await repo.count_pending_capability_consents(org_id, str(bob["id"])) == 0

    async def test_attacher_never_sees_their_own_attach_as_pending(
        self, db_pool, consent_org, alice, shared_agent, code_review_skill
    ):
        org_id = str(consent_org["id"])
        repo = DefinitionRepository(db_pool)
        await repo.grant_capability(
            agent_id=str(shared_agent["id"]),
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )
        assert await repo.count_pending_capability_consents(org_id, str(alice["id"])) == 0

    async def test_pending_listing_does_not_disclose_unreadable_capabilities(
        self, db_pool, consent_org, alice, bob, shared_agent, pep_talk_skill
    ):
        """An unreadable capability must not appear in the approvals panel.

        Showing it would leak its name and description, and would ask Bob to
        approve something that can never take effect for him (the runtime
        resolver skips it entirely).
        """
        org_id = str(consent_org["id"])
        repo = DefinitionRepository(db_pool)
        await repo.grant_capability(
            agent_id=str(shared_agent["id"]),
            capability_type="skill",
            capability_id=str(pep_talk_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )

        pending = await repo.list_pending_capability_consents(org_id, str(bob["id"]))
        names = {item["capability_name"] for item in pending}
        assert "Pep Talk" not in names
        assert await repo.count_pending_capability_consents(org_id, str(bob["id"])) == 0

    async def test_pending_listing_matches_runtime_pending(
        self, db_pool, consent_org, alice, bob, shared_agent, code_review_skill
    ):
        """The panel and the resolver must agree on what is pending."""
        from lucent.agent_capabilities import resolve_agent_capabilities

        org_id = str(consent_org["id"])
        agent_id = str(shared_agent["id"])
        repo = DefinitionRepository(db_pool)
        await repo.grant_capability(
            agent_id=agent_id,
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            org_id=org_id,
            actor_user_id=str(alice["id"]),
        )

        listed = {
            item["capability_id"]
            for item in await repo.list_pending_capability_consents(org_id, str(bob["id"]))
            if str(item["agent_id"]) == agent_id
        }
        view = await resolve_agent_capabilities(
            pool=db_pool,
            user={"id": str(bob["id"]), "organization_id": org_id},
            agent_id=agent_id,
            org_id=org_id,
        )
        assert listed == {item["capability_id"] for item in view.pending}


# ── Consent cannot be recorded for a detached capability ─────────────────


class TestConsentRequiresAttachment:
    async def test_consent_for_unattached_capability_is_refused(
        self, db_pool, consent_org, bob, shared_agent, code_review_skill
    ):
        org_id = str(consent_org["id"])
        repo = DefinitionRepository(db_pool)
        recorded = await repo.upsert_capability_consent(
            org_id=org_id,
            agent_id=str(shared_agent["id"]),
            user_id=str(bob["id"]),
            capability_type="skill",
            capability_id=str(code_review_skill["id"]),
            state="approved",
        )
        assert recorded is False

    async def test_invalid_state_is_rejected(
        self, db_pool, consent_org, bob, shared_agent, code_review_skill
    ):
        org_id = str(consent_org["id"])
        repo = DefinitionRepository(db_pool)
        with pytest.raises(ValueError):
            await repo.upsert_capability_consent(
                org_id=org_id,
                agent_id=str(shared_agent["id"]),
                user_id=str(bob["id"]),
                capability_type="skill",
                capability_id=str(code_review_skill["id"]),
                state="maybe",
            )


# ── Authorized-pool requirement ──────────────────────────────────────────


class TestAccessibleCapabilityRequiresAuthorizedPool:
    async def test_plain_pool_fails_closed(self, db_pool, consent_org):
        repo = DefinitionRepository(db_pool)
        with pytest.raises(TypeError):
            await repo.get_accessible_capability_ids("skill", str(consent_org["id"]))

    async def test_authorized_pool_returns_only_clearances(
        self, db_pool, consent_org, bob, code_review_skill, pep_talk_skill
    ):
        pool = await get_authorized_definitions_pool(
            db_pool,
            {"id": str(bob["id"]), "organization_id": str(consent_org["id"])},
        )
        ids = await DefinitionRepository(pool).get_accessible_capability_ids(
            "skill", str(consent_org["id"])
        )
        assert str(code_review_skill["id"]) in ids
        assert str(pep_talk_skill["id"]) not in ids
