"""Tests for MCP tool functions in src/lucent/tools/memories.py.

Tests the MCP tool layer that wraps the DB layer, verifying:
- Input validation and error handling
- Auth context integration
- JSON serialization of responses
- Access control enforcement
"""

import json
import pytest
import pytest_asyncio
from uuid import uuid4, UUID

from mcp.server.fastmcp import FastMCP

from lucent.auth import set_current_user
from lucent.db import MemoryRepository
from lucent.tools.memories import register_tools


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def mcp_tools(db_pool):
    """Create a FastMCP instance with all memory tools registered."""
    mcp = FastMCP("test")
    register_tools(mcp)
    return mcp


@pytest_asyncio.fixture
async def auth_user(test_user):
    """Set the auth context to the test user and clean up after."""
    set_current_user({
        "id": test_user["id"],
        "organization_id": test_user["organization_id"],
        "role": "member",
        "display_name": "Test User",
        "email": "test@test.com",
    })
    yield test_user
    set_current_user(None)


async def _call(mcp, tool_name: str, args: dict | None = None) -> dict | list | str:
    """Call an MCP tool and parse the JSON response."""
    result = await mcp._tool_manager.call_tool(tool_name, args or {})
    return json.loads(result)


# ============================================================================
# create_memory
# ============================================================================


class TestCreateMemory:
    """Tests for the create_memory MCP tool."""

    async def test_create_basic_memory(self, mcp_tools, auth_user, clean_test_data):
        """Test creating a valid experience memory."""
        prefix = clean_test_data
        result = await _call(mcp_tools, "create_memory", {
            "type": "experience",
            "content": f"{prefix} Test experience memory",
            "username": f"{prefix}user",
            "tags": ["test", "mcp"],
            "importance": 7,
        })

        assert "id" in result
        assert result["type"] == "experience"
        assert result["content"] == f"{prefix} Test experience memory"
        assert sorted(result["tags"]) == ["mcp", "test"]
        assert result["importance"] == 7
        assert result["version"] == 1

    async def test_create_memory_invalid_type(self, mcp_tools, auth_user):
        """Test that an invalid memory type returns an error."""
        result = await _call(mcp_tools, "create_memory", {
            "type": "invalid_type",
            "content": "Should fail",
        })

        assert "error" in result

    async def test_create_individual_memory_rejected(self, mcp_tools, auth_user):
        """Test that individual memories cannot be created via MCP."""
        result = await _call(mcp_tools, "create_memory", {
            "type": "individual",
            "content": "Should be rejected",
        })

        assert "error" in result
        assert "Individual memories" in result["error"]

    async def test_create_memory_with_metadata(self, mcp_tools, auth_user, clean_test_data):
        """Test creating a memory with type-specific metadata."""
        prefix = clean_test_data
        result = await _call(mcp_tools, "create_memory", {
            "type": "technical",
            "content": f"{prefix} Technical memory with metadata",
            "username": f"{prefix}user",
            "metadata": {"language": "python", "repo": "lucent"},
        })

        assert "id" in result
        assert result["type"] == "technical"
        assert result["metadata"]["language"] == "python"

    async def test_create_memory_defaults(self, mcp_tools, auth_user, clean_test_data):
        """Test that default values are applied correctly."""
        prefix = clean_test_data
        result = await _call(mcp_tools, "create_memory", {
            "type": "experience",
            "content": f"{prefix} Minimal memory",
            "username": f"{prefix}user",
        })

        assert result["importance"] == 5
        assert result["tags"] == []
        assert result["related_memory_ids"] == []


# ============================================================================
# get_memory
# ============================================================================


class TestGetMemory:
    """Tests for the get_memory MCP tool."""

    async def test_get_existing_memory(self, mcp_tools, auth_user, test_memory):
        """Test retrieving a memory that exists."""
        result = await _call(mcp_tools, "get_memory", {
            "memory_id": str(test_memory["id"]),
        })

        assert result["id"] == str(test_memory["id"])
        assert result["content"] == test_memory["content"]

    async def test_get_nonexistent_memory(self, mcp_tools, auth_user):
        """Test retrieving a memory that doesn't exist."""
        result = await _call(mcp_tools, "get_memory", {
            "memory_id": str(uuid4()),
        })

        assert "error" in result
        assert "not found" in result["error"].lower()

    async def test_get_memory_invalid_uuid(self, mcp_tools, auth_user):
        """Test retrieving with an invalid UUID format."""
        result = await _call(mcp_tools, "get_memory", {
            "memory_id": "not-a-uuid",
        })

        assert "error" in result
        assert "Invalid" in result["error"]


# ============================================================================
# get_memories (batch)
# ============================================================================


class TestGetMemories:
    """Tests for the get_memories MCP tool."""

    async def test_get_single_memory(self, mcp_tools, auth_user, test_memory):
        """Test batch retrieval with a single ID."""
        result = await _call(mcp_tools, "get_memories", {
            "memory_ids": [str(test_memory["id"])],
        })

        assert result["total_requested"] == 1
        assert result["total_found"] == 1
        assert len(result["memories"]) == 1
        assert result["memories"][0]["id"] == str(test_memory["id"])

    async def test_get_memories_mixed(self, mcp_tools, auth_user, test_memory):
        """Test batch retrieval with mix of found and not-found IDs."""
        fake_id = str(uuid4())
        result = await _call(mcp_tools, "get_memories", {
            "memory_ids": [str(test_memory["id"]), fake_id],
        })

        assert result["total_requested"] == 2
        assert result["total_found"] == 1
        assert len(result["not_found"]) == 1
        assert fake_id in result["not_found"]

    async def test_get_memories_empty_list(self, mcp_tools, auth_user):
        """Test batch retrieval with empty list returns error."""
        result = await _call(mcp_tools, "get_memories", {
            "memory_ids": [],
        })

        assert "error" in result


# ============================================================================
# search_memories
# ============================================================================


class TestSearchMemories:
    """Tests for the search_memories MCP tool."""

    async def test_search_by_query(self, mcp_tools, auth_user, clean_test_data):
        """Test searching memories by content query."""
        prefix = clean_test_data
        # Create a searchable memory
        await _call(mcp_tools, "create_memory", {
            "type": "experience",
            "content": f"{prefix} Python async programming patterns",
            "username": f"{prefix}user",
            "tags": ["python", "async"],
        })

        result = await _call(mcp_tools, "search_memories", {
            "query": f"{prefix} Python async",
        })

        assert "memories" in result
        assert result["total_count"] >= 1
        assert "offset" in result
        assert "limit" in result
        assert "has_more" in result

    async def test_search_with_tag_filter(self, mcp_tools, auth_user, clean_test_data):
        """Test searching with tag filter."""
        prefix = clean_test_data
        await _call(mcp_tools, "create_memory", {
            "type": "technical",
            "content": f"{prefix} Tagged memory for search",
            "username": f"{prefix}user",
            "tags": ["unique-search-tag-xyz"],
        })

        result = await _call(mcp_tools, "search_memories", {
            "tags": ["unique-search-tag-xyz"],
        })

        assert result["total_count"] >= 1
        for mem in result["memories"]:
            assert "unique-search-tag-xyz" in mem["tags"]

    async def test_search_with_type_filter(self, mcp_tools, auth_user, clean_test_data):
        """Test searching with type filter."""
        prefix = clean_test_data
        await _call(mcp_tools, "create_memory", {
            "type": "technical",
            "content": f"{prefix} Technical search content",
            "username": f"{prefix}user",
        })

        result = await _call(mcp_tools, "search_memories", {
            "query": f"{prefix} Technical search",
            "type": "technical",
        })

        assert result["total_count"] >= 1
        for mem in result["memories"]:
            assert mem["type"] == "technical"

    async def test_search_limit_and_offset(self, mcp_tools, auth_user, clean_test_data):
        """Test pagination with limit and offset."""
        prefix = clean_test_data
        # Create multiple memories
        for i in range(3):
            await _call(mcp_tools, "create_memory", {
                "type": "experience",
                "content": f"{prefix} Pagination test memory {i}",
                "username": f"{prefix}user",
                "tags": ["pagination-test"],
            })

        result = await _call(mcp_tools, "search_memories", {
            "tags": ["pagination-test"],
            "limit": 2,
            "offset": 0,
        })

        assert len(result["memories"]) <= 2
        assert result["limit"] == 2
        assert result["offset"] == 0

    async def test_search_no_results(self, mcp_tools, auth_user):
        """Test search that returns no results."""
        result = await _call(mcp_tools, "search_memories", {
            "query": "completely_nonexistent_query_string_xyz_12345",
        })

        assert result["total_count"] == 0
        assert result["memories"] == []

    async def test_search_invalid_type(self, mcp_tools, auth_user):
        """Test search with invalid type filter."""
        result = await _call(mcp_tools, "search_memories", {
            "type": "nonexistent_type",
        })

        assert "error" in result

    async def test_search_content_truncation(self, mcp_tools, auth_user, clean_test_data):
        """Test that search results truncate long content."""
        prefix = clean_test_data
        long_content = f"{prefix} " + "x" * 2000
        await _call(mcp_tools, "create_memory", {
            "type": "experience",
            "content": long_content,
            "username": f"{prefix}user",
            "tags": ["truncation-test"],
        })

        result = await _call(mcp_tools, "search_memories", {
            "tags": ["truncation-test"],
        })

        assert result["total_count"] >= 1
        mem = result["memories"][0]
        assert "content_truncated" in mem


# ============================================================================
# search_memories_full
# ============================================================================


class TestSearchMemoriesFull:
    """Tests for the search_memories_full MCP tool."""

    async def test_full_search(self, mcp_tools, auth_user, clean_test_data):
        """Test full-text search across all fields."""
        prefix = clean_test_data
        await _call(mcp_tools, "create_memory", {
            "type": "technical",
            "content": f"{prefix} Full search test content",
            "username": f"{prefix}user",
            "tags": ["full-search-test"],
        })

        result = await _call(mcp_tools, "search_memories_full", {
            "query": f"{prefix} Full search test",
        })

        assert "memories" in result
        assert result["total_count"] >= 1

    async def test_full_search_empty_query_rejected(self, mcp_tools, auth_user):
        """Test that empty query is rejected."""
        result = await _call(mcp_tools, "search_memories_full", {
            "query": "   ",
        })

        assert "error" in result
        assert "required" in result["error"].lower()

    async def test_full_search_with_type_filter(self, mcp_tools, auth_user, clean_test_data):
        """Test full search with type filter."""
        prefix = clean_test_data
        await _call(mcp_tools, "create_memory", {
            "type": "procedural",
            "content": f"{prefix} Procedural full search",
            "username": f"{prefix}user",
        })

        result = await _call(mcp_tools, "search_memories_full", {
            "query": f"{prefix} Procedural full search",
            "type": "procedural",
        })

        assert result["total_count"] >= 1
        for mem in result["memories"]:
            assert mem["type"] == "procedural"


# ============================================================================
# update_memory
# ============================================================================


class TestUpdateMemory:
    """Tests for the update_memory MCP tool."""

    async def test_update_content(self, mcp_tools, auth_user, test_memory):
        """Test updating memory content."""
        result = await _call(mcp_tools, "update_memory", {
            "memory_id": str(test_memory["id"]),
            "content": "Updated content via MCP",
        })

        assert result["content"] == "Updated content via MCP"
        assert result["version"] == 2

    async def test_update_tags_and_importance(self, mcp_tools, auth_user, test_memory):
        """Test updating tags and importance."""
        result = await _call(mcp_tools, "update_memory", {
            "memory_id": str(test_memory["id"]),
            "tags": ["updated", "mcp-test"],
            "importance": 9,
        })

        assert sorted(result["tags"]) == ["mcp-test", "updated"]
        assert result["importance"] == 9

    async def test_update_nonexistent_memory(self, mcp_tools, auth_user):
        """Test updating a memory that doesn't exist."""
        result = await _call(mcp_tools, "update_memory", {
            "memory_id": str(uuid4()),
            "content": "Should fail",
        })

        assert "error" in result

    async def test_update_with_expected_version_success(self, mcp_tools, auth_user, test_memory):
        """Test optimistic locking with correct expected_version."""
        result = await _call(mcp_tools, "update_memory", {
            "memory_id": str(test_memory["id"]),
            "content": "Version-checked update",
            "expected_version": test_memory["version"],
        })

        assert result["content"] == "Version-checked update"
        assert result["version"] == test_memory["version"] + 1

    async def test_update_with_expected_version_conflict(self, mcp_tools, auth_user, test_memory):
        """Test optimistic locking with wrong expected_version."""
        result = await _call(mcp_tools, "update_memory", {
            "memory_id": str(test_memory["id"]),
            "content": "Should conflict",
            "expected_version": 999,
        })

        assert "error" in result
        assert "Version conflict" in result["error"] or "version" in result["error"].lower()

    async def test_update_requires_auth(self, mcp_tools, test_memory):
        """Test that update requires authentication."""
        set_current_user(None)
        result = await _call(mcp_tools, "update_memory", {
            "memory_id": str(test_memory["id"]),
            "content": "No auth",
        })

        assert "error" in result
        assert "Authentication" in result["error"] or "auth" in result["error"].lower()

    async def test_update_ownership_check(self, mcp_tools, db_pool, test_memory, test_organization, clean_test_data):
        """Test that only the owner can update a memory."""
        from lucent.db import UserRepository
        prefix = clean_test_data

        # Create a different user
        user_repo = UserRepository(db_pool)
        other_user = await user_repo.create(
            external_id=f"{prefix}other",
            provider="local",
            organization_id=test_organization["id"],
            email=f"{prefix}other@test.com",
            display_name=f"{prefix}Other User",
        )

        # Set auth to the other user
        set_current_user({
            "id": other_user["id"],
            "organization_id": other_user["organization_id"],
            "role": "member",
        })

        result = await _call(mcp_tools, "update_memory", {
            "memory_id": str(test_memory["id"]),
            "content": "Should not work",
        })

        assert "error" in result
        assert "not accessible" in result["error"].lower() or "Permission denied" in result["error"]
        set_current_user(None)


# ============================================================================
# delete_memory
# ============================================================================


class TestDeleteMemory:
    """Tests for the delete_memory MCP tool."""

    async def test_soft_delete(self, mcp_tools, auth_user, clean_test_data):
        """Test soft deleting a memory."""
        prefix = clean_test_data
        # Create a memory to delete
        created = await _call(mcp_tools, "create_memory", {
            "type": "experience",
            "content": f"{prefix} Memory to delete",
            "username": f"{prefix}user",
        })
        memory_id = created["id"]

        result = await _call(mcp_tools, "delete_memory", {
            "memory_id": memory_id,
        })

        assert result["success"] is True

        # Verify it's no longer retrievable
        get_result = await _call(mcp_tools, "get_memory", {
            "memory_id": memory_id,
        })
        assert "error" in get_result

    async def test_delete_nonexistent(self, mcp_tools, auth_user):
        """Test deleting a memory that doesn't exist."""
        result = await _call(mcp_tools, "delete_memory", {
            "memory_id": str(uuid4()),
        })

        assert "error" in result

    async def test_delete_requires_auth(self, mcp_tools, test_memory):
        """Test that delete requires authentication."""
        set_current_user(None)
        result = await _call(mcp_tools, "delete_memory", {
            "memory_id": str(test_memory["id"]),
        })

        assert "error" in result
        assert "Authentication" in result["error"] or "auth" in result["error"].lower()

    async def test_delete_individual_memory_rejected(self, mcp_tools, db_pool, auth_user, clean_test_data):
        """Test that individual memories cannot be deleted via MCP."""
        prefix = clean_test_data
        # Create an individual memory directly in DB (bypassing MCP restriction)
        repo = MemoryRepository(db_pool)
        memory = await repo.create(
            username=f"{prefix}user",
            type="individual",
            content=f"{prefix} Individual memory",
            user_id=auth_user["id"],
            organization_id=auth_user["organization_id"],
        )

        result = await _call(mcp_tools, "delete_memory", {
            "memory_id": str(memory["id"]),
        })

        assert "error" in result
        assert "Individual memories" in result["error"]

    async def test_delete_ownership_check(self, mcp_tools, db_pool, test_memory, test_organization, clean_test_data):
        """Test that only the owner can delete a memory."""
        from lucent.db import UserRepository
        prefix = clean_test_data

        user_repo = UserRepository(db_pool)
        other_user = await user_repo.create(
            external_id=f"{prefix}other2",
            provider="local",
            organization_id=test_organization["id"],
            email=f"{prefix}other2@test.com",
            display_name=f"{prefix}Other User 2",
        )

        set_current_user({
            "id": other_user["id"],
            "organization_id": other_user["organization_id"],
            "role": "member",
        })

        result = await _call(mcp_tools, "delete_memory", {
            "memory_id": str(test_memory["id"]),
        })

        assert "error" in result
        assert "not accessible" in result["error"].lower() or "Permission denied" in result["error"]
        set_current_user(None)


# ============================================================================
# get_existing_tags
# ============================================================================


class TestGetExistingTags:
    """Tests for the get_existing_tags MCP tool."""

    async def test_get_tags(self, mcp_tools, auth_user, clean_test_data):
        """Test retrieving existing tags with counts."""
        prefix = clean_test_data
        # Create memories with known tags
        for _ in range(3):
            await _call(mcp_tools, "create_memory", {
                "type": "experience",
                "content": f"{prefix} Tag count test",
                "username": f"{prefix}user",
                "tags": ["tag-count-test-abc"],
            })

        result = await _call(mcp_tools, "get_existing_tags", {})

        assert "tags" in result
        assert "total_returned" in result
        # Find our test tag
        tag_entry = next((t for t in result["tags"] if t["tag"] == "tag-count-test-abc"), None)
        assert tag_entry is not None
        assert tag_entry["count"] >= 3

    async def test_get_tags_with_type_filter(self, mcp_tools, auth_user, clean_test_data):
        """Test getting tags filtered by memory type."""
        prefix = clean_test_data
        await _call(mcp_tools, "create_memory", {
            "type": "technical",
            "content": f"{prefix} Tech tag test",
            "username": f"{prefix}user",
            "tags": ["tech-tag-filter-test"],
        })

        result = await _call(mcp_tools, "get_existing_tags", {
            "type": "technical",
        })

        assert "tags" in result
        tag_names = [t["tag"] for t in result["tags"]]
        assert "tech-tag-filter-test" in tag_names


# ============================================================================
# get_current_user_context
# ============================================================================


class TestGetCurrentUserContext:
    """Tests for the get_current_user_context MCP tool."""

    async def test_authenticated_user(self, mcp_tools, auth_user):
        """Test getting context for authenticated user."""
        result = await _call(mcp_tools, "get_current_user_context", {})

        assert "user" in result
        assert result["user"]["id"] == str(auth_user["id"])

    async def test_unauthenticated_user(self, mcp_tools):
        """Test getting context when not authenticated."""
        set_current_user(None)
        result = await _call(mcp_tools, "get_current_user_context", {})

        assert "error" in result
        assert "Not authenticated" in result["error"]
