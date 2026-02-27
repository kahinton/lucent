"""Tests for database repositories."""

import pytest
from uuid import UUID, uuid4
from datetime import datetime, timezone

from lucent.db import (
    MemoryRepository,
    UserRepository,
    OrganizationRepository,
    ApiKeyRepository,
    AuditRepository,
    AccessRepository,
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
    
    async def test_get_accessible_shared_memory(self, db_pool, test_user, test_organization, clean_test_data):
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
    
    async def test_create_user_creates_individual_memory(self, db_pool, test_organization, clean_test_data):
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
        assert plain_key.startswith("mcp_")
        assert key_record["key_prefix"] == plain_key[:12]
    
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
        
        verified = await repo.verify("mcp_invalid_key_12345")
        
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
