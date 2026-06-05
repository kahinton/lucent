"""Tests for db/groups.py — GroupRepository.

Covers: group CRUD, membership management, cross-org isolation.
"""

import pytest
import pytest_asyncio

from lucent.db.groups import GroupRepository


@pytest_asyncio.fixture
async def repo(db_pool):
    return GroupRepository(db_pool)


@pytest_asyncio.fixture
async def group(repo, test_organization):
    """Create a test group."""
    return await repo.create_group(
        name="Engineering",
        org_id=str(test_organization["id"]),
        description="Engineering team",
    )


@pytest_asyncio.fixture
async def second_user(db_pool, test_organization, clean_test_data):
    """Create a second test user."""
    from lucent.db import UserRepository

    prefix = clean_test_data
    user_repo = UserRepository(db_pool)
    return await user_repo.create(
        external_id=f"{prefix}user2",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{prefix}user2@test.com",
        display_name=f"{prefix}Second User",
    )


@pytest_asyncio.fixture
async def other_org(db_pool, clean_test_data):
    """Create a second organization for cross-org isolation tests."""
    from lucent.db import OrganizationRepository

    prefix = clean_test_data
    repo = OrganizationRepository(db_pool)
    return await repo.create(name=f"{prefix}other_org")


@pytest_asyncio.fixture(autouse=True)
async def cleanup_groups(db_pool, test_organization, clean_test_data):
    """Clean up groups data after each test."""
    yield
    prefix = clean_test_data
    async with db_pool.acquire() as conn:
        # user_groups cascade from groups
        await conn.execute(
            "DELETE FROM groups WHERE organization_id = $1",
            test_organization["id"],
        )
        # Also clean up any other-org groups
        other_orgs = await conn.fetch(
            "SELECT id FROM organizations WHERE name LIKE $1 AND id != $2",
            f"{prefix}%",
            test_organization["id"],
        )
        for org in other_orgs:
            await conn.execute("DELETE FROM groups WHERE organization_id = $1", org["id"])


# ── Group CRUD ────────────────────────────────────────────────────────────


class TestCreateGroup:
    @pytest.mark.asyncio
    async def test_create_basic(self, repo, test_organization):
        g = await repo.create_group(
            name="Backend",
            org_id=str(test_organization["id"]),
        )
        assert g["name"] == "Backend"
        assert g["description"] == ""
        assert g["organization_id"] == test_organization["id"]
        assert g["id"] is not None

    @pytest.mark.asyncio
    async def test_create_with_description(self, repo, test_organization):
        g = await repo.create_group(
            name="Frontend",
            org_id=str(test_organization["id"]),
            description="Frontend team",
        )
        assert g["description"] == "Frontend team"

    @pytest.mark.asyncio
    async def test_create_with_created_by(self, repo, test_organization, test_user):
        g = await repo.create_group(
            name="DevOps",
            org_id=str(test_organization["id"]),
            created_by=str(test_user["id"]),
        )
        assert g["created_by"] == test_user["id"]

    @pytest.mark.asyncio
    async def test_duplicate_name_same_org_fails(self, repo, test_organization, group):
        with pytest.raises(Exception):  # asyncpg.UniqueViolationError
            await repo.create_group(
                name="Engineering",
                org_id=str(test_organization["id"]),
            )

    @pytest.mark.asyncio
    async def test_same_name_different_org_ok(self, repo, test_organization, other_org, group):
        g = await repo.create_group(
            name="Engineering",
            org_id=str(other_org["id"]),
        )
        assert g["name"] == "Engineering"
        assert g["organization_id"] == other_org["id"]


class TestGetGroup:
    @pytest.mark.asyncio
    async def test_get_existing(self, repo, test_organization, group):
        result = await repo.get_group(str(group["id"]), str(test_organization["id"]))
        assert result is not None
        assert result["name"] == "Engineering"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repo, test_organization):
        result = await repo.get_group(
            "00000000-0000-0000-0000-000000000000",
            str(test_organization["id"]),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_get_wrong_org(self, repo, other_org, group):
        """Group from org A is not visible to org B."""
        result = await repo.get_group(str(group["id"]), str(other_org["id"]))
        assert result is None


class TestListGroups:
    @pytest.mark.asyncio
    async def test_list_empty(self, repo, test_organization):
        result = await repo.list_groups(str(test_organization["id"]))
        assert result["items"] == []
        assert result["total_count"] == 0

    @pytest.mark.asyncio
    async def test_list_with_groups(self, repo, test_organization, group):
        await repo.create_group(name="Design", org_id=str(test_organization["id"]))
        result = await repo.list_groups(str(test_organization["id"]))
        assert result["total_count"] == 2
        # Ordered by name
        assert result["items"][0]["name"] == "Design"
        assert result["items"][1]["name"] == "Engineering"

    @pytest.mark.asyncio
    async def test_list_pagination(self, repo, test_organization, group):
        await repo.create_group(name="Design", org_id=str(test_organization["id"]))
        await repo.create_group(name="QA", org_id=str(test_organization["id"]))
        result = await repo.list_groups(str(test_organization["id"]), limit=2, offset=0)
        assert len(result["items"]) == 2
        assert result["has_more"] is True
        result2 = await repo.list_groups(str(test_organization["id"]), limit=2, offset=2)
        assert len(result2["items"]) == 1
        assert result2["has_more"] is False


class TestUpdateGroup:
    @pytest.mark.asyncio
    async def test_update_name(self, repo, test_organization, group):
        updated = await repo.update_group(
            str(group["id"]), str(test_organization["id"]), name="Platform"
        )
        assert updated is not None
        assert updated["name"] == "Platform"

    @pytest.mark.asyncio
    async def test_update_description(self, repo, test_organization, group):
        updated = await repo.update_group(
            str(group["id"]), str(test_organization["id"]), description="New desc"
        )
        assert updated["description"] == "New desc"

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, repo, test_organization):
        result = await repo.update_group(
            "00000000-0000-0000-0000-000000000000",
            str(test_organization["id"]),
            name="Ghost",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_update_no_fields(self, repo, test_organization, group):
        result = await repo.update_group(str(group["id"]), str(test_organization["id"]))
        assert result is not None
        assert result["name"] == "Engineering"


class TestDeleteGroup:
    @pytest.mark.asyncio
    async def test_delete_existing(self, repo, test_organization, group):
        deleted = await repo.delete_group(str(group["id"]), str(test_organization["id"]))
        assert deleted is True
        assert await repo.get_group(str(group["id"]), str(test_organization["id"])) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, repo, test_organization):
        deleted = await repo.delete_group(
            "00000000-0000-0000-0000-000000000000",
            str(test_organization["id"]),
        )
        assert deleted is False

    @pytest.mark.asyncio
    async def test_delete_cascades_membership(self, repo, test_organization, group, test_user):
        await repo.add_member(str(group["id"]), str(test_user["id"]))
        await repo.delete_group(str(group["id"]), str(test_organization["id"]))
        groups = await repo.get_user_groups(str(test_user["id"]), str(test_organization["id"]))
        assert len(groups) == 0


# ── Membership ────────────────────────────────────────────────────────────


class TestAddMember:
    @pytest.mark.asyncio
    async def test_add_member(self, repo, group, test_user):
        m = await repo.add_member(str(group["id"]), str(test_user["id"]))
        assert m["user_id"] == test_user["id"]
        assert m["group_id"] == group["id"]
        assert m["role"] == "member"

    @pytest.mark.asyncio
    async def test_add_admin(self, repo, group, test_user):
        m = await repo.add_member(str(group["id"]), str(test_user["id"]), role="admin")
        assert m["role"] == "admin"

    @pytest.mark.asyncio
    async def test_add_invalid_role(self, repo, group, test_user):
        with pytest.raises(ValueError, match="Invalid role"):
            await repo.add_member(str(group["id"]), str(test_user["id"]), role="superadmin")

    @pytest.mark.asyncio
    async def test_add_duplicate_fails(self, repo, group, test_user):
        await repo.add_member(str(group["id"]), str(test_user["id"]))
        with pytest.raises(Exception):  # asyncpg.UniqueViolationError
            await repo.add_member(str(group["id"]), str(test_user["id"]))


class TestRemoveMember:
    @pytest.mark.asyncio
    async def test_remove_member(self, repo, group, test_user):
        await repo.add_member(str(group["id"]), str(test_user["id"]))
        removed = await repo.remove_member(str(group["id"]), str(test_user["id"]))
        assert removed is True

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self, repo, group, test_user):
        removed = await repo.remove_member(str(group["id"]), str(test_user["id"]))
        assert removed is False


class TestUpdateMemberRole:
    @pytest.mark.asyncio
    async def test_promote_to_admin(self, repo, group, test_user):
        await repo.add_member(str(group["id"]), str(test_user["id"]))
        updated = await repo.update_member_role(
            str(group["id"]), str(test_user["id"]), "admin"
        )
        assert updated is not None
        assert updated["role"] == "admin"

    @pytest.mark.asyncio
    async def test_demote_to_member(self, repo, group, test_user):
        await repo.add_member(str(group["id"]), str(test_user["id"]), role="admin")
        updated = await repo.update_member_role(
            str(group["id"]), str(test_user["id"]), "member"
        )
        assert updated["role"] == "member"

    @pytest.mark.asyncio
    async def test_update_invalid_role(self, repo, group, test_user):
        await repo.add_member(str(group["id"]), str(test_user["id"]))
        with pytest.raises(ValueError, match="Invalid role"):
            await repo.update_member_role(str(group["id"]), str(test_user["id"]), "owner")


class TestListMembers:
    @pytest.mark.asyncio
    async def test_list_members(self, repo, test_organization, group, test_user, second_user):
        await repo.add_member(str(group["id"]), str(test_user["id"]))
        await repo.add_member(str(group["id"]), str(second_user["id"]), role="admin")
        members = await repo.list_members(str(group["id"]), str(test_organization["id"]))
        assert len(members) == 2
        # Verify user details are included
        assert all("display_name" in m for m in members)
        assert all("email" in m for m in members)

    @pytest.mark.asyncio
    async def test_list_members_wrong_org(self, repo, other_org, group):
        members = await repo.list_members(str(group["id"]), str(other_org["id"]))
        assert members == []


class TestGetUserGroups:
    @pytest.mark.asyncio
    async def test_user_groups(self, repo, test_organization, test_user):
        g1 = await repo.create_group(name="Alpha", org_id=str(test_organization["id"]))
        g2 = await repo.create_group(name="Beta", org_id=str(test_organization["id"]))
        await repo.add_member(str(g1["id"]), str(test_user["id"]))
        await repo.add_member(str(g2["id"]), str(test_user["id"]), role="admin")
        groups = await repo.get_user_groups(str(test_user["id"]), str(test_organization["id"]))
        assert len(groups) == 2
        names = [g["name"] for g in groups]
        assert "Alpha" in names
        assert "Beta" in names

    @pytest.mark.asyncio
    async def test_user_no_groups(self, repo, test_organization, test_user):
        groups = await repo.get_user_groups(str(test_user["id"]), str(test_organization["id"]))
        assert groups == []


class TestMembershipChecks:
    @pytest.mark.asyncio
    async def test_is_member(self, repo, group, test_user):
        assert await repo.is_member(str(test_user["id"]), str(group["id"])) is False
        await repo.add_member(str(group["id"]), str(test_user["id"]))
        assert await repo.is_member(str(test_user["id"]), str(group["id"])) is True

    @pytest.mark.asyncio
    async def test_is_group_admin(self, repo, group, test_user):
        await repo.add_member(str(group["id"]), str(test_user["id"]))
        assert await repo.is_group_admin(str(test_user["id"]), str(group["id"])) is False
        await repo.update_member_role(str(group["id"]), str(test_user["id"]), "admin")
        assert await repo.is_group_admin(str(test_user["id"]), str(group["id"])) is True

    @pytest.mark.asyncio
    async def test_get_user_group_ids(self, repo, test_organization, test_user):
        g1 = await repo.create_group(name="Team1", org_id=str(test_organization["id"]))
        g2 = await repo.create_group(name="Team2", org_id=str(test_organization["id"]))
        await repo.add_member(str(g1["id"]), str(test_user["id"]))
        await repo.add_member(str(g2["id"]), str(test_user["id"]))
        ids = await repo.get_user_group_ids(str(test_user["id"]))
        assert len(ids) == 2
        assert str(g1["id"]) in ids
        assert str(g2["id"]) in ids
