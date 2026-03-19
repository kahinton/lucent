"""Tests for database repositories."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from lucent.db import (
    AccessRepository,
    ApiKeyRepository,
    AuditRepository,
    MemoryRepository,
    OrganizationRepository,
    UserRepository,
)


class TestMemoryRepository:
    """Tests for MemoryRepository."""

    async def test_create_memory(self, db_pool, test_user, clean_test_data):
        """Test creating a basic memory."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        memory = await repo.create(
            username=f"{prefix}user",
            type="experience",
            content="Test memory content",
            tags=["test"],
            importance=7,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        assert memory["id"] is not None
        assert isinstance(memory["id"], UUID)
        assert memory["username"] == f"{prefix}user"
        assert memory["type"] == "experience"
        assert memory["content"] == "Test memory content"
        assert memory["tags"] == ["test"]
        assert memory["importance"] == 7
        assert memory["deleted_at"] is None

    async def test_create_memory_with_metadata(self, db_pool, test_user, clean_test_data):
        """Test creating a memory with metadata."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        memory = await repo.create(
            username=f"{prefix}user",
            type="technical",
            content="Code pattern example",
            metadata={"language": "python", "repo": "lucent"},
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        assert memory["metadata"]["language"] == "python"
        assert memory["metadata"]["repo"] == "lucent"

    async def test_get_memory(self, db_pool, test_memory):
        """Test retrieving a memory by ID."""
        repo = MemoryRepository(db_pool)

        memory = await repo.get(test_memory["id"])

        assert memory is not None
        assert memory["id"] == test_memory["id"]
        assert memory["content"] == test_memory["content"]

    async def test_get_nonexistent_memory(self, db_pool):
        """Test retrieving a memory that doesn't exist."""
        repo = MemoryRepository(db_pool)

        memory = await repo.get(uuid4())

        assert memory is None

    async def test_get_accessible_own_memory(self, db_pool, test_memory, test_user):
        """Test that user can access their own memory."""
        repo = MemoryRepository(db_pool)

        memory = await repo.get_accessible(
            test_memory["id"],
            test_user["id"],
            test_user["organization_id"],
        )

        assert memory is not None
        assert memory["id"] == test_memory["id"]

    async def test_get_accessible_shared_memory(
        self, db_pool, test_user, test_organization, clean_test_data
    ):
        """Test that user can access shared org memories."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)
        user_repo = UserRepository(db_pool)

        # Create a second user in the same org
        other_user = await user_repo.create(
            external_id=f"{prefix}other",
            provider="local",
            organization_id=test_organization["id"],
            email=f"{prefix}other@test.com",
            display_name=f"{prefix}Other User",
        )

        # Create a memory from the other user
        memory = await repo.create(
            username=f"{prefix}other_user",
            type="experience",
            content="Shared content",
            user_id=other_user["id"],
            organization_id=test_organization["id"],
        )

        # Share it using the set_shared method
        await repo.set_shared(memory["id"], other_user["id"], shared=True)

        # Should be accessible to test_user in same org
        accessible = await repo.get_accessible(
            memory["id"],
            test_user["id"],
            test_user["organization_id"],
        )

        assert accessible is not None

    async def test_update_memory(self, db_pool, test_memory):
        """Test updating a memory."""
        repo = MemoryRepository(db_pool)

        updated = await repo.update(
            test_memory["id"],
            content="Updated content",
            importance=9,
        )

        assert updated is not None
        assert updated["content"] == "Updated content"
        assert updated["importance"] == 9
        assert updated["updated_at"] > test_memory["updated_at"]

    async def test_soft_delete_memory(self, db_pool, test_memory):
        """Test soft deleting a memory."""
        repo = MemoryRepository(db_pool)

        deleted = await repo.delete(test_memory["id"])

        assert deleted is True

        # Should not be retrievable via get
        retrieved = await repo.get(test_memory["id"])
        assert retrieved is None

    async def test_search_memories(self, db_pool, test_user, clean_test_data):
        """Test searching memories."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        # Create searchable memories
        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Python async programming patterns",
            tags=["python", "async"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        result = await repo.search(
            query="Python async",
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        assert result["total_count"] >= 1
        assert any("Python" in m["content"] for m in result["memories"])

    async def test_search_with_type_filter(self, db_pool, test_user, clean_test_data):
        """Test searching with type filter."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        # Create memories of different types
        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Experience content",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        await repo.create(
            username=f"{prefix}user",
            type="technical",
            content=f"{prefix} Technical content",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        result = await repo.search(
            query=prefix,
            type="technical",
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        assert all(m["type"] == "technical" for m in result["memories"])

    async def test_search_with_tag_filter(self, db_pool, test_user, clean_test_data):
        """Test searching with tag filter uses containment (all tags must match)."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        # Create memories with different tag combinations
        m1 = await repo.create(
            username=f"{prefix}user",
            type="procedural",
            content=f"{prefix} Investigate auth rate limiting",
            tags=["daemon-task", "pending", "code"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        m2 = await repo.create(
            username=f"{prefix}user",
            type="procedural",
            content=f"{prefix} Memory consolidation routine",
            tags=["daemon-task", "completed", "maintenance"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        m3 = await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Self-observation notes",
            tags=["daemon", "self-observation"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        # Single tag filter
        result = await repo.search(
            tags=["daemon-task"],
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )
        ids = {m["id"] for m in result["memories"]}
        assert m1["id"] in ids
        assert m2["id"] in ids
        assert m3["id"] not in ids

        # Multi-tag filter requires ALL tags (containment)
        result = await repo.search(
            tags=["daemon-task", "pending"],
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )
        assert result["total_count"] == 1
        assert result["memories"][0]["id"] == m1["id"]

        # Tag that doesn't exist
        result = await repo.search(
            tags=["nonexistent-tag"],
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )
        assert result["total_count"] == 0

    async def test_search_full(self, db_pool, test_user, clean_test_data):
        """Test full-text search across content, tags, and metadata."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        await repo.create(
            username=f"{prefix}user",
            type="technical",
            content=f"{prefix} Generic content",
            tags=["unique-tag-xyz"],
            metadata={"repo": "lucent"},
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        # Search by tag content
        result = await repo.search_full(
            query="unique-tag-xyz",
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        assert result["total_count"] >= 1
        assert any("unique-tag-xyz" in m["tags"] for m in result["memories"])

    async def test_get_existing_tags(self, db_pool, test_user, clean_test_data):
        """Test retrieving existing tags with counts."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        # Create memories with known tags
        for i in range(3):
            await repo.create(
                username=f"{prefix}user",
                type="experience",
                content=f"{prefix} Memory {i}",
                tags=[f"{prefix}common", f"{prefix}tag{i}"],
                user_id=test_user["id"],
                organization_id=test_user["organization_id"],
            )

        tags = await repo.get_existing_tags(
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        tag_names = [t["tag"] for t in tags]
        assert f"{prefix}common" in tag_names

        # The common tag should have count 3
        common_tag = next(t for t in tags if t["tag"] == f"{prefix}common")
        assert common_tag["count"] == 3

    async def test_get_individual_memory_for_user(
        self, db_pool, test_user, test_organization, clean_test_data
    ):
        """Test retrieving a user's individual memory."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        # Create an individual memory for the user
        await repo.create(
            username=f"{prefix}user",
            type="individual",
            content=f"{prefix} Individual memory for user",
            user_id=test_user["id"],
            organization_id=test_organization["id"],
        )

        result = await repo.get_individual_memory_for_user(test_user["id"])

        assert result is not None
        assert result["type"] == "individual"
        assert result["user_id"] == test_user["id"]

    async def test_delete_nonexistent_memory(self, db_pool):
        """Test deleting a memory that doesn't exist returns False."""
        repo = MemoryRepository(db_pool)

        result = await repo.delete(uuid4())

        assert result is False

    async def test_update_no_changes(self, db_pool, test_memory):
        """Test update with no fields returns existing memory unchanged."""
        repo = MemoryRepository(db_pool)

        result = await repo.update(test_memory["id"])

        assert result is not None
        assert result["content"] == test_memory["content"]
        assert result["importance"] == test_memory["importance"]

    async def test_update_increments_version(self, db_pool, test_memory):
        """Test that update increments the version number."""
        repo = MemoryRepository(db_pool)

        original_version = test_memory["version"]

        updated = await repo.update(
            test_memory["id"],
            content="Version bump test",
        )

        assert updated is not None
        assert updated["version"] == original_version + 1

    async def test_get_accessible_denies_other_org(
        self, db_pool, test_memory, test_user, clean_test_data
    ):
        """Test that a user in a different org cannot access an unshared memory."""
        _ = clean_test_data
        repo = MemoryRepository(db_pool)

        # Use a random UUID as the "other org" user — should not match
        other_user_id = uuid4()
        other_org_id = uuid4()

        result = await repo.get_accessible(
            test_memory["id"],
            other_user_id,
            other_org_id,
        )

        assert result is None

    async def test_update_with_version_conflict(self, db_pool, test_memory):
        """Test that expected_version mismatch raises VersionConflictError."""
        from lucent.db import VersionConflictError

        repo = MemoryRepository(db_pool)

        with pytest.raises(VersionConflictError) as exc_info:
            await repo.update(
                test_memory["id"],
                content="Should fail",
                expected_version=999,
            )

        assert exc_info.value.memory_id == test_memory["id"]
        assert exc_info.value.expected_version == 999

    async def test_update_with_correct_version(self, db_pool, test_memory):
        """Test that update succeeds when expected_version matches."""
        repo = MemoryRepository(db_pool)

        updated = await repo.update(
            test_memory["id"],
            content="Version matched",
            expected_version=test_memory["version"],
        )

        assert updated is not None
        assert updated["content"] == "Version matched"
        assert updated["version"] == test_memory["version"] + 1

    async def test_claim_task(self, db_pool, test_user, clean_test_data):
        """Test atomically claiming a pending daemon task."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        task = await repo.create(
            username=f"{prefix}user",
            type="procedural",
            content=f"{prefix} Pending task",
            tags=["daemon-task", "pending"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        claimed = await repo.claim_task(task["id"], "instance-1")

        assert claimed is not None
        assert "pending" not in claimed["tags"]
        assert "claimed-by-instance-1" in claimed["tags"]

    async def test_claim_task_already_claimed(self, db_pool, test_user, clean_test_data):
        """Test that claiming an already-claimed task returns None."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        task = await repo.create(
            username=f"{prefix}user",
            type="procedural",
            content=f"{prefix} Task to double-claim",
            tags=["daemon-task", "pending"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        await repo.claim_task(task["id"], "instance-1")
        second_claim = await repo.claim_task(task["id"], "instance-2")

        assert second_claim is None

    async def test_release_claim(self, db_pool, test_user, clean_test_data):
        """Test releasing a claimed task back to pending."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        task = await repo.create(
            username=f"{prefix}user",
            type="procedural",
            content=f"{prefix} Task to release",
            tags=["daemon-task", "pending"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        await repo.claim_task(task["id"], "instance-1")
        released = await repo.release_claim(task["id"], "instance-1")

        assert released is not None
        assert "pending" in released["tags"]
        assert not any(t.startswith("claimed-by-") for t in released["tags"])

    async def test_release_claim_wrong_instance(self, db_pool, test_user, clean_test_data):
        """Test that releasing with wrong instance_id returns None."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        task = await repo.create(
            username=f"{prefix}user",
            type="procedural",
            content=f"{prefix} Task wrong release",
            tags=["daemon-task", "pending"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        await repo.claim_task(task["id"], "instance-1")
        released = await repo.release_claim(task["id"], "instance-2")

        assert released is None

    async def test_set_shared(self, db_pool, test_memory, test_user):
        """Test toggling the shared flag on a memory."""
        repo = MemoryRepository(db_pool)

        shared = await repo.set_shared(test_memory["id"], test_user["id"], shared=True)
        assert shared is not None
        assert shared["shared"] is True

        unshared = await repo.set_shared(test_memory["id"], test_user["id"], shared=False)
        assert unshared is not None
        assert unshared["shared"] is False

    async def test_set_shared_non_owner_denied(self, db_pool, test_memory):
        """Test that non-owner cannot change shared status."""
        repo = MemoryRepository(db_pool)

        result = await repo.set_shared(test_memory["id"], uuid4(), shared=True)
        assert result is None

    async def test_search_with_importance_filter(self, db_pool, test_user, clean_test_data):
        """Test searching with importance range filters."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Low importance",
            importance=2,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} High importance",
            importance=9,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        result = await repo.search(
            query=prefix,
            importance_min=8,
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        assert all(m["importance"] >= 8 for m in result["memories"])

    async def test_get_tag_suggestions(self, db_pool, test_user, clean_test_data):
        """Test fuzzy tag suggestion lookup."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Tag suggestion test",
            tags=[f"{prefix}authentication", f"{prefix}auth-flow"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        suggestions = await repo.get_tag_suggestions(
            query=f"{prefix}auth",
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        assert len(suggestions) >= 1
        assert all("similarity" in s for s in suggestions)

    async def test_create_with_related_memory_ids(
        self, db_pool, test_user, test_memory, clean_test_data
    ):
        """Test creating a memory with valid related_memory_ids."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        memory = await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Related memory",
            related_memory_ids=[test_memory["id"]],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        assert test_memory["id"] in memory["related_memory_ids"]

    async def test_create_with_invalid_related_ids(self, db_pool, test_user, clean_test_data):
        """Test that creating with nonexistent related IDs raises ValueError."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        with pytest.raises(ValueError, match="not found"):
            await repo.create(
                username=f"{prefix}user",
                type="experience",
                content=f"{prefix} Bad related",
                related_memory_ids=[uuid4()],
                user_id=test_user["id"],
                organization_id=test_user["organization_id"],
            )


class TestUserRepository:
    """Tests for UserRepository."""

    async def test_create_user(self, db_pool, test_organization, clean_test_data):
        """Test creating a user."""
        prefix = clean_test_data
        repo = UserRepository(db_pool)

        user = await repo.create(
            external_id=f"{prefix}newuser",
            provider="github",
            organization_id=test_organization["id"],
            email=f"{prefix}new@test.com",
            display_name=f"{prefix}New User",
        )

        assert user["id"] is not None
        assert user["external_id"] == f"{prefix}newuser"
        assert user["provider"] == "github"
        assert user["email"] == f"{prefix}new@test.com"

    async def test_create_user_creates_individual_memory(
        self, db_pool, test_organization, clean_test_data
    ):
        """Test that creating a user also creates an individual memory."""
        prefix = clean_test_data
        user_repo = UserRepository(db_pool)
        memory_repo = MemoryRepository(db_pool)

        user = await user_repo.create(
            external_id=f"{prefix}mem_user",
            provider="local",
            organization_id=test_organization["id"],
            email=f"{prefix}mem@test.com",
            display_name=f"{prefix}Memory User",
        )

        # Search for individual memory
        result = await memory_repo.search(
            type="individual",
            requesting_user_id=user["id"],
            requesting_org_id=test_organization["id"],
        )

        # Should find at least one individual memory for this user
        user_memories = [m for m in result["memories"] if m["user_id"] == user["id"]]
        assert len(user_memories) >= 1
        assert user_memories[0]["type"] == "individual"

    async def test_get_user_by_id(self, db_pool, test_user):
        """Test retrieving a user by ID."""
        repo = UserRepository(db_pool)

        user = await repo.get_by_id(test_user["id"])

        assert user is not None
        assert user["id"] == test_user["id"]
        assert user["email"] == test_user["email"]

    async def test_get_user_by_external_id(self, db_pool, test_user):
        """Test retrieving a user by external ID."""
        repo = UserRepository(db_pool)

        user = await repo.get_by_external_id(
            test_user["external_id"],
            test_user["provider"],
        )

        assert user is not None
        assert user["id"] == test_user["id"]

    async def test_update_user(self, db_pool, test_user):
        """Test updating a user."""
        repo = UserRepository(db_pool)

        updated = await repo.update(
            test_user["id"],
            display_name="Updated Name",
        )

        assert updated is not None
        assert updated["display_name"] == "Updated Name"

    async def test_get_or_create_existing(self, db_pool, test_user, test_organization):
        """Test get_or_create returns existing user without creating."""
        repo = UserRepository(db_pool)

        user, created = await repo.get_or_create(
            external_id=test_user["external_id"],
            provider=test_user["provider"],
            organization_id=test_organization["id"],
        )

        assert created is False
        assert user["id"] == test_user["id"]

    async def test_get_or_create_new(self, db_pool, test_organization, clean_test_data):
        """Test get_or_create creates a new user when not found."""
        prefix = clean_test_data
        repo = UserRepository(db_pool)

        user, created = await repo.get_or_create(
            external_id=f"{prefix}brand_new",
            provider="local",
            organization_id=test_organization["id"],
            email=f"{prefix}brandnew@test.com",
        )

        assert created is True
        assert user["external_id"] == f"{prefix}brand_new"

    async def test_delete_user(self, db_pool, test_organization, clean_test_data):
        """Test deleting a user also soft-deletes their individual memory."""
        prefix = clean_test_data
        user_repo = UserRepository(db_pool)
        memory_repo = MemoryRepository(db_pool)

        user = await user_repo.create(
            external_id=f"{prefix}deleteme",
            provider="local",
            organization_id=test_organization["id"],
            display_name=f"{prefix}Delete Me",
        )

        deleted = await user_repo.delete(user["id"])
        assert deleted is True

        # User should be gone
        assert await user_repo.get_by_id(user["id"]) is None

        # Individual memory should be soft-deleted (not retrievable)
        individual = await memory_repo.get_individual_memory_for_user(user["id"])
        assert individual is None

    async def test_update_role(self, db_pool, test_user):
        """Test updating a user's role."""
        repo = UserRepository(db_pool)

        updated = await repo.update_role(test_user["id"], "admin")

        assert updated is not None
        assert updated["role"] == "admin"

    async def test_get_by_organization(self, db_pool, test_user, test_organization):
        """Test listing users by organization."""
        repo = UserRepository(db_pool)

        users = await repo.get_by_organization(test_organization["id"])

        assert len(users) >= 1
        user_ids = [u["id"] for u in users]
        assert test_user["id"] in user_ids

    async def test_get_by_organization_with_role_filter(
        self, db_pool, test_user, test_organization
    ):
        """Test listing org users filtered by role."""
        repo = UserRepository(db_pool)

        # test_user has default 'member' role
        users = await repo.get_by_organization(test_organization["id"], role="member")
        user_ids = [u["id"] for u in users]
        assert test_user["id"] in user_ids

        users = await repo.get_by_organization(test_organization["id"], role="admin")
        user_ids = [u["id"] for u in users]
        assert test_user["id"] not in user_ids


class TestOrganizationRepository:
    """Tests for OrganizationRepository."""

    async def test_create_organization(self, db_pool, clean_test_data):
        """Test creating an organization."""
        prefix = clean_test_data
        repo = OrganizationRepository(db_pool)

        org = await repo.create(name=f"{prefix}TestOrg")

        assert org["id"] is not None
        assert org["name"] == f"{prefix}TestOrg"

    async def test_get_organization_by_id(self, db_pool, test_organization):
        """Test retrieving an organization by ID."""
        repo = OrganizationRepository(db_pool)

        org = await repo.get_by_id(test_organization["id"])

        assert org is not None
        assert org["id"] == test_organization["id"]


class TestApiKeyRepository:
    """Tests for ApiKeyRepository."""

    async def test_create_api_key(self, db_pool, test_user):
        """Test creating an API key."""
        repo = ApiKeyRepository(db_pool)

        key_record, plain_key = await repo.create(
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
            name="Test Key",
        )

        assert key_record["id"] is not None
        assert key_record["name"] == "Test Key"
        assert plain_key.startswith("hs_")
        assert key_record["key_prefix"] == plain_key[:11]

    async def test_verify_valid_api_key(self, db_pool, test_user):
        """Test verifying a valid API key."""
        repo = ApiKeyRepository(db_pool)

        key_record, plain_key = await repo.create(
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
            name="Verify Test Key",
        )

        verified = await repo.verify(plain_key)

        assert verified is not None
        assert verified["id"] == key_record["id"]
        assert verified["user_id"] == test_user["id"]

    async def test_verify_invalid_api_key(self, db_pool):
        """Test verifying an invalid API key."""
        repo = ApiKeyRepository(db_pool)

        verified = await repo.verify("hs_invalid_key_12345")

        assert verified is None

    async def test_revoke_api_key(self, db_pool, test_user):
        """Test revoking an API key."""
        repo = ApiKeyRepository(db_pool)

        key_record, plain_key = await repo.create(
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
            name="Revoke Test Key",
        )

        revoked = await repo.revoke(key_record["id"], test_user["id"])

        assert revoked is True

        # Should no longer verify
        verified = await repo.verify(plain_key)
        assert verified is None

    async def test_duplicate_key_name_fails(self, db_pool, test_user):
        """Test that creating a key with duplicate name fails."""
        repo = ApiKeyRepository(db_pool)

        await repo.create(
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
            name="Duplicate Name",
        )

        with pytest.raises(ValueError, match="already exists"):
            await repo.create(
                user_id=test_user["id"],
                organization_id=test_user["organization_id"],
                name="Duplicate Name",
            )

    async def test_list_by_user(self, db_pool, test_user):
        """Test listing all API keys for a user."""
        repo = ApiKeyRepository(db_pool)

        await repo.create(
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
            name="List Key 1",
        )
        await repo.create(
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
            name="List Key 2",
        )

        result = await repo.list_by_user(test_user["id"])
        keys = result["items"]

        names = [k["name"] for k in keys]
        assert "List Key 1" in names
        assert "List Key 2" in names

    async def test_get_by_id(self, db_pool, test_user):
        """Test retrieving an API key by ID with ownership check."""
        repo = ApiKeyRepository(db_pool)

        key_record, _ = await repo.create(
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
            name="Get By ID Key",
        )

        found = await repo.get_by_id(key_record["id"], test_user["id"])
        assert found is not None
        assert found["id"] == key_record["id"]

        # Different user should get None
        not_found = await repo.get_by_id(key_record["id"], uuid4())
        assert not_found is None

    async def test_update_name(self, db_pool, test_user):
        """Test renaming an API key."""
        repo = ApiKeyRepository(db_pool)

        key_record, _ = await repo.create(
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
            name="Original Name",
        )

        updated = await repo.update_name(key_record["id"], test_user["id"], "New Name")
        assert updated is not None
        assert updated["name"] == "New Name"

    async def test_verify_non_hs_prefix_returns_none(self, db_pool):
        """Test that keys without hs_ prefix are immediately rejected."""
        repo = ApiKeyRepository(db_pool)

        result = await repo.verify("not_a_valid_key")
        assert result is None

    async def test_revoked_key_not_listed(self, db_pool, test_user):
        """Test that revoked keys don't appear in list_by_user."""
        repo = ApiKeyRepository(db_pool)

        key_record, _ = await repo.create(
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
            name="Soon Revoked",
        )

        await repo.revoke(key_record["id"], test_user["id"])

        result = await repo.list_by_user(test_user["id"])
        key_ids = [k["id"] for k in result["items"]]
        assert key_record["id"] not in key_ids


class TestAuditRepository:
    """Tests for AuditRepository."""

    async def test_log_audit_entry(self, db_pool, test_memory, test_user):
        """Test creating an audit log entry."""
        repo = AuditRepository(db_pool)

        entry = await repo.log(
            memory_id=test_memory["id"],
            action_type="update",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
            changed_fields=["content"],
            old_values={"content": "old"},
            new_values={"content": "new"},
        )

        assert entry["id"] is not None
        assert entry["action_type"] == "update"
        assert entry["memory_id"] == test_memory["id"]
        assert entry["changed_fields"] == ["content"]

    async def test_get_by_memory_id(self, db_pool, test_memory, test_user):
        """Test retrieving audit entries by memory ID."""
        repo = AuditRepository(db_pool)

        # Create two audit entries for the same memory
        await repo.log(
            memory_id=test_memory["id"],
            action_type="update",
            user_id=test_user["id"],
            changed_fields=["content"],
        )
        await repo.log(
            memory_id=test_memory["id"],
            action_type="update",
            user_id=test_user["id"],
            changed_fields=["tags"],
        )

        result = await repo.get_by_memory_id(test_memory["id"])

        assert result["total_count"] >= 2
        assert len(result["entries"]) >= 2
        assert result["offset"] == 0
        assert result["limit"] == 50
        assert all(e["memory_id"] == test_memory["id"] for e in result["entries"])

    async def test_get_by_memory_id_pagination(self, db_pool, test_memory, test_user):
        """Test pagination in get_by_memory_id."""
        repo = AuditRepository(db_pool)

        # Create 3 entries
        for i in range(3):
            await repo.log(
                memory_id=test_memory["id"],
                action_type="update",
                user_id=test_user["id"],
                changed_fields=[f"field_{i}"],
            )

        result = await repo.get_by_memory_id(test_memory["id"], limit=2, offset=0)

        assert len(result["entries"]) == 2
        assert result["has_more"] is True

    async def test_get_by_user_id(self, db_pool, test_memory, test_user):
        """Test retrieving audit entries by user ID."""
        repo = AuditRepository(db_pool)

        await repo.log(
            memory_id=test_memory["id"],
            action_type="create",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        result = await repo.get_by_user_id(test_user["id"])

        assert result["total_count"] >= 1
        assert all(e["user_id"] == test_user["id"] for e in result["entries"])

    async def test_get_by_user_id_with_action_filter(self, db_pool, test_memory, test_user):
        """Test filtering audit entries by user ID and action type."""
        repo = AuditRepository(db_pool)

        await repo.log(
            memory_id=test_memory["id"],
            action_type="create",
            user_id=test_user["id"],
        )
        await repo.log(
            memory_id=test_memory["id"],
            action_type="update",
            user_id=test_user["id"],
        )

        result = await repo.get_by_user_id(test_user["id"], action_type="create")

        assert result["total_count"] >= 1
        assert all(e["action_type"] == "create" for e in result["entries"])

    async def test_get_by_organization_id(self, db_pool, test_memory, test_user):
        """Test retrieving audit entries by organization ID."""
        repo = AuditRepository(db_pool)

        await repo.log(
            memory_id=test_memory["id"],
            action_type="update",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        result = await repo.get_by_organization_id(test_user["organization_id"])

        assert result["total_count"] >= 1
        assert all(e["organization_id"] == test_user["organization_id"] for e in result["entries"])

    async def test_get_recent(self, db_pool, test_memory, test_user):
        """Test retrieving recent audit entries."""
        repo = AuditRepository(db_pool)

        await repo.log(
            memory_id=test_memory["id"],
            action_type="update",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        entries = await repo.get_recent(
            organization_id=test_user["organization_id"],
            limit=10,
        )

        assert len(entries) >= 1
        assert entries[0]["action_type"] == "update"

    async def test_get_recent_with_action_types(self, db_pool, test_memory, test_user):
        """Test filtering recent audit entries by action types."""
        repo = AuditRepository(db_pool)

        await repo.log(
            memory_id=test_memory["id"],
            action_type="create",
            user_id=test_user["id"],
        )
        await repo.log(
            memory_id=test_memory["id"],
            action_type="delete",
            user_id=test_user["id"],
        )

        entries = await repo.get_recent(action_types=["delete"])

        assert all(e["action_type"] == "delete" for e in entries)

    async def test_get_versions(self, db_pool, test_memory, test_user):
        """Test retrieving version history for a memory."""
        repo = AuditRepository(db_pool)

        # Create versioned audit entries
        await repo.log(
            memory_id=test_memory["id"],
            action_type="create",
            user_id=test_user["id"],
            version=1,
            snapshot={"content": "v1"},
        )
        await repo.log(
            memory_id=test_memory["id"],
            action_type="update",
            user_id=test_user["id"],
            version=2,
            snapshot={"content": "v2"},
        )

        result = await repo.get_versions(test_memory["id"])

        assert result["total_count"] >= 2
        # Versions should be ordered descending
        versions = [e["version"] for e in result["versions"]]
        assert versions == sorted(versions, reverse=True)

    async def test_get_version_snapshot(self, db_pool, test_memory, test_user):
        """Test retrieving a specific version snapshot."""
        repo = AuditRepository(db_pool)

        snapshot_data = {"content": "snapshot content", "tags": ["test"]}
        await repo.log(
            memory_id=test_memory["id"],
            action_type="update",
            user_id=test_user["id"],
            version=1,
            snapshot=snapshot_data,
        )

        result = await repo.get_version_snapshot(test_memory["id"], version=1)

        assert result is not None
        assert result["version"] == 1
        assert result["snapshot"] == snapshot_data

    async def test_get_version_snapshot_not_found(self, db_pool, test_memory):
        """Test that missing version returns None."""
        repo = AuditRepository(db_pool)

        result = await repo.get_version_snapshot(test_memory["id"], version=9999)

        assert result is None

    async def test_log_with_notes_and_context(self, db_pool, test_memory, test_user):
        """Test creating an audit entry with notes and context."""
        repo = AuditRepository(db_pool)

        entry = await repo.log(
            memory_id=test_memory["id"],
            action_type="update",
            user_id=test_user["id"],
            notes="Manual correction by admin",
            context={"ip": "127.0.0.1", "user_agent": "test"},
        )

        assert entry["notes"] == "Manual correction by admin"
        assert entry["context"]["ip"] == "127.0.0.1"

    async def test_get_by_user_id_with_since_filter(self, db_pool, test_memory, test_user):
        """Test filtering audit entries by user ID with since timestamp."""
        repo = AuditRepository(db_pool)

        await repo.log(
            memory_id=test_memory["id"],
            action_type="update",
            user_id=test_user["id"],
        )

        # Query with a future date should return nothing
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        result = await repo.get_by_user_id(test_user["id"], since=future)
        assert result["total_count"] == 0


class TestAccessRepository:
    """Tests for AccessRepository."""

    async def test_log_access(self, db_pool, test_memory, test_user):
        """Test logging memory access."""
        repo = AccessRepository(db_pool)

        # Should not raise
        await repo.log_access(
            memory_id=test_memory["id"],
            access_type="view",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        # Verify last_accessed_at was updated
        from lucent.db import MemoryRepository

        mem_repo = MemoryRepository(db_pool)
        memory = await mem_repo.get(test_memory["id"])

        # Note: memory might be None if soft-deleted, but last_accessed_at should be set
        # In our case, test_memory should still be accessible
        assert memory is not None
        assert memory["last_accessed_at"] is not None

    async def test_log_batch_access(self, db_pool, test_user, clean_test_data):
        """Test logging access for multiple memories at once."""
        prefix = clean_test_data
        mem_repo = MemoryRepository(db_pool)
        repo = AccessRepository(db_pool)

        # Create multiple memories
        memories = []
        for i in range(3):
            m = await mem_repo.create(
                username=f"{prefix}user",
                type="experience",
                content=f"{prefix} Batch memory {i}",
                user_id=test_user["id"],
                organization_id=test_user["organization_id"],
            )
            memories.append(m)

        memory_ids = [m["id"] for m in memories]

        await repo.log_batch_access(
            memory_ids=memory_ids,
            access_type="search_result",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
            context={"query": "batch test"},
        )

        # Verify last_accessed_at was updated on all memories
        for mid in memory_ids:
            m = await mem_repo.get(mid)
            assert m is not None
            assert m["last_accessed_at"] is not None

    async def test_log_batch_access_empty(self, db_pool):
        """Test that log_batch_access with empty list is a no-op."""
        repo = AccessRepository(db_pool)

        # Should not raise
        await repo.log_batch_access(
            memory_ids=[],
            access_type="view",
        )

    async def test_get_access_history(self, db_pool, test_memory, test_user):
        """Test retrieving access history for a memory."""
        repo = AccessRepository(db_pool)

        # Log multiple accesses
        for _ in range(3):
            await repo.log_access(
                memory_id=test_memory["id"],
                access_type="view",
                user_id=test_user["id"],
                organization_id=test_user["organization_id"],
            )

        result = await repo.get_access_history(test_memory["id"])

        assert result["total_count"] >= 3
        assert len(result["entries"]) >= 3
        assert result["offset"] == 0
        assert result["limit"] == 50
        assert all(e["memory_id"] == test_memory["id"] for e in result["entries"])

    async def test_get_access_history_pagination(self, db_pool, test_memory, test_user):
        """Test pagination in access history."""
        repo = AccessRepository(db_pool)

        for _ in range(3):
            await repo.log_access(
                memory_id=test_memory["id"],
                access_type="view",
                user_id=test_user["id"],
            )

        result = await repo.get_access_history(test_memory["id"], limit=2, offset=0)

        assert len(result["entries"]) == 2
        assert result["has_more"] is True

    async def test_get_search_history(self, db_pool, test_memory, test_user):
        """Test retrieving search queries that returned a memory."""
        repo = AccessRepository(db_pool)

        # Log a view and a search_result access
        await repo.log_access(
            memory_id=test_memory["id"],
            access_type="view",
            user_id=test_user["id"],
        )
        await repo.log_access(
            memory_id=test_memory["id"],
            access_type="search_result",
            user_id=test_user["id"],
            context={"query": "test search"},
        )

        results = await repo.get_search_history(test_memory["id"])

        assert len(results) >= 1
        # Should only contain search_result entries
        assert all(e["access_type"] == "search_result" for e in results)

    async def test_get_user_activity(self, db_pool, test_memory, test_user):
        """Test retrieving access activity for a user."""
        repo = AccessRepository(db_pool)

        await repo.log_access(
            memory_id=test_memory["id"],
            access_type="view",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        results = await repo.get_user_activity(test_user["id"])

        assert len(results) >= 1
        assert all(e["user_id"] == test_user["id"] for e in results)

    async def test_get_most_accessed(self, db_pool, test_user, clean_test_data):
        """Test retrieving most frequently accessed memories."""
        prefix = clean_test_data
        mem_repo = MemoryRepository(db_pool)
        repo = AccessRepository(db_pool)

        # Create two memories, access one more than the other
        m1 = await mem_repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Popular memory",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        m2 = await mem_repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Less popular memory",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        # Access m1 three times, m2 once
        for _ in range(3):
            await repo.log_access(
                memory_id=m1["id"],
                access_type="view",
                user_id=test_user["id"],
                organization_id=test_user["organization_id"],
            )
        await repo.log_access(
            memory_id=m2["id"],
            access_type="view",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        results = await repo.get_most_accessed(
            user_id=test_user["id"],
        )

        assert len(results) >= 2
        # First result should be the more-accessed memory
        m1_entry = next(r for r in results if r["memory_id"] == m1["id"])
        m2_entry = next(r for r in results if r["memory_id"] == m2["id"])
        assert m1_entry["access_count"] > m2_entry["access_count"]

    async def test_get_user_activity_with_since(self, db_pool, test_memory, test_user):
        """Test filtering user activity with a since timestamp."""
        repo = AccessRepository(db_pool)

        await repo.log_access(
            memory_id=test_memory["id"],
            access_type="view",
            user_id=test_user["id"],
        )

        # Future date should yield no results
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        results = await repo.get_user_activity(test_user["id"], since=future)
        assert len(results) == 0

    async def test_get_most_accessed_with_org_filter(self, db_pool, test_user, test_memory):
        """Test most accessed memories filtered by organization."""
        repo = AccessRepository(db_pool)

        await repo.log_access(
            memory_id=test_memory["id"],
            access_type="view",
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        results = await repo.get_most_accessed(
            organization_id=test_user["organization_id"],
        )
        assert len(results) >= 1

        # Non-matching org should not find anything from our test data
        results_other = await repo.get_most_accessed(organization_id=uuid4())
        # Results may include other test data, but our specific memory shouldn't be there
        our_ids = [r["memory_id"] for r in results_other]
        assert test_memory["id"] not in our_ids
