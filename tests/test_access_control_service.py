"""Tests for AccessControlService resolution and immediate membership revocation."""

from uuid import uuid4

import pytest
import pytest_asyncio

from lucent.access_control import AccessControlService
from lucent.db import UserRepository
from lucent.db.definitions import DefinitionRepository
from lucent.db.groups import GroupRepository
from lucent.db.models import ModelRepository


@pytest_asyncio.fixture
async def acl_prefix():
    test_id = str(uuid4())[:8]
    return f"test_acl_{test_id}_"


@pytest_asyncio.fixture
async def acl_users(db_pool, test_organization, acl_prefix):
    user_repo = UserRepository(db_pool)
    org_id = test_organization["id"]
    user1 = await user_repo.create(
        external_id=f"{acl_prefix}user1",
        provider="local",
        organization_id=org_id,
        email=f"{acl_prefix}user1@test.com",
        display_name=f"{acl_prefix}User 1",
        role="member",
    )
    user2 = await user_repo.create(
        external_id=f"{acl_prefix}user2",
        provider="local",
        organization_id=org_id,
        email=f"{acl_prefix}user2@test.com",
        display_name=f"{acl_prefix}User 2",
        role="member",
    )
    admin = await user_repo.create(
        external_id=f"{acl_prefix}admin",
        provider="local",
        organization_id=org_id,
        email=f"{acl_prefix}admin@test.com",
        display_name=f"{acl_prefix}Admin",
        role="admin",
    )
    owner = await user_repo.create(
        external_id=f"{acl_prefix}owner",
        provider="local",
        organization_id=org_id,
        email=f"{acl_prefix}owner@test.com",
        display_name=f"{acl_prefix}Owner",
        role="owner",
    )
    return {"user1": user1, "user2": user2, "admin": admin, "owner": owner}


@pytest_asyncio.fixture(autouse=True)
async def cleanup_acl_data(db_pool, test_organization, acl_prefix):
    yield
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM models WHERE id LIKE $1", f"{acl_prefix}%")
        await conn.execute("DELETE FROM agent_definitions WHERE name LIKE $1", f"{acl_prefix}%")
        await conn.execute(
            "DELETE FROM user_groups WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{acl_prefix}%",
        )
        await conn.execute("DELETE FROM groups WHERE organization_id = $1", test_organization["id"])
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{acl_prefix}%")


@pytest.mark.asyncio
async def test_access_control_service_resolution(db_pool, test_organization, acl_users, acl_prefix):
    org_id = str(test_organization["id"])
    user1_id = str(acl_users["user1"]["id"])
    user2_id = str(acl_users["user2"]["id"])
    admin_id = str(acl_users["admin"]["id"])
    owner_id = str(acl_users["owner"]["id"])

    def_repo = DefinitionRepository(db_pool)
    group_repo = GroupRepository(db_pool)
    acl = AccessControlService(db_pool)

    own_agent = await def_repo.create_agent(
        name=f"{acl_prefix}own_agent",
        description="own",
        content="# own",
        org_id=org_id,
        created_by=user1_id,
        owner_user_id=user1_id,
    )
    group = await group_repo.create_group(
        name=f"{acl_prefix}eng",
        org_id=org_id,
        created_by=user1_id,
    )
    await group_repo.add_member(str(group["id"]), user2_id)
    group_agent = await def_repo.create_agent(
        name=f"{acl_prefix}group_agent",
        description="group",
        content="# group",
        org_id=org_id,
        created_by=user1_id,
        owner_group_id=str(group["id"]),
    )
    other_group = await group_repo.create_group(
        name=f"{acl_prefix}othergrp", org_id=org_id, created_by=user1_id
    )
    other_group_agent = await def_repo.create_agent(
        name=f"{acl_prefix}other_group_agent",
        description="other-group",
        content="# other-group",
        org_id=org_id,
        created_by=user1_id,
        owner_group_id=str(other_group["id"]),
    )
    async with db_pool.acquire() as conn:
        built_in = await conn.fetchrow(
            """
            INSERT INTO agent_definitions (name, content, status, scope, organization_id)
            VALUES ($1, '# built-in', 'active', 'built-in', $2)
            RETURNING id
            """,
            f"{acl_prefix}builtin_agent",
            test_organization["id"],
        )

    assert await acl.can_access(user1_id, "agent", str(own_agent["id"]), org_id) is True
    assert await acl.can_access(user2_id, "agent", str(group_agent["id"]), org_id) is True
    assert await acl.can_access(user2_id, "agent", str(own_agent["id"]), org_id) is False
    assert await acl.can_access(user2_id, "agent", str(other_group_agent["id"]), org_id) is False
    assert await acl.can_access(admin_id, "agent", str(own_agent["id"]), org_id) is True
    assert await acl.can_access(owner_id, "agent", str(own_agent["id"]), org_id) is True
    assert await acl.can_access(user2_id, "agent", str(built_in["id"]), org_id) is True

    org_shared_agent = await def_repo.create_agent(
        name=f"{acl_prefix}org_shared_agent",
        description="org shared",
        content="# org shared",
        org_id=org_id,
        created_by=user1_id,
        shared_with_org=True,
    )
    assert org_shared_agent["owner_user_id"] is None
    assert org_shared_agent["owner_group_id"] is None
    assert await acl.can_access(user2_id, "agent", str(org_shared_agent["id"]), org_id) is True
    assert await acl.can_modify(user2_id, "agent", str(org_shared_agent["id"]), org_id) is False
    assert await acl.can_modify(admin_id, "agent", str(org_shared_agent["id"]), org_id) is True

    accessible_user2 = await acl.list_accessible(user2_id, "agent", org_id)
    assert str(group_agent["id"]) in accessible_user2
    assert str(own_agent["id"]) not in accessible_user2
    assert str(built_in["id"]) in accessible_user2
    assert str(org_shared_agent["id"]) in accessible_user2

    await group_repo.remove_member(str(group["id"]), user2_id)
    assert await acl.can_access(user2_id, "agent", str(group_agent["id"]), org_id) is False


@pytest.mark.asyncio
async def test_model_access_control_resolution(
    db_pool, test_organization, acl_users, acl_prefix
):
    org_id = str(test_organization["id"])
    user1_id = str(acl_users["user1"]["id"])
    user2_id = str(acl_users["user2"]["id"])
    admin_id = str(acl_users["admin"]["id"])
    group_repo = GroupRepository(db_pool)
    model_repo = ModelRepository(db_pool)
    acl = AccessControlService(db_pool)

    group = await group_repo.create_group(
        name=f"{acl_prefix}models", org_id=org_id, created_by=user1_id
    )
    await group_repo.add_member(str(group["id"]), user2_id)
    private_model = await model_repo.create_model(
        model_id=f"{acl_prefix}private",
        provider="test",
        name="Private model",
        org_id=org_id,
        owner_user_id=user1_id,
    )
    group_model = await model_repo.create_model(
        model_id=f"{acl_prefix}group",
        provider="test",
        name="Group model",
        org_id=org_id,
        owner_group_id=str(group["id"]),
    )
    shared_model = await model_repo.create_model(
        model_id=f"{acl_prefix}shared",
        provider="test",
        name="Shared model",
        org_id=org_id,
    )

    assert await acl.can_access(user1_id, "model", private_model["id"], org_id) is True
    assert await acl.can_access(user2_id, "model", private_model["id"], org_id) is False
    assert await acl.can_access(user2_id, "model", group_model["id"], org_id) is True
    assert await acl.can_access(user2_id, "model", shared_model["id"], org_id) is True
    assert await acl.can_access(admin_id, "model", private_model["id"], org_id) is True
    assert await acl.can_modify(user2_id, "model", group_model["id"], org_id) is False
    assert await acl.can_modify(admin_id, "model", private_model["id"], org_id) is True

    accessible = await acl.list_accessible(user2_id, "model", org_id)
    assert private_model["id"] not in accessible
    assert group_model["id"] in accessible
    assert shared_model["id"] in accessible

    await group_repo.remove_member(str(group["id"]), user2_id)
    assert await acl.can_access(user2_id, "model", group_model["id"], org_id) is False


@pytest.mark.asyncio
async def test_daemon_created_definitions_default_to_org_shared(
    db_pool, test_organization, acl_prefix
):
    org_id = str(test_organization["id"])
    user_repo = UserRepository(db_pool)
    daemon = await user_repo.create(
        external_id=f"{acl_prefix}daemon",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{acl_prefix}daemon@test.com",
        display_name=f"{acl_prefix}Daemon",
        role="daemon",
    )
    member = await user_repo.create(
        external_id=f"{acl_prefix}member",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{acl_prefix}member@test.com",
        display_name=f"{acl_prefix}Member",
        role="member",
    )

    def_repo = DefinitionRepository(db_pool)
    agent = await def_repo.create_agent(
        name=f"{acl_prefix}daemon_agent",
        description="daemon-created",
        content="# daemon-created",
        org_id=org_id,
        created_by=str(daemon["id"]),
    )

    assert agent["owner_user_id"] is None
    assert agent["owner_group_id"] is None
    assert await AccessControlService(db_pool).can_access(
        str(member["id"]), "agent", str(agent["id"]), org_id
    )
