"""The chat composer's context injection reads the caller's own view.

The composer context used to call the nonexistent ``list_memories`` (the
block silently never loaded) with org-wide intent. It now reads through the
same clearance-driven ``MemoryAccessService.search`` the dashboard composer
uses: every caller — admins included — sees exactly their own view (own +
granted + org-shared memories). The model must never see more than the human
driving the chat.
"""

from uuid import uuid4

import pytest
import pytest_asyncio

from lucent.api.routers import chat
from lucent.db import (
    MemoryRepository,
    OrganizationRepository,
    UserRepository,
)


@pytest_asyncio.fixture
async def chat_ctx(db_pool):
    """Org + two members + a shared and a private memory."""
    prefix = f"test_chatctx_{str(uuid4())[:8]}_"
    org_repo = OrganizationRepository(db_pool)
    user_repo = UserRepository(db_pool)
    org = await org_repo.create(name=f"{prefix}org")
    member = await user_repo.create(
        external_id=f"{prefix}member",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}member@test.com",
        display_name="ChatCtx Member",
    )
    other = await user_repo.create(
        external_id=f"{prefix}other",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}other@test.com",
        display_name="ChatCtx Other",
    )
    repo = MemoryRepository(db_pool)
    visible = await repo.create(
        username=f"{prefix}visible",
        type="experience",
        content=f"{prefix} member sees this latest memory",
        tags=["chatctx-test"],
        importance=5,
        user_id=member["id"],
        organization_id=org["id"],
    )
    return {"prefix": prefix, "org": org, "member": member, "other": other,
            "repo": repo, "visible": visible}


@pytest.mark.asyncio
async def test_composer_prompt_includes_callers_accessible_memories(
    db_pool, chat_ctx
):
    """The recent-memories block actually loads and shows the member's own
    memory (and no one else's private one)."""
    ctx = chat_ctx
    repo = ctx["repo"]
    private = await repo.create(
        username=f"{ctx['prefix']}priv",
        type="experience",
        content=f"{ctx['prefix']} other member private memory",
        tags=["chatctx-test"],
        importance=9,
        user_id=ctx["other"]["id"],
        organization_id=ctx["org"]["id"],
    )
    try:
        user = dict(ctx["member"])
        user["role"] = "member"
        prompt = await chat._build_system_prompt(user, db_pool, None)
        assert f"{ctx['prefix']} member sees this latest memory" in prompt
        assert f"{ctx['prefix']} other member private memory" not in prompt
        assert "Recent Memories" in prompt
    finally:
        await repo.delete(private["id"], organization_id=ctx["org"]["id"])


@pytest.mark.asyncio
async def test_composer_prompt_shows_org_shared_other_member_memory(db_pool, chat_ctx):
    """Another member's memory reaches a caller's prompt once it is shared
    with the org — the prompt is tested through the real ``_build_system_prompt``
    call with an admin caller (``is_admin`` does not widen the search; the
    dashboard composer's precedent gives every caller their clearance view)."""
    ctx = chat_ctx
    repo = ctx["repo"]
    shared = await repo.create(
        username=f"{ctx['prefix']}shared",
        type="experience",
        content=f"{ctx['prefix']} other member org-shared memory",
        tags=["chatctx-test"],
        importance=9,
        user_id=ctx["other"]["id"],
        organization_id=ctx["org"]["id"],
    )
    try:
        # The other member shares it org-wide (their decision as owner).
        await repo.set_shared(
            shared["id"],
            user_id=ctx["other"]["id"],
            shared=True,
            organization_id=ctx["org"]["id"],
        )
        user = dict(ctx["member"])
        user["role"] = "admin"
        prompt = await chat._build_system_prompt(user, db_pool, None)
        assert f"{ctx['prefix']} other member org-shared memory" in prompt
    finally:
        await repo.delete(shared["id"], organization_id=ctx["org"]["id"])


@pytest.mark.asyncio
async def test_composer_prompt_does_not_crash_without_org(db_pool, chat_ctx):
    """A user with no org degrades gracefully (no memories block added)."""
    user = {"id": str(uuid4()), "organization_id": None, "role": "member",
            "display_name": "ChatCtx Orgless"}
    prompt = await chat._build_system_prompt(user, db_pool, None)
    assert "Recent Memories" not in prompt
